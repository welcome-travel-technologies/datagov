# Problematic chat questions — regression fixture

`problematic_chat_questions.json` is a frozen list of **real production questions
that historically made the AI assistant time out, loop on tool calls, or error**.
Use it to regression-test agent changes (prompt edits, tool changes, model swaps)
so the loop/timeout failures we fixed don't silently come back.

## How it was built

Mined from production `ChatMessage.debug_meta`: every assistant turn whose
recorded stats had `error_count > 0` **or** `tool_call_count >= 15`, deduped by
question, with disambiguation-flow / trivial turns (`"1"`, `"hey"`,
`"Selected: …"`) excluded because they don't reproduce standalone.

Each entry carries:

| field | meaning |
|---|---|
| `question` | the user message to replay |
| `pattern` | failure class (see below) |
| `note` | why it was problematic + the expected good behaviour |
| `original` | stats from the original production run (`tool_calls`, `errors`, `duration_ms`, `session_id`) |
| `baseline_replay` | result of the before-fix replay, when available |

### Failure patterns

- **`where_used_graph_walk`** — "where is X used? / which reports use Y?" The
  agent used to walk `get_lineage` hop-by-hop (20–73 calls → 180s timeout). Now
  it should call `where_is_used` once.
- **`multi_metric_fanout`** — "give me the definitions of A, B, C…": one schema
  call per metric. Definitions are front-loaded, so this should need ~no calls.
- **`measure_disambiguation`** — a bare measure name that resolves to several
  candidates; the agent used to re-call the schema tool in a loop. It should
  disambiguate once and stop.
- **`tool_error`** — produced tool errors (bad DAX / ids); should report the
  error, not blindly retry.
- **`high_loop_other`** — high tool-call count for some other reason; should
  finish within the tool-call cap.

## How to replay

The `replay_chat` management command can replay the fixture directly
(standalone, read-only — it never writes chat rows). Run it with the working
tree mounted; point `DEBUG` at the catalog you want as context.

```bash
# from repo root; web container has the source mounted at /app
docker compose run --rm -v "$PWD/backend/app:/app" -v "$PWD/_out:/out" web \
  python manage.py replay_chat \
    --questions-file catalog/tests/fixtures/problematic_chat_questions.json \
    --model google:gemini-3.5-flash --max 74 --out /out
```

Outputs per run: wall-clock, loops (model round-trips), tool-call sequence +
counts, tool errors, whether it hit the timeout or the `budget_capped` path,
plus a `replay_summary.md` with category counts. A healthy result keeps
`tool_calls` under the 20-call cap with **no `timeout` rows**.

## What "fixed" looks like

The agent now enforces `UsageLimits(tool_calls_limit=20, request_limit=30)` and,
when the cap is hit, returns a best-effort partial answer instead of timing out.
The `where_used_graph_walk` questions should resolve in **1** `where_is_used`
call instead of dozens of `get_lineage` calls.
