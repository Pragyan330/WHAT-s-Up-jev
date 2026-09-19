# Experiment 01: tool calling

## The premise

When an LLM does tool calling, it pays full generation price to decide two
things: which function to call, and what its arguments are. Both are typed
judgements over the user's request. Neither needs prose.

Jev is a System One model: it returns typed judgements and probabilities and
generates no text at all. So the question this experiment asks is whether the
tool-selection step can move off the LLM entirely, leaving the LLM to do what
only it can do — write the reply, carry the conversation, handle whatever comes
back from the tool.

Run it:

```bash
.venv/Scripts/python.exe manual_test/tool_caller.py
```

```
YOU: text Sarah right now, it's an emergency
JEV: {
  "tool": "send_message",
  "arguments": {
    "recipient": "Sarah",
    "urgency": "urgent"
  },
  "confidence": 1.0,
  "weakest_link": "tool"
}
     [9 questions, 1 request, 1213 in / 542 out]
```

## The design, and the parts that are not obvious

Five toy tools: `get_weather`, `send_message`, `play_music`, `create_reminder`,
`search_web`. One request per user turn. Four decisions did most of the work.

**Routing and arguments go out together, speculatively.** A single Choice picks
the tool. Every tool's argument questions ride in the same request, including
the four tools that will turn out to be wrong. They run in parallel and cannot
see the routing answer, so each argument question states its own premise —
"Assume the user is asking to play music. Which artist, song, or genre should be
played?" — and code reads only the chosen tool's answers. Nine questions, one
round trip. Waiting for the route before asking about arguments would double the
latency to save tokens that are cheap by comparison.

**Free-text arguments are selected, never generated.** Jev cannot produce a
string that is not already an option. So `location` and `recipient` are not
generated: a recall-tuned regex in code over-finds candidate spans in the
request, and Jev picks one with a Choice whose options *are* those spans. The
returned value is a verbatim copy of the user's text. Every such question
carries a no-match option, so "the request does not say" is a real answer rather
than a forced guess.

**Numeric and date arguments get no question at all.** `days` and `when` keep
their defaults and the JSON says so out loud. This is deliberate: jev-1.13 is
documented as reading dates as text rather than ordered quantities, and as not
counting reliably. A wrong date silently filled is worse than an absent one.

**Confidence is the weakest judgement, not the product.** A call with a
confident route and a shaky argument is a bad call. Reporting the minimum, and
naming which judgement it came from, makes the output say where it is weak:

```json
"confidence": 0.73,
"weakest_link": "units"
```

## Results

Ten requests, after the fixes described below. Correct means the call is one a
person would accept without editing.

| Request | Tool | Arguments | Confidence |
| --- | --- | --- | --- |
| what's the weather in Tokyo tomorrow | `get_weather` | `Tokyo`, `celsius` | 0.73 |
| whats the weather in San Francisco | `get_weather` | `San Francisco`, `fahrenheit` | 0.73 |
| text Sarah right now, it's an emergency | `send_message` | `Sarah`, `urgent` | 1.00 |
| email Priya about the budget review, it can wait | `send_message` | `Priya`, `normal` | 0.82 |
| play some jazz, shuffled | `play_music` | `jazz`, `shuffle: true` | 0.98 |
| put on some Miles Davis | `play_music` | `some Miles Davis`, `shuffle: false` | 0.85 |
| remind me to call the dentist on friday | `create_reminder` | `call the dentist` | 1.00 |
| who won the world cup in 1998 | `search_web` | `who` | 0.46 |
| write me a poem about the ocean | none | — | 1.00 |

The units argument is worth a look. Nothing in "weather in Tokyo" mentions
temperature units, and Jev inferred `celsius` for Tokyo and `fahrenheit` for San
Francisco — both right, both at 0.73, and both correctly flagged as the weakest
judgement in the call. That is the shape you want: a defensible guess that says
it is a guess.

The world cup row is the honest failure. `who` is a useless search topic, and
the call reports 0.46. Nothing in the pipeline is broken — the correct span,
something like "world cup 1998", was never a candidate, so it could not be
chosen. Low confidence caught it. Acting on a call below threshold would have
sent a bad query; that is what the threshold is for.

## Three things that changed the results

All three were prompt and candidate design, not model quality. Worth knowing
before concluding anything about the model.

**Criteria must describe something decidable from the state.** `search_web` was
first described as "search the internet for information the assistant does not
already hold". Factual questions then routed to *no tool at all*, at 0.73. The
description asked Jev about the assistant's knowledge, which is not in the state
and not knowable. Rewriting it around the shape of the request — "the user wants
to know something, rather than have an action performed" — made the tool fire.

**A span that is not a candidate cannot be chosen.** "remind me to call the
dentist on friday" first returned subject `call`, at 0.56. The regex only
captured single words after a preposition, so `call the dentist` was never on
the list. Allowing a short lowercase continuation moved the same request to
`call the dentist` at 1.00. The model was never the bottleneck; the candidate
list was.

**Trim the spans.** The wider regex then produced `call the dentist on`, a
trailing preposition dragged in by greedy matching. Stripping leading and
trailing stopwords in code fixed it. Candidate quality is ordinary string work,
and it sets the ceiling on everything downstream.

Each of these showed up as a low confidence number before it showed up as a
wrong answer, which was the useful part.

## Cost and latency

Measured over five requests, nine questions each, from a consumer connection:

```
n=5  median 390 ms  min 365  max 996
mean tokens: 1201 in / 521 out
```

The 996 ms is the first call, including connection setup. Steady state is
roughly 370–400 ms for the full route-plus-arguments decision.

Two caveats that matter more than the numbers. TypeSafe does not publish
pricing, so **the "cheaper" half of the premise is untested** — it needs the
dashboard. And we did not measure an LLM doing the same tool call on the same
requests, so there is no baseline here; the latency figure stands alone and is
not yet a comparison.

Note that the speculative design pays for it: all five tools' arguments are
priced on every request, and only one tool's answers get used. That trade buys a
single round trip, and it is a trade, not a free win. With five tools it is
clearly worth it. With fifty it would need rethinking — a cheap route first,
then arguments for the winner.

## What this does not settle

- **No cost comparison.** Pricing is unpublished and no LLM baseline was run.
- **Five toy tools, ten requests.** Enough to show the shape works, nowhere near
  enough to claim an accuracy number.
- **No threshold yet.** 0.46 was visibly bad and 1.00 was visibly good, but
  where the cutoff belongs is a question for real traffic and real consequences.
- **Multi-tool requests are unhandled.** "text Sarah and set a reminder" gets one
  Choice and therefore one tool. A per-tool Noul would suit that better than a
  Choice, and was not tried.
