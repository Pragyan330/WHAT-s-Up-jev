# 05 — Can a small UI-element detector see what the accessibility tree cannot?

> **TL;DR** — Yes, and it is the right layer. A 40 MB YOLOv8 detector finds the
> page content UIA is blind to, as **interactive regions** rather than lines of
> text — which is what OCR got wrong. On a dense search page: UIA 49 controls,
> detector 67 boxes, **31 of them things UIA has nothing for**, visually confirmed
> as real clickable elements. On GPU it costs **51 ms**, against 790 ms on CPU.
>
> Three caveats that matter: it is **single-class** (where, not what), recall
> against UIA is **0.82–0.91** so it must supplement rather than replace, and the
> boxes carry **no labels**.

## Why this experiment exists

The phase-2 agent reads a window through UI Automation. That works well for
native and UWP applications and not at all for others — Windows 11 Start-menu
search results are in no window's tree at any depth, and a browser publishes
nothing about its page unless renderer accessibility is forced on.

OCR was tried first and half-worked. It found what UIA missed, but returned about
105 text lines per window, nearly all headings, captions and timestamps rather
than things to click. Option lists went from 11 to 163 and the correct pick fell
from **0.92 to 0.35**. Coverage went up; usable signal went down.

A detector should behave differently because it is trained to find *interactive*
regions, not any text at all. This measures whether that holds on real Windows
applications — which is not obvious, because the public GUI datasets (RICO, VINS)
are mostly mobile and desktop chrome is not what these models saw most of.

## Setup

[OmniParser v2](https://huggingface.co/microsoft/OmniParser-v2.0)'s `icon_detect`
model: a fine-tuned YOLOv8, **40.6 MB**. OmniParser ships a second stage — a
Florence-2 captioner that describes each icon — which is deliberately **not used
here**. It is a small vision-language model, and the whole point of this stack is
that the only model doing judgement is Jev. Labels can come from UIA names, which
are free, or from OCR of the individual box.

Windows are captured with `PrintWindow`, not a screen grab, so a window that is
behind another still renders its own pixels.

```bash
.venv/Scripts/python.exe experiments/05-element-detection/compare.py
.venv/Scripts/python.exe experiments/05-element-detection/compare.py --windows Calculator Chrome
```

Read-only: no clicking, no Jev calls.

## What is measured

| | |
| --- | --- |
| **recall** | of the controls UIA already knows about, how many does the detector also find? UIA is ground truth on apps where it works, because a control it reports really is there |
| **gain** | how many detected regions has UIA nothing for? Either the coverage win or false positives — the OCR text inside each is printed so the difference is visible |
| **volume** | how many options would actually be offered, against UIA's count and OCR's line count |

A detection counts as finding a control if the control's centre falls inside the
box, or IoU ≥ 0.3. Centre-inside matters alongside IoU because detectors draw
tighter or looser boxes than a control's own rectangle, and a strict IoU would
call that a miss when a click at the detected centre would land correctly — which
is the only thing that matters downstream.

## Results

| Window | UIA | OCR lines | Detector | Recall vs UIA | Boxes UIA lacks |
| --- | --- | --- | --- | --- | --- |
| Calculator | 33 | 8 | 38 | **0.91** | 5 |
| Chrome (search results) | 49 | 43 | 67 | **0.82** | 31 |

**On Calculator the detector adds nothing**, which is the correct result: UIA
already describes that window well. It reproduces it at 0.91 recall, missing the
three small memory buttons, and adds five boxes around labels like `Standard`.

**On Chrome the 31 extra boxes are the finding.** Their OCR text was mostly empty,
so the numbers alone could not distinguish a coverage win from false positives.
Rendering the boxes and looking settled it: they are the search input, the filter
chips, every result card, the related-search entries, the apps grid, the mic and
lens icons. Real, clickable, and invisible to UIA — which saw only browser chrome.

### Speed, after moving to the GPU

1938×1098 window, median of 7 runs after warm-up:

| | Median | Best | Boxes |
| --- | --- | --- | --- |
| CPU | 790.5 ms | 772.2 ms | 63 |
| **GPU fp32** | **51.1 ms** | 47.5 ms | 63 |
| GPU fp16 | 54.4 ms | 44.0 ms | 63 |

Identical box counts, so nothing was traded for the speed. **fp16 is not worth
it** at this model size: its best case is lower but its median is worse, because
fixed overhead dominates, and it adds a precision question for nothing.

At 51 ms the detector fits inside the agent loop, where a UIA walk alone costs
100–300 ms. At 790 ms it did not.

The GPU here is an RTX 5050 (Blackwell, **sm_120**). Blackwell needs CUDA 12.8+,
so the `cu126` index is out despite carrying the newest torch; of the rest only
`cu130` ships a cp314 build of torch 2.14.0. Verify with
`torch.cuda.get_arch_list()` rather than `is_available()` — a wheel can report
available and then fail at kernel launch or silently JIT from PTX.

## What this does not give you

- **Element type.** The model is single-class: `{0: 'icon'}`. It finds *where*
  interactive regions are, not *what* they are. An earlier note in this project
  claimed it provided button/input/icon typing; that was wrong. Type still has to
  come from UIA, or be inferred.
- **Labels.** Boxes carry no text. Pairing is needed: UIA name where a box
  overlaps a known control, otherwise OCR of that box alone — far cheaper than
  OCR-ing whole windows, and it yields labelled options instead of raw lines.
  Image-only result cards will stay unlabelable, and "unlabeled region, row 7" is
  a weak thing to ask a model to choose.
- **Completeness.** Recall of 0.82–0.91 means it misses real controls, including
  Calculator's memory buttons and some browser chrome. It has to supplement UIA,
  never replace it: prefer the UIA element wherever a box overlaps one, because
  that carries real identity, current state, and an Invoke addressed to the
  element rather than a click at a coordinate.

## Next

Merge the two sources — UIA first, detector boxes only for regions UIA has
nothing for, OCR each of those for a label, dedupe geometrically — then measure
the thing that actually decides it: on Chrome and the Start menu, is the correct
target in the option list, and how long is that list?
