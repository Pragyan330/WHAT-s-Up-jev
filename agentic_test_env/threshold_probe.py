"""Where should the act/decline threshold sit?

The live run failed one task by a hair: for "Change my billing address", the
`target_present` Noul came back 0.56 against a threshold of 0.5, so the agent
clicked Open Preferences instead of declining. Across three runs the same task
gave 0.44, 0.23 and 0.56 - it straddles the line.

0.5 was a placeholder, not a measurement. The docs are explicit that thresholds
belong to us and have to be set against our own data and consequences, so this
samples every task a few times and reports the gap between the tasks that should
act and the tasks that should decline. The threshold belongs in that gap.

Nothing here clicks anything: it only asks.

    .venv/Scripts/python.exe agentic_test_env/threshold_probe.py --reps 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uiautomation as auto

from agent import NONE_KEY, BudgetExceeded, BudgetGuard, decide
from mock_ui import MAIN_TITLE, PREFS_TITLE
from run_test import ALLOWLIST, ensure_precondition
from tasks import TASKS
from typesafe_sdk import TypeSafeClient
from uia_scrape import scrape

OUT = HERE / "runtime" / "threshold_probe.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--only", nargs="*", default=None,
                    help="task ids to probe; omit for all")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--lifetime-cap", type=int, default=400)
    args = ap.parse_args()

    if not auto.WindowControl(searchDepth=1, Name=MAIN_TITLE).Exists(2):
        sys.exit(f"'{MAIN_TITLE}' is not open. Launch mock_ui.py first.")

    budget = BudgetGuard(args.max_calls, args.lifetime_cap)
    client = TypeSafeClient(timeout=60.0)

    # Each task gets its own precondition and its own scrape. The first version
    # probed everything against one frozen screen, which quietly broke
    # close_panel: with Preferences shut its target genuinely was not present, so
    # it declined correctly and got scored as a miss.
    rows = []
    try:
        for task in TASKS:
            if args.only and task["id"] not in args.only:
                continue
            should_act = task["expect"]["kind"] != "none"
            ensure_precondition(task)
            windows = [w for w in ALLOWLIST
                       if auto.WindowControl(searchDepth=1, Name=w).Exists(0)]
            elements, stats = scrape(windows)
            for rep in range(args.reps):
                d = decide(client, budget, task["request"], elements, None, "Ready.")
                rows.append({"task": task["id"], "should_act": should_act,
                             "rep": rep, "picked": d.target_key,
                             "picked_label": d.target.label if d.target else None,
                             "target_present": round(d.target_present, 4),
                             "p_target": round(d.p_target, 4),
                             "chose_none": d.target_key == NONE_KEY})
            vals = [r["target_present"] for r in rows if r["task"] == task["id"]]
            nones = sum(r["chose_none"] for r in rows if r["task"] == task["id"])
            print(f"  {'ACT ' if should_act else 'DECL'} {task['id']:<17} "
                  f"[{stats['kept']} ctrls] "
                  f"present={[f'{v:.2f}' for v in vals]} "
                  f"chose_none={nones}/{args.reps}")
    except BudgetExceeded as exc:
        print(f"\n!! stopped: {exc}")
    finally:
        budget.flush()

    act_vals = [r["target_present"] for r in rows if r["should_act"]]
    dec_vals = [r["target_present"] for r in rows if not r["should_act"]]
    summary = {
        "n": len(rows),
        "should_act": {"n": len(act_vals), "min": min(act_vals, default=None),
                       "median": statistics.median(act_vals) if act_vals else None},
        "should_decline": {"n": len(dec_vals), "max": max(dec_vals, default=None),
                           "median": statistics.median(dec_vals) if dec_vals else None},
    }
    if act_vals and dec_vals:
        lo, hi = max(dec_vals), min(act_vals)
        summary["separable"] = hi > lo
        summary["gap"] = [round(lo, 3), round(hi, 3)]
        summary["suggested_threshold"] = round((lo + hi) / 2, 3) if hi > lo else None
        # A Choice that says "none" is a second, independent signal. Where the
        # Noul is marginal, requiring both to agree costs nothing extra - they
        # come back in the same call.
        summary["none_option_would_catch"] = sum(
            1 for r in rows if not r["should_act"] and r["chose_none"])
        summary["none_option_false_alarms"] = sum(
            1 for r in rows if r["should_act"] and r["chose_none"])

    print("\n" + json.dumps(summary, indent=2))
    OUT.write_text(json.dumps({"summary": summary, "rows": rows,
                               "budget": budget.summary()}, indent=2),
                   encoding="utf-8")
    print(f"\nwrote {OUT.name}  budget={json.dumps(budget.summary())}")


if __name__ == "__main__":
    main()
