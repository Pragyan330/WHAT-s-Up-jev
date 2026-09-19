"""Manual test harness: type a state and some questions, see how Jev answers.

Interactive and throwaway — for poking at the model by hand before an
experiment is worth writing down properly. Not a test suite.

    .venv/Scripts/python.exe manual_test/main.py

All questions in a round go out in a single request. They run in parallel and
cannot see each other's answers, so only put independent questions in one round.
"""

import json
import os
import sys

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient, TypeSafeError

MENU = """  [c] choice  one option from a defined set
  [n] noul    whether a condition holds, as a probability
  [s] score   position on ordered levels
  [enter]     send what you have"""


def ask(prompt: str) -> str:
    """Read one line. EOF and Ctrl+C mean quit, not an error."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def read_state() -> object | None:
    """Read the state block. Valid JSON is sent as structure, anything else as text."""
    print("\nSTATE - the context Jev judges. JSON or plain text; blank line ends it.")
    lines = []
    while True:
        line = ask("  ")
        if not line:
            break
        lines.append(line)
    raw = "\n".join(lines)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def read_choice() -> Choice | None:
    instructions = ask("  question: ")
    print("  options - blank label when done")
    criteria: dict[str, str | None] = {}
    while True:
        label = ask("    label: ")
        if not label:
            break
        criteria[label] = ask(f"    describe '{label}' (optional): ") or None
    if len(criteria) < 2:
        print("  ! a choice needs at least two options - skipped")
        return None
    return Choice(instructions=instructions or None, criteria=criteria)


def read_noul() -> Noul:
    instructions = ask("  question (phrase it so 'yes' is meaningful): ")
    # criteria is optional on a noul; omit it entirely rather than sending empties.
    yes = ask("  describe the yes outcome (optional): ")
    no = ask("  describe the no outcome (optional): ")
    criteria = {k: v for k, v in (("true", yes), ("false", no)) if v} or None
    return Noul(instructions=instructions or None, criteria=criteria)


def read_score() -> Score | None:
    instructions = ask("  question: ")
    print("  levels in order from 0 up - each must describe a concrete situation")
    print("  and stand on its own. Blank line when done.")
    criteria = []
    while True:
        level = ask(f"    level {len(criteria)}: ")
        if not level:
            break
        criteria.append(level)
    if len(criteria) < 2:
        print("  ! a score needs at least two levels - skipped")
        return None
    return Score(instructions=instructions or None, criteria=criteria)


READERS = {"c": read_choice, "n": read_noul, "s": read_score}


def read_questions() -> dict[str, object]:
    """Collect a round of questions. The ids are for our code only, never sent."""
    questions: dict[str, object] = {}
    while True:
        print(f"\nQUESTION {len(questions) + 1}")
        print(MENU)
        kind = ask("  > ").lower()
        if not kind:
            if questions:
                return questions
            print("  ! nothing to send yet")
            continue
        reader = READERS.get(kind)
        if reader is None:
            print(f"  ! '{kind}' is not one of c, n, s")
            continue
        question = reader()
        if question is not None:
            questions[f"q{len(questions) + 1}"] = question


def show(qid: str, answer) -> None:
    """Print one answer. Each primitive carries different fields."""
    if answer.type == "noul":
        # A noul is a probability of yes, with no separate confidence value.
        # 0.5 means torn between yes and no, not "medium intensity".
        print(f"  {qid}  noul {answer.noul:.2f}  probability of yes")
        return

    if answer.type == "choice":
        print(f"  {qid}  choice '{answer.choice}'  confidence {answer.confidence:.2f}")
        ranked = sorted(answer.probabilities.items(), key=lambda kv: -kv[1])
        for option, probability in ranked:
            print(f"          {probability:5.2f}  {option}")
        return

    if answer.type == "score":
        # legend and probabilities are keyed by int level, so sort numerically.
        print(f"  {qid}  score {answer.score:.2f}  confidence {answer.confidence:.2f}")
        probabilities = answer.probabilities or {}
        for level, description in sorted((answer.legend or {}).items()):
            probability = probabilities.get(level)
            share = f"{probability:5.2f}" if probability is not None else "    -"
            # A level description can be an object or array, not only text.
            if not isinstance(description, str):
                description = json.dumps(description)
            print(f"          {share}  [{level}] {description}")
        return

    print(f"  {qid}  {answer!r}")


def main() -> None:
    load_dotenv()
    if not os.getenv("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set. Copy .env.example to .env and fill it in.")

    client = TypeSafeClient()
    print("Manual Jev harness. Ctrl+C or an empty state to quit.")

    while True:
        state = read_state()
        if state is None:
            return
        questions = read_questions()

        try:
            response = client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            # Keep the loop alive — a bad question shouldn't end the session.
            print(f"\n  ! {type(exc).__name__}: {exc}")
            continue

        # Both token counts are optional on the wire.
        used = response.usage
        tokens = f"{used.input_tokens or '?'} in / {used.output_tokens or '?'} out"
        print(f"\nANSWERS  ({response.model}, {tokens})")
        for qid, answer in response.answers.items():
            show(qid, answer)

        if ask("\nAnother round? [Y/n] ").lower().startswith("n"):
            return


if __name__ == "__main__":
    main()
