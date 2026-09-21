"""The decision half: hand Jev a numbered screen and read back what to do.

One request per step, carrying four independent judgements over the same state.
They cannot see each other's answers and do not need to, which is the condition
for asking them together:

  click_target    Choice over the numbered controls, plus a "none" option
  target_present  Noul - is the control this task needs on screen at all
  task_complete   Noul - has the screen already reached the goal
  text_to_type    Choice over candidate strings pulled out of the request by code

`click_target` gets its own "none" option AND a separate `target_present` Noul on
purpose. They answer different questions: the option keeps the distribution
honest when nothing fits, while the Noul gives code a number to threshold on.
Experiment 04 measured the option list itself as safe up to 255, so the risk here
is not capacity but the no-match case, which is exactly what the Noul is for.

Jev returns judgements, not text, so it cannot invent the string to type. Code
pulls candidate spans out of the request and Jev picks which one was meant - the
pre-parsed value extraction shape from the cookbooks. If the candidates are
wrong, that is a code bug, and keeping it in code is what makes it debuggable.

Every call goes through BudgetGuard, which caps a single run and also keeps a
cumulative ledger on disk, so an overnight mistake cannot quietly spend the
quota.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from uia_scrape import Element

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime"
RUNTIME.mkdir(exist_ok=True)
LEDGER = RUNTIME / "api_budget.json"

load_dotenv(str(HERE.parent / ".env"))

from typesafe_sdk import Choice, Noul, TypeSafeClient  # noqa: E402

# Blended rate measured in experiment 04: $0.021 for 835,264 tokens.
USD_PER_TOKEN = 0.021 / 835_264

NONE_KEY = "none"


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    """Caps calls for this run and for all runs ever, and prices what it spent."""

    def __init__(self, max_calls_this_run: int = 25, lifetime_cap: int = 400):
        self.max_calls_this_run = max_calls_this_run
        self.lifetime_cap = lifetime_cap
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() \
            else {"total_calls": 0, "total_in": 0, "total_out": 0}
        # Snapshot the starting total. flush() writes this run's calls into the
        # ledger, so anything reading ledger["total_calls"] afterwards and adding
        # self.calls counts them twice - which reported 127 when the true total
        # was 118. Harmless in the safe direction, wrong either way.
        self.calls_before = self.ledger["total_calls"]
        if self.ledger["total_calls"] >= lifetime_cap:
            raise BudgetExceeded(
                f"lifetime cap reached: {self.ledger['total_calls']} calls already "
                f"spent against a cap of {lifetime_cap}. Raise --lifetime-cap "
                f"deliberately or delete {LEDGER.name} to reset.")

    def check(self) -> None:
        if self.calls >= self.max_calls_this_run:
            raise BudgetExceeded(
                f"this run hit its {self.max_calls_this_run}-call cap")
        if self.calls_before + self.calls >= self.lifetime_cap:
            raise BudgetExceeded(f"lifetime cap of {self.lifetime_cap} calls reached")

    def record(self, usage) -> None:
        self.calls += 1
        self.in_tokens += usage.input_tokens
        self.out_tokens += usage.output_tokens

    @property
    def est_usd(self) -> float:
        return (self.in_tokens + self.out_tokens) * USD_PER_TOKEN

    def flush(self) -> None:
        self.ledger["total_calls"] += self.calls
        self.ledger["total_in"] += self.in_tokens
        self.ledger["total_out"] += self.out_tokens
        self.ledger["est_usd_lifetime"] = round(
            (self.ledger["total_in"] + self.ledger["total_out"]) * USD_PER_TOKEN, 6)
        LEDGER.write_text(json.dumps(self.ledger, indent=2), encoding="utf-8")

    def summary(self) -> dict:
        return {"calls": self.calls, "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens, "est_usd": round(self.est_usd, 6),
                "lifetime_calls": self.calls_before + self.calls,
                "lifetime_cap": self.lifetime_cap}


# --------------------------------------------------------------------------
# candidate text spans, found by code
# --------------------------------------------------------------------------

TRIGGERS = r"(?:search(?:\s+for)?|type|enter|write|put|fill in|look up|find)"


def text_candidates(request: str) -> list[str]:
    """Plausible strings the user might have meant to type, most specific first."""
    out: list[str] = []
    for m in re.finditer(r"[\"'“‘]([^\"'”’]{1,80})[\"'”’]",
                         request):
        out.append(m.group(1).strip())
    m = re.search(TRIGGERS + r"\s+(?:for\s+)?(.{2,80})", request, re.I)
    if m:
        out.append(m.group(1).strip(" .!?\"'"))
    cleaned = request.strip(" .!?")
    if cleaned and len(cleaned) <= 80:
        out.append(cleaned)
    seen, unique = set(), []
    for c in out:
        k = c.lower()
        if c and k not in seen:
            seen.add(k)
            unique.append(c)
    return unique[:6]


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------

@dataclass
class Decision:
    target: Element | None
    target_key: str
    p_target: float
    confidence: float
    target_present: float
    task_complete: float
    text_to_type: str | None
    ms: float
    in_tokens: int
    out_tokens: int
    n_options: int

    def as_dict(self) -> dict:
        return {"picked": self.target_key,
                "picked_label": self.target.describe() if self.target else None,
                "p_target": round(self.p_target, 4),
                "confidence": round(self.confidence, 4),
                "target_present": round(self.target_present, 4),
                "task_complete": round(self.task_complete, 4),
                "text_to_type": self.text_to_type,
                "ms": round(self.ms, 1), "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens, "n_options": self.n_options}


CLICK_INSTRUCTIONS = (
    "The user is looking at an application window. Every numbered option below is "
    "one control that is visible and clickable in that window right now, written "
    "as its on-screen label followed by what kind of control it is and which "
    "window it belongs to. The user asked for what is in `user_request`, and "
    "`screen_state.actions_taken_so_far` lists what has already been done toward "
    "it. Choose the single control that should be used NEXT to make progress, "
    "without repeating a step that is already done. A request may take several "
    "actions to finish: a control that begins a multi-step flow, or advances one "
    "already under way, is the correct answer even though more steps will follow "
    "it. When the request states a preference for a later step, use it to pick "
    "between options at that later step, not as a reason to avoid starting."
)

# The first version of this asked whether the control needed to "carry out" the
# request was on screen. That reads as "finish it", and it broke every multi-step
# task: at step one, "Start Send Report" does not carry out "send it to legal as
# an Excel workbook with a cover note, scheduled for later" - it only begins it,
# and the Noul came back 0.46. The gate code actually needs is about the NEXT
# action, so that is what it now asks.
PRESENT_INSTRUCTIONS = (
    "The controls listed in `visible_controls` are everything the user can click "
    "or type into right now, and `screen_state.actions_taken_so_far` lists what "
    "has already been done toward `user_request`. Is at least one of these "
    "controls the correct next action to take?"
)

COMPLETE_INSTRUCTIONS = (
    "`user_request` is what the user asked for. `screen_state` describes the "
    "window as it is right now, including every action already taken in "
    "`actions_taken_so_far`. Has the whole request been carried out, so that no "
    "further action is needed?"
)

TYPE_INSTRUCTIONS = (
    "The user asked for what is in `user_request`, and it involves typing text "
    "into a field. Each option is a candidate string that code pulled out of the "
    "request. Choose the one that is the actual text the user wants typed, not "
    "the instruction wrapped around it."
)


def decide(client: TypeSafeClient, budget: BudgetGuard, request: str,
           elements: list[Element], actions: list[str] | str | None,
           status_text: str | None) -> Decision:
    budget.check()

    options = {str(e.number): e.describe() for e in elements}
    # This option's text matters as much as the instructions. When it read "none
    # of these would carry out the request" it drew probability away from the
    # correct first step of a long flow - opening a wizard does not carry out
    # "send it to legal as an Excel workbook, scheduled", so 'none' looked
    # defensible and won at p=0.49. Scoped to the next action, it stops competing
    # with legitimate progress.
    options[NONE_KEY] = (
        "No control listed here should be used as the next action. Choose this "
        "only when there is nothing useful to click or type next: the controls "
        "needed belong to a different application or do not exist here, or the "
        "request is already fully done, or every step still on offer is one the "
        "user explicitly asked not to take. Do not choose this merely because one "
        "click will not finish the whole request on its own.")

    listing = "; ".join(f"{e.number}. {e.describe()}" for e in elements)
    if actions is None:
        history: list[str] = []
    elif isinstance(actions, str):
        history = [actions]
    else:
        history = list(actions)
    state = {
        "user_request": request,
        "visible_controls": listing,
        "screen_state": {
            "window_status_line": status_text or "unknown",
            # The full history, not just the last step. Code owns the goal and the
            # record of what has been done; Jev judges the screen in front of it.
            # Without the history it cannot tell a half-finished sequence from one
            # that has not started, and re-picks a step it already completed.
            "actions_taken_so_far": history,
            "last_action_taken": history[-1] if history else "nothing yet",
            "controls_on_screen": listing,
        },
    }

    questions = {
        "click_target": Choice(instructions=CLICK_INSTRUCTIONS, criteria=options),
        "target_present": Noul(
            instructions=PRESENT_INSTRUCTIONS,
            criteria={
                "true": "At least one listed control is the right next action. This "
                        "includes the very first step of a longer flow and any "
                        "middle step, even when several more actions would still be "
                        "needed afterwards to finish the request.",
                "false": "No listed control is the right next action. Either the "
                         "control needed is not on screen and belongs to some other "
                         "application or does not exist here, or the request has "
                         "already been carried out, or the only steps still "
                         "available are ones the user explicitly asked not to take.",
            }),
        "task_complete": Noul(
            instructions=COMPLETE_INSTRUCTIONS,
            criteria={
                "true": "The request has already been carried out. Acting again "
                        "would repeat work that is done.",
                "false": "The request has not been carried out yet, or only part "
                         "of it has, so at least one more action is needed.",
            }),
    }

    candidates = text_candidates(request)
    if candidates:
        crit = {f"c{i}": c for i, c in enumerate(candidates)}
        crit["no_text"] = "The request does not involve typing any text."
        questions["text_to_type"] = Choice(instructions=TYPE_INSTRUCTIONS,
                                           criteria=crit)

    start = time.perf_counter()
    response = client.system_one(state=state, questions=questions)
    ms = (time.perf_counter() - start) * 1000
    budget.record(response.usage)

    answers = response.answers
    key = answers["click_target"].choice
    probs = answers["click_target"].probabilities
    by_number = {str(e.number): e for e in elements}

    typed = None
    if "text_to_type" in answers:
        tkey = answers["text_to_type"].choice
        if tkey != "no_text":
            typed = candidates[int(tkey[1:])]

    return Decision(
        target=by_number.get(key),
        target_key=key,
        p_target=probs.get(key, 0.0),
        confidence=answers["click_target"].confidence,
        target_present=answers["target_present"].noul,
        task_complete=answers["task_complete"].noul,
        text_to_type=typed,
        ms=ms,
        in_tokens=response.usage.input_tokens,
        out_tokens=response.usage.output_tokens,
        n_options=len(options),
    )
