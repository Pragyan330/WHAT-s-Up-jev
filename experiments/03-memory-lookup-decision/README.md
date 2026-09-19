# Experiment 03: should this turn look anything up?

A retrieval layer that fires on every turn spends context on turns that cannot
use it. "What is the capital of Australia" does not get better with three lines
of the user's old notes stapled to it — it gets longer, and a small model's
answer degrades as the window fills with text the question never needed.

The question here is not whether retrieval works. It is whether a cheap
judgement can decide, *before* retrieving, that this turn has nothing to
retrieve for.

## Setup

Thirty held-out turns across four classes: self-contained (general knowledge,
reasoning, arithmetic, language), personal (only answerable from the user's own
history), live (changes over time), and a deliberately awkward middle.

Two judgements, asked **together in one request**:

| Question | True when |
| --- | --- |
| `needs_memory` | the answer depends on the user's own notes, past conversations, or personal facts |
| `needs_fresh` | the answer depends on information that changes over time |

They are separate Nouls rather than one Choice, because a turn can need both,
either, or neither — "is it going to rain on my trip next week" needs the trip
from memory and the forecast from outside. A Choice would force a false
exclusive.

```bash
.venv/Scripts/python.exe experiments/03-memory-lookup-decision/benchmark.py jev
.venv/Scripts/python.exe experiments/03-memory-lookup-decision/benchmark.py local --label qwen3.5-4b
```

## Results

| | Jev | Qwen3.5-4B | Qwen3.5-2B |
| --- | --- | --- | --- |
| `needs_memory` correct | **30/30** | 28/30 | 25/30 |
| `needs_fresh` correct | 29/30 | **30/30** | 29/30 |
| False positives *(wasted lookup)* | **0** | **0** | **0** |
| False negatives *(lost answer)* | **0** | 2 | 5 |
| Median latency | 365 ms | 280 ms | **199 ms** |

**All three had zero false positives.** That is the headline answer to the
original question: yes, a cheap gate works, and it does not have to be Jev to
work. None of the three sent a general-knowledge turn to the memory store.

The difference is lost answers. The 2B declined to look up five of fourteen
personal questions, including "when is my dentist appointment" and "what's my
favourite programming language" — turns that are simply unanswerable without
the store, and which fail *silently*, looking like the model just did not know.

On this set the gate skipped retrieval on 16 of 30 turns with no lost answers.

## The margin is not delicate

| Turn class | mean `p_memory` |
| --- | --- |
| Self-contained | **0.013** |
| Personal / past-context | **0.94** |

There is essentially no overlap. Any threshold between 0.1 and 0.8 produces
identical behaviour on all thirty cases, so the gate does not need tuning to
be useful — which matters, because a threshold that has to be tuned per user is
a threshold that will be wrong for most of them.

It also kept *live* distinct from *personal*:

```
what's the weather right now      memory 0.14   fresh 0.99
what did I say about the budget   memory 0.98   fresh 0.13
```

A gate that confused the two would route live lookups into a memory store that
cannot answer them.

## The measurement error worth repeating

Our first local runs had the 2B at **17/30** and a 6733 ms median — barely
above what you would get by answering "no" to everything. That was the harness,
not the model.

Qwen3.5 is a reasoning model, and with `--reasoning-format deepseek` the entire
reply lands in `reasoning_content` while `content` stays empty until it
finishes thinking. Over a binary gate it does not finish. At a 3072-token cap
the 2B was still deliberating:

```
"Wait, I need to check if the instruction implies I should output the decision I made about..."
```

Setting `enable_thinking: false` answered the same question in **20 tokens**.

| Qwen3.5-2B | thinking on | thinking off |
| --- | --- | --- |
| Correct | 17/30 | **25/30** |
| Median latency | 6733 ms | **199 ms** |
| Parse failures | 15 | **0** |

A reasoning model is the wrong configuration for a gate that runs before every
turn. Anyone benchmarking one without checking this will measure their own
configuration and publish it as a finding about the model. We nearly did.

## What this does not settle

- **Thirty turns, one author.** The classes were written by the same person who
  wrote the questions, which is the easiest possible test. Real utterances are
  messier, shorter, and more ambiguous.
- **Ground truth is a judgement call in the middle cases.** "How far along am I
  with the training data" was labelled as needing memory; both local models
  disagreed, and a reasonable person could too.
- **No end-to-end quality measurement.** This shows the gate decides correctly.
  It does not show how much a real answer improves when the irrelevant context
  is withheld — that needs an answer-quality evaluation this does not attempt.
- **Latency is single-request on an idle GPU.**
