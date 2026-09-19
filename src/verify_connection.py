"""Smallest real call to Jev: confirms the key, the wire shape, and all three primitives.

Run once after filling in .env. Not an experiment — it only proves the pipe works.

    .venv/Scripts/python.exe src/verify_connection.py
"""

import os
import sys

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient, TypeSafeError

load_dotenv()

if not os.getenv("TYPESAFE_API_KEY"):
    sys.exit("TYPESAFE_API_KEY is not set. Copy .env.example to .env and fill it in.")

# A throwaway state with an obvious right answer, so a wrong result means the
# integration is broken rather than the model being interesting.
STATE = {
    "ticket": {
        "subject": "Charged twice for the October invoice",
        "messages": [
            {"author": "customer", "text": "You billed my card twice this month. Refund one."}
        ],
    }
}

QUESTIONS = {
    # Choice: criteria is a label -> description map.
    "category": Choice(
        instructions="What is `ticket` about?",
        criteria={
            "billing": "Payments, invoices, charges, refunds.",
            "technical": "The product is broken or behaving incorrectly.",
            "other": "Anything the labels above do not cover.",
        },
    ),
    # Noul: yes/no, criteria optional. Returns a probability, no confidence.
    "wants_refund": Noul(
        instructions="Is the customer in `ticket` asking for money back?",
        criteria={
            "true": "They ask for a refund, reversal, or credit.",
            "false": "They report a problem without asking for money back.",
        },
    ),
    # Score: criteria is an ordered list, one concrete situation per level from 0.
    "urgency": Score(
        instructions="How urgent is `ticket` for a support team?",
        criteria=[
            "No time pressure; the customer is asking a general question.",
            "Should be handled this week; the customer is inconvenienced.",
            "Should be handled today; the customer is losing money right now.",
        ],
    ),
}


def main() -> None:
    client = TypeSafeClient()
    try:
        response = client.system_one(state=STATE, questions=QUESTIONS)
    except TypeSafeError as exc:
        sys.exit(f"Request failed: {type(exc).__name__}: {exc}")

    answers = response.answers
    print(f"model: {response.model}")
    print(f"usage: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    print()
    # Each primitive is read differently; that is the point of printing all three.
    print(f"category     {answers['category'].choice}  (confidence {answers['category'].confidence:.2f})")
    print(f"             {answers['category'].probabilities}")
    print(f"wants_refund {answers['wants_refund'].noul:.2f}  (probability of yes; no confidence field)")
    print(f"urgency      {answers['urgency'].score:.2f}  (confidence {answers['urgency'].confidence:.2f})")
    print(f"             {answers['urgency'].legend}")


if __name__ == "__main__":
    main()
