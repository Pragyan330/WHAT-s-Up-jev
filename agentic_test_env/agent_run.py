"""Type a prompt, Jev drives the window you pointed it at.

This is the mock-app loop aimed at real software. The decision layer is unchanged
- same four judgements in one request, same risk gate - but two things had to
change once the target became a window someone actually works in.

**Identity is the window handle, not the title.** Real applications rewrite their
titles constantly: an editor marks a file dirty, a browser retitles on every
navigation. A title-based allowlist silently stops matching the window it was
meant to protect, or starts matching a different one. Handles do not move.

**The agent is not allowed to touch the thing running it.** The console this is
launched from, every window belonging to that console's process, and this
process's own windows are refused as targets. That is not politeness; a computer
control agent whose first plausible action is closing its own terminal is a very
short experiment. `--allow-protected` exists for when that is the actual test,
and it is off by default.

Everything else is inherited: unrecoverable actions are never taken
automatically, consequential ones ask first, budgets cap the run, and every step
is written to runtime/agent_run_log.jsonl.

    .venv/Scripts/python.exe agentic_test_env/agent_run.py --list
    .venv/Scripts/python.exe agentic_test_env/agent_run.py
    .venv/Scripts/python.exe agentic_test_env/agent_run.py --target "Notepad" --once "turn on word wrap"
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Window titles contain characters a cp1252 console cannot encode - a Visual
# Studio Code title here carries a zero-width space - and printing one raises
# UnicodeEncodeError, which would kill a run for the sake of a log line.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uiautomation as auto

import risk
from agent import NONE_KEY, BudgetExceeded, BudgetGuard, decide, pick_window
from typesafe_sdk import TypeSafeClient
from keyboard import KeyControl, key_controls
from detect_scrape import DetectedControl, load_detector
from detect_scrape import merge as detect_merge, scrape as detect_scrape
from ocr_scrape import OcrControl, merge as ocr_merge, read_window
from uia_scrape import Element, list_top_level, scrape_hwnds

LOG = HERE / "runtime" / "agent_run_log.jsonl"

# Windows that are never targets unless --allow-protected says otherwise. The
# console's own process covers the terminal and, when launched from an
# integrated terminal, the editor hosting it.
DENY_TITLE = re.compile(
    r"(visual studio code|vscode|windows powershell|command prompt|"
    r"task manager|registry editor|credential manager|windows security)", re.I)

SHELL_TITLES = ("Start", "Taskbar", "Search")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class Stopped(RuntimeError):
    pass


_VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "esc": 0x1B,
       "space": 0x20, "pause": 0x13, "scroll": 0x91, "insert": 0x2D,
       "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
       "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}
_VK.update({chr(ord("a") + i): 0x41 + i for i in range(26)})
_VK.update({str(i): 0x30 + i for i in range(10)})


class EmergencyStop:
    """A global key chord that halts the agent wherever it is.

    Polls GetAsyncKeyState on a daemon thread rather than registering a hotkey,
    because RegisterHotKey needs a message loop and this process is busy walking
    UIA trees. Polling works no matter which window has focus, which is the whole
    point: the moment you want to stop an agent is usually the moment it has
    taken focus somewhere you did not expect.

    The flag is checked before every decision, before every action, and inside
    the settle wait. A UIA Invoke already in flight cannot be interrupted, but
    those take milliseconds; nothing further will be started.
    """

    def __init__(self, chord: str = "ctrl+shift+q", poll: float = 0.04):
        self.chord = chord
        try:
            self.keys = [_VK[k.strip().lower()] for k in chord.split("+")]
        except KeyError as exc:
            raise SystemExit(f"unknown key in --stop-key {chord!r}: {exc}")
        self.poll = poll
        self.event = threading.Event()
        self._alive = True
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while self._alive:
            try:
                if all(user32.GetAsyncKeyState(k) & 0x8000 for k in self.keys):
                    if not self.event.is_set():
                        self.event.set()
                        print()
                        print(f"  !! STOP ({self.chord}) - halting now",
                              flush=True)
            except Exception:
                pass
            time.sleep(self.poll)

    @property
    def stopped(self) -> bool:
        return self.event.is_set()

    def check(self, where: str) -> None:
        if self.event.is_set():
            raise Stopped(f"stopped by {self.chord} before {where}")

    def clear(self) -> None:
        self.event.clear()


def protected_pids() -> set[int]:
    pids = {os.getpid()}
    try:
        console = kernel32.GetConsoleWindow()
        if console:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(console, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
    except Exception:
        pass
    return pids


def pid_of(hwnd: int) -> int:
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


GW_OWNER = 4


def owner_of(hwnd: int) -> int:
    """The window that owns this one, which is how dialogs relate to a parent."""
    try:
        return int(user32.GetWindow(hwnd, GW_OWNER) or 0)
    except Exception:
        return 0


# Anything that closes a window. Whether that is cheap or not cannot be read off
# the label: "Close Preferences" is a panel, "Close Calculator" is the
# application. The structural question is answerable though - is this control
# closing a window we were explicitly pointed at, or one that appeared during the
# task? Closing the former can discard someone's work; the latter is a dialog
# going away, which is what dialogs do.
_CLOSE_ISH = re.compile(r"^\s*(close|exit|quit)\b")


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1


def screen_of(hwnd: int) -> str:
    """Which display a window is on, as a short label for the listing.

    Worth showing because a second monitor is invisible from the terminal. An
    agent driving a window you cannot see is the same problem as an agent driving
    the wrong window, except you find out later.
    """
    try:
        mon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd),
                                       MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return "?"
        if info.dwFlags & MONITORINFOF_PRIMARY:
            return "screen 1"
        return f"screen @{info.rcMonitor.left},{info.rcMonitor.top}"
    except Exception:
        return "?"


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def window_at(x: int, y: int) -> int:
    """The root window actually under a screen point, for validating a click."""
    try:
        hwnd = user32.WindowFromPoint(_POINT(x, y))
        return root_of(int(hwnd)) if hwnd else 0
    except Exception:
        return 0


def is_protected(win: dict, own: set[int]) -> str | None:
    if pid_of(win["hwnd"]) in own:
        return "belongs to this agent's own process or its terminal"
    if DENY_TITLE.search(win["title"]):
        return "matches the protected-window denylist"
    return None


def log(event: str, **fields) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": round(time.time(), 3), "event": event,
                             **fields}) + "\n")


def _timing(steps: list[dict], t_task: float) -> dict:
    """Split a task's wall-clock into model, scrape, and everything else.

    The three are worth keeping apart. Jev is the part being evaluated; the UIA
    walk is our scraper; the remainder is waiting on the application to repaint
    after a click, which is neither of ours and usually the largest slice.
    """
    total = (time.perf_counter() - t_task) * 1000
    decide_ms = sum(s["decision"]["ms"] for s in steps if "decision" in s)
    scrape_ms = sum(s["stats"]["ms"] for s in steps if "stats" in s)
    return {"total_ms": round(total, 1), "decide_ms": round(decide_ms, 1),
            "scrape_ms": round(scrape_ms, 1),
            "other_ms": round(total - decide_ms - scrape_ms, 1),
            "decisions": sum(1 for s in steps if "decision" in s)}


def _timing_line(steps: list[dict], t_task: float) -> str:
    t = _timing(steps, t_task)
    return (f"time: {t['total_ms'] / 1000:.2f}s total = "
            f"{t['decide_ms']:.0f} ms Jev ({t['decisions']} calls) + "
            f"{t['scrape_ms']:.0f} ms scrape + "
            f"{t['other_ms']:.0f} ms app/waits")


GA_ROOT = 2


def root_of(hwnd: int) -> int:
    """The root window of a child HWND, which is not always its parent.

    This exists because of UWP. A Store app like Calculator puts its whole visual
    tree inside a Windows.UI.Core.CoreWindow child, hosted by an outer
    ApplicationFrameWindow - and the outer frame is the one with the title you
    targeted. Walking up to the *nearest* window ancestor therefore lands on the
    inner CoreWindow, whose handle is not the target, so the scope check refused
    Calculator's own buttons. GetAncestor(GA_ROOT) resolves the child back to the
    frame. Without this the agent cannot touch any modern Windows application.
    """
    try:
        return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
    except Exception:
        return hwnd


def top_level_hwnd(control) -> int:
    """Walk up to the owning top-level window and read its handle."""
    node, guard = control, 0
    while node is not None and guard < 30:
        try:
            if node.ControlTypeName in ("WindowControl", "PaneControl"):
                h = node.NativeWindowHandle
                if h:
                    return root_of(int(h))
            node = node.GetParentControl()
        except Exception:
            break
        guard += 1
    return 0


class Session:
    def __init__(self, args):
        self.args = args
        self.own = protected_pids()
        self.targets: dict[int, str] = {}          # hwnd -> title at select time
        self.target_pids: set[int] = set()
        self.root_targets: set[int] = set()   # explicitly chosen, not followed
        self.baseline: set[int] = set()    # windows open when the task started
        self.task_hint: str | None = None  # the prompt, for narrowing OCR lines
        self.last: tuple[str, object, list] | None = None   # prompt, decision, els
        self.budget = BudgetGuard(args.max_calls, args.lifetime_cap)
        self.client = TypeSafeClient(timeout=60.0)
        self.stop = EmergencyStop(args.stop_key)

    # -- targeting ---------------------------------------------------------

    def windows(self) -> list[dict]:
        wins = list_top_level()
        for w in wins:
            w["protected"] = is_protected(w, self.own)
        return wins

    def show_windows(self) -> list[dict]:
        wins = self.windows()
        print(f"\n  {len(wins)} visible top-level windows:")
        for i, w in enumerate(wins, start=1):
            mark = "  [protected]" if w["protected"] else ""
            where = screen_of(w["hwnd"])
            tag = "" if where == "screen 1" else f"  <{where}>"
            print(f"   {i:>3}. {w['title'][:60]}{tag}{mark}")
        print()
        return wins

    def set_target(self, spec: str) -> None:
        wins = self.windows()
        chosen = []
        take_all = spec.lower().startswith("all ")
        if take_all:
            spec = spec[4:].strip()

        if spec.isdigit():
            idx = int(spec) - 1
            if 0 <= idx < len(wins):
                chosen = [wins[idx]]
        else:
            matches = [w for w in wins if spec.lower() in w["title"].lower()]
            exact = [w for w in matches if w["title"].lower() == spec.lower()]
            if exact:
                chosen = exact[:1]
            elif len(matches) > 1 and not take_all:
                # A substring that hits several windows must not quietly target
                # them all. Asking for "Jev Agent Test Env" matched the mock app
                # AND its decoy dialog - two windows when one was meant, which is
                # the ordinary way an agent ends up acting somewhere nobody
                # intended. Disambiguate, or say "all" and mean it.
                print(f"  {spec!r} matches {len(matches)} windows; pick one by "
                      f"number, or use 'target all {spec}' to drive them all:")
                for w in matches:
                    n = wins.index(w) + 1
                    print(f"   {n:>3}. {w['title'][:70]}")
                return
            else:
                chosen = matches

        if not chosen and not spec.isdigit():
            # Nothing matched as text, so ask Jev which window was meant. This is
            # the same shape as every other decision here - a Choice over a list
            # with a no-match option - which is why "the notepad window" or "my
            # browser" can stand in for an exact title.
            eligible = [w for w in wins if not w["protected"]]
            if eligible:
                try:
                    pick = pick_window(self.client, self.budget, spec, eligible)
                except BudgetExceeded as exc:
                    print(f"  budget: {exc}")
                    return
                if pick["index"] is None:
                    hidden = [w["title"] for w in wins if w["protected"]]
                    print(f"  no window matches {spec!r} "
                          f"(is_open={pick['is_open']:.2f}, {pick['ms']:.0f} ms)")
                    if hidden:
                        print(f"  note: {len(hidden)} protected window(s) were "
                              f"not offered: {hidden[0][:50]!r}")
                    return
                # The is_open Noul was being asked and then ignored: only
                # "did it pick something" was checked. On "play back in black on
                # youtube" it answered 0.44 - genuinely unsure the described
                # window was even open - and the target was set to Chrome anyway,
                # which then had nothing useful in it. A judgement you ask for
                # and discard is worse than not asking.
                if pick["is_open"] < 0.6:
                    print(f"  best guess for {spec!r} is "
                          f"{eligible[pick['index']]['title'][:48]!r} (p="
                          f"{pick['p']:.2f}) but is_open={pick['is_open']:.2f} "
                          f"- not confident that window is really the one.")
                    print("  say which window with 'windows' then 'target <n>', "
                          "or name the app more directly.")
                    return
                chosen = [eligible[pick["index"]]]
                print(f"  matched {spec!r} -> p={pick['p']:.2f} "
                      f"is_open={pick['is_open']:.2f} ({pick['ms']:.0f} ms)")

        if not chosen:
            print(f"  no window matches {spec!r}. Try 'windows'.")
            return
        blocked = [w for w in chosen if w["protected"]]
        if blocked and not self.args.allow_protected:
            for w in blocked:
                print(f"  refusing {w['title'][:60]!r}: {w['protected']}")
            chosen = [w for w in chosen if not w["protected"]]
        if not chosen:
            return

        self.targets = {w["hwnd"]: w["title"] for w in chosen}
        self.target_pids = {pid_of(w["hwnd"]) for w in chosen}
        self.root_targets = {w["hwnd"] for w in chosen}
        for w in chosen:
            where = screen_of(w["hwnd"])
            print(f"  target: {w['title'][:60]!r}  (hwnd {w['hwnd']}, {where})")
        log("target_set", targets=[{"hwnd": h, "title": t}
                                   for h, t in self.targets.items()])

    # -- acting ------------------------------------------------------------

    def confirm(self, element: Element, tier: str, why: str) -> bool:
        if self.args.yes:
            return True
        # isatty() is not enough: under a pipe or a non-interactive runner it can
        # report a terminal and then hand input() an immediate EOF, which used to
        # crash the run mid-task. An unanswerable question is a no.
        try:
            if not sys.stdin.isatty():
                raise EOFError
            answer = input(f"      {tier} action: {element.label!r}. "
                           f"Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n      refusing {tier} action: nothing here can answer the "
                  f"prompt (pass --yes to allow it unattended)")
            return False
        return answer.strip().lower() in ("y", "yes")

    def open_start_menu(self) -> bool:
        """Press Win and adopt whatever appears as the target.

        The escape hatch for "the application is not open". Pressing Win is
        global, so it needs no focus, and it surfaces two windows worth having:
        Start, whose search box can find any installed application, and Taskbar,
        which reaches the ones already running. On a machine with the taskbar
        hidden this is the only route to either.

        Adopted as followed targets rather than root targets, so closing them is
        not treated as discarding someone's work.
        """
        # Look before pressing. The Windows key toggles, so calling this while
        # Start is already open closes it and reports that nothing appeared -
        # which is exactly what happened on the second attempt at "Open Paint.",
        # because the first attempt had left Start open.
        # Start from a known state. A shell left open by an earlier run is not a
        # fresh Start menu - it may already have text typed into its search box,
        # and adopting it hands this task someone else's half-finished one. Escape
        # dismisses whatever is up; then Win opens a clean one.
        stale = [w for w in self.windows() if w["title"] in SHELL_TITLES
                 and w["title"] != "Taskbar"]
        if stale:
            print("  dismissing a shell left open from an earlier run")
            auto.SendKeys("{Esc}", waitTime=0)
            time.sleep(0.5)

        # Only a real Start or Search window counts as "already open". The Taskbar
        # survives Escape, so accepting it here would short-circuit the Win press
        # and leave the agent with a taskbar and no search box.
        already = [w for w in self.windows()
                   if w["title"] in SHELL_TITLES and not w["protected"]]
        if any(w["title"] != "Taskbar" for w in already):
            for w in already:
                self.targets[w["hwnd"]] = w["title"]
            self.target_pids |= {pid_of(w["hwnd"]) for w in already}
            names = ", ".join(repr(w["title"]) for w in already)
            print(f"  the shell is already open: {names}")
            return True

        before = {w["hwnd"] for w in self.windows()}
        auto.SendKeys("{Win}", waitTime=0)
        deadline = time.perf_counter() + 2.5
        while time.perf_counter() < deadline and not self.stop.stopped:
            time.sleep(0.2)
            fresh = [w for w in self.windows()
                     if w["hwnd"] not in before and not w["protected"]]
            if fresh:
                for w in fresh:
                    self.targets[w["hwnd"]] = w["title"]
                self.target_pids |= {pid_of(w["hwnd"]) for w in fresh}
                names = ", ".join(repr(w["title"][:28]) for w in fresh)
                print(f"  opened the shell: {names}")
                log("start_menu_opened", windows=[w["title"] for w in fresh])
                return True
        print("  pressed the Windows key but nothing new appeared")
        return False

    def owned_by_target(self, hwnd: int) -> bool:
        """Is this window a popup belonging to something we are driving?

        Menu flyouts are their own top-level HWNDs, and they are unnamed, so they
        never show up in window enumeration and cannot be followed by title. That
        is why Notepad's "Word wrap" was found by the scrape - it sits inside the
        app's UIA tree - and then refused by the scope check as a stranger. The
        owner chain is the real relationship: the flyout is owned by the window
        that opened it. Walking a few links covers a submenu off a menu.
        """
        seen, node = set(), hwnd
        for _ in range(4):
            node = owner_of(node)
            if not node or node in seen:
                return False
            if node in self.targets:
                return True
            seen.add(node)
        return False

    def focus_target(self) -> int | None:
        """Bring a target window to the front and confirm it actually got there.

        SetForegroundWindow is advisory - Windows refuses it from a process that
        does not own the foreground - so the result has to be checked rather than
        assumed. Returning None means "do not send keystrokes".
        """
        for hwnd in self.targets:
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                continue
            time.sleep(0.12)
            front = user32.GetForegroundWindow()
            if int(front) in self.targets:
                return int(front)
            try:
                if top_level_hwnd(auto.ControlFromHandle(front)) in self.targets:
                    return int(front)
            except Exception:
                pass
        return None

    def press(self, key: KeyControl) -> dict:
        if not key.global_ok and self.focus_target() is None:
            raise PermissionError(
                f"refusing {key.label!r}: the target window is not in the "
                f"foreground, so the keystroke would land in whatever is")
        auto.SendKeys(key.sequence, waitTime=0)
        return {"via": "sendkeys", "keys": key.sequence}

    def click_point(self, element) -> dict:
        """Click an OCR line by position, after proving the point is ours.

        Two checks, because a coordinate is not an identity. The point has to sit
        inside a target window's rectangle, and the window actually under it has
        to be a target - otherwise something has moved in front since the pixels
        were read, and the click would land on a bystander.
        """
        x, y = element.centre
        inside = False
        for hwnd in self.targets:
            win = next((w for w in self.windows() if w["hwnd"] == hwnd), None)
            if not win:
                continue
            left, top, width, height = win["rect"]
            if left <= x < left + width and top <= y < top + height:
                inside = True
                break
        if not inside:
            raise PermissionError(
                f"refusing: ({x},{y}) for {element.label!r} is outside every "
                f"target window")

        on_top = window_at(x, y)
        if on_top not in self.targets and not self.owned_by_target(on_top):
            raise PermissionError(
                f"refusing: the window under ({x},{y}) is {on_top}, not a "
                f"target - something moved in front of what was read")

        auto.Click(x, y, waitTime=0)
        return {"via": "ocr_click", "at": (x, y)}

    def ask_text(self, element) -> str | None:
        """Ask what to type. Returns None when there is nobody to ask."""
        try:
            if not sys.stdin.isatty():
                raise EOFError
            answer = input(f"      text for {element.label!r} "
                           f"(blank to skip): ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        answer = answer.strip()
        return answer or None

    def act(self, element, value: str | None) -> dict:
        if isinstance(element, KeyControl):
            return self.press(element)
        if isinstance(element, (OcrControl, DetectedControl)):
            return self.click_point(element)

        # Provenance first: this element came out of a scrape of a target window,
        # which is exact. Re-deriving the window by walking up from the control is
        # the fallback, and on composed windows it is simply wrong - Start is
        # several bridged HWNDs, so its own search box looked like a stranger.
        #
        # Re-derivation existed to catch the screen changing between deciding and
        # acting, so that protection is kept a different way: the control has to
        # still be on screen right now.
        if element.source_hwnd and element.source_hwnd in self.targets:
            try:
                if element.control.IsOffscreen:
                    raise PermissionError(
                        f"refusing: {element.label!r} is no longer on screen; "
                        f"the window changed after it was read")
            except PermissionError:
                raise
            except Exception:
                raise PermissionError(
                    f"refusing: {element.label!r} can no longer be read; the "
                    f"window changed after it was scraped")
        else:
            hwnd = top_level_hwnd(element.control)
            if hwnd not in self.targets and not self.owned_by_target(hwnd):
                raise PermissionError(
                    f"refusing: {element.label!r} belongs to window {hwnd}, "
                    f"which is not the target ({list(self.targets)})")

        if element.kind == "type":
            text = value or ""
            try:
                element.control.GetValuePattern().SetValue(text)
                return {"via": "value_pattern", "typed": text}
            except Exception:
                # SendKeys goes to whatever holds focus, so it is only safe once
                # the target window is verifiably in front. Anything else and the
                # keystrokes land in a window nobody asked us to touch.
                element.control.SetFocus()
                time.sleep(0.15)
                front = user32.GetForegroundWindow()
                if top_level_hwnd(auto.ControlFromHandle(front)) not in self.targets:
                    raise PermissionError(
                        "refusing to send keystrokes: the target window is not in "
                        "the foreground, so they would land somewhere else")
                auto.SendKeys(text.replace("{", "{{").replace("}", "}}"),
                              waitTime=0)
                return {"via": "sendkeys", "typed": text}

        # Menus open with ExpandCollapse, not Invoke. Invoke on Notepad's "View"
        # reported success and did nothing visible, so the next scrape saw the
        # same 22 controls and the agent re-picked View - which looked like the
        # model looping when it was the executor using the wrong pattern. Only
        # expand a node that is actually collapsed: a leaf item like "Word wrap"
        # reports LeafNode and has to fall through to Invoke.
        if element.control_type in ("MenuItemControl", "SplitButtonControl",
                                    "ComboBoxControl", "TreeItemControl"):
            try:
                ec = element.control.GetExpandCollapsePattern()
                if ec.ExpandCollapseState == 0:          # 0 = Collapsed
                    ec.Expand()
                    return {"via": "expand"}
            except Exception:
                pass

        try:
            element.control.GetInvokePattern().Invoke()
            return {"via": "invoke"}
        except Exception:
            pass
        try:
            element.control.GetTogglePattern().Toggle()
            return {"via": "toggle"}
        except Exception:
            pass
        try:
            element.control.GetSelectionItemPattern().Select()
            return {"via": "select"}
        except Exception:
            pass
        element.control.Click(simulateMove=False)
        return {"via": "click"}

    # -- looking at the target ---------------------------------------------

    def scrape_targets(self) -> tuple[list[Element], dict]:
        """Enumerate windows once, then scrape.

        Liveness, dialog-following and the scrape all need the window list, and
        the first cut called for it twice a step - a full top-level walk purely to
        check the target still existed. That redundancy was most of the 1.8s of
        harness overhead in a 4.3s task.
        """
        wins = self.windows()
        present = {w["hwnd"]: w for w in wins}

        for hwnd in [h for h in self.targets if h not in present]:
            print(f"      target window closed: {self.targets[hwnd][:50]!r}")
            self.targets.pop(hwnd)

        # Follow windows the target application opens. Without this the agent is
        # blind to every dialog - a Save As box, a confirmation, a wizard step -
        # which is most of what real software does in response to a click. Scoped
        # to the process we were pointed at, and protected windows never qualify.
        if not self.args.no_follow:
            for w in wins:
                if w["hwnd"] in self.targets or w["protected"]:
                    continue
                # Two narrow reasons to pull a window into scope, and they are
                # both about causation rather than kinship:
                #
                #   owned      a modal or dialog whose owner is a target window
                #   appeared   a window that did not exist when the task started,
                #              so it is a consequence of something the agent did
                #              - a file picker, a confirmation, the Start menu,
                #              which belong to other processes entirely
                #
                # Plain same-PID following was the first attempt and it was too
                # loose: it silently swallowed the mock app's decoy window, the
                # one deliberately left off the allowlist to detect exactly this,
                # and duly offered "Delete All Records" as an option. Sharing a
                # process is not evidence that a window is part of the task.
                # --follow-app restores it for when whole-app scope is wanted.
                owned = owner_of(w["hwnd"]) in self.targets
                appeared = bool(self.baseline) and w["hwnd"] not in self.baseline
                same_app = (self.args.follow_app
                            and pid_of(w["hwnd"]) in self.target_pids)
                if owned or appeared or same_app:
                    self.targets[w["hwnd"]] = w["title"]
                    why = ("owned by the target" if owned
                           else "opened after the task began" if appeared
                           else "same application")
                    print(f"      following new window ({why}): "
                          f"{w['title'][:55]!r}")

        if not self.targets:
            return [], {"kept": 0, "walked": 0, "ms": 0.0}
        elements, stats = scrape_hwnds(list(self.targets), self.targets,
                                       max_depth=self.args.depth,
                                       include_chrome=not self.args.no_chrome)
        # OCR is a supplement, not a replacement: UIA gives real control identity
        # and state, OCR only gives text and a position. It runs when asked for,
        # or automatically when a window yields so few controls that the tree is
        # clearly not describing what is on screen.
        if self.args.detect:
            added = 0
            for w in present.values():
                if w["hwnd"] not in self.targets:
                    continue
                try:
                    found, dstats = detect_scrape(w["hwnd"], w["title"],
                                                  w["rect"], elements,
                                                  device=self.args.detect_device)
                except Exception as exc:
                    print(f"      detector failed on {w['title'][:26]!r}: "
                          f"{type(exc).__name__}: {exc}")
                    continue
                if found:
                    elements = detect_merge(elements, found)
                    added += len(found)
                stats["detect_ms"] = (stats.get("detect_ms", 0.0)
                                      + dstats["detect_ms"])
                stats["detect_dropped"] = (stats.get("detect_dropped", 0)
                                           + dstats["unlabelled"])
            if added:
                stats["detect_added"] = added
                stats["kept"] = len(elements)

        want_ocr = self.args.ocr or (self.args.ocr_auto
                                     and stats.get("kept", 0) < self.args.ocr_below)
        if want_ocr:
            found: list[OcrControl] = []
            for w in present.values():
                if w["hwnd"] not in self.targets:
                    continue
                try:
                    lines, ostats = read_window(w["rect"], w["title"], w["hwnd"])
                except Exception as exc:
                    print(f"      OCR failed on {w['title'][:30]!r}: "
                          f"{type(exc).__name__}")
                    continue
                found += lines
                stats["ocr_ms"] = stats.get("ocr_ms", 0.0) + ostats["ocr_ms"]
            if found:
                before_n = len(elements)
                elements = ocr_merge(elements, found, hint=self.task_hint,
                                     limit=self.args.ocr_limit)
                stats["ocr_added"] = len(elements) - before_n
                stats["kept"] = len(elements)
        if not self.args.no_keys:
            keys = key_controls(len(elements) + 1)
            # The Windows key toggles. Offering "press the Windows key to open
            # the Start menu" while Start is already a target makes the agent
            # close the very thing it was given to work with - which is exactly
            # what happened on "Open Paint.": it opened Start, then chose the key
            # at p=0.97, and every target vanished.
            if any(t in SHELL_TITLES for t in self.targets.values()):
                keys = [k for k in keys if "Windows key" not in k.label]
                for i, k in enumerate(keys, start=len(elements) + 1):
                    k.number = i
            elements = elements + keys
            stats["keys"] = len(keys)
            stats["kept"] = len(elements)
        return elements, stats

    @staticmethod
    def _sig(elements: list[Element], stats: dict | None = None) -> tuple:
        """What counts as the screen having changed.

        Control labels alone are not enough. Pressing a digit in Calculator
        leaves every label exactly as it was and only moves the display, so a
        signature built from labels looks identical and the wait runs its full
        length on every step. The window's readable text is the part that moved,
        so it belongs in the signature.
        """
        labels = tuple(sorted((e.label, e.window) for e in elements))
        texts = tuple((stats or {}).get("texts", ()))
        return (labels, texts)

    def settle(self, before: tuple, before_windows: set[int] | None = None,
               timeout: float = 2.5) -> tuple[list[Element], dict]:
        """Wait for the screen to actually change, then hand back that scrape.

        Replaces a fixed sleep. A blind wait is either too short - the agent reads
        a screen the dialog has not appeared on yet - or wasted time. This polls
        until the control set differs, and the scrape it settled on is reused as
        the next step's input rather than being thrown away.
        """
        deadline = time.perf_counter() + timeout
        # Losing a target is the signal that something was launched: clicking a
        # Start-menu result closes the shell we were driving. Waiting for a new
        # window on *every* change instead cost 1.2s a step and turned a 13s
        # Calculator run into a 23s one, for a window that was never coming.
        before_targets = set(self.targets)
        result = self.scrape_targets()
        while time.perf_counter() < deadline and not self.stop.stopped:
            # A new top-level window counts as the screen settling, not just a
            # change in the control list. Launching an application is the case
            # that matters: clicking "Paint" in the Start menu closes the shell
            # immediately, so the control set changes at once and the old wait
            # returned before Paint existed. The agent then scraped the dying
            # shell, found nothing to do, and stopped without ever seeing that
            # it had succeeded.
            if before_windows is not None:
                now = {w["hwnd"] for w in self.windows()}
                if now - before_windows:
                    return self.scrape_targets()
            if self._sig(result[0], result[1]) != before:
                # Something changed, but give a launch a moment to produce a
                # window before accepting this as the final state.
                lost_a_target = bool(before_targets - set(self.targets))
                if before_windows is None or not lost_a_target:
                    return result
                grace = time.perf_counter() + 1.5
                while time.perf_counter() < grace and not self.stop.stopped:
                    if {w["hwnd"] for w in self.windows()} - before_windows:
                        return self.scrape_targets()
                    time.sleep(0.1)
                return self.scrape_targets()
            time.sleep(0.08)
            result = self.scrape_targets()
        return result

    def show_controls(self) -> None:
        if not self.targets:
            print("  no target set.")
            return
        elements, stats = self.scrape_targets()
        print()
        print(f"  {stats['kept']} actionable controls "
              f"({stats['walked']} nodes, {stats['ms']} ms):")
        for e in elements:
            tier = e.tier if isinstance(e, KeyControl) else risk.tier(e.label)
            print(f"   {e.number:>3}. [{tier:<14}] {e.describe()}")
        print()

    def explain(self) -> None:
        """The distribution behind the last decision, not just its winner."""
        if not self.last:
            print("  nothing decided yet.")
            return
        prompt, d, elements = self.last
        by_num = {str(e.number): e for e in elements}
        print()
        print(f"  last prompt: {prompt!r}")
        print(f"  target_present {d.target_present:.3f}   "
              f"task_complete {d.task_complete:.3f}   "
              f"confidence {d.confidence:.3f}")
        if d.text_to_type:
            print(f"  text_to_type: {d.text_to_type!r}")
        ranked = sorted(d.probabilities.items(), key=lambda kv: kv[1],
                        reverse=True)[:6]
        print("  top options by probability:")
        for key, prob in ranked:
            label = ("(none of these)" if key == NONE_KEY
                     else by_num[key].describe() if key in by_num else key)
            print(f"    {prob:6.3f}  {label}")
        print()

    # -- the loop ----------------------------------------------------------

    def run(self, prompt: str) -> dict:
        if not self.targets:
            # No target chosen, so read one out of the task itself. "turn on word
            # wrap in notepad" names its own window; making the user restate it
            # as a separate command is busywork.
            print("  no target set; working it out from the task...")
            self.set_target(prompt)
            if not self.targets and not self.args.no_start_fallback:
                # Nothing open matches, so rather than dead-ending, go through
                # the shell. The application the task needs may simply not be
                # running yet, and Start is how a person would deal with that.
                print("  nothing open matches; going through the Start menu")
                self.open_start_menu()
            if not self.targets:
                print("  could not tell which window you mean. "
                      "Try 'windows' then 'target <n>'.")
                return {"ok": False, "reason": "no_target"}

        history: list[str] = []
        steps: list[dict] = []
        self.task_hint = prompt
        t_task = time.perf_counter()
        # Offscreen and hidden windows are included deliberately. The rule
        # "appeared after the task began" was meant to catch dialogs the agent
        # caused, but a window that merely becomes visible - restored from
        # minimised, or sitting on a second monitor and raised by a focus change
        # - also counts as new against a visible-only snapshot. That is how a
        # Settings window already open on another screen could have been adopted
        # as a target by a task that had nothing to do with it.
        self.baseline = {w["hwnd"] for w in list_top_level(include_offscreen=True)}
        log("task_start", prompt=prompt, targets=list(self.targets))

        pending: tuple[list[Element], dict] | None = None
        # With 15 steps instead of 6 there is room to wander. Repeat detection
        # catches picking the same control twice; this catches the other shape,
        # where the agent keeps choosing different controls and nothing on screen
        # ever changes. Three of those and the loop is going nowhere, whatever it
        # thinks it is doing.
        stalled = 0
        last_screen: tuple | None = None

        for step in range(1, self.args.max_steps + 1):
            # The scrape the previous step already waited for is reused here
            # rather than taken again; settling and looking are the same act.
            elements, stats = pending if pending else self.scrape_targets()
            pending = None
            before_sig = self._sig(elements, stats)
            if last_screen is not None and before_sig == last_screen:
                stalled += 1
                if stalled >= 3:
                    print("      stopping: three actions and the screen has "
                          "not changed at all")
                    break
            else:
                stalled = 0
            last_screen = before_sig

            if not self.targets:
                print("      all target windows are gone; stopping")
                break
            if not elements:
                print("      no actionable controls in the target; stopping")
                break

            visible = stats.get("texts") or []
            state_text = " | ".join(visible[:25]) if visible else None
            self.stop.check("the next decision")
            try:
                d = decide(self.client, self.budget, prompt, elements, history,
                           state_text)
            except BudgetExceeded as exc:
                print(f"      budget: {exc}")
                break
            self.last = (prompt, d, elements)

            dropped = sum(stats.get("pruned", {}).values())
            print(f"   step {step}: {stats['kept']} controls "
                  f"({stats['walked']} nodes, {dropped} pruned, "
                  f"{stats['ms']} ms) -> "
                  f"{d.target_key!r} p={d.p_target:.2f} "
                  f"present={d.target_present:.2f} done={d.task_complete:.2f} "
                  f"({d.ms:.0f} ms)")

            rec = {"step": step, "stats": stats, "decision": d.as_dict()}

            if d.task_complete >= 0.5 and step > 1:
                print(f"      done: task_complete {d.task_complete:.2f}")
                rec["outcome"] = "complete"
                steps.append(rec)
                log("task_end", prompt=prompt, outcome="complete",
                    steps=len(steps), **_timing(steps, t_task))
                print(f"   {_timing_line(steps, t_task)}")
                return {"ok": True, "steps": steps}

            if d.target is None or d.target_key == NONE_KEY:
                print(f"      nothing to do here "
                      f"(present={d.target_present:.2f})")
                rec["outcome"] = "declined_none"
                steps.append(rec)
                break

            # A keystroke's tier is fixed and known. Everywhere else the tier
            # is inferred from a label, which has already misfired three times.
            if isinstance(d.target, KeyControl):
                tier = d.target.tier
                # Enter is consequential because it fires whatever holds focus
                # and the agent cannot see what that is. Directly after typing
                # into a field it can: submitting a search box is navigation, not
                # a commitment. Without this the Start-menu route stalls one step
                # from opening the app it just searched for.
                if ("Enter" in d.target.label and history
                        and history[-1].startswith("type on")):
                    tier = risk.REVERSIBLE
                p_min, present_min = risk.GATES[tier]
                may = d.p_target >= p_min and d.target_present >= present_min
                why = (f"{tier}: p={d.p_target:.2f} vs {p_min}, "
                       f"present={d.target_present:.2f} vs {present_min}")
            else:
                may, tier, why = risk.allows(d.target.label, d.p_target,
                                             d.target_present,
                                             self.args.allow_unrecoverable)
                if (tier == risk.REVERSIBLE
                        and _CLOSE_ISH.match(d.target.label.lower())
                        and top_level_hwnd(d.target.control) in self.root_targets):
                    tier = risk.CONSEQUENTIAL
                    p_min, present_min = risk.GATES[tier]
                    may = (d.p_target >= p_min
                           and d.target_present >= present_min)
                    why = (f"closing a window you targeted directly, so held to "
                           f"the {tier} bar: p={d.p_target:.2f} vs {p_min}, "
                           f"present={d.target_present:.2f} vs {present_min}")
            rec["tier"] = tier
            print(f"      -> {d.target.describe()}  [{tier}]")
            if not may:
                print(f"      declined: {why}")
                rec["outcome"] = f"blocked_{tier}"
                steps.append(rec)
                break
            if tier != risk.REVERSIBLE and not self.confirm(d.target, tier, why):
                print("      skipped by you")
                rec["outcome"] = "user_declined"
                steps.append(rec)
                break

            value = d.text_to_type if d.target.kind == "type" else None
            if d.target.kind == "type" and not value:
                # Jev returns judgements, not text, so it can pick the field but
                # never invent what goes in it. Code extracts candidates from the
                # request; when the request contains none, the person at the
                # terminal is the only source left.
                value = self.ask_text(d.target)
                if value is None:
                    print("      skipped: nothing to type")
                    rec["outcome"] = "no_text"
                    steps.append(rec)
                    break
            if self.args.dry_run:
                print(f"      would {d.target.kind}"
                      + (f" {value!r}" if value else ""))
                rec["outcome"] = "dry_run"
                steps.append(rec)
                break

            self.stop.check(f"acting on {d.target.label!r}")
            try:
                how = self.act(d.target, value)
            except PermissionError as exc:
                print(f"      BLOCKED: {exc}")
                rec["outcome"] = "blocked_scope"
                steps.append(rec)
                break
            except Exception as exc:
                print(f"      action failed: {type(exc).__name__}: {exc}")
                rec["outcome"] = "action_error"
                steps.append(rec)
                break

            print(f"      {d.target.kind} via {how['via']}"
                  + (f": {value!r}" if value else ""))
            rec["outcome"] = "acted"
            rec["how"] = how
            steps.append(rec)
            # Latency belongs in the log, not only on the terminal. It is the
            # thing this whole approach is being judged on, and the first time it
            # was asked for after a run, the answer had to come from scrollback.
            log("step", prompt=prompt, **{k: rec[k] for k in ("step", "outcome")},
                label=d.target.label, tier=tier,
                decide_ms=round(d.ms, 1), scrape_ms=stats["ms"],
                controls=stats["kept"], nodes=stats["walked"],
                in_tokens=d.in_tokens, out_tokens=d.out_tokens)
            history.append(f"{d.target.kind} on {d.target.label!r}")
            pending = self.settle(before_sig,
                                  {w["hwnd"] for w in self.windows()})

        log("task_end", prompt=prompt, outcome="stopped", steps=len(steps),
            **_timing(steps, t_task))
        print(f"   {_timing_line(steps, t_task)}")
        print(f"   budget: {json.dumps(self.budget.summary())}")
        return {"ok": True, "steps": steps}


HELP = """  commands:
    windows            list visible top-level windows
    target <n|text>    choose the window to drive. A number or exact title wins;
                       anything else is read as a description and Jev picks the
                       window ('the notepad window', 'my browser').
                       'target all <text>' drives every text match.
    start              open the Start menu and drive it (finds apps that are
                       not running; also reaches a hidden taskbar)
    controls           dump the numbered controls Jev would be offered
    explain            the probability distribution behind the last decision
    steps <n>          max actions per prompt (now: {steps})
    dry on|off         decide without acting (now: {dry})
    detect on|off      also offer detected on-screen elements, for windows
                       whose accessibility tree does not describe them
                       (browser pages, Start-menu results) (now: {detect})
    budget             show call usage
    help               this
    quit               exit
  anything else is a task for the current target."""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="window title substring to drive")
    ap.add_argument("--once", help="run a single prompt and exit")
    ap.add_argument("--list", action="store_true", help="list windows and exit")
    ap.add_argument("--max-steps", type=int, default=15,
                    help="actions per task. Raised from 6 because real "
                         "tasks are longer than the mock ones: opening an "
                         "application through the shell costs three")
    ap.add_argument("--depth", type=int, default=14,
                    help="how deep to walk each window's control tree. Real apps "
                         "nest far deeper than the old default of 6: the Windows "
                         "Search window yields 26 controls at depth 6 and 42 at "
                         "depth 20, so controls were being silently dropped")
    ap.add_argument("--max-calls", type=int, default=60,
                    help="hard cap on Jev calls for this run. A longer "
                         "horizon needs headroom; 60 calls is under a cent")
    ap.add_argument("--lifetime-cap", type=int, default=800)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="do not ask before consequential actions")
    ap.add_argument("--allow-unrecoverable", action="store_true",
                    help="permit data-destroying actions (off by default)")
    ap.add_argument("--allow-protected", action="store_true",
                    help="permit targeting this agent's own terminal or editor")
    ap.add_argument("--no-follow", action="store_true",
                    help="do not follow dialogs the target application opens")
    ap.add_argument("--follow-app", action="store_true",
                    help="also scope in every window of the target's process, "
                         "not just its dialogs and windows it opened")
    ap.add_argument("--no-chrome", action="store_true",
                    help="hide menu bars and window buttons from the model")
    ap.add_argument("--detect", action="store_true",
                    help="offer detected on-screen elements for the regions UIA "
                         "has nothing for, labelled from OCR inside each box")
    ap.add_argument("--detect-device", default="cuda",
                    help="cuda or cpu; cpu costs 790 ms a window against 51 ms")
    ap.add_argument("--ocr", action="store_true",
                    help="always OCR the target windows and offer the text lines "
                         "as clickable options alongside the UIA controls")
    ap.add_argument("--ocr-auto", action="store_true",
                    help="OCR only when a window exposes suspiciously few "
                         "controls, which means its tree is not describing it")
    ap.add_argument("--ocr-limit", type=int, default=12,
                    help="how many OCR text lines to offer, after narrowing them "
                         "to the ones related to the task")
    ap.add_argument("--ocr-below", type=int, default=12,
                    help="control count under which --ocr-auto kicks in")
    ap.add_argument("--no-keys", action="store_true",
                    help="do not offer keyboard shortcuts as options")
    ap.add_argument("--no-start-fallback", action="store_true",
                    help="do not fall back to the Start menu when no open "
                         "window matches the task")
    ap.add_argument("--stop-key", default="ctrl+shift+q",
                    help="global panic chord that halts the agent (default "
                         "ctrl+shift+q)")
    args = ap.parse_args()

    try:
        session = Session(args)
    except BudgetExceeded as exc:
        sys.exit(f"budget: {exc}")

    if args.list:
        session.show_windows()
        return

    if args.detect and args.once:
        from PIL import Image
        model, weights = load_detector(args.detect_device)
        model.predict(Image.new("RGB", (640, 480)), verbose=False)
        print(f"detector ready: {weights.name} on {args.detect_device}")

    if args.target:
        session.set_target(args.target)
    if args.once:
        try:
            session.run(args.once)
        except Stopped as exc:
            print(f"  {exc}")
        except KeyboardInterrupt:
            print("  interrupted")
        finally:
            session.budget.flush()
        return

    if args.detect:
        from PIL import Image
        model, weights = load_detector(args.detect_device)
        model.predict(Image.new("RGB", (640, 480)), verbose=False)
        print(f"detector ready: {weights.name} on {args.detect_device}")

    print("Jev computer control. Type a task, or 'help'.")
    print(f"  panic stop: hold {args.stop_key} at any time to halt mid-task.")
    print("  no target needed - say which app in the task, or use 'target'.")
    if not session.targets:
        session.show_windows()
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("quit", "exit"):
            break
        if low == "help":
            print(HELP.format(steps=args.max_steps,
                              dry="on" if args.dry_run else "off",
                              detect="on" if args.detect else "off"))
        elif low == "windows":
            session.show_windows()
        elif low == "start":
            session.open_start_menu()
        elif low in ("controls", "show"):
            session.show_controls()
        elif low in ("explain", "why"):
            session.explain()
        elif low.startswith("target "):
            session.set_target(line[7:].strip())
        elif low.startswith("steps "):
            args.max_steps = max(1, int(line.split()[1]))
            print(f"  max steps per prompt: {args.max_steps}")
        elif low.startswith("detect "):
            on = low.split()[1] == "on"
            if on and not args.detect:
                from PIL import Image
                model, weights = load_detector(args.detect_device)
                # One real inference, because loading the weights is not the
                # slow part - the first CUDA predict is, and paying it here
                # keeps it out of the first step's timing.
                model.predict(Image.new("RGB", (640, 480)), verbose=False)
                print(f"  detector ready: {weights.name} on "
                      f"{args.detect_device}")
            args.detect = on
            print(f"  detector: {'on' if on else 'off'}")
        elif low.startswith("dry "):
            args.dry_run = low.split()[1] == "on"
            print(f"  dry run: {'on' if args.dry_run else 'off'}")
        elif low == "budget":
            print(f"  {json.dumps(session.budget.summary())}")
        else:
            try:
                session.run(line)
            except Stopped as exc:
                print(f"  {exc}")
            except KeyboardInterrupt:
                print("  interrupted")
            except BudgetExceeded as exc:
                print(f"  budget: {exc}")
                break
            finally:
                # The panic flag is per task, not per session: stopping one run
                # should not wedge the prompt for every run after it.
                session.stop.clear()
    session.budget.flush()
    print(f"budget: {json.dumps(session.budget.summary())}")


if __name__ == "__main__":
    main()
