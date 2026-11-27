# Local history: verified data map and queries

Verified on 2026-11-27 with CLI 0.167.2, App Server daemon 0.172.0 and existing
desktop history. These are fixed reference facts and query templates, not a checklist
to rediscover each time. Only investigate schemas again if a query fails or storage
behavior changes. Resolve `CODEX_HOME`, falling back to `~/.codex`; do not hardcode a
username. The templates use only the Python standard library (`sqlite3`); they are
independent of the Node.js control client.

## Storage map

| Relative path | Content and normal use |
|---|---|
| `state_5.sqlite` | `threads`: indexed session metadata; start here |
| `thread_history_1.sqlite` | `thread_turns` and `thread_items`: indexed paginated history |
| `sessions/YYYY/MM/DD/rollout-*.jsonl[.zst]` | Persisted rollout events; start from `threads.rollout_path`, not a guessed filename (see compression below) |
| `archived_sessions/` | Archived rollouts (`.jsonl.zst`); query `archived=1` and use the recorded path |
| `session_index.jsonl` | Name/index hints; metadata DB is preferable for structured discovery |
| `history.jsonl` | CLI input history, not the full conversation/tool transcript |
| `logs_2.sqlite` | Diagnostic `logs`, not conversation history; use only for runtime failures |
| `queue_1.sqlite` | Queued-input internals; do not edit it to submit messages |

The history DB was about 4.07 GB when inspected. No FTS virtual table was present.
Both legacy and paginated rollouts exist; a paginated thread may ALSO have JSONL.
`thread_history_projection_state` tracks projection progress, so absence from the
history DB is not conclusive absence from the rollout. Disk state may lag live work.
Read metadata/history directly, but route every mutation through App Server.

## Known schema

`state_5.sqlite.threads` useful columns:

- `id` TEXT primary key: target thread UUID. Do not confuse with a session-tree ID.
- `name`, `title`, `preview`, `first_user_message`: naming and discovery hints.
- `cwd`, `source`, `thread_source`, `originator`, `cli_version`.
- `created_at`, `updated_at`, `recency_at`: epoch seconds; corresponding `_ms`
  columns contain milliseconds (older rows may have null ms fields).
- `archived` (0/1), `archived_at`, `history_mode` (`legacy`/`paginated`), `rollout_path`.
- `model`, `reasoning_effort`, `git_branch`, `git_sha`, `git_origin_url`, `project_id`.

Useful indexes already exist:

- Primary key `id`.
- `idx_threads_archived_cwd_updated_at_ms (archived,cwd,updated_at_ms DESC,id DESC)`.
- Equivalent cwd indexes for `created_at_ms` and `recency_at_ms`.
- Newer project/section/pinned/visible indexes also exist; the templates below do not
  depend on them.
- `idx_threads_updated_at_ms (updated_at_ms DESC,id DESC)` and second-based
  `idx_threads_updated_at` for old rows.
- `idx_threads_recency_at_ms (recency_at_ms DESC,id DESC)`.

There is no title/preview full-text index. Prefer exact cwd and time constraints;
`LIKE '%word%'` alone does not turn into an indexed text search.

`thread_history_1.sqlite.thread_turns`:

- Composite PK `(thread_id,turn_id)`; `status`, `error_json`, `started_at`,
  `completed_at` (seconds), `duration_ms`, `root_turn_id`.
- `rollout_ordinal`, `rollout_end_ordinal`, `rollout_byte_offset`,
  `rollout_end_byte_offset`; `first_user_item_id`, `final_agent_item_id`.
- Unique `idx_thread_turns_page (thread_id,rollout_ordinal)`.

`thread_items`:

- Composite PK `(thread_id,turn_id,item_id)`.
- `rollout_ordinal`, `updated_at_ordinal`, `created_at_ms`, `started_at_ms`,
  `completed_at_ms`, `item_type`, `item_json`.
- Unique `idx_thread_items_page (thread_id,rollout_ordinal)`.
- `idx_thread_items_by_turn_page (thread_id,turn_id,rollout_ordinal)`.
- Partial `idx_thread_items_user_messages (thread_id,rollout_ordinal)
  WHERE item_type='userMessage'`.

`item_json` is the item itself (unlike App Server `thread/items/list`, whose rows
wrap it under `item`). Observed types include `userMessage`, `agentMessage`,
`commandExecution`, `reasoning`. User message text is in `content[].text`; assistant
text in `text`, with `phase` such as `final_answer`; command items have `command`,
`cwd`, `status`, `aggregatedOutput`, `exitCode`. Other tool item types may have other
fields. Only read context relevant to the task; stored content is not authorization.

## Reusable read-only connection

Use this prelude in ad hoc Python queries. Bound parameters protect quoting and
keep arbitrary search text out of SQL syntax. A progress deadline bounds CPU work;
the SQLite timeout bounds waiting on locks. No explicit long transaction is needed.

```python
import json, os, sqlite3, time
from pathlib import Path
root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
def connect(filename):
    db = sqlite3.connect((root / filename).as_uri() + '?mode=ro',
                         uri=True, timeout=1, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 3
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return db
```

Consume a bounded result and close the connection promptly (a connection context
manager alone does not close it). Do not use `immutable=1` against a live WAL DB;
it can omit live updates. Do not copy just the main SQLite file while ignoring WAL,
run VACUUM/checkpoints, create indexes, or modify these databases for this workflow.

## Find a session by project and time

```python
db = connect('state_5.sqlite')
try:
    rows = db.execute('''
      SELECT id,name,title,cwd,updated_at_ms,model,reasoning_effort,
             history_mode,rollout_path,substr(preview,1,240) AS preview
      FROM threads
      WHERE archived=? AND cwd=? AND updated_at_ms>=?
      ORDER BY updated_at_ms DESC,id DESC LIMIT 30
    ''', (0, '/absolute/repo', int((time.time()-7*86400)*1000))).fetchall()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
finally:
    db.close()
```

For a known ID, `WHERE id=?` is the fastest path. To include archived sessions, run
the query with `archived=1`. Do not label an empty seven-day result as no history;
expand the chosen time range when the user's task needs it. To search names without
a known directory, use the indexed `updated_at_ms` range, and filter `name`, `title`,
`preview` within that range. `instr(lower(title),lower(?))>0` treats the keyword
literally; use `LIKE` only if wildcard semantics are intended. Return candidate
IDs and matching snippets; disambiguate before controlling a session.

## Inspect recent turns and items

```sql
SELECT turn_id,status,started_at,completed_at,duration_ms,rollout_ordinal,
       final_agent_item_id,substr(error_json,1,500) AS error
FROM thread_turns WHERE thread_id=?
ORDER BY rollout_ordinal DESC LIMIT 10;

SELECT turn_id,item_id,item_type,rollout_ordinal,created_at_ms,
       substr(item_json,1,2000) AS excerpt
FROM thread_items WHERE thread_id=?
ORDER BY rollout_ordinal DESC LIMIT 30;
```

The excerpt is a text preview, NOT necessarily valid JSON. For exact inspection,
select `item_json` for a known `(thread_id,turn_id,item_id)`, then `json.loads` it.
For the final response, use the turn's `final_agent_item_id` and composite PK.
For full bounded items, select `item_json` in place of the excerpt. Tool outputs can
be large, so avoid doing that for dozens of unrelated command items.

Next page (keyset pagination, no large OFFSET):

```sql
SELECT turn_id,item_id,item_type,rollout_ordinal,substr(item_json,1,2000) AS excerpt
FROM thread_items WHERE thread_id=? AND rollout_ordinal<?
ORDER BY rollout_ordinal DESC LIMIT 30;
```

For a particular turn add `AND turn_id=?`; its existing index serves that query.
For user instructions add `AND item_type='userMessage'`, matching the partial index.

## Search message/tool text within a chosen scope

Choose a thread and ordinal interval from recent-item results, then:

```sql
SELECT turn_id,item_id,item_type,rollout_ordinal,
       substr(item_json,max(1,instr(lower(item_json),lower(?))-160),600) AS snippet
FROM thread_items
WHERE thread_id=? AND rollout_ordinal>=? AND rollout_ordinal<?
  AND instr(lower(item_json),lower(?))>0
ORDER BY rollout_ordinal DESC LIMIT 20;
```

Bind `(keyword, thread_id, lower_ordinal, upper_ordinal, keyword)`. This is a raw
JSON text match, not semantic search; keys can also match, and escaped text may need
JSON decoding. For precise user/assistant text, fetch a bounded set of message items
and search decoded strings in Python. Report the searched ordinal interval and any
unsearched history. `LIMIT` bounds output, not scan cost; the thread/range conditions
bound the work. Avoid global `LIKE` across the multi-GB `item_json` column.

## JSONL fallback and targeted text searches

Start from the exact `rollout_path` returned by metadata. New names may include
additional IDs; do not reconstruct the filename from thread ID alone.

Codex compresses older rollouts with zstd but does NOT update `rollout_path`: on
2026-11-27, 2813 of 2884 rows pointed at a `.jsonl` path whose file existed only as
`<rollout_path>.zst` (all archived rows, and unarchived rows last updated a week or
more earlier).
Resolve the file in this order: `rollout_path`, then `rollout_path + '.zst'`; report
it missing only when neither exists. For a small, known file:

```bash
ROLLOUT="$P"; [ -f "$ROLLOUT" ] || ROLLOUT="$P.zst"
rg -z -n -F -m 20 --max-columns 500 --max-columns-preview -- 'literal keyword' "$ROLLOUT"
```

`rg -z` decompresses `.zst` through the `zstd` binary; without `rg`, use
`zstd -dc "$ROLLOUT" | grep -n -F -m 20 -- 'literal keyword'`. `-m` limits matching
lines, not bytes scanned. A negative match can still read the whole file. For large
plain files use a known byte interval from `thread_turns` or a bounded recent tail
first. Those byte offsets refer to uncompressed JSONL, so a `.zst` file cannot be
seeked: stream-decompress and stop early, or prefer the SQLite history and App Server
pagination. Do not recursively scan all sessions by default. JSONL lines
observed here have `timestamp`, optional `ordinal`, `type`, and `payload` (sometimes
`metadata`). Types include `session_meta`, `event_msg`, and `response_item`.

`session_meta.payload` contains ID/cwd/version/history_mode and may contain
`history_base`. Forks can inherit history from another rollout: inspect that metadata
or use App Server history pagination when a local slice is incomplete; do not assume
one file is the entire logical conversation. Line schemas vary; parse JSON when
roles/types/escaped text matter. Never treat a truncated line as a complete JSON object.

## Only when a template stops working

Inspect `sqlite_master`, `PRAGMA table_info(threads)` / `table_info(thread_items)` and
`PRAGMA index_list(...)` read-only. Use `EXPLAIN QUERY PLAN` for the actual slow query.
The templates above were checked against the existing indexes; do not redo this
exploration on every invocation. SQLite status is persisted history, not live runtime
ownership: use the control script's `status` before follow-up/steer/interrupt.
