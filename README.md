# WHAT's Up Jev

Notes and code from evaluating **Jev**, TypeSafe AI's System One model. This is a
testing log, not a product. Nothing here is a recommendation.

**This repo is public.** No keys, no customer data, no internal material. `.env`
is gitignored; `.env.example` shows the variable names only.

## What Jev is

You give it application state and a set of typed questions; it returns typed
judgments and probabilities. It does not generate text or reasoning traces. Code
keeps the workflow and calls the model where semantic understanding is needed.

Three primitives: **Choice** (one option from a set), **Noul** (whether a
condition holds, as a probability), **Score** (position on ordered levels).

## Setup

```bash
py -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env        # then paste your key into .env
```

Confirm the integration works end to end:

```bash
.venv/Scripts/python.exe src/verify_connection.py
```

That script exercises all three primitives on a trivial input with an obvious
right answer, so a bad result points at the integration rather than the model.

## Layout

| Path | What it holds |
| --- | --- |
| `PHASE-1-INITIAL-TESTING.md` | **Start here** — the phase 1 write-up, all three experiments |
| `docs/setup-findings.md` | Verified API/SDK shapes and two naming corrections |
| `src/` | Shared client code and the connection check |
| `manual_test/` | `chat.py` — yes/no chat loop; `tool_caller.py` — tool calls as JSON |
| `experiments/01-tool-calling/` | Can Jev replace the tool-calling step of an LLM |
| `experiments/02-jev-vs-local-llm/` | The same task against local Qwen3.5 4B and 2B |
| `experiments/03-memory-lookup-decision/` | Can a cheap judgement decide when to skip retrieval |
| `experiments/04-choice-option-scaling/` | How many options a Choice holds (255), and whether accuracy, latency and cost hold up at that size |

## Sources

Live docs at [docs.typesafe.ai](https://docs.typesafe.ai) are the source of
truth — Mintlify serves markdown by appending `.md` to any page path. Shapes in
`docs/setup-findings.md` were additionally checked against the installed SDK.
