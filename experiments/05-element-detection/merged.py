"""Does the merged pipeline put the right target in a list short enough to matter?

Two numbers decide whether this goes into the agent. Both have to be good at the
same time, and the OCR attempt failed by getting only the first one:

  presence   is the control the task needs actually in the option list?
  volume     how long is that list? Every option dilutes the distribution, and
             a correct pick fell from 0.92 to 0.35 when options went 11 -> 163.

The Start menu is the case that matters, because its search results exist only in
pixels - no window's UIA tree contains them at any depth. It is also the one case
here where the content is ours to control: we type "Paint" and then ask whether
anything in the list says Paint.

    .venv/Scripts/python.exe experiments/05-element-detection/merged.py
    .venv/Scripts/python.exe experiments/05-element-detection/merged.py --no-start
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "agentic_test_env"))

import uiautomation as auto

from detect_scrape import merge, scrape as detect_scrape
from uia_scrape import list_top_level, scrape_hwnds

RESULTS = HERE / "merged_results.json"


def look_at(win: dict, device: str, want: list[str]) -> dict:
    hwnd, title, rect = win["hwnd"], win["title"], win["rect"]
    t0 = time.perf_counter()
    uia, ustats = scrape_hwnds([hwnd], {hwnd: title}, max_depth=14,
                               include_chrome=True)
    uia_ms = ustats["ms"]
    found, dstats = detect_scrape(hwnd, title, rect, uia, device=device)
    options = merge(uia, found)
    total_ms = round((time.perf_counter() - t0) * 1000, 1)

    hits = {}
    for needle in want:
        match = next((o for o in options
                      if needle.lower() in o.label.lower()), None)
        hits[needle] = None if match is None else {
            "label": match.label[:60],
            "source": "detector" if type(match).__name__ == "DetectedControl"
                      else "uia",
        }

    return {
        "window": title, "options": len(options),
        "uia": ustats["kept"], "uia_ms": uia_ms,
        "detections": dstats["detections"], "novel": dstats["novel"],
        "labelled": dstats.get("labelled_unique", 0),
        "unlabelled": dstats["unlabelled"],
        "detect_ms": dstats["detect_ms"], "ocr_ms": dstats["ocr_ms"],
        "total_ms": total_ms, "targets": hits,
    }


def report(row: dict) -> None:
    print(f"  {row['window'][:52]}")
    print(f"     options offered : {row['options']}  "
          f"(UIA {row['uia']} + detector {row['labelled']})")
    print(f"     detector        : {row['detections']} boxes, "
          f"{row['novel']} novel, {row['labelled']} labelled, "
          f"{row['unlabelled']} dropped unlabelled")
    print(f"     time            : {row['total_ms']} ms total "
          f"(uia {row['uia_ms']} + detect {row['detect_ms']} "
          f"+ ocr {row['ocr_ms']})")
    for needle, hit in row["targets"].items():
        if hit:
            print(f"     FOUND {needle!r} via {hit['source']}: {hit['label']!r}")
        else:
            print(f"     MISSING {needle!r} - not in the option list")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--no-start", action="store_true",
                    help="skip the Start-menu case, which types into the shell")
    args = ap.parse_args()

    rows = []

    for hint, want in (("Calculator", ["Seven", "Equals"]),
                       ("Chrome", ["Search"])):
        match = [w for w in list_top_level()
                 if hint.lower() in w["title"].lower()]
        if not match:
            print(f"  {hint} not open, skipped\n")
            continue
        row = look_at(match[0], args.device, want)
        rows.append(row)
        report(row)

    if not args.no_start:
        # The decisive case. Typing is the only way to make search results
        # exist, and they are exactly what UIA cannot see.
        print("  -- Start menu, searching for 'Paint' --")
        auto.SendKeys("{Win}", waitTime=0)
        time.sleep(1.4)
        auto.SendKeys("Paint", waitTime=0)
        time.sleep(1.8)
        shell = [w for w in list_top_level()
                 if w["title"] in ("Start", "Search")]
        if not shell:
            print("     the shell did not stay open; skipped\n")
        else:
            row = look_at(shell[0], args.device, ["Paint"])
            rows.append(row)
            report(row)
        auto.SendKeys("{Esc}", waitTime=0)

    RESULTS.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote {RESULTS.name}")


if __name__ == "__main__":
    main()
