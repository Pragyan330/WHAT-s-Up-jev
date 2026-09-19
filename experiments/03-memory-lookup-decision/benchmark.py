"""Should this turn pay for a memory lookup at all?

    .venv/Scripts/python.exe experiments/03-memory-lookup-decision/benchmark.py jev
    .venv/Scripts/python.exe experiments/03-memory-lookup-decision/benchmark.py local --label qwen3.5-4b

A retrieval layer that fires on every turn spends context on turns that cannot
use it. "What is the capital of Australia" does not get better with three lines
of the user's old notes stapled to it. It gets longer, and a small model's
answer gets worse as the window fills with text the question never needed.

The question under test is whether a cheap judgement can decide, before
retrieving, that this turn has nothing to retrieve for.

Two independent judgements, asked together in one request:

  needs_memory   the answer depends on the user's own stored notes, past
                 conversations, or personal facts
  needs_fresh    the answer depends on information that changes over time and
                 must be looked up live

They are separate Nouls, not one Choice, because a turn can need both, either,
or neither, and a Choice would force a false exclusive.

The baseline to beat is "always retrieve", which is what a system with no
gate does.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx2 as httpx

LOCAL_URL = "http://127.0.0.1:8081"

# (query, needs_memory, needs_fresh)
# needs_memory is the judgement under test. needs_fresh comes along because a
# gate that confuses "I do not know this" with "the user told me this" would
# route live lookups into the memory store and find nothing there.
CASES = [
    # --- self-contained: general knowledge, reasoning, language. No lookup. ---
    ("what's the capital of Australia", False, False),
    ("who wrote pride and prejudice", False, False),
    ("how many continents are there", False, False),
    ("explain how a transformer model works", False, False),
    ("what's the difference between TCP and UDP", False, False),
    ("translate good morning into spanish", False, False),
    ("what's 15 percent of 240", False, False),
    ("tell me a joke", False, False),
    ("give me a word that rhymes with orange", False, False),
    ("why is the sky blue", False, False),

    # --- personal: only answerable from the user's own history. Lookup. ---
    ("what did I say about the budget yesterday", True, False),
    ("when is my dentist appointment", True, False),
    ("what was that restaurant I mentioned last week", True, False),
    ("what project was I working on on friday", True, False),
    ("what did we decide about the database migration", True, False),
    ("how far along am I with the training data", True, False),
    ("did I already email Priya about this", True, False),
    ("what were my notes on the vision work", True, False),
    ("summarise what we talked about today", True, False),
    ("what did I mean earlier when I said it was blocked", True, False),

    # --- live: changes over time, not in the user's notes. No memory lookup. ---
    ("what's the weather right now", False, True),
    ("what time is it", False, True),
    ("who won the match last night", False, True),
    ("what's the current price of bitcoin", False, True),

    # --- the genuinely awkward middle ---
    ("what's my favourite programming language", True, False),
    ("remind me what you are capable of", False, False),
    ("how long have we been working together", True, False),
    ("is it going to rain on my trip next week", True, True),
    ("what's a good name for a cat", False, False),
    ("pick up where we left off", True, False),
]

SYSTEM = (
    "You decide what a voice assistant needs before it answers. For the user's "
    "request, answer with a single JSON object and nothing else:\n"
    '{"needs_memory": true|false, "needs_fresh": true|false}\n'
    "needs_memory is true only when answering depends on the user's own stored "
    "notes, past conversations, or personal facts. It is false for general "
    "knowledge, reasoning, language, and arithmetic.\n"
    "needs_fresh is true when answering depends on information that changes over "
    "time and must be looked up live."
)


def jev_questions():
    from typesafe_sdk import Noul
    return {
        "needs_memory": Noul(
            instructions=(
                "Does answering the request in `request` depend on the user's own "
                "stored notes, past conversations with this assistant, or personal "
                "facts about the user?"
            ),
            criteria={
                "true": "The answer requires something only this particular user's own "
                        "history, notes, or personal details could supply. Without them "
                        "the assistant cannot answer at all.",
                "false": "The answer is general knowledge, reasoning, language, or "
                         "arithmetic, or it depends on live external information. Any "
                         "competent assistant could answer without knowing this user.",
            },
        ),
        "needs_fresh": Noul(
            instructions=(
                "Does answering the request in `request` depend on information that "
                "changes over time and would have to be looked up live right now?"
            ),
            criteria={
                "true": "The correct answer today differs from the correct answer last "
                        "month: current weather, the time, prices, scores, news.",
                "false": "The answer is stable, or it comes from the user's own history "
                         "rather than from the outside world.",
            },
        ),
    }


def grade(case, got_memory, got_fresh) -> dict:
    _query, want_memory, want_fresh = case
    return {
        "memory_ok": got_memory == want_memory,
        "fresh_ok": got_fresh == want_fresh,
        "want_memory": want_memory,
        "got_memory": got_memory,
        "want_fresh": want_fresh,
        "got_fresh": got_fresh,
    }


def run_jev() -> list[dict]:
    from dotenv import load_dotenv
    load_dotenv(str(Path(__file__).resolve().parents[2] / ".env"))
    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(timeout=60.0)
    questions = jev_questions()
    rows = []
    for case in CASES:
        query = case[0]
        start = time.perf_counter()
        response = client.system_one(state={"request": query}, questions=questions)
        ms = (time.perf_counter() - start) * 1000
        p_mem = response.answers["needs_memory"].noul
        p_fresh = response.answers["needs_fresh"].noul
        rows.append({
            "query": query, "ms": round(ms, 1),
            "p_memory": round(p_mem, 3), "p_fresh": round(p_fresh, 3),
            "in_tokens": response.usage.input_tokens,
            "out_tokens": response.usage.output_tokens,
            **grade(case, p_mem >= 0.5, p_fresh >= 0.5),
        })
        flag = "  " if rows[-1]["memory_ok"] else "XX"
        print(f"  {flag} {ms:6.0f} ms  mem {p_mem:.2f}  fresh {p_fresh:.2f}  {query[:42]}")
    return rows


def identify_local() -> dict:
    meta = httpx.get(f"{LOCAL_URL}/v1/models", timeout=10.0).json()["data"][0]
    n = meta.get("meta", {}).get("n_params")
    return {"served_id": meta.get("id"), "n_params": n, "size": "2B" if n and n < 3e9 else "4B"}


def parse_json_reply(text: str) -> dict:
    """The models are told to emit bare JSON; accept it wrapped in prose or fences."""
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def run_local() -> list[dict]:
    rows = []
    with httpx.Client(timeout=180.0) as client:
        for case in CASES:
            query = case[0]
            # Thinking is off. Qwen3.5 is a reasoning model, and left in
            # thinking mode it deliberates for thousands of tokens over a
            # binary gate. At a 3072-token cap the 2B was still arguing with
            # itself and never emitted a verdict; enable_thinking=false answers
            # in about 20 tokens. A gate that runs before every turn has to be
            # cheap, so this is how one would really be deployed.
            payload = {"model": "qwen3.5-4b", "temperature": 0, "max_tokens": 256,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": query}]}
            start = time.perf_counter()
            reply = client.post(f"{LOCAL_URL}/v1/chat/completions", json=payload).json()
            ms = (time.perf_counter() - start) * 1000
            message = reply["choices"][0]["message"]
            content = message.get("content") or ""
            parsed = parse_json_reply(content)
            if not parsed:
                # Fall back to the reasoning trace: if it stated the verdict there,
                # count it rather than scoring the model down for the wrapper.
                parsed = parse_json_reply(message.get("reasoning_content") or "")
            got_mem = bool(parsed.get("needs_memory"))
            got_fresh = bool(parsed.get("needs_fresh"))
            usage = reply.get("usage", {})
            rows.append({
                "query": query, "ms": round(ms, 1),
                "p_memory": None, "p_fresh": None,
                "parsed": bool(parsed), "raw": content[:200],
                "in_tokens": usage.get("prompt_tokens"),
                "out_tokens": usage.get("completion_tokens"),
                **grade(case, got_mem, got_fresh),
            })
            flag = "  " if rows[-1]["memory_ok"] else "XX"
            print(f"  {flag} {ms:6.0f} ms  mem {int(got_mem)}  fresh {int(got_fresh)}  {query[:42]}")
    return rows


def summarise(rows: list[dict]) -> dict:
    lat = [r["ms"] for r in rows]
    # The two errors cost different things. A false positive spends context on
    # a turn that cannot use it; a false negative loses an answer entirely.
    fp = sum(1 for r in rows if r["got_memory"] and not r["want_memory"])
    fn = sum(1 for r in rows if not r["got_memory"] and r["want_memory"])
    want_yes = sum(1 for r in rows if r["want_memory"])
    want_no = len(rows) - want_yes
    lookups = sum(1 for r in rows if r["got_memory"])
    return {
        "n": len(rows),
        "memory_correct": sum(r["memory_ok"] for r in rows),
        "fresh_correct": sum(r["fresh_ok"] for r in rows),
        "false_positive": fp, "false_negative": fn,
        "of_which_need_memory": want_yes, "of_which_do_not": want_no,
        "lookups_performed": lookups,
        "lookups_avoided": len(rows) - lookups,
        "wasted_lookup_rate": round(fp / want_no, 3) if want_no else None,
        "median_ms": round(statistics.median(lat), 1),
        "mean_ms": round(statistics.mean(lat), 1),
        "parse_failures": sum(1 for r in rows if r.get("parsed") is False),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=["jev", "local"])
    parser.add_argument("--label", default=None)
    args = parser.parse_args()

    if args.backend == "jev":
        label = args.label or "jev-latest"
        print(f"== {label} ==")
        rows, meta = run_jev(), {"backend": "jev"}
    else:
        meta = identify_local()
        label = args.label or f"qwen3.5-{meta['size'].lower()}"
        print(f"== {label}  (n_params {meta['n_params']}) ==")
        rows = run_local()

    summary = summarise(rows)
    print("\n" + json.dumps(summary, indent=2))
    out = Path(__file__).resolve().parent / f"results_{label}.json"
    out.write_text(json.dumps({"label": label, "meta": meta, "summary": summary,
                               "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nwrote {out.name}")


if __name__ == "__main__":
    main()
