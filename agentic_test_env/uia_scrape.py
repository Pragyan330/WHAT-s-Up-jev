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

    def describe(self) -> str:
        """What the model sees. Label plus a hint at what kind of control it is."""
        noun = {"type": "text field", "click": "button"}.get(self.kind, "control")
        if self.control_type == "MenuItemControl":
            noun = "menu item"
        elif self.control_type == "CheckBoxControl":
            noun = "checkbox"
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


def scrape(titles: list[str], max_depth: int = 6) -> tuple[list[Element], dict]:
    """Walk the allowlisted windows and return numbered, actionable elements."""
    t0 = time.perf_counter()
    elements: list[Element] = []
    stats = {"walked": 0, "pruned": {"chrome": 0, "disabled": 0, "offscreen": 0,
                                     "no_rect": 0, "unnamed": 0, "not_actionable": 0}}

    for title, win in find_windows(titles):
        nodes = list(auto.WalkControl(win, includeTop=False, maxDepth=max_depth))
        stats["walked"] += len(nodes)
        texts = [(c, c.Name) for c, _ in nodes
                 if c.ControlTypeName == "TextControl" and c.Name]

        for control, _depth in nodes:
            ctype = control.ControlTypeName
            if ctype in CHROME or control.Name in CHROME_NAMES:
                stats["pruned"]["chrome"] += 1
                continue
            if ctype not in CLICKABLE and ctype not in TYPEABLE:
                stats["pruned"]["not_actionable"] += 1
                continue
            try:
                if not control.IsEnabled:
                    stats["pruned"]["disabled"] += 1
                    continue
                if control.IsOffscreen:
                    stats["pruned"]["offscreen"] += 1
                    continue
                r = control.BoundingRectangle
                if r.width() <= 0 or r.height() <= 0:
                    stats["pruned"]["no_rect"] += 1
                    continue
            except Exception:
                stats["pruned"]["no_rect"] += 1
                continue

            label = (control.Name or "").strip()
            if not label and ctype in TYPEABLE:
                label = _nearest_label(control, texts)
            if not label:
                stats["pruned"]["unnamed"] += 1
                continue

            elements.append(Element(
                number=0, label=label,
                kind="type" if ctype in TYPEABLE else "click",
                control_type=ctype, window=title,
                rect=(r.left, r.top, r.width(), r.height()), control=control))

    # Reading order, which is how a human numbering overlay would do it. Sorting
    # is cosmetic - experiment 04 found no positional bias - but it keeps the
    # numbers stable between two scrapes of an unchanged screen.
    elements.sort(key=lambda e: (e.window, e.rect[1] // 20, e.rect[0]))
    for i, e in enumerate(elements, start=1):
        e.number = i

    stats["kept"] = len(elements)
    stats["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return elements, stats


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
