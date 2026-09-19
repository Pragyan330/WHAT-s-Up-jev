# Phase 1: initial testing

Jev against a local LLM, and what a typed-judgement model is actually for.

First pass at putting Jev (TypeSafe's System One model, `jev-1.13.0`) against
Qwen3.5-4B and Qwen3.5-2B running locally on llama.cpp, on the two decisions a
voice assistant makes before it says anything:

1. Which tool should run, with which arguments?
2. Does this turn need a memory lookup at all?

Both were measured on one machine, an RTX 5050 Laptop with 8 GB, with the local
models loaded one at a time. Raw per-request results are in `experiments/`.

These are initial results. The sample sizes are small by design: phase 1 was
about finding where the differences are, so a later phase can measure the ones
that matter properly. The open questions at the end are the point of the
exercise.

Short version so far. Jev does not win on speed, and it barely wins on
accuracy. On this evidence it earns its place because it is the only one of the
three that tells you when it is unsure, which looks like what decides whether
an automatic action is safe to take. Whether that holds at scale is a phase 2
question.

## The premise

An LLM doing tool calling pays full generation price to decide two things:
which function to call, and what its arguments are. Both are typed judgements
over the user's sentence. Neither needs prose.

Jev is a System One model. It returns typed judgements and calibrated
probabilities and generates no text at all. So the question is whether that
step can move off the LLM, leaving the LLM to write the reply and carry the
conversation.

## Initial finding 1: on tool calling, all three are level

Fifteen requests, five tools, `temperature=0`.

| | Jev | Qwen3.5-4B | Qwen3.5-2B |
| --- | --- | --- | --- |
| Right tool | 15/15 | 15/15 | 14/15 |
| Right tool and arguments | 14/15 | 15/15 | 14/15 |
| Median latency | 541 ms | 633 ms | 406 ms |
| Free-text / date args filled | 0/5 | 3/5 | 3/5 |
| VRAM | none | 4.2 GB | 2.4 GB |

Each failed at most once, and on a different request:

```
jev-latest   who won the world cup in 1998   search_web(topic="who")
qwen3.5-2b   tell me a joke                  play_music(artist_or_track="joke")
qwen3.5-4b   -
```

A 2B model running locally decides faster than a network round trip to a hosted
service, and a 4B got every call exactly right. We did not expect either. If
the comparison ended at the accuracy column there would be no reason to send
this decision off the machine.

### The measurement error that nearly buried this

Our first run had the 4B at 2406 ms and the 2B at 1052 ms, and the memory
results were worse: the 2B scored 17/30, barely above answering "no" every
time.

That was our harness. Qwen3.5 is a reasoning model, and left in thinking mode
it deliberates for thousands of tokens over a binary question. At a 3072-token
cap the 2B was still arguing with itself (`"Wait, I need to check if the
instruction implies I should output the decision I made about..."`) and never
emitted a verdict. Setting `enable_thinking: false` answered the same question
in 20 tokens.

| Qwen3.5-2B | thinking on | thinking off |
| --- | --- | --- |
| Memory decision | 17/30, 6733 ms | 25/30, 199 ms |
| Tool calling | 14/15, 1052 ms | 14/15, 406 ms |

A reasoning model is the wrong configuration for a gate that runs before every
turn. If you benchmark one without checking this, you will measure your own
configuration and publish it as a finding about the model.

## Initial finding 2: on the memory decision, Jev is clean

A retrieval layer that fires on every turn spends context on turns that cannot
use it. "What is the capital of Australia" does not get better with three lines
of the user's old notes stapled to it. It gets longer, and a small model's
answer degrades as the window fills with text the question never needed.

Can a cheap judgement decide, before retrieving, that this turn has nothing to
retrieve for?

Thirty held-out turns: general knowledge, personal history, live lookups, and a
deliberately awkward middle.

| | Jev | Qwen3.5-4B | Qwen3.5-2B |
| --- | --- | --- | --- |
| Correct | 30/30 | 28/30 | 25/30 |
| False positives (wasted lookup) | 0 | 0 | 0 |
| False negatives (lost answer) | 0 | 2 | 5 |
| Median latency | 365 ms | 280 ms | 199 ms |

All three had zero false positives. None of them wastes context on general
knowledge, which answers the original question: a cheap gate works, and it does
not have to be Jev.

The difference is lost answers. The 2B silently declined to look up "when is my
dentist appointment" and "what's my favourite programming language", both
unanswerable without the store. Five of fourteen personal questions came back
with no memory at all.

### The margin is not delicate

Jev's probabilities separated almost completely:

| Turn class | mean `p_memory` |
| --- | --- |
| Self-contained (knowledge, reasoning, arithmetic) | 0.013 |
| Personal / past-context | 0.94 |

Any threshold between 0.1 and 0.8 gives identical behaviour on all thirty
cases. It also kept live distinct from personal: "what's the weather right now"
scored `memory 0.14 / fresh 0.99`, so live lookups never get misrouted into a
memory store that could not answer them.

On this set the gate skipped retrieval on 16 of 30 turns with no lost answers.

## Initial finding 3: the thing that actually separates them

Jev returns a probability with every judgement. The local models return an
answer and nothing else.

Sorted by the confidence Jev reported on the tool-calling set, the one wrong
call sits below every correct call except two:

```
wrong calls:   0.41
correct calls: 0.16  0.36  0.73  0.77  0.83  0.87  0.88  0.90  0.98  1.00 ×5
```

Gate on it and the error disappears:

```
threshold 0.5 →  12 correct auto-executed,  0 wrong,  3 held for review
```

Every incorrect call is caught. The cost is two correct calls held back, and
both deserved it. `shuffle my playlist` came back at 0.16 having chosen
`artist_or_track="playlist"`; `look up the population of Iceland` at 0.36 with
`topic="Iceland"`, dropping "population". Our grading script accepted both. The
confidence number was stricter than the grader, and closer to right.

Neither local model offers this. A wrong call from them looks exactly like a
right one, so the only way to catch `play_music(artist_or_track="joke")` is to
execute it and hear music start. For any workflow where a bad call sends a
message, spends money, or changes a file, that is where the difference lies,
and it appears nowhere in the accuracy column.

## What Jev cannot do

It cannot generate text. That is what a System One model is. Of 29 tools in the
integration, 18 arguments are enums or booleans and Jev decides them. Thirty
are free strings and it cannot.

The workaround from TypeSafe's own cookbooks is to select instead of generate:
find candidate spans in code with a recall-tuned regex, then have Jev pick one
with a Choice whose options are those spans. The returned value is a verbatim
copy of the user's words, and a no-match option lets "the request does not say"
come back as an answer.

That works, and it moves the ceiling onto candidate quality:

| | subject extracted | confidence |
| --- | --- | --- |
| single-word regex | `"call"` | 0.56 |
| phrase regex | `"call the dentist on"` | 0.84 |
| phrase + stopword trim | `"call the dentist"` | 1.00 |

Same model, same request, three different answers. The candidate list was the
bottleneck.

Numeric and date arguments get no question at all. `jev-1.13` is documented as
reading dates as text rather than ordered quantities, and as not counting
reliably. We confirmed both: asked whether the sun would fit inside Jupiter it
returned 0.43, a shrug where the answer is a confident no. Size comparison
needs counting, value proximity, and "a property of a property", all three of
which are on the known-issues list. A wrong date silently filled is worse than
an absent one, so these keep their defaults and the output says so.

## Three prompt-design lessons that moved the numbers

None of these were model quality. All three showed up as a low confidence
number before they showed up as a wrong answer.

Criteria must describe something decidable from the state. A `search_web` tool
first described as "information the assistant does not already hold" caused
factual questions to route to no tool at all. That description asks the model
about its own knowledge, which is not in the state. Rewriting it around the
shape of the request, "the user wants to know something, rather than have an
action performed", made it fire.

A span that is not a candidate cannot be chosen. See the table above.

Ask independent questions together. They run in parallel and cannot see each
other's answers, so each speculative question must state its own premise
("Assume the user is asking to play music..."). One round trip instead of two.

## What phase 1 shipped

Phase 1 ends with a working integration, on the principle that the next useful
measurements come from real traffic. It asks everything in a single request:
the route, every tool's enum and boolean arguments speculatively, and both
memory judgements.

```
21 questions · 1 request · ~380–430 ms median
```

```
turn the lights off                   tool=light_power       memory=SKIP  p=0.13
what did I tell you about the budget  tool=None              memory=RUN   p=0.92
what is the capital of Australia      tool=None              memory=SKIP  p=0.02
pause it                              tool=music_transport   memory=SKIP  p=0.04
```

Routing on the real 29-tool set: 15/15 on a 15-utterance smoke test, median
431 ms. Bare control words resolve correctly because the live situation rides
along as prose in the state. That set is a sanity check rather than a score.

Two properties worth copying if you build something similar. Confidence is the
weakest judgement in the call rather than the product of all of them, so a
confident route with a shaky argument is still a bad call; report the minimum
and name which judgement it came from. And every failure path degrades to the
old behaviour: with no key, no network, a timeout, or an unparseable answer,
the tool decision becomes "no action" and the memory gate defaults to retrieve.
A routing layer that throws takes the whole loop down with it.

## What phase 1 does not settle: the phase 2 agenda

Everything here is provisional. These are the questions a second phase has to
answer before any of it becomes a recommendation.

- Small samples. Fifteen tool requests and thirty memory turns, enough to see
  that the failure modes differ and nowhere near enough to separate 14/15 from
  15/15. Every accuracy gap in this document is within noise. Phase 2 needs a
  held-out set an order of magnitude larger, and utterances nobody on the
  project wrote.
- No cost comparison. TypeSafe does not publish pricing. The local models cost
  electricity and VRAM; Jev costs an unknown amount per request. The "cheaper"
  half of the original premise is untested, and it is the biggest gap in phase 1.
- The speculative design pays for what it discards. All tools' arguments are
  priced on every request and only one tool's answers are used. That buys a
  single round trip. Fine at five tools, fine at 29; at several hundred it would
  need a cheap route first, then arguments for the winner.
- Latency is single-request on an idle GPU. No concurrency, no contention, one
  machine, one network connection. Jev's mean (1185 ms) is more than double its
  median because of network outliers, one request having taken 6.7 s. A local
  model never fails because the connection did, and for an always-on assistant
  a predictable 1 s can beat an unpredictable 0.5 s.
- Thresholds are ours. Typed output guarantees the interface, not the truth. The
  0.5 gate that caught every error here was chosen after seeing the results,
  which is exactly the way to fool yourself. It needs setting on one set and
  testing on another.
- No answer-quality measurement. We showed the memory gate decides correctly.
  We did not show that withholding irrelevant context makes the final answer
  better, which is the premise behind gating it. That needs an end-to-end
  evaluation phase 1 does not attempt.

## Where phase 1 leaves us

If you are choosing purely on whether the right tool fires, a small local model
is competitive, cheaper to run, faster, and never goes down with your
connection. That is a real result.

The case for a System One model, on this evidence, is that it tells you how
sure the decision was. That matters when a wrong call costs more than a held
one, and something downstream has to decide whether to act, ask, or escalate.
On our set that number caught every error we made, including two the grader
missed.

The judgement is roughly as good as a small LLM's. Knowing when not to trust it
is the part phase 2 has to confirm or break.

Phase 1 was enough to justify wiring it into a real assistant loop and running
it. That integration is live, and what it does on real traffic rather than on
authored test sets is the next thing worth measuring.
