"""Chat-style terminal loop: you ask a yes/no question, Jev answers yes or no.

    .venv/Scripts/python.exe manual_test/chat.py

There is no chatbot underneath. Jev generates no text at all. Each line you
type becomes the state, and one Noul asks whether the answer is yes. The reply
is a probability that the code turns into a word.

Earlier turns are sent along as state, so follow-ups that lean on what was
already said ("is it dangerous?") still resolve.
"""

import os
import sys

from dotenv import load_dotenv
from typesafe_sdk import Noul, TypeSafeClient, TypeSafeError

# How many previous turns to carry. Enough for pronouns to resolve, short
# enough that old topics don't colour a new question.
HISTORY_TURNS = 6

QUESTION = Noul(
    instructions=(
        "Answer the question in `question` as a yes/no judgement. "
        "`history` holds the earlier turns of the same conversation and is "
        "context only - answer `question`, not anything in `history`."
    ),
    criteria={
        "true": "The honest answer to the question is yes, or the claim it makes holds.",
        "false": "The honest answer to the question is no, or the claim it makes does not hold.",
    },
)


def verdict(probability: float) -> str:
    """A noul near 0.5 means torn between yes and no. Say that instead of rounding it."""
    if probability >= 0.6:
        return "yes"
    if probability <= 0.4:
        return "no"
    return "could go either way"


def main() -> None:
    load_dotenv()
    if not os.getenv("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set. Copy .env.example to .env and fill it in.")

    client = TypeSafeClient()
    history: list[dict[str, str]] = []

    print("Ask Jev a yes/no question. Blank line or Ctrl+C to quit.\n")

    while True:
        try:
            question = input("YOU: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            return

        state = {"question": question}
        if history:
            state["history"] = history[-HISTORY_TURNS:]

        try:
            response = client.system_one(state=state, questions={"answer": QUESTION})
        except TypeSafeError as exc:
            # Keep the conversation alive; a failed turn is not a reason to exit.
            print(f"JEV: (no answer - {type(exc).__name__}: {exc})\n")
            continue

        probability = response.answers["answer"].noul
        answer = verdict(probability)
        print(f"JEV: {answer}   [yes {probability:.2f}]\n")

        history.append({"you": question, "jev": answer})


if __name__ == "__main__":
    main()
