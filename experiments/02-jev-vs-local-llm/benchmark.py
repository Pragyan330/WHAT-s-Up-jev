"""Same tool-calling task, three backends: Jev, Qwen3.5-4B, Qwen3.5-2B.

    .venv/Scripts/python.exe experiments/02-jev-vs-local-llm/benchmark.py jev
    .venv/Scripts/python.exe experiments/02-jev-vs-local-llm/benchmark.py local --label qwen3.5-4b

The local backends are llama.cpp on 127.0.0.1:8081, started one at a time. If
both are served under the same --alias, the model id is NOT a reliable label,
so this script reads n_params from /v1/models instead.

The LLM is given the full tool schema including the free-text, numeric and date
arguments, because that is what you would actually do with an LLM. Jev is given
only the arguments it can decide. That asymmetry is deliberate, and the report
says which columns it affects.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx2 as httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manual_test"))

LOCAL_URL = "http://127.0.0.1:8081"

# (query, expected tool or None, graded arguments)
# A graded span passes if any listed fragment appears in the produced value,
# case-insensitively. The test is whether a person would accept the call, so
# exact string equality is too strict.
CASES = [
    ("what's the weather in Tokyo tomorrow", "get_weather", {"location": ["tokyo"]}),
    ("whats the weather in San Francisco", "get_weather", {"location": ["san francisco"]}),
    ("what's the temperature in Berlin", "get_weather", {"location": ["berlin"]}),
    ("text Sarah right now, it's an emergency", "send_message",
     {"recipient": ["sarah"], "urgency": ["urgent"]}),
    ("email Priya about the budget review, it can wait", "send_message",
     {"recipient": ["priya"], "urgency": ["normal"]}),
    ("send a quick note to Marcus, no rush", "send_message",
     {"recipient": ["marcus"], "urgency": ["normal"]}),
    ("play some jazz, shuffled", "play_music",
     {"artist_or_track": ["jazz"], "shuffle": [True]}),
    ("put on some Miles Davis", "play_music", {"artist_or_track": ["miles davis"]}),
    ("shuffle my playlist", "play_music", {"shuffle": [True]}),
    ("remind me to call the dentist on friday", "create_reminder",
     {"subject": ["dentist"]}),
    ("set a reminder to water the plants tomorrow morning", "create_reminder",
     {"subject": ["water the plants", "plants"]}),
    ("who won the world cup in 1998", "search_web", {"topic": ["world cup"]}),
    ("look up the population of Iceland", "search_web", {"topic": ["iceland", "population"]}),
    ("write me a poem about the ocean", None, {}),
    ("tell me a joke", None, {}),
]

# Arguments Jev is never asked (numeric/date). Tracked separately so the report
# can say plainly what the LLM fills that Jev structurally cannot.
FREEFORM = {"get_weather": "days", "create_reminder": "when"}

SYSTEM = (
    "You are the tool router for a voice assistant. Decide whether one of the "
    "provided tools should be called for the user's request, and if so call it "
    "with the right arguments. If no tool fits, do not call a tool."
)


def openai_tools() -> list[dict]:
    """The same five tools, as an OpenAI-style schema for the local models."""
    return [
        {"type": "function", "function": {
            "name": "get_weather",
            "description": "Look up the weather forecast for a place.",
            "parameters": {"type": "object", "properties": {
                "location": {"type": "string", "description": "The place to forecast."},
                "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                "days": {"type": "integer", "description": "How many days ahead."},
            }, "required": ["location"]}}},
        {"type": "function", "function": {
            "name": "send_message",
            "description": "Send a message or email to a person.",
            "parameters": {"type": "object", "properties": {
                "recipient": {"type": "string", "description": "Who to send to."},
                "urgency": {"type": "string", "enum": ["normal", "urgent"]},
            }, "required": ["recipient"]}}},
        {"type": "function", "function": {
            "name": "play_music",
            "description": "Play music, a song, an artist, or a genre.",
            "parameters": {"type": "object", "properties": {
                "artist_or_track": {"type": "string", "description": "Artist, song, or genre."},
                "shuffle": {"type": "boolean"},
            }, "required": []}}},
        {"type": "function", "function": {
            "name": "create_reminder",
            "description": "Create a reminder or a calendar event for later.",
            "parameters": {"type": "object", "properties": {
                "subject": {"type": "string", "description": "What the reminder is about."},
                "when": {"type": "string", "description": "When it should fire."},
            }, "required": ["subject"]}}},
        {"type": "function", "function": {
            "name": "search_web",
            "description": "Look up a fact or information. The user wants to know "
                           "something, rather than have an action performed.",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "What to search for."},
            }, "required": ["topic"]}}},
    ]


def matches(produced, accepted) -> bool:
    if produced is None:
        return False
    if isinstance(accepted[0], bool):
        return produced is accepted[0] or produced == accepted[0]
    text = str(produced).lower()
    return any(fragment in text for fragment in accepted)


def grade(case, tool, arguments) -> dict:
    query, want_tool, want_args = case
    tool_ok = tool == want_tool
    arg_results = {}
    if tool_ok and want_tool is not None:
        for arg, accepted in want_args.items():
            arg_results[arg] = matches(arguments.get(arg), accepted)
    args_ok = all(arg_results.values()) if arg_results else tool_ok
    freeform = None
    if tool_ok and want_tool in FREEFORM:
        value = arguments.get(FREEFORM[want_tool])
        freeform = value is not None and value != ""
    return {"tool_ok": tool_ok, "args_ok": args_ok, "call_ok": tool_ok and args_ok,
            "per_arg": arg_results, "freeform_filled": freeform}


def run_jev() -> list[dict]:
    from dotenv import load_dotenv
    load_dotenv(str(Path(__file__).resolve().parents[2] / ".env"))
    import tool_caller as tc
    from typesafe_sdk import TypeSafeClient

    client = TypeSafeClient(timeout=60.0)
    rows = []
    for case in CASES:
        query = case[0]
        questions, state = tc.build(query)
        start = time.perf_counter()
        response = client.system_one(state=state, questions=questions)
        ms = (time.perf_counter() - start) * 1000
        route = response.answers[tc.ROUTE]
        if route.choice == tc.NONE:
            tool, arguments, confidence = None, {}, route.confidence
        else:
            tool = route.choice
            arguments, _unfilled, weakest, _which = tc.assemble(tool, response.answers)
            confidence = min(route.confidence, weakest) if weakest is not None else route.confidence
        rows.append({"query": query, "tool": tool, "arguments": arguments,
                     "ms": round(ms, 1), "confidence": round(confidence, 3),
                     "in_tokens": response.usage.input_tokens,
                     "out_tokens": response.usage.output_tokens,
                     **grade(case, tool, arguments)})
        print(f"  {ms:7.0f} ms  {str(tool):16} {query[:44]}")
    return rows


def identify_local() -> dict:
    meta = httpx.get(f"{LOCAL_URL}/v1/models", timeout=10.0).json()["data"][0]
    n_params = meta.get("meta", {}).get("n_params")
    return {"served_id": meta.get("id"), "n_params": n_params,
            "size": "2B" if n_params and n_params < 3e9 else "4B"}


def run_local() -> list[dict]:
    tools = openai_tools()
    rows = []
    with httpx.Client(timeout=180.0) as client:
        for case in CASES:
            query = case[0]
            # Thinking is off, so the routing decision does not pay for a
            # reasoning trace nobody reads. See experiment 03: left on, these
            # models spend thousands of tokens deliberating before they answer,
            # and the 2B sometimes never finishes at all.
            payload = {"model": "qwen3.5-4b", "temperature": 0, "max_tokens": 512,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": query}],
                       "tools": tools, "tool_choice": "auto"}
            start = time.perf_counter()
            reply = client.post(f"{LOCAL_URL}/v1/chat/completions", json=payload).json()
            ms = (time.perf_counter() - start) * 1000

            message = reply["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            tool, arguments = None, {}
            if calls:
                tool = calls[0]["function"]["name"]
                try:
                    arguments = json.loads(calls[0]["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    # A malformed argument blob counts as a failed call.
                    arguments = {"__unparseable__": calls[0]["function"]["arguments"]}
            usage = reply.get("usage", {})
            rows.append({"query": query, "tool": tool, "arguments": arguments,
                         "ms": round(ms, 1), "confidence": None,
                         "in_tokens": usage.get("prompt_tokens"),
                         "out_tokens": usage.get("completion_tokens"),
                         **grade(case, tool, arguments)})
            print(f"  {ms:7.0f} ms  {str(tool):16} {query[:44]}")
    return rows


def summarise(rows: list[dict]) -> dict:
    lat = [r["ms"] for r in rows]
    freeform = [r["freeform_filled"] for r in rows if r["freeform_filled"] is not None]
    return {
        "n": len(rows),
        "tool_correct": sum(r["tool_ok"] for r in rows),
        "call_correct": sum(r["call_ok"] for r in rows),
        "median_ms": round(statistics.median(lat), 1),
        "mean_ms": round(statistics.mean(lat), 1),
        "min_ms": round(min(lat), 1),
        "max_ms": round(max(lat), 1),
        "mean_in_tokens": round(statistics.mean(r["in_tokens"] or 0 for r in rows), 1),
        "mean_out_tokens": round(statistics.mean(r["out_tokens"] or 0 for r in rows), 1),
        "freeform_filled": f"{sum(bool(f) for f in freeform)}/{len(freeform)}" if freeform else "n/a",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=["jev", "local"])
    parser.add_argument("--label", default=None, help="name for the results file")
    args = parser.parse_args()

    if args.backend == "jev":
        label = args.label or "jev-latest"
        print(f"== {label} ==")
        rows = run_jev()
        meta = {"backend": "jev"}
    else:
        meta = identify_local()
        label = args.label or f"qwen3.5-{meta['size'].lower()}"
        print(f"== {label}  (served id {meta['served_id']}, n_params {meta['n_params']}) ==")
        rows = run_local()

    summary = summarise(rows)
    print("\n" + json.dumps(summary, indent=2))

    out = Path(__file__).resolve().parent / f"results_{label}.json"
    out.write_text(json.dumps({"label": label, "meta": meta, "summary": summary,
                               "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nwrote {out.name}")


if __name__ == "__main__":
    main()
