"""Turn a window into a numbered list of controls, and act on one by number.

This is the half of the agent that does not involve a model. It walks the UIA
tree of an explicitly named window, throws away everything that cannot be acted
on, and hands back a numbered list - the equivalent of the numbers Apple's Voice
Control paints over the screen.

The safety property that matters here is the window allowlist. The scraper takes
window titles, never "the foreground window", and `act` re-checks that the
element's own top-level window is on the allowlist immediately before doing
anything. That check is the reason this can be tested on a working machine: the
editor is not on the allowlist, so it cannot be clicked, and the mock app ships a
decoy window that is also off the allowlist to prove the check fires.

Actions go through UIA patterns (Invoke, Toggle, SetValue) rather than moving the
physical mouse. A synthesised click lands wherever the cursor happens to be if a
window moved underneath it; Invoke is addressed to the element itself, steals no
focus, and cannot hit a bystander. `--mouse` exists for when we want to test the
real input path, and it refuses unless the target window is in the foreground.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field

import uiautomation as auto

auto.SetGlobalSearchTimeout(2)

# Control types worth offering to a model: these are things a person clicks.
CLICKABLE = {
    "ButtonControl", "MenuItemControl", "CheckBoxControl", "RadioButtonControl",
    "HyperlinkControl", "ListItemControl", "TabItemControl", "TreeItemControl",
    "SplitButtonControl", "MenuControl",
}
TYPEABLE = {"EditControl", "DocumentControl", "ComboBoxControl"}

# Window chrome is pruned. The title bar's own Close button would otherwise be a
# second defensible answer to "close this panel", which makes an accuracy number
# unreadable - and on the main window it would end the test run. Excluding it is
# a deliberate simplification, not an oversight; a real agent would want it back,
# scoped by task.
CHROME = {"TitleBarControl", "MenuBarControl"}
CHROME_NAMES = {"Minimize", "Maximize", "Close", "Restore", "System"}


@dataclass
class Element:
    number: int
    label: str
    kind: str                 # "click" or "type"
    control_type: str
    window: str
    rect: tuple[int, int, int, int]
    control: object = field(repr=False, default=None)
    state: str = ""          # "on", "off", "selected"... when the control has one
    # Which target window this element was scraped from. Provenance is exact,
    # whereas walking back up from the element to guess its window is not: Start
    # on Windows 11 is composed of several bridged top-level HWNDs, so the search
    # box's root handle is not the Start handle the scraper was given, and a
    # scope check built on re-derivation refused an element it had just produced.
    source_hwnd: int = 0

    def describe(self) -> str:
        """What the model sees. Label plus a hint at what kind of control it is."""
        noun = {"type": "text field", "click": "button"}.get(self.kind, "control")
        if self.control_type == "MenuItemControl":
            noun = "menu item"
        elif self.control_type == "CheckBoxControl":
            noun = "checkbox"
        # Current state belongs in the description. Without it "Word wrap" reads
        # identically whether it is on or off, so a request to turn it ON gets a
        # blind toggle that turns it off - which is exactly what happened in
        # Notepad, and the agent then reported the task complete.
        if self.state:
            return f"{self.label} ({noun}, currently {self.state}, {self.window})"
        return f"{self.label} ({noun}, {self.window})"


class WindowNotFound(RuntimeError):
    pass


class NotAllowed(RuntimeError):
    pass


def find_windows(titles: list[str]) -> list[tuple[str, object]]:
    """Locate the allowlisted windows by exact title. Never falls back."""
    found = []
    for title in titles:
        win = auto.WindowControl(searchDepth=1, Name=title)
        if win.Exists(1):
            found.append((title, win))
    if not found:
        raise WindowNotFound(
            f"none of the allowlisted windows are open: {titles}. "
            "Launch the mock UI first - this tool will not fall back to the "
            "foreground window.")
    return found


def _nearest_label(element, siblings) -> str:
    """Name an unlabelled field from the closest text to its left, then above.

    Win32 usually hands an EDIT the name of the STATIC created before it, but
    plenty of real apps get this wrong and leave the field anonymous, so the
    fallback is worth having.
    """
    try:
        r = element.BoundingRectangle
    except Exception:
        return ""
    best, best_d = "", 10**9
    for s, text in siblings:
        try:
            sr = s.BoundingRectangle
        except Exception:
            continue
        vertical_overlap = min(sr.bottom, r.bottom) - max(sr.top, r.top)
        if sr.right <= r.left + 4 and vertical_overlap > 4:
            d = r.left - sr.right
        elif sr.bottom <= r.top + 4 and abs(sr.left - r.left) < 120:
            d = (r.top - sr.bottom) + 200          # prefer left over above
        else:
            continue
        if d < best_d:
            best, best_d = text, d
    return best


def list_top_level(include_offscreen: bool = False) -> list[dict]:
    """Every visible top-level window, for picking a target by hand.

    Identity is the window HANDLE, not the title. Real applications rewrite their
    titles constantly - an editor prepends a dot when a file is dirty, a browser
    retitles on every navigation - so a title-based allowlist silently stops
    matching the window it was meant to protect, or starts matching a different
    one. The handle does not move.
    """
    out: list[dict] = []
    root = auto.GetRootControl()
    for control, _ in auto.WalkControl(root, includeTop=False, maxDepth=1):
        if control.ControlTypeName not in ("WindowControl", "PaneControl"):
            continue
        title = (control.Name or "").strip()
        if not title:
            continue
        try:
            if control.IsOffscreen and not include_offscreen:
                continue
            r = control.BoundingRectangle
            if (r.width() <= 0 or r.height() <= 0) and not include_offscreen:
                continue
            hwnd = control.NativeWindowHandle
        except Exception:
            continue
        if not hwnd:
            continue
        out.append({"hwnd": int(hwnd), "title": title,
                    "class_name": control.ClassName or "",
                    "rect": (r.left, r.top, r.width(), r.height()),
                    "control": control})
    return out


_TOGGLE_WORDS = {0: "off", 1: "on", 2: "partly on"}


def _read_state(control, ctype: str) -> str:
    """Read a control's on/off or selected state, where it has one.

    Only attempted for the control types that can carry one, because each probe
    is a cross-process UIA call and doing it for every button would cost more
    than it returns.
    """
    if ctype in ("CheckBoxControl", "MenuItemControl", "RadioButtonControl",
                 "ButtonControl"):
        try:
            return _TOGGLE_WORDS.get(control.GetTogglePattern().ToggleState, "")
        except Exception:
            pass
    if ctype in ("ListItemControl", "TabItemControl", "RadioButtonControl"):
        try:
            return ("selected" if control.GetSelectionItemPattern().IsSelected
                    else "not selected")
        except Exception:
            pass
    return ""


def _is_readable(text: str) -> bool:
    """Reject icon-font glyphs masquerading as text.

    Modern Windows apps label icons with characters from the Unicode Private Use
    Area, so Calculator's own tree yields entries like U+E81C alongside "Display
    is 1". They are meaningless to a reader and they crowd out the real state.
    """
    if not any(ch.isalnum() for ch in text):
        return False
    pua = sum(1 for ch in text if "" <= ch <= "")
    return pua * 2 <= len(text)


def _collect(win, title: str, max_depth: int, elements: list[Element],
             stats: dict, include_chrome: bool = False,
             source_hwnd: int = 0) -> None:
    """Prune one window's tree down to the controls worth offering."""
    nodes = list(auto.WalkControl(win, includeTop=False, maxDepth=max_depth))
    stats["walked"] += len(nodes)
    texts = [(c, c.Name) for c, _ in nodes
             if c.ControlTypeName == "TextControl" and c.Name]

    # The readable text of a window is state, not decoration, and it was being
    # thrown away after being used to label anonymous fields. A calculator's
    # display, a status bar, a validation message: without them the agent works
    # from its own action history alone and has no way to confirm where it got to.
    seen = stats.setdefault("texts", [])
    for _c, name in texts:
        name = name.strip()
        if name and name not in seen and _is_readable(name):
            seen.append(name)

    for control, _depth in nodes:
        _consider(control, title, texts, elements, stats, include_chrome,
                  source_hwnd)


def _dedupe(elements: list[Element], stats: dict) -> list[Element]:
    """One control exposed twice is one option, not two.

    Notepad's menu bar reports File, Edit and View as both a MenuItemControl and
    a ButtonControl at byte-identical rectangles. Offering both splits the
    probability between two answers that are equally correct, which drags the
    winner's score down toward the act/decline bar for no reason - the model was
    not uncertain, the option list was redundant. Same window, same label, same
    rectangle means the same thing to click.
    """
    seen: set[tuple] = set()
    kept: list[Element] = []
    for e in elements:
        key = (e.window, e.label.strip().lower(), e.kind, e.rect)
        if key in seen:
            continue
        seen.add(key)
        kept.append(e)
    stats["deduped"] = len(elements) - len(kept)
    return kept


def _number(elements: list[Element], t0: float, stats: dict
            ) -> tuple[list[Element], dict]:
    elements = _dedupe(elements, stats)
    elements.sort(key=lambda e: (e.window, e.rect[1] // 20, e.rect[0]))
    for i, e in enumerate(elements, start=1):
        e.number = i
    stats["kept"] = len(elements)
    stats["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return elements, stats


def _new_stats() -> dict:
    return {"walked": 0, "pruned": {"chrome": 0, "disabled": 0, "offscreen": 0,
                                    "no_rect": 0, "unnamed": 0,
                                    "not_actionable": 0}}


def scrape_hwnds(handles: list[int], titles: dict[int, str],
                 max_depth: int = 6, include_chrome: bool = False
                 ) -> tuple[list[Element], dict]:
    """Scrape windows identified by handle, which is what a live target needs.

    `include_chrome` keeps menu bars and window buttons. Off for the mock tests,
    where the title bar's Close button was a second right answer that made
    accuracy unreadable. On for real software, where "File" is how you get
    anywhere and dropping the menu bar removes most of the application.
    """
    t0 = time.perf_counter()
    elements: list[Element] = []
    stats = _new_stats()
    for hwnd in handles:
        try:
            win = auto.ControlFromHandle(hwnd)
        except Exception:
            continue
        if win is None:
            continue
        _collect(win, titles.get(hwnd, win.Name or str(hwnd)), max_depth,
                 elements, stats, include_chrome, source_hwnd=hwnd)
    return _number(elements, t0, stats)


def scrape(titles: list[str], max_depth: int = 6) -> tuple[list[Element], dict]:
    """Walk the allowlisted windows and return numbered, actionable elements."""
    t0 = time.perf_counter()
    elements: list[Element] = []
    stats = _new_stats()

    for title, win in find_windows(titles):
        try:
            handle = int(win.NativeWindowHandle or 0)
        except Exception:
            handle = 0
        _collect(win, title, max_depth, elements, stats, source_hwnd=handle)

    return _number(elements, t0, stats)


def _consider(control, title: str, texts, elements: list[Element],
              stats: dict, include_chrome: bool = False,
              source_hwnd: int = 0) -> None:
    """Keep or prune one node. Shared by both scrape entry points.

    This was lifted out of the scrape loop, so every skip is a `return` rather
    than a `continue`. Worth saying because the first cut of the extraction left
    the `continue`s in place, which `ast.parse` accepts happily - that check runs
    at compile time, not parse time, so a syntax check that only parses will miss
    it.
    """
    ctype = control.ControlTypeName
    if not include_chrome and (ctype in CHROME or control.Name in CHROME_NAMES):
        stats["pruned"]["chrome"] += 1
        return
    if ctype not in CLICKABLE and ctype not in TYPEABLE:
        stats["pruned"]["not_actionable"] += 1
        return
    try:
        if not control.IsEnabled:
            stats["pruned"]["disabled"] += 1
            return
        if control.IsOffscreen:
            stats["pruned"]["offscreen"] += 1
            return
        r = control.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            stats["pruned"]["no_rect"] += 1
            return
    except Exception:
        stats["pruned"]["no_rect"] += 1
        return

    label = (control.Name or "").strip()
    if not label and ctype in TYPEABLE:
        label = _nearest_label(control, texts)
    if not label:
        stats["pruned"]["unnamed"] += 1
        return

    elements.append(Element(
        number=0, label=label,
        kind="type" if ctype in TYPEABLE else "click",
        control_type=ctype, window=title,
        rect=(r.left, r.top, r.width(), r.height()), control=control,
        state=_read_state(control, ctype), source_hwnd=source_hwnd))


def _top_level_title(control) -> str:
    """Walk up to the owning top-level window and read its title."""
    node, guard = control, 0
    while node is not None and guard < 24:
        try:
            if node.ControlTypeName == "WindowControl":
                return node.Name or ""
            node = node.GetParentControl()
        except Exception:
            break
        guard += 1
    return ""


def act(element: Element, allowlist: list[str], value: str | None = None,
        use_mouse: bool = False, dry_run: bool = False) -> dict:
    """Click or type into one element, after re-checking the window allowlist.

    The allowlist is verified here and not only at scrape time, because the
    screen can change between deciding and acting - which is precisely when an
    agent would otherwise click the wrong thing.
    """
    window = _top_level_title(element.control) or element.window
    if window not in allowlist:
        raise NotAllowed(
            f"refusing to act on {element.label!r}: its window {window!r} is not "
            f"on the allowlist {allowlist}")

    plan = {"action": element.kind, "label": element.label, "window": window,
            "value": value, "via": "mouse" if use_mouse else "uia_pattern"}
    if dry_run:
        plan["performed"] = False
        return plan

    if element.kind == "type":
        text = value or ""
        try:
            element.control.GetValuePattern().SetValue(text)
        except Exception:
            element.control.SetFocus()
            auto.SendKeys("{Ctrl}a", waitTime=0)
            auto.SendKeys(text.replace("{", "{{").replace("}", "}}"), waitTime=0)
    elif use_mouse:
        win = auto.WindowControl(searchDepth=1, Name=window)
        if not win.Exists(0):
            raise NotAllowed(f"target window {window!r} vanished before the click")
        x, y, w, h = element.rect
        element.control.Click(simulateMove=False)
        plan["clicked_at"] = (x + w // 2, y + h // 2)
    else:
        try:
            element.control.GetInvokePattern().Invoke()
        except Exception:
            try:
                element.control.GetTogglePattern().Toggle()
            except Exception:
                element.control.Click(simulateMove=False)
                plan["via"] = "mouse_fallback"

    plan["performed"] = True
    time.sleep(0.15)   # let the app process the message before the next scrape
    return plan


def close_window(title: str, allowlist: list[str]) -> None:
    """Politely close one allowlisted window (used to reset between tasks)."""
    if title not in allowlist:
        raise NotAllowed(f"{title!r} is not on the allowlist")
    win = auto.WindowControl(searchDepth=1, Name=title)
    if win.Exists(0):
        ctypes.windll.user32.PostMessageW(win.NativeWindowHandle, 0x0010, 0, 0)
        time.sleep(0.2)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Print the numbered controls of a window")
    ap.add_argument("--window", action="append", required=True,
                    help="exact window title; repeatable")
    args = ap.parse_args()

    els, st = scrape(args.window)
    print(f"walked {st['walked']} nodes, kept {st['kept']}, {st['ms']} ms")
    print(f"pruned: {st['pruned']}")
    for e in els:
        print(f"  {e.number:>3}  {e.describe()}")
