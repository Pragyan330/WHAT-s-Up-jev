"""Does a Jev Choice hold up with hundreds of options, and where does it stop?

Before building a screen-control agent on top of numbered UI elements, we need to
know two things, and they are different questions:

  cap    the hard server-side limit. How many options does the API accept before
         it rejects the request? Docs say 255 per Choice; this checks it, checks
         what 256 does, and checks the error is clean rather than a hang.

  scale  the useful limit, which is the one that matters. A request that returns
         200 is worthless if the model quietly stops discriminating past the
         first couple of dozen options. So: hide one correct control among N-1
         plausible distractors, vary N, and vary WHERE in the list the correct
         one sits. If attention decays down the list, accuracy at position 0.9
         collapses while position 0.0 stays fine - and that pattern is invisible
         if you only test one position.

  hard   the same sweep with each target's near-lookalikes forced into the list
         ("Autofit Row Height" beside "Autofit Column Width"), to tell "too many
         options" apart from "options that look alike".

Run:
    .venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py cap
    .venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py scale
    .venv/Scripts/python.exe experiments/04-choice-option-scaling/verify_option_limit.py hard

Option keys are the numbers "1".."N", exactly as a Voice-Control-style overlay
would paint them on screen. That is deliberate: the keys carry no meaning, so
every bit of signal has to come from the descriptions, which is the real
architecture under test rather than a friendlier version of it.
"""

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv

import screen_elements as se

load_dotenv(str(HERE.parents[1] / ".env"))

from typesafe_sdk import Choice, TypeSafeClient, TypeSafeError  # noqa: E402

SEED = 20260921

INSTRUCTIONS = (
    "The user is looking at a computer screen. Every option below is one control "
    "that is visible on that screen right now, written as its on-screen label "
    "followed by where in the interface it appears. The user said what is in "
    "`user_request`. Choose the single numbered control that should be clicked to "
    "do what the user asked."
)


def describe(element: tuple[str, str]) -> str:
    label, where = element
    return f"{label} ({where})"


def build_options(target: tuple[str, str], n: int, position: float, rng: random.Random,
                  required: list[tuple[str, str]] | None = None):
    """N options, numbered 1..N, with `target` placed at `position` of the way down.

    Distractors are sampled fresh per trial so a single unlucky draw cannot carry
    the whole result, and the sample is recorded so any failure is reproducible.
    `required` distractors are always present and scattered through the list, so
    the hard mode cannot get an easy trial by failing to draw the near-rivals.
    """
    pool = [e for e in se.POOL if e not in se.TARGET_ELEMENTS]
    filler = [(f"Toolbar control {i}", "application toolbar overflow")
              for i in range(len(pool), n + 8)]
    required = list(required or [])
    sample_n = max(0, min(n - 1 - len(required), len(pool)))
    distractors = rng.sample(pool, sample_n)
    if len(distractors) + len(required) < n - 1:
        distractors += filler[: (n - 1) - len(distractors) - len(required)]
    for element in required:
        distractors.insert(rng.randrange(len(distractors) + 1), element)

    idx = min(n - 1, max(0, round(position * (n - 1))))
    ordered = distractors[:idx] + [target] + distractors[idx:]
    criteria = {str(i + 1): describe(e) for i, e in enumerate(ordered)}
    return criteria, str(idx + 1)


def ask(client, request_text: str, criteria: dict) -> dict:
    start = time.perf_counter()
    response = client.system_one(
        state={"user_request": request_text},
        questions={"click": Choice(instructions=INSTRUCTIONS, criteria=criteria)},
    )
    ms = (time.perf_counter() - start) * 1000
    answer = response.answers["click"]
    return {
        "picked": answer.choice,
        "confidence": answer.confidence,
        "probabilities": answer.probabilities,
        "ms": ms,
        "in_tokens": response.usage.input_tokens,
        "out_tokens": response.usage.output_tokens,
    }


# --------------------------------------------------------------------------
# cap: where does the API stop accepting options?
# --------------------------------------------------------------------------

CAP_COUNTS = [2, 10, 64, 128, 200, 254, 255, 256, 260, 300, 512, 1000]


def run_cap(client) -> list[dict]:
    rng = random.Random(SEED)
    task_id, request_text, target = se.TARGETS[4]  # wifi; unambiguous at any N
    rows = []
    for n in CAP_COUNTS:
        criteria, correct_key = build_options(target, n, 0.5, rng)
        row = {"n_options": n, "correct_key": correct_key}
        try:
            row.update(ask(client, request_text, criteria))
            row["accepted"] = True
            row["correct"] = row["picked"] == correct_key
            print(f"  n={n:<5} accepted   picked {row['picked']:>4} "
                  f"(correct {correct_key:>4})  conf {row['confidence']:.2f}  "
                  f"{row['ms']:6.0f} ms  {row['in_tokens']} in")
        except TypeSafeError as exc:
            row.update({"accepted": False, "error": type(exc).__name__,
                        "message": str(exc)[:300],
                        "status": getattr(exc, "status_code", None)})
            print(f"  n={n:<5} REJECTED   {type(exc).__name__}: {str(exc)[:160]}")
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# scale: does accuracy survive the option count, and the position in the list?
# --------------------------------------------------------------------------

SCALE_COUNTS = [10, 40, 100, 180, 255]
POSITIONS = [0.0, 0.25, 0.5, 0.75, 1.0]
# The hard mode only needs the two ends of the range: if near-rivals are what
# break it, they break it at 10 options too, and that is the finding.
HARD_COUNTS = [10, 255]


def run_scale(client, counts: list[int], hard: bool = False) -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    for n in counts:
        print(f"\n  -- {n} options " + "-" * 40)
        for task_id, request_text, target in se.TARGETS:
            rivals = se.RIVALS[task_id] if hard else None
            for position in POSITIONS:
                criteria, correct_key = build_options(target, n, position, rng, rivals)
                try:
                    result = ask(client, request_text, criteria)
                except TypeSafeError as exc:
                    print(f"     {task_id:<8} pos {position:<4} ERROR {exc}")
                    rows.append({"n_options": n, "task": task_id, "position": position,
                                 "error": str(exc)[:200]})
                    continue

                probs = result["probabilities"]
                # Rank of the right answer in the distribution, not just whether
                # it won. Rank 2 of 255 is a near miss; rank 200 is the model
                # never really considering it.
                ranked = sorted(probs, key=lambda k: probs[k], reverse=True)
                rank = ranked.index(correct_key) + 1 if correct_key in ranked else None
                ok = result["picked"] == correct_key
                rows.append({
                    "n_options": n, "task": task_id, "position": position,
                    "correct_key": correct_key, "correct_index_0based": int(correct_key) - 1,
                    "picked": result["picked"],
                    "picked_label": criteria.get(result["picked"]),
                    "correct_label": criteria[correct_key],
                    "correct": ok,
                    "p_correct": round(probs.get(correct_key, 0.0), 4),
                    "p_picked": round(probs.get(result["picked"], 0.0), 4),
                    "rank_of_correct": rank,
                    "confidence": round(result["confidence"], 4),
                    "ms": round(result["ms"], 1),
                    "in_tokens": result["in_tokens"],
                    "out_tokens": result["out_tokens"],
                })
                flag = "  " if ok else "XX"
                print(f"     {flag} {task_id:<8} pos {position:<4} "
                      f"picked {result['picked']:>4} / correct {correct_key:>4}  "
                      f"p_correct {probs.get(correct_key, 0.0):.3f}  rank {rank}  "
                      f"{result['ms']:6.0f} ms"
                      + ("" if ok else f"   -> {criteria.get(result['picked'])}"))
    return rows


def summarise_scale(rows: list[dict]) -> dict:
    good = [r for r in rows if "error" not in r]
    by_n, by_pos = {}, {}
    for r in good:
        by_n.setdefault(r["n_options"], []).append(r)
        by_pos.setdefault(r["position"], []).append(r)

    def block(group: list[dict]) -> dict:
        return {
            "n": len(group),
            "correct": sum(r["correct"] for r in group),
            "accuracy": round(sum(r["correct"] for r in group) / len(group), 3),
            "median_p_correct": round(statistics.median(r["p_correct"] for r in group), 3),
            "median_rank_of_correct": statistics.median(
                r["rank_of_correct"] for r in group if r["rank_of_correct"]),
            "median_ms": round(statistics.median(r["ms"] for r in group), 1),
            "median_in_tokens": int(statistics.median(r["in_tokens"] for r in group)),
        }

    return {
        "total": len(rows),
        "errors": len(rows) - len(good),
        "overall_accuracy": round(sum(r["correct"] for r in good) / len(good), 3),
        "by_option_count": {str(k): block(v) for k, v in sorted(by_n.items())},
        "by_target_position": {str(k): block(v) for k, v in sorted(by_pos.items())},
        "misses": [{"n": r["n_options"], "task": r["task"], "pos": r["position"],
                    "picked": r["picked_label"], "should_be": r["correct_label"],
                    "p_correct": r["p_correct"], "rank": r["rank_of_correct"]}
                   for r in good if not r["correct"]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["cap", "scale", "hard"])
    parser.add_argument("--counts", type=int, nargs="*", default=None,
                        help="override the option counts for scale mode")
    args = parser.parse_args()

    client = TypeSafeClient(timeout=120.0)

    if args.mode == "cap":
        print("== hard cap probe ==")
        rows = run_cap(client)
        out = HERE / "results_cap.json"
        out.write_text(json.dumps({"counts": CAP_COUNTS, "rows": rows}, indent=2),
                       encoding="utf-8")
        accepted = [r["n_options"] for r in rows if r.get("accepted")]
        rejected = [r["n_options"] for r in rows if not r.get("accepted")]
        print(f"\naccepted: {accepted}\nrejected: {rejected}")
        print(f"wrote {out.name}")
    else:
        hard = args.mode == "hard"
        counts = args.counts or (HARD_COUNTS if hard else SCALE_COUNTS)
        print(f"== accuracy vs option count{' (near-rivals injected)' if hard else ''} "
              f"==  counts={counts} targets={len(se.TARGETS)} positions={POSITIONS}")
        rows = run_scale(client, counts, hard=hard)
        summary = summarise_scale(rows)
        print("\n" + json.dumps(summary, indent=2))
        out = HERE / (f"results_{'hard' if hard else 'scale'}.json")
        out.write_text(json.dumps({"seed": SEED, "counts": counts, "hard": hard,
                                   "positions": POSITIONS, "summary": summary,
                                   "rows": rows}, indent=2), encoding="utf-8")
        print(f"\nwrote {out.name}")


if __name__ == "__main__":
    main()
