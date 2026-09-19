# Setup findings

Recorded while preparing the repo, before any experiment. Everything below was
checked against the live docs and the installed SDK, not from memory.

## Noul vs boolean: there is no drift

Worth pinning down early: the Vercel AI SDK provider documents `type: 'boolean'`
while the skill calls that primitive **Noul**. These are two different layers,
both current:

- **Wire / HTTP API** (`POST https://api.typesafe.ai/v1/systemone`) uses
  `"noul"`. Confirmed in `docs.typesafe.ai/api.md`.
- **Vercel provider** (`@ai-sdk/typesafe-ai`) exposes `'boolean'` at its own
  surface and translates it on the way out and back.

From `@ai-sdk/typesafe-ai@3.0.4`, `src/typesafe-ai-evaluation-model.ts`:

```ts
readonly supportedQuestionTypes = ['choice', 'score', 'boolean'] as const;
// request:
question.type === 'boolean' ? { ...question, type: 'noul' } : question
// response:
case 'noul': return [id, { type: 'boolean', probability: answer.noul }];
```

So neither source is stale. Write `noul` for the HTTP API and both Typesafe
SDKs; write `boolean` only through the Vercel provider.

That provider is genuine, despite not being listed on `docs.typesafe.ai`:
published by `vercel-release-bot` from `github.com/vercel/ai`
(`packages/typesafe-ai`), Apache-2.0, with SLSA provenance attestation.

The same file drops `confidence` for noul answers
(`answer.type !== 'noul' && answer.confidence != null`), which matches the
skill's rule that a Noul carries a probability and no separate confidence.

## Two names that are easy to get wrong

1. **Env var is `TYPESAFE_API_KEY`**, not `TYPESAFE_AI_API_KEY`. From
   `typesafe_sdk/constants.py`: `API_KEY_ENV = "TYPESAFE_API_KEY"`.
2. **Python package is `typesafe-sdk`, imported as `typesafe_sdk`.** The older
   name `typesafe-client` was renamed in v1 (see `migrating-to-v1.md`), along
   with `client.evaluate(...)` -> `client.system_one(...)`.

## Verified shapes (installed `typesafe-sdk==0.7.0`)

Client and call:

```python
TypeSafeClient(*, api_key=None, model=None, retry=None, timeout=None,
               headers=None, transport=None, http_client=None, base_url=None)

client.system_one(state, questions, *, model=None, retry=None, timeout=None,
                  extra_headers=None, extra_body=None, response_model=None)
```

Defaults: `base_url=https://api.typesafe.ai`, `model=jev-latest`, `timeout=10.0`.

Question objects — `Noul`, `Choice`, `Score`:

| Primitive | `criteria` shape |
| --- | --- |
| `Noul` | optional `{"true": ..., "false": ...}`; may be omitted entirely |
| `Choice` | **required** mapping of label -> description (or `None`) |
| `Score` | **required** nonempty ordered sequence, one entry per level from zero |

`instructions` is optional on all three and accepts text, an object, or an array.
The SDK rejects a `Choice`/`Score` with no `criteria`, and a `Score` with an
empty one, before the request leaves the process.

Answer fields, which differ per primitive:

| Answer | Fields |
| --- | --- |
| `NoulAnswer` | `type`, `noul` — **no `confidence`** |
| `ChoiceAnswer` | `type`, `choice`, `confidence`, `probabilities` |
| `ScoreAnswer` | `type`, `score`, `confidence`, `legend`, `probabilities` |

`SystemOneResponse`: `model`, `usage`, `answers`. `usage` is
`input_tokens` / `output_tokens` (the v1 rename from `billing_units`).

## Still open

- **Pricing** is not published in the docs. Needs a look at the dashboard.
- **Test workload** is undecided.
- `docs.typesafe.ai/model-jaggedness/jev-1.13.md` lists known rough edges for
  the current model. Worth reading before interpreting any experiment result.
