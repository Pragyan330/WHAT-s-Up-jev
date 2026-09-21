# 04 — How many options can a Choice hold, and does it still discriminate?

> **TL;DR** — A Jev Choice accepts a hard maximum of **255 options** and rejects
> 256 with a clean `400`. Accuracy does **not** decay on the way there: **210/210
> trials correct** across 10 → 255 options, with no positional bias and the right
> answer ranked first in the probability distribution every time. Latency is flat
> (~350 ms at 10 options, ~390 ms at 255). Measured cost of the whole day's
> testing: **$0.021 for 217 calls and 835,264 tokens**.

Prep work for the phase-2 idea: a computer-control agent where a scraper flattens
the screen into numbered controls and Jev picks the number to click, the way
Apple's Voice Control paints numbers over the UI. The whole approach dies if a
Choice quietly stops paying attention past the first couple of dozen options, so
that gets checked before anything gets built on top of it.

## The two questions, which get conflated a lot

| | Question | Why it is separate |
| --- | --- | --- |
| **Hard cap** | How many options will the API accept? | Answerable from the error response |
| **Useful cap** | How many can it accept *and still be right*? | A request that returns a number looks identical whether the model discriminated or guessed |

A stated limit of 255 is worthless if the model stops reading at 30. Both got
tested.

## Results at a glance

| | Finding |
| --- | --- |
| Hard cap | **255** options per Choice, exactly as documented |
| At 256+ | `400 Too many choices. Must have at most 255 choices.` — loud, with a request id |
| Silent truncation | **None.** Nothing is dropped behind our back |
| Accuracy, 10 → 255 options | **150/150** (clean) + **60/60** (with near-lookalikes) |
| Positional bias | **None.** Option 255 of 255 is as findable as option 1 |
| Rank of correct answer | **1st in every one of 210 trials** — never an argmax rescue |
| Latency | 354 ms @ 10 options → 382 ms @ 255 (**+8% for 25× the list**) |
| Cost at 255 options | **~$0.000195/call** ≈ **$0.20 per 1,000 decisions** |
| The "limit is 10" worry | Misplaced — that 10 is the ceiling on **Score levels**, a different primitive |

---

## What we actually tested

### The screen

[`screen_elements.py`](screen_elements.py) holds **269 controls** from a plausibly
busy desktop — mail client, browser, spreadsheet, calendar, chat app, media
player, menu bars, OS shell. Each is written as a label plus where it sits:

```
Autofit Row Height (Spreadsheet row header context menu)
Schedule Send (Compose window send split button)
Do Not Disturb (Quick settings flyout)
```

That is about all a flattened accessibility tree gives you, which is the point.

### The question

Each trial is **one Choice** whose option keys are the bare numbers `1..N` and
whose descriptions are the controls:

```python
Choice(
    instructions=(
        "The user is looking at a computer screen. Every option below is one "
        "control that is visible on that screen right now, written as its "
        "on-screen label followed by where in the interface it appears. The user "
        "said what is in `user_request`. Choose the single numbered control that "
        "should be clicked to do what the user asked."
    ),
    criteria={"1": "Reply (Mail toolbar)", "2": "Night Light (Quick settings flyout)", ...},
)
```

State is just `{"user_request": "<what the user said>"}`.

The numeric keys carry **no meaning on purpose**. All the signal has to come from
the descriptions, which makes this the architecture we would actually ship rather
than a friendlier version of it. (Per the docs, question IDs and option keys are
for code — they are not sent to the model.)

### The six tasks

Phrased so the match is semantic rather than a keyword hit:

| Task | What the user said | The one control that does it |
| --- | --- | --- |
| `pdf` | "Save a copy of this document as a PDF so I can send it to the legal team." | Export as PDF |
| `undo` | "I just pasted the wrong text in — take back that last change." | Undo |
| `column` | "The dates in this spreadsheet column are cut off. Make the column wide enough to show them in full." | Autofit Column Width |
| `meeting` | "It is time for the 3pm standup. Get me into the video call for that event." | Join Meeting |
| `wifi` | "I need to connect to a different wireless network." | Wi-Fi Settings |
| `louder` | "This video's audio is too quiet. Turn it up." | Volume Up |

### The two knobs — and why the second one matters

- **N**, the option count: 10, 40, 100, 180, 255.
- **Position of the correct option** in the list: 0%, 25%, 50%, 75%, 100%.

The second knob is the one that catches the failure we care about. If attention
decays as the list grows, accuracy at the bottom of the list collapses while the
top stays fine. **Test a single position and that is invisible** — you would read
a healthy average and ship a bug.

### Keeping the trials interpretable

Targets **and their obvious rivals** are both held out of the distractor pool, so
every trial has exactly one defensible answer. Without that scrub, a wrong pick
could mean "the list got too long" *or* "there were two right answers", and the
result could not distinguish them.

`hard` mode then puts the rivals back — "Autofit Row Height" next to "Autofit
Column Width", "Volume Mixer" next to "Volume Up", "Undo Send" next to "Undo",
26 of them in all — to separate *many options* from *options that look alike*.
Each rival is plausible but wrong, never a second correct answer, and injection
is asserted per trial so a lucky draw cannot produce an easy run.

Distractors are sampled fresh per trial under a fixed seed (`20260921`), so one
unlucky draw cannot carry the result and any failure is reproducible.

### Reproducing

```bash
.venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py cap
.venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py scale
.venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py hard
```

Raw per-trial records land in `results_cap.json`, `results_scale.json`,
`results_hard.json` — every option count, position, probability distribution,
latency and token count.

---

## Finding 1 — the hard cap is 255, and it fails loudly

| N | Result |
| --- | --- |
| 2, 10, 64, 128, 200, 254, **255** | accepted, correct answer, ~350–400 ms |
| **256**, 260, 300, 512, 1000 | `400 Too many choices. Must have at most 255 choices.` |

Exactly the documented number. The part that matters operationally is the failure
mode: a clean `400` carrying a request id, **not** a silently truncated list that
gets scored anyway. So a screen with more than 255 controls is a code problem we
can see, not an accuracy problem we cannot.

## Finding 2 — no accuracy decay, and no positional bias

`scale` — 150 trials (5 counts × 6 tasks × 5 positions): **150/150 correct.**

| N | Accuracy | Lowest p(correct) | Median input tokens | Median latency |
| --- | --- | --- | --- | --- |
| 10 | 30/30 | 1.00 | 553 | 354 ms |
| 40 | 30/30 | 0.98 | 1,145 | 365 ms |
| 100 | 30/30 | 0.98 | 2,330 | 365 ms |
| 180 | 30/30 | 0.97 | 3,978 | 374 ms |
| 255 | 30/30 | 0.96 | 5,530 | 382 ms |

By position of the correct option — the decay test:

| Correct option at | 0% | 25% | 50% | 75% | 100% |
| --- | --- | --- | --- | --- | --- |
| Accuracy | 30/30 | 30/30 | 30/30 | 30/30 | 30/30 |

**No positional bias.** Being option 255 of 255 is as findable as being option 1.

The right answer was **rank 1 in the probability distribution on all 150 trials** —
never a near miss that `argmax` happened to rescue. p(correct) sags from 1.00 to
0.96 across the range, a real trend but roughly 25× away from changing a decision.

## Finding 3 — lookalike controls cost a little, and list length does not make it worse

`hard` — 60 trials with the 26 near-rivals injected, at N=10 and N=255:
**60/60 correct**, lowest p(correct) **0.92**.

| N | Accuracy | Lowest p(correct) |
| --- | --- | --- |
| 10 | 30/30 | 0.92 |
| 255 | 30/30 | 0.93 |

The rivals cost about 0.05 of probability mass and changed **no** decisions. The
informative detail: the penalty is the same at 10 options as at 255. What little
difficulty exists comes from controls that look alike, **not** from list length —
which is the distinction the whole experiment was built to draw.

## Finding 4 — latency is effectively flat

| N | Median latency |
| --- | --- |
| 10 | 354 ms |
| 100 | 365 ms |
| 255 | 382 ms |

Across 210 trials: min 329 ms, median 369 ms, p95 426 ms. A 25× longer option
list costs about **8%** more wall-clock.

The first call in a process ran 1,142 ms and every subsequent one sat in the
330–430 ms band, so that is connection warm-up, not model behaviour. Worth
knowing for an interactive agent: keep the client alive.

---

## Finding 5 — what it costs

Measured across all three runs (usage figures come from the API response, so the
token counts are exact rather than estimated):

| | |
| --- | --- |
| Billable calls issued | **217** |
| Total input tokens | **609,762** |
| Total output tokens | **225,502** |
| Total tokens | **835,264** |
| Billed on the dashboard | **$0.021** |
| Blended rate | **≈ $0.025 per 1M tokens** |
| Average per call | **≈ $0.0001** |

**Output tokens scale with option count**, because a probability comes back per
option. This is the cost driver to design around, and it is not obvious up front:

| N | Input tokens | Output tokens | Est. $/call | Est. $/1,000 calls |
| --- | --- | --- | --- | --- |
| 10 | 556 | 88 | $0.000016 | $0.02 |
| 40 | 1,145 | 329 | $0.000037 | $0.04 |
| 100 | 2,327 | 810 | $0.000079 | $0.08 |
| 180 | 3,979 | 1,530 | $0.000139 | $0.14 |
| 255 | 5,534 | 2,205 | $0.000195 | $0.19 |

<sub>Per-N dollar figures apportion the billed $0.021 across calls by token
footprint; they are derived, not separately invoiced.</sub>

At agent scale, a full-screen 255-option decision works out to roughly **$0.20
per 1,000 actions**. A 20-step task costs about **$0.004**. That is the number
that makes the phase-2 idea worth building rather than just worth discussing.

**Caveats on the money.** TypeSafe does not publish per-token pricing publicly,
so the rate above is inferred from one day's dashboard total and may not separate
input from output pricing — the blended figure is what we can honestly state. The
$0.021 is shown to three decimals, so it carries roughly ±2.4% rounding. The
dashboard counted 204 requests where our scripts issued 217 successful calls; the
token totals are read straight from API responses and so are unaffected, but the
discrepancy is unexplained and worth a look before anyone quotes a per-request
price. Trial credits or promotional rates have not been ruled out.

---

## Caveats

Stated plainly, because a 100% score invites over-reading:

- **The targets are unambiguous by construction.** Real screens contain genuinely
  ambiguous controls. A test where every task has one defensible answer measures
  *capacity*, not *judgement*. A perfect score here means "option count is not the
  bottleneck" — not "this will be right on your screen."
- **One synthetic screen, six tasks, one seed.** Enough to clear a go/no-go, not
  enough to quote as an accuracy figure.
- **Distractors are tidy.** Real accessibility-tree text is noisier, duplicated,
  and full of unlabelled nodes.
- **The hard half of the agent is untested here**: what happens when *no* control
  is right, when an action needs several steps, or when the scraper's own output
  is wrong.

## What this means for the phase-2 build

Numbered-option screen control is **not** blocked by option count, latency, or
cost. The one real constraint is the firm 255 per Choice, which real screens will
exceed — a busy web page's accessibility tree runs into the thousands. That is a
code problem, and the options are ours:

1. **Prune first** to visible, enabled, actionable nodes — which a scraper should
   do regardless, and which is free.
2. **Chunk into several Choices** in a single request and compare the winners;
   independent questions run in parallel, so the latency cost is small.
3. **Route by region, then by control** — the shape the
   [hierarchical classification cookbook](https://docs.typesafe.ai/cookbooks/hierarchical_classification.md)
   describes.

Given that output tokens scale with option count, aggressive pruning is the
cheapest of the three on both cost and accuracy grounds.

The next unknown worth spending on is the **no-match case**: how the model behaves
when the right control is not on screen at all. That is the failure a real agent
hits constantly, and this experiment deliberately excluded it.
