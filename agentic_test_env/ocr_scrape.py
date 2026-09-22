"""Read a window with OCR when the accessibility tree does not have it.

The numbered-control approach depends on something enumerating the controls. UIA
does that well for native and UWP apps, and not at all for some things: Windows 11
Start-menu search results are not in any window's tree at any depth, and a browser
publishes nothing about its page unless renderer accessibility is on. In those
cases the screen still shows the options - they simply are not in the model that
code can read.

So: capture the window, OCR it, and treat each line of text as a numbered option
clicked by position. Jev's job does not change at all; it still picks a number
from a list. Only where the list comes from changes.

Two things to be careful about, both of which shape the design.

**OCR sends screen text to the API**, so what gets captured matters. Capturing
the screen region under a window's rectangle is NOT the same as capturing that
window: asked to read a Chrome window sitting behind the editor, the first version
returned the editor's file tree, because those were the pixels in that rectangle.
Every option would have come from the wrong application, and unrelated on-screen
text would have been sent while the code claimed to be scoped to the target.

PrintWindow is used instead - the window renders its own content, occluded or not.
The screen-grab path remains only as a fallback for windows that refuse to render,
and it is taken only when the window is genuinely in the foreground.

**A coordinate click is blind.** UIA's Invoke is addressed to an element; a click
at (x, y) hits whatever is there now, which may not be what was read. So before
clicking, the point is checked twice: it must fall inside a target window's
rectangle, and the window actually under that point must be a target. That stops a
click landing on something that moved in front.

Windows' own OCR engine is used, through winocr, so there is no external binary to
install. A 1920x1080 capture takes about 130 ms, which is the same order as a UIA
walk of a complex window.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image, ImageGrab

import winocr

# --------------------------------------------------------------------------
# capturing one window's own pixels
# --------------------------------------------------------------------------
#
# The first version grabbed the screen region under the window's rectangle, and
# that is not the same thing at all. Asked to OCR a Chrome window sitting behind
# the editor, it returned the editor's file tree - "claude.md", ".venv",
# "_pycache" - because those pixels were what occupied that rectangle. Every
# option offered would have belonged to another application, and unrelated
# on-screen text would have gone to the API under the claim that capture was
# scoped to the target.
#
# PrintWindow asks the window to render itself into a bitmap, so what comes back
# is the window's own content whether or not anything is in front of it. That
# fixes the correctness problem and the privacy one together.

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def capture_window(hwnd: int, width: int, height: int):
    """The window's own pixels, via PrintWindow. None if it cannot render."""
    if not hwnd or width <= 0 or height <= 0:
        return None
    window_dc = mem_dc = bitmap = None
    try:
        window_dc = _user32.GetWindowDC(wintypes.HWND(hwnd))
        if not window_dc:
            return None
        mem_dc = _gdi32.CreateCompatibleDC(window_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not mem_dc or not bitmap:
            return None
        _gdi32.SelectObject(mem_dc, bitmap)
        if not _user32.PrintWindow(wintypes.HWND(hwnd), mem_dc,
                                   PW_RENDERFULLCONTENT):
            return None

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height          # negative = top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                ctypes.byref(info), DIB_RGB_COLORS):
            return None
        return Image.frombuffer("RGBA", (width, height), buffer.raw,
                                "raw", "BGRA", 0, 1).convert("RGB")
    except Exception:
        return None
    finally:
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        if window_dc:
            _user32.ReleaseDC(wintypes.HWND(hwnd), window_dc)


@dataclass
class OcrControl:
    """Duck-typed to match uia_scrape.Element so agent.decide() is unchanged."""
    number: int
    label: str
    rect: tuple[int, int, int, int]          # absolute screen coords
    window: str
    kind: str = "click"
    control_type: str = "OcrText"
    state: str = ""
    source_hwnd: int = 0
    control: object = None

    row: int = 0
    rows: int = 0

    def describe(self) -> str:
        # Position is the only thing separating ten near-identical lines. On a
        # search result list the top entry is the best match and the ones below
        # it are suggestions; "Paint" reads identically to "paint 3d" and "Q paint
        # brush" without knowing which came first, so the probability split ten
        # ways and nothing cleared the bar. Reading order carries that.
        where = f"row {self.row} of {self.rows} from the top" if self.rows else ""
        bits = ", ".join(x for x in ("text on screen", where,
                                     "clicked by position", self.window) if x)
        return f"{self.label} ({bits})"

    @property
    def centre(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)


def _usable(text: str) -> bool:
    text = text.strip()
    if len(text) < 2:
        return False
    return any(ch.isalnum() for ch in text)


def read_window(rect: tuple[int, int, int, int], title: str, hwnd: int = 0,
                start_number: int = 1) -> tuple[list[OcrControl], dict]:
    """OCR one window's rectangle into numbered, clickable text lines.

    Each line becomes one option rather than each word: a menu entry, a search
    result or a button caption is line-shaped, and per-word options would flood
    the list with fragments that mean nothing on their own.
    """
    import time
    t0 = time.perf_counter()
    left, top, width, height = rect
    # Clip to the virtual desktop; a window can extend past a screen edge and
    # ImageGrab will happily return a rect that no longer matches the offsets.
    left, top = max(left, -32000), max(top, -32000)
    if width <= 0 or height <= 0:
        return [], {"ocr_lines": 0, "ocr_ms": 0.0}

    # The window's own pixels first. Falling back to a screen grab is only safe
    # when the window is genuinely in front, because otherwise the grab returns
    # whatever is covering it - which is how a Chrome capture came back full of
    # the editor's file tree.
    image = capture_window(hwnd, width, height)
    source = "printwindow"
    if image is None:
        foreground = int(_user32.GetForegroundWindow() or 0)
        if hwnd and foreground and hwnd != foreground:
            return [], {"ocr_lines": 0, "ocr_ms": 0.0,
                        "ocr_skipped": "window is behind another and will not "
                                       "render itself"}
        image = ImageGrab.grab(bbox=(left, top, left + width, top + height),
                               all_screens=True).convert("RGB")
        source = "screengrab"
    result = winocr.recognize_pil_sync(image, lang="en")

    controls: list[OcrControl] = []
    for line in result.get("lines", []):
        text = (line.get("text") or "").strip()
        if not _usable(text):
            continue
        boxes = [w.get("bounding_rect") for w in line.get("words", [])
                 if w.get("bounding_rect")]
        if not boxes:
            continue
        x0 = min(b["x"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        x1 = max(b["x"] + b["width"] for b in boxes)
        y1 = max(b["y"] + b["height"] for b in boxes)
        controls.append(OcrControl(
            number=0, label=text, window=title, source_hwnd=hwnd,
            rect=(int(left + x0), int(top + y0),
                  int(x1 - x0), int(y1 - y0))))

    # Top to bottom, left to right, so the row index means what it says.
    controls.sort(key=lambda c: (c.rect[1], c.rect[0]))
    for i, c in enumerate(controls, start=start_number):
        c.number = i
    for i, c in enumerate(controls, start=1):
        c.row, c.rows = i, len(controls)

    return controls, {"ocr_lines": len(controls), "ocr_source": source,
                      "ocr_ms": round((time.perf_counter() - t0) * 1000, 1)}


def _inside(point: tuple[int, int], rect: tuple[int, int, int, int]) -> bool:
    x, y = point
    left, top, width, height = rect
    return left <= x < left + width and top <= y < top + height


_STOP = {"the", "a", "an", "open", "click", "press", "go", "to", "on", "in",
         "of", "for", "my", "me", "please", "and", "then", "with", "it", "this",
         "that", "into", "up", "at", "is", "be", "do", "some", "any"}


def _tokens(text: str) -> set[str]:
    out = set()
    for raw in text.lower().replace("/", " ").replace("-", " ").split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) > 1 and word not in _STOP:
            out.add(word)
    return out


def _relevant(controls: list[OcrControl], hint: str, limit: int
              ) -> list[OcrControl]:
    """Keep only the OCR lines that have something to do with the request.

    Offering every line of text was the mistake. A window yields around a hundred
    of them - headings, timestamps, pinned app names, captions - and almost none
    are things to click. With 163 options the probability spread so thin that even
    the search box, an obvious pick at 0.92 on a short list, fell to 0.40. More
    coverage bought less usable signal.

    So code narrows and the model selects, which is the same split used for text
    candidates and for the window shortlist. Scoring on shared words is crude, but
    it does not have to be clever: it only has to get the handful of plausible
    lines into a list short enough for a distribution to mean something.
    """
    wanted = _tokens(hint)
    if not wanted:
        return controls[:limit]
    scored = []
    for c in controls:
        have = _tokens(c.label)
        overlap = len(wanted & have)
        if not overlap:
            continue
        # A line that is mostly the thing asked for beats one that merely mentions
        # it, so "Paint" outranks "Coloring Games: Coloring Book & Painting".
        precision = overlap / max(1, len(have))
        scored.append((overlap + precision, c))
    scored.sort(key=lambda pair: (-pair[0], pair[1].rect[1]))
    return [c for _score, c in scored[:limit]]


def merge(uia_elements: list, ocr_controls: list[OcrControl],
          hint: str | None = None, limit: int = 12) -> list:
    """Append OCR lines, dropping any that describe a control UIA already offered.

    Matching on text alone is not enough, and the Start menu shows why: UIA calls
    the search field "Search box" while OCR reads its placeholder, "Search for
    apps, settings, and documents". Different strings, one control, two options -
    and the probability splits between them. With 146 options that left the
    correct pick at 0.51.

    So the test is geometric: if an OCR line's centre sits inside a UIA element's
    rectangle, UIA already has that thing, and its version is strictly better -
    real identity, current state, and an Invoke addressed to the element instead
    of a click at a coordinate.
    """
    known_text = {e.label.strip().lower() for e in uia_elements}
    boxes = [e.rect for e in uia_elements if getattr(e, "rect", None)]

    kept = []
    for c in ocr_controls:
        if c.label.strip().lower() in known_text:
            continue
        # Containment alone is not enough of a test. A list, pane or group in the
        # UIA tree can cover the entire results area, so "centre inside any UIA
        # rect" suppresses every OCR line in it - which would delete exactly what
        # OCR was added to find. Only a control of comparable size is plausibly
        # the same thing; a box eight times the area of a line of text is
        # furniture around it, not it.
        line_area = max(1, c.rect[2] * c.rect[3])
        if any(_inside(c.centre, box) and (box[2] * box[3]) <= line_area * 8
               for box in boxes):
            continue
        kept.append(c)

    if hint:
        kept = _relevant(kept, hint, limit)

    kept.sort(key=lambda c: (c.rect[1], c.rect[0]))
    for i, c in enumerate(kept, start=1):
        c.row, c.rows = i, len(kept)

    merged = list(uia_elements) + kept
    for i, e in enumerate(merged, start=1):
        e.number = i
    return merged
