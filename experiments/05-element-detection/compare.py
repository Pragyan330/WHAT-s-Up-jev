"""Does a small UI-element detector see what the accessibility tree cannot?

Read-only. No clicks, no Jev calls. The point is to answer one question before
committing to an architecture: if we swap OCR for a purpose-built detector, does
the correct target actually end up in the option list, and does the list stay
short enough for a probability distribution to mean anything?

The OCR attempt failed on the second half of that. It found what UIA missed - the
Start menu's search results are in pixels only - but it returned about 105 text
lines per window, nearly all of them headings, captions and timestamps rather
than things to click. Options went from 11 to 163 and the correct pick fell from
0.92 to 0.35. Coverage went up, usable signal went down.

A detector should behave differently because it is trained to find *interactive*
regions rather than any text at all. This measures whether that holds on real
Windows applications, which matters because the public GUI datasets (RICO, VINS)
are mostly mobile, and desktop chrome is not what these models saw most of.

Three things are measured per window:

  recall      of the controls UIA already knows about, how many does the
              detector also find? UIA is the ground truth here, on apps where it
              works, because a control it reports really is there.
  gain        how many detected regions have no UIA control at all? Those are
              either the coverage win or false positives, and the OCR text
              inside each one is printed so the difference is visible.
  volume      how many options would actually be offered, against UIA's count
              and OCR's line count.

    .venv/Scripts/python.exe experiments/05-element-detection/compare.py
    .venv/Scripts/python.exe experiments/05-element-detection/compare.py --windows Calculator Notepad
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "agentic_test_env"))

from huggingface_hub import snapshot_download
from ultralytics import YOLO

from ocr_scrape import capture_window, read_window
from uia_scrape import list_top_level, scrape_hwnds

RESULTS = HERE / "results.json"
DEFAULT_WINDOWS = ["Calculator", "Notepad", "Chrome", "Start", "Taskbar"]


def load_detector():
    path = snapshot_download(repo_id="microsoft/OmniParser-v2.0",
                             allow_patterns=["icon_detect/*"])
    weights = Path(path) / "icon_detect" / "model.pt"
    model = YOLO(str(weights))
    return model, weights


def boxes_from(model, image, conf: float, imgsz: int) -> list[tuple]:
    """Detected regions as (x, y, w, h, confidence), in image coordinates."""
    result = model.predict(image, conf=conf, iou=0.1, imgsz=imgsz,
                           verbose=False)[0]
    out = []
    for box, score in zip(result.boxes.xyxy.tolist(),
                          result.boxes.conf.tolist()):
        x0, y0, x1, y1 = box
        out.append((int(x0), int(y0), int(x1 - x0), int(y1 - y0), round(score, 3)))
    return out


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / float(aw * ah + bw * bh - inter)


def _covers(box: tuple[int, int, int, int], target: tuple[int, int, int, int]
            ) -> bool:
    """A detection counts as finding a control if it overlaps it meaningfully.

    Centre-inside is used alongside IoU because a detector often draws a tighter
    or looser box than the control's own rectangle, and a strict IoU would call
    that a miss when a click at the detected centre would land correctly - which
    is the only thing that matters downstream.
    """
    tx, ty, tw, th = target
    centre = (tx + tw // 2, ty + th // 2)
    bx, by, bw, bh = box
    inside = bx <= centre[0] < bx + bw and by <= centre[1] < by + bh
    return inside or _iou(box, target) >= 0.3


def examine(title_hint: str, model, conf: float, imgsz: int) -> dict | None:
    match = [w for w in list_top_level()
             if title_hint.lower() in w["title"].lower()]
    if not match:
        return None
    win = match[0]
    hwnd, title, rect = win["hwnd"], win["title"], win["rect"]
    left, top, width, height = rect

    image = capture_window(hwnd, width, height)
    if image is None:
        return {"window": title, "skipped": "window would not render itself"}

    uia, ustats = scrape_hwnds([hwnd], {hwnd: title}, max_depth=14,
                              include_chrome=True)
    lines, ostats = read_window(rect, title, hwnd)

    t0 = time.perf_counter()
    dets = boxes_from(model, image, conf, imgsz)
    detect_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Detections come back in image coordinates; UIA is in screen coordinates.
    screen_boxes = [(x + left, y + top, w, h, c) for x, y, w, h, c in dets]

    found, missed = 0, []
    for e in uia:
        if any(_covers(b[:4], e.rect) for b in screen_boxes):
            found += 1
        elif len(missed) < 6:
            missed.append(e.label[:40])

    extra = []
    for b in screen_boxes:
        if any(_covers(b[:4], e.rect) for e in uia):
            continue
        texts = [l.label for l in lines if _covers(b[:4], l.rect)]
        extra.append({"box": b[:4], "conf": b[4],
                      "ocr_text": (texts[0][:44] if texts else "")})

    return {
        "window": title, "rect": rect,
        "uia_controls": ustats["kept"],
        "ocr_lines": ostats.get("ocr_lines", 0),
        "detections": len(screen_boxes),
        "detect_ms": detect_ms,
        "uia_found_by_detector": found,
        "uia_recall": round(found / len(uia), 3) if uia else None,
        "uia_missed_examples": missed,
        "detections_uia_has_no_control_for": len(extra),
        "extra_examples": extra[:10],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", nargs="*", default=DEFAULT_WINDOWS)
    ap.add_argument("--conf", type=float, default=0.05,
                    help="detector confidence floor; OmniParser runs low on "
                         "purpose because UI elements are small and dense")
    ap.add_argument("--imgsz", type=int, default=1280)
    args = ap.parse_args()

    model, weights = load_detector()
    print(f"detector: {weights.name} ({weights.stat().st_size / 1e6:.1f} MB)")
    # Worth printing rather than assuming: if this is a single class, the
    # detector finds *where* things are but not *what* they are, and the element
    # type has to come from UIA or be inferred.
    print(f"classes : {model.names}")
    print(f"device  : {model.device}\n")

    rows = []
    for hint in args.windows:
        row = examine(hint, model, args.conf, args.imgsz)
        if row is None:
            print(f"  {hint:<12} not open, skipped")
            continue
        if "skipped" in row:
            print(f"  {hint:<12} {row['skipped']}")
            rows.append(row)
            continue
        rows.append(row)
        print(f"  {row['window'][:44]}")
        print(f"     UIA {row['uia_controls']:>3} controls | "
              f"OCR {row['ocr_lines']:>3} lines | "
              f"detector {row['detections']:>3} boxes "
              f"({row['detect_ms']} ms)")
        print(f"     detector found {row['uia_found_by_detector']}/"
              f"{row['uia_controls']} of the UIA controls "
              f"(recall {row['uia_recall']})")
        print(f"     {row['detections_uia_has_no_control_for']} boxes UIA has "
              f"nothing for")
        for e in row["extra_examples"][:4]:
            label = e["ocr_text"] or "(no text in box)"
            print(f"        conf {e['conf']:.2f}  {label}")
        if row["uia_missed_examples"]:
            print(f"     missed by detector: {row['uia_missed_examples'][:4]}")
        print()

    RESULTS.write_text(json.dumps({"conf": args.conf, "imgsz": args.imgsz,
                                   "rows": rows}, indent=2), encoding="utf-8")
    print(f"wrote {RESULTS.name}")


if __name__ == "__main__":
    main()
