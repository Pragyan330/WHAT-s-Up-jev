"""Tool calling with Jev: type a request, get the tool call back as JSON.

    .venv/Scripts/python.exe manual_test/tool_caller.py

An LLM doing tool calling pays full generation price to pick a function and
fill its arguments. That step is a set of typed judgements, which is what
System One is for. The prose and the follow-up afterwards stay the LLM's job.

Shape, following the function-calling and value-extraction cookbooks:

  * One Choice routes the request to a tool.
  * Every tool's argument questions go out in the same request, speculatively.
    They cannot see the routing answer, so each states its premise ("Assume
    the user is asking to ..."). Code then reads only the chosen tool's args.
  * Jev cannot generate text. Free-text arguments are handled by finding
    candidate spans in code with a recall-tuned regex and asking Jev to pick
    one, so the value is a verbatim copy of the input or the no-match hatch.
  * Numeric and date arguments get no question at all and keep their default.
    jev-1.13 reads dates as text and does not count reliably.
  * Reported confidence is the weakest judgement in the call. One wrong
    argument spoils the result however sure the rest were.
"""

import json
import os
import re
import sys

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, TypeSafeClient, TypeSafeError

NONE = "__none__"

# Recall-tuned: over-find on purpose. A wrong extra candidate costs one option
# in a Choice; a missing one cannot be selected at all.
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "to", "for", "of", "in", "on", "at",
    "and", "or", "my", "me", "i", "it", "that", "this", "please", "can", "you",
    "what", "whats", "how", "do", "does", "with", "from", "about", "get", "set",
    "put", "tell", "give", "s", "be", "will", "would", "should", "let", "make",
}
QUOTED = re.compile(r"[\"'`]([^\"'`]{2,60})[\"'`]")
CAPPED = re.compile(r"\b[A-Z][A-Za-z'\-]{1,}\b")
# Allow a lowercase continuation so that "to call the dentist" offers the
# whole phrase as well as its head word. A span that is never a candidate can
# never be chosen, so both go in the list.
AFTER_PREP = re.compile(
    r"\b(?:in|to|for|at|about|with|near|on)\s+([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*){0,3})"
)
WORD = re.compile(r"\b[A-Za-z][A-Za-z'\-]{1,}\b")


def trim(span: str) -> str:
    """Drop leading and trailing stopwords, so a phrase match does not end on 'on' or 'the'."""
    words = span.split()
    while words and words[0].lower() in STOPWORDS:
        words.pop(0)
    while words and words[-1].lower() in STOPWORDS:
        words.pop()
    return " ".join(words)


def candidates(text: str, limit: int = 12) -> list[str]:
    """Code-side candidate finder. Deduped, in document order, recall over precision."""
    found: list[str] = []
    seen: set[str] = set()
    for source in (QUOTED.findall(text), AFTER_PREP.findall(text), CAPPED.findall(text), WORD.findall(text)):
        for raw in source:
            span = trim(raw.strip())
            if not span or span.lower() in STOPWORDS or span.lower() in seen:
                continue
            seen.add(span.lower())
            found.append(span)
            if len(found) >= limit:
                return found
    return found


# Each tool lists only the arguments Jev can actually decide. "numeric" and
# "date" args are declared so the output can say plainly they were never asked.
TOOLS = {
    "get_weather": {
        "description": "Look up the weather forecast for a place.",
        "premise": "the user is asking for a weather forecast",
        "args": {
            "location": {"kind": "span", "ask": "Which place should the forecast be for?"},
            "units": {
                "kind": "choice",
                "ask": "Which temperature units does the user want?",
                "options": {
                    "celsius": "Celsius, or the user is somewhere that uses it.",
                    "fahrenheit": "Fahrenheit, or the user is somewhere that uses it.",
                },
            },
            "days": {"kind": "numeric", "default": 1},
        },
    },
    "send_message": {
        "description": "Send a message or email to a person.",
        "premise": "the user is asking to send a message to someone",
        "args": {
            "recipient": {"kind": "span", "ask": "Who should the message be sent to?"},
            "urgency": {
                "kind": "choice",
                "ask": "How urgent is this message?",
                "options": {
                    "normal": "An ordinary message with no time pressure.",
                    "urgent": "The user signals haste, or the matter is time critical.",
                },
            },
        },
    },
    "play_music": {
        "description": "Play music, a song, an artist, or a genre.",
        "premise": "the user is asking to play music",
        "args": {
            "artist_or_track": {"kind": "span", "ask": "Which artist, song, or genre should be played?"},
            "shuffle": {"kind": "flag", "ask": "Does the user want the music shuffled or played in a random order?"},
        },
    },
    "create_reminder": {
        "description": "Create a reminder or a calendar event for later.",
        "premise": "the user is asking to be reminded of something later",
        "args": {
            "subject": {"kind": "span", "ask": "What is the reminder about?"},
            "when": {"kind": "date", "default": None},
        },
    },
    "search_web": {
        # Criteria must describe something decidable from the request itself.
        # Describing this tool by what the assistant already knows asks the
        # model about its own knowledge, which is nowhere in the state, and
        # factual questions then route to no tool at all.
        "description": "Look up a fact or information. The user wants to know something, "
                       "rather than have an action performed.",
        "premise": "the user is asking to search the web",
        "args": {
            "topic": {"kind": "span", "ask": "What is the search mainly about?"},
        },
    },
}

ROUTE = "__route__"


def build(query: str) -> tuple[dict, dict]:
    """One request: the route plus every tool's arguments, asked speculatively."""
    spans = candidates(query)
    questions: dict[str, object] = {
        ROUTE: Choice(
            instructions="What is the user in `request` asking the assistant to do?",
            criteria={name: tool["description"] for name, tool in TOOLS.items()}
            | {NONE: "None of these tools fits the request."},
        )
    }
    for name, tool in TOOLS.items():
        for arg, spec in tool["args"].items():
            qid = f"{name}::{arg}"
            # Each speculative question carries its own premise, since it cannot
            # see how the routing question was answered.
            premise = f"Assume {tool['premise']}. "
            if spec["kind"] == "span":
                if not spans:
                    continue
                questions[qid] = Choice(
                    instructions=premise + spec["ask"] + " Pick the span from `request` that plays this role.",
                    criteria={span: None for span in spans} | {NONE: "None of these is the value."},
                )
            elif spec["kind"] == "choice":
                questions[qid] = Choice(
                    instructions=premise + spec["ask"],
                    criteria=dict(spec["options"]) | {NONE: "The request does not say."},
                )
            elif spec["kind"] == "flag":
                questions[qid] = Noul(instructions=premise + spec["ask"])
    return questions, {"request": query}


def assemble(tool_name: str, answers: dict) -> tuple[dict, dict, float | None, str | None]:
    """Read only the chosen tool's answers. Confidence is the weakest of them."""
    arguments: dict[str, object] = {}
    unfilled: dict[str, str] = {}
    weakest_value: float | None = None
    weakest_arg: str | None = None

    for arg, spec in TOOLS[tool_name]["args"].items():
        if spec["kind"] in ("numeric", "date"):
            unfilled[arg] = f"{spec['kind']} - never asked, kept default {spec['default']!r}"
            continue
        answer = answers.get(f"{tool_name}::{arg}")
        if answer is None:
            unfilled[arg] = "no candidates found in the request"
            continue

        if spec["kind"] == "flag":
            arguments[arg] = answer.noul >= 0.5
            certainty = max(answer.noul, 1 - answer.noul)
        elif answer.choice == NONE:
            unfilled[arg] = "the request does not say"
            continue
        else:
            arguments[arg] = answer.choice
            certainty = answer.confidence

        if weakest_value is None or certainty < weakest_value:
            weakest_value, weakest_arg = certainty, arg

    return arguments, unfilled, weakest_value, weakest_arg


def main() -> None:
    load_dotenv()
    if not os.getenv("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set. Copy .env.example to .env and fill it in.")

    client = TypeSafeClient()
    print("Describe what you want done. Blank line or Ctrl+C to quit.\n")

    while True:
        try:
            query = input("YOU: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not query:
            return

        questions, state = build(query)
        try:
            response = client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            print(f"JEV: (no call - {type(exc).__name__}: {exc})\n")
            continue

        answers = response.answers
        route = answers[ROUTE]

        if route.choice == NONE:
            call = {
                "tool": None,
                "reason": "no tool fits this request",
                "route_confidence": round(route.confidence, 3),
            }
        else:
            arguments, unfilled, weakest_value, weakest_arg = assemble(route.choice, answers)
            # The route is a judgement too, so it competes to be the weakest link.
            confidence, limiter = route.confidence, "tool"
            if weakest_value is not None and weakest_value < confidence:
                confidence, limiter = weakest_value, weakest_arg
            call = {
                "tool": route.choice,
                "arguments": arguments,
                "confidence": round(confidence, 3),
                "weakest_link": limiter,
            }
            if unfilled:
                call["unfilled"] = unfilled

        print(f"JEV: {json.dumps(call, indent=2)}")
        used = response.usage
        print(
            f"     [{len(questions)} questions, 1 request, "
            f"{used.input_tokens or '?'} in / {used.output_tokens or '?'} out]\n"
        )


if __name__ == "__main__":
    main()
