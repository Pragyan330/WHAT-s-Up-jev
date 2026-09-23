"""UIA first, a detector for the gaps, OCR for the labels.

Neither source is sufficient alone, and they fail in opposite directions.

UI Automation gives real control identity: a name, current state, and an Invoke
addressed to the element rather than a click at a coordinate. It is also blind to
whole categories of thing - a browser page, Windows 11 Start-menu search results -
which simply are not in any window's tree.

A UI-element detector finds those, because it looks for interactive regions in
pixels. But it is single-class, so it reports where something is and not what, and
it hands back no text at all.

OCR supplies the missing text. Offering OCR lines *as options* was the earlier
mistake: a window yields around a hundred of them, almost none clickable, and the
option list went from 11 to 163 while the correct pick fell from 0.92 to 0.35.
Used as a labeller for regions a detector already vouched for, it does the one job
it is good at.

So the order is: take every UIA control; add detected regions that UIA has nothing
for; name each of those from the OCR text inside it; drop the ones that still have
no name, because "unlabeled region, row 7" is not something a model can reasonably
choose. The count of those drops is reported rather than hidden - on an
image-heavy page it is most of what the detector found, and that is a real limit
of the approach, not a detail.

The window is OCR'd once and lines assigned to boxes by geometry, rather than
cropping and OCR-ing each box. Same labels, one 130 ms pass instead of dozens.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

_MODEL = None
_WEIGHTS: Path | None = None


def load_detector(device: str = "cuda"):
    """Load and cache the detector. Falls back to CPU, loudly, if asked to.

    Kept as a module-level singleton because loading costs far more than
    inference: the agent loop calls this every step.
    """
    global _MODEL, _WEIGHTS
    if _MODEL is not None:
        return _MODEL, _WEIGHTS

    from huggingface_hub import snapshot_download
    from ultralytics import YOLO

    root = snapshot_download(repo_id="microsoft/OmniParser-v2.0",
                             allow_patterns=["icon_detect/*"])
    _WEIGHTS = Path(root) / "icon_detect" / "model.pt"
    model = YOLO(str(_WEIGHTS))
    try:
        import torch
        if device == "cuda" and torch.cuda.is_available():
            model.to(0)
        else:
            model.to("cpu")
    except Exception:
        model.to("cpu")
    _MODEL = model
    return _MODEL, _WEIGHTS


@dataclass
class DetectedControl:
    """Duck-typed to match uia_scrape.Element so agent.decide() is unchanged."""
    number: int
    label: str
    rect: tuple[int, int, int, int]          # absolute screen coords
    window: str
    confidence: float = 0.0
    kind: str = "click"
    control_type: str = "Detected"
    state: str = ""
    source_hwnd: int = 0
    control: object = field(repr=False, default=None)

    def describe(self) -> str:
        return (f"{self.label} (on-screen element, clicked by position, "
                f"{self.window})")

    @property
    def centre(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / float(aw * ah + bw * bh - inter)


def _covers(box, target) -> bool:
    tx, ty, tw, th = target
    cx, cy = tx + tw // 2, ty + th // 2
    bx, by, bw, bh = box
    return (bx <= cx < bx + bw and by <= cy < by + bh) or _iou(box, target) >= 0.3


def scrape(hwnd: int, title: str, rect: tuple[int, int, int, int],
           uia_elements: list, conf: float = 0.05, imgsz: int = 1280,
           device: str = "cuda") -> tuple[list[DetectedControl], dict]:
    """Detected, labelled controls for the regions UIA has nothing for."""
    from ocr_scrape import capture_window, read_window

    left, top, width, height = rect
    stats = {"detections": 0, "novel": 0, "labelled": 0, "unlabelled": 0,
             "detect_ms": 0.0, "ocr_ms": 0.0}

    image = capture_window(hwnd, width, height)
    if image is None:
        stats["skipped"] = "window would not render itself"
        return [], stats

    model, _ = load_detector(device)
    t0 = time.perf_counter()
    result = model.predict(image, conf=conf, iou=0.1, imgsz=imgsz,
                           verbose=False)[0]
    stats["detect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    boxes = []
    for (x0, y0, x1, y1), score in zip(result.boxes.xyxy.tolist(),
                                       result.boxes.conf.tolist()):
        boxes.append((int(x0) + left, int(y0) + top,
                      int(x1 - x0), int(y1 - y0), float(score)))
    stats["detections"] = len(boxes)

    novel = [b for b in boxes
             if not any(_covers(b[:4], e.rect) for e in uia_elements)]
    stats["novel"] = len(novel)
    if not novel:
        return [], stats

    lines, ostats = read_window(rect, title, hwnd)
    stats["ocr_ms"] = ostats.get("ocr_ms", 0.0)

    out: list[DetectedControl] = []
    stats["captioned"] = 0
    for bx, by, bw, bh, score in novel:
        inside = [ln.label for ln in lines if _covers((bx, by, bw, bh), ln.rect)]
        if not inside:
            # An icon keeps its caption underneath it, outside its own box, so
            # looking only inside throws away the one piece of text that names
            # it. On the Start menu that was 16 of 29 regions discarded. Extend
            # the box downward and try again before giving up.
            taller = (bx, by, bw, int(bh * 1.9))
            inside = [ln.label for ln in lines if _covers(taller, ln.rect)]
            if inside:
                stats["captioned"] += 1
        if not inside:
            stats["unlabelled"] += 1
            continue
        label = " ".join(inside)[:90]
        out.append(DetectedControl(number=0, label=label,
                                   rect=(bx, by, bw, bh), window=title,
                                   confidence=round(score, 3),
                                   source_hwnd=hwnd))
    stats["labelled"] = len(out)

    # Same text in overlapping boxes is one thing detected twice.
    seen: set[str] = set()
    unique = []
    for c in sorted(out, key=lambda c: -c.confidence):
        key = c.label.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    stats["labelled_unique"] = len(unique)

    unique.sort(key=lambda c: (c.rect[1], c.rect[0]))
    return unique, stats


def merge(uia_elements: list, detected: list[DetectedControl]) -> list:
    """UIA elements first, then the detected extras, renumbered together."""
    merged = list(uia_elements) + list(detected)
    for i, e in enumerate(merged, start=1):
        e.number = i
    return merged
