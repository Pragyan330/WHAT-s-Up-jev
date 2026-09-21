"""Point the same agent at a real site: find a song on YouTube and play it.

Everything about the decision is unchanged from the desktop test. Same
`agent.decide()`, same four judgements in one request, same two-signal risk gate.
Only the scraper and the executor are swapped, so if the numbering approach were
quietly depending on something UIA-shaped, this is where it would show.

This is also the first test where the option list is not comfortably small. A
mock window offered eleven controls; a YouTube results page offers far more, which
is where the 255-option Choice cap from experiment 04 stops being theoretical.

Safety, adapted for the web:

  * A **domain allowlist**, checked against `driver.current_url` immediately
    before every action, not just at scrape time. A click can navigate, so the
    page you are about to act on may not be the page you scraped.
  * A **fresh throwaway Chrome profile** in the scratchpad. No cookies, no
    session, no logged-in account - the agent cannot touch anything that belongs
    to the person running it, and nothing it does persists.
  * The **risk gate** from risk.py, which on a real page earns its keep
    immediately: Subscribe, Share, Report and Sign in are all one click from
    everything else, and all of them are tiered above plain navigation.

    .venv/Scripts/python.exe agentic_test_env/youtube_demo.py
    .venv/Scripts/python.exe agentic_test_env/youtube_demo.py --query "..." --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import risk
from agent import NONE_KEY, BudgetExceeded, BudgetGuard, decide
from typesafe_sdk import TypeSafeClient
from web_scrape import DomainNotAllowed, act, playback_state, scrape

ALLOWED_DOMAINS = ["youtube.com", "youtu.be", "consent.youtube.com",
                   "consent.google.com"]
START_URL = "https://www.youtube.com"
RESULTS = HERE / "runtime" / "youtube_run.json"

# A public-domain composition, so the demo does not hinge on anything in
# particular being available.
DEFAULT_QUERY = "Beethoven Moonlight Sonata"


def goal_for(query: str) -> str:
    return (
        f"Play the song '{query}' on YouTube. Use the search box to search for "
        f"it, then open the most relevant video result so that it starts "
        f"playing. If a cookie or consent notice is blocking the page, dismiss "
        f"it, preferring an option that rejects non-essential cookies over one "
        f"that accepts everything. Do not sign in, subscribe, comment, or share "
        f"anything."
    )


def describe_playback(state: dict) -> str:
    if not state.get("found"):
        return f"No video is loaded on this page. Page title: {state.get('title')!r}."
    if state.get("paused"):
        return (f"A video is loaded but paused at {state.get('currentTime')}s. "
                f"Page title: {state.get('title')!r}.")
    return (f"A video is playing: {state.get('currentTime')}s of "
            f"{state.get('duration')}s elapsed. Page title: "
            f"{state.get('title')!r}.")


def is_playing(state: dict) -> bool:
    return bool(state.get("found") and not state.get("paused")
                and (state.get("currentTime") or 0) > 0.4)


def build_driver(mute: bool, profile_dir: str) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--window-size=1400,950")
    opts.add_argument("--disable-features=Translate")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    if mute:
        opts.add_argument("--mute-audio")
    return webdriver.Chrome(options=opts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--max-calls", type=int, default=14)
    ap.add_argument("--lifetime-cap", type=int, default=800)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mute", action="store_true", help="play with audio muted")
    ap.add_argument("--keep-open", action="store_true",
                    help="leave the browser open after the run")
    args = ap.parse_args()

    goal = goal_for(args.query)
    print(f"goal    : {goal}\n")
    print(f"domains : {ALLOWED_DOMAINS}")

    try:
        budget = BudgetGuard(args.max_calls, args.lifetime_cap)
    except BudgetExceeded as exc:
        sys.exit(f"budget: {exc}")

    client = TypeSafeClient(timeout=60.0)
    profile = tempfile.mkdtemp(prefix="jev-yt-profile-")
    print(f"profile : {profile} (throwaway; no cookies, no session)\n")

    driver = build_driver(args.mute, profile)
    steps: list[dict] = []
    state = {}
    try:
        driver.get(START_URL)
        # Waits here are page-load budget, not model time. Jev answers in about
        # 400 ms, so anything generous added around it is what makes the run
        # feel slow, and the whole point of the approach is that it is not.
        time.sleep(1.2)

        history: list[str] = []
        for step in range(1, args.max_steps + 1):
            state = playback_state(driver)
            if is_playing(state):
                print(f"\n  playing: {describe_playback(state)}")
                break

            controls, stats = scrape(driver, ALLOWED_DOMAINS)
            if not controls:
                print("  no actionable controls found; stopping")
                break

            d = decide(client, budget, goal, controls, history,
                       describe_playback(state))
            print(f"\n  step {step}: {stats['kept']} controls offered "
                  f"(found {stats['found']}, {stats['after_dedupe']} after dedupe, "
                  f"{stats['dropped_for_cap']} dropped for the cap, "
                  f"{stats['ms']} ms)")
            print(f"     picked {d.target_key!r} p={d.p_target:.2f} "
                  f"present={d.target_present:.2f} done={d.task_complete:.2f} "
                  f"({d.ms:.0f} ms)  -> "
                  f"{d.target.describe() if d.target else 'none'}")

            record = {"step": step, "stats": stats, "decision": d.as_dict(),
                      "playback_before": state}

            # Completion is checked before the none-selection branch. When the
            # goal is already met both are true at once - nothing is worth
            # clicking precisely because the job is done - and reporting that as
            # "declined, nothing selected" describes a stall rather than a
            # success. On the YouTube run it logged exactly that at done=0.64.
            if d.task_complete >= 0.5 and step > 1:
                print(f"     stopping: task_complete {d.task_complete:.2f}")
                record["outcome"] = "stopped_complete"
                steps.append(record)
                break
            if d.target is None or d.target_key == NONE_KEY:
                print("     declined: nothing selected")
                record["outcome"] = "declined_none"
                steps.append(record)
                break

            may, tier_name, why = risk.allows(d.target.label, d.p_target,
                                              d.target_present)
            record["tier"] = tier_name
            if not may:
                print(f"     declined [{tier_name}]: {why}")
                record["outcome"] = f"blocked_{tier_name}"
                steps.append(record)
                break

            value = d.text_to_type if d.target.kind == "type" else None
            if d.target.kind == "type" and not value:
                value = args.query   # code owns the string; the model picked the field
            try:
                plan = act(driver, d.target, ALLOWED_DOMAINS, value=value,
                           submit=(d.target.kind == "type"),
                           dry_run=args.dry_run)
            except DomainNotAllowed as exc:
                print(f"     BLOCKED by domain allowlist: {exc}")
                record["outcome"] = "blocked_domain"
                steps.append(record)
                break

            verb = "would " if args.dry_run else ""
            print(f"     {verb}{plan['action']} {d.target.label!r}"
                  + (f" <- {value!r}" if value else ""))
            record["outcome"] = "acted"
            record["plan"] = plan
            steps.append(record)

            if args.dry_run:
                break
            history.append(f"{plan['action']} on {d.target.label!r}")
            time.sleep(0.7)

        state = playback_state(driver)
        print("\n" + "=" * 68)
        print(f"  final url   : {state.get('url')}")
        print(f"  page title  : {state.get('title')}")
        print(f"  video found : {state.get('found')}")
        if state.get("found"):
            print(f"  paused      : {state.get('paused')}")
            print(f"  position    : {state.get('currentTime')}s / "
                  f"{state.get('duration')}s")
        print(f"  RESULT      : {'PLAYING' if is_playing(state) else 'not playing'}")
        print(f"  budget      : {json.dumps(budget.summary())}")
        print("=" * 68)

    except BudgetExceeded as exc:
        print(f"\n!! stopped: {exc}")
    finally:
        budget.flush()
        RESULTS.write_text(json.dumps(
            {"goal": goal, "query": args.query, "dry_run": args.dry_run,
             "allowed_domains": ALLOWED_DOMAINS, "steps": steps,
             "final": state, "playing": is_playing(state),
             "budget": budget.summary()}, indent=2), encoding="utf-8")
        print(f"  wrote {RESULTS.name}")
        if not args.keep_open:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
