# Experiment 02: Jev against a local LLM

Experiment 01 showed Jev can do tool calling. It did not show whether that is
worth doing, because there was nothing to compare against. This adds the
comparison that matters for a local-first assistant: a small model running on
the same machine, where the tool call costs no network round trip and no
per-token fee.

## Setup

Same fifteen requests, same five tools, three backends.

| Backend | What it is | Where it runs |
| --- | --- | --- |
| `jev-latest` | jev-1.13.0, System One | TypeSafe API, over the network |
| `qwen3.5-4b` | Qwen3.5-4B-GGUF Q4_K_M, llama.cpp | local, ~4.2 GB VRAM |
| `qwen3.5-2b` | Qwen3.5-2B-GGUF Q4_K_M, llama.cpp | local, ~2.4 GB VRAM |

Both local models were served by llama.cpp on `127.0.0.1:8081`, loaded one at a
time on an RTX 5050 Laptop (8 GB), at `temperature=0`, with the tools supplied
as an ordinary OpenAI-style schema. Reproduce with:

```bash
.venv/Scripts/python.exe experiments/02-jev-vs-local-llm/benchmark.py jev
.venv/Scripts/python.exe experiments/02-jev-vs-local-llm/benchmark.py local --label qwen3.5-4b
```

Raw per-request results are in `results_*.json`.

**The comparison is not symmetric, deliberately.** The LLMs get the full tool
schema, free-text and date arguments included, because that is what you would
actually do with an LLM. Jev is asked only about arguments it can decide; dates
and numbers are never put to it. The tables below keep that in its own column
rather than hiding it in the totals.

## Results

| | Jev | Qwen3.5-4B | Qwen3.5-2B |
| --- | --- | --- | --- |
| Right tool | 15/15 | 15/15 | 14/15 |
| Right tool *and* arguments | 14/15 | **15/15** | 14/15 |
| Median latency | 541 ms | 633 ms | **406 ms** |
| Mean latency | 1185 ms | 665 ms | 445 ms |
| Date/numeric arguments filled | 0/5 | **3/5** | **3/5** |
| VRAM | none (remote) | 4.2 GB | 2.4 GB |

The local models were run with `enable_thinking: false`. Left in thinking mode
they deliberate for thousands of tokens over a routing decision; see
`../03-memory-lookup-decision/` for what that costs.

The three are level, and the 4B is the only one that got every call exactly
right. Each of the other two failed once, on a different request:

```
jev-latest   who won the world cup in 1998   search_web(topic="who")
qwen3.5-2b   tell me a joke                  play_music(artist_or_track="joke")
qwen3.5-4b   -
```

Jev picked a useless span, because the right one was never a candidate — the
known limit from experiment 01. The 2B fired a tool at ordinary conversation,
which is the failure that would hurt most in a voice assistant: a request to
tell a joke started music instead.

Worth stating plainly, because it is not the result we set out to find: **a 2B
running locally decides faster than a network round trip, and a 4B was the most
accurate backend on this set.** If the comparison stopped at this table there
would be no reason to send the decision off the machine.

## The difference that is not in the totals

Jev returns a probability with every judgement. The local models return a tool
call and nothing else. On this set that difference is decisive.

Sorted by the confidence Jev reported, the one wrong call is not at the bottom
by luck — it sits below every call that was right except two:

```
wrong calls:   0.41
correct calls: 0.16  0.36  0.73  0.77  0.83  0.87  0.88  0.90  0.98  1.00 x5
```

Gate on it and the error disappears:

```
threshold 0.5:  12 correct auto-executed, 0 wrong;  3 held for review
```

Every incorrect call is caught. The cost is two correct calls held back — and
both deserved it. `shuffle my playlist` came back at 0.16 having chosen
`artist_or_track="playlist"`, and `look up the population of Iceland` at 0.36
with `topic="Iceland"`, dropping "population". The grading script accepted both;
the confidence number was stricter than the grader, and closer to right.

Neither local model offers this. Their wrong calls look exactly like their right
ones, so the only way to catch `search_web(topic="joke")` is to execute it and
see. For a workflow where a bad tool call sends a message or books something,
that is the whole difference, and it does not show up anywhere in the accuracy
column.

## Latency

With thinking off, the 2B (406 ms) is faster than Jev (541 ms) and the 4B
(633 ms) is close behind. Jev is competitive over a network, not dominant.

Jev's mean (1185 ms) is more than double its median because of network
outliers — one request took 6.7 s. The local models have a much tighter spread,
and a local model never fails because the connection did. For an always-on
assistant a predictable 1 s can beat an unpredictable 0.5 s.

Jev spends far more tokens per request (mean 1164 in / 480 out, against 718 in /
40 out) because all five tools' arguments are asked speculatively in one
request. That buys the single round trip. Whether it is good value depends on
pricing, which is still unpublished.

## Two operational notes on llama.cpp

Neither is about model quality; both cost real time here.

**`LLAMA_CACHE` decides whether `-hf` re-downloads.** If one launch script sets
it and another does not, the second ignores a multi-gigabyte GGUF already on
disk and pulls it again. At the transfer rate seen here that was about two
hours, and it presents as a hang: the process is up, the port is unbound, and
nothing says why. Set it consistently across every script that starts a
backend.

**`--alias` is not an identity.** Two backends serving different models under
the same alias both report that alias at `/v1/models`, so results from one get
filed as the other without complaint. The reliable discriminator is `n_params`
(1881825088 for the 2B, 4205751296 for the 4B), which is what this benchmark
labels its output by.

## What this does not settle

- **Fifteen requests.** Enough to see the failure modes differ, nowhere near
  enough to separate 14/15 from 15/15. Every gap here is within noise.
- **Still no cost comparison.** Pricing is unpublished. The local models cost
  electricity and VRAM; Jev costs an unknown amount per request.
- **The local models were given an easier argument job and a harder routing
  job.** They can generate dates, which Jev cannot; they were also asked to
  decide routing and arguments in a single generation.
- **No concurrency or sustained load.** All timings are single requests on an
  otherwise idle GPU. Contention with anything else using the card is not
  reproduced here and would change the local numbers.
- **One machine, one network.** The latency comparison would look different on a
  worse connection or a better GPU.
