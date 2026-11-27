# App Server control

Verified against Codex CLI 0.167.2 with App Server daemon 0.172.0 on 2026-11-27.
Protocol is experimental; keep CLI and daemon versions compatible. Do not generate
schemas on every call. If an upgrade breaks the client, generate the schema with the
daemon's own binary (it can be newer than the `codex` on PATH; `managedCodexPath` in
`codex app-server daemon version` names it) and update only the affected calls:

```bash
"$MANAGED_CODEX" app-server generate-json-schema --experimental --out /temporary/directory
```

Official protocol: https://learn.chatgpt.com/docs/app-server

## Connection and lifetime

The default endpoint is
`${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock`. If that
file is absent and `--sock` is not given, the client asks
`codex app-server daemon version` for its `socketPath` before failing.
`codex app-server daemon start` starts the shared service if needed. Do not restart,
update or stop a shared service while unrelated work is running.

The client implements the WebSocket protocol itself with the standard library: an
HTTP Upgrade handshake over the Unix socket (no public TCP port), then masked RFC
6455 text frames carrying JSON-RPC. It answers pings and reassembles fragmented
messages. `codex app-server proxy` is a raw byte proxy; it is NOT a JSONL RPC adapter.
Do not send newline-delimited JSON directly to that socket.

Override the owning endpoint with the global option before the subcommand:

```bash
python3 "$S" --sock /absolute/other.sock status THREAD_ID
```

The shared daemon retains active work after this short-lived client disconnects.
Each invocation initializes its own connection; `follow-up` resumes the thread if
needed. `status` and `wait` read without resuming. Local storage visibility does not
imply control of a desktop or independent server's active thread. `notLoaded` means
not loaded in THIS server, not proof it is idle everywhere. Do not resume a known
externally active thread on a different server; connect to its owner instead.

## Commands and behavior

- `models`: paginates `model/list`, prints IDs, default/supported efforts.
- `start`: `thread/start`, optional `thread/name/set`, then `turn/start`. Requires
  `--cwd`, either `--kind` or `--model`, and either `--message` or `--task-file`
  (`-` reads stdin). Optional `--effort`, `--name`, `--sandbox`. Local inputs (cwd,
  UTF-8 task text) are validated before connecting.
- `follow-up ID`: checks idle state, `thread/resume`, validates effective model/effort,
  then `turn/start`. Accepts task input and optional model/effort overrides. It does
  not queue work. Serialize writes to the same thread; an external concurrent sender
  can race the idle check because `turn/start` itself can steer an active turn.
- `steer ID`: finds current active turn and calls `turn/steer` with `expectedTurnId`.
  Optional `--turn-id` adds the caller's expected-turn check. Requires task input.
- `interrupt ID`: verifies active turn, then `turn/interrupt`. Optional `--turn-id`.
  Acceptance is not completion; use `wait` to observe the interrupted state.
- `status ID`: metadata/runtime state and five recent turn summaries.
- `wait ID --turn-id TURN --seconds 30`: polls metadata and turn summaries every
  two seconds, reads at most 100 recent items of the completed turn for assistant
  output. `olderItemsCursor` indicates additional older items. Pin the turn ID to
  avoid accidentally waiting for someone else's subsequent turn. Timeout is 0–60 s.

`--effort` accepts any value the server catalog advertises for the model; other
values are rejected before any mutation. An option value that starts with `-` must
use the `--message=-text` form.

Standard output is one JSON result. Standard error contains structured errors and,
for `start`, a `thread_created` recovery record before dispatch. Capture both when
running unattended. Save that recovery ID if naming or turn creation fails.

Exit codes: 0 successful request/completed turn; 1 usage/validation/protocol/transport
error; 2 wait timed out; 3 targeted turn failed or was interrupted; 4 needs attention;
130 client interrupted. Global `--rpc-timeout` defaults to 30 seconds per RPC; the wait
duration controls polling, not a strict total bound including network delays. Ctrl-C
only ends the client; use `interrupt` explicitly to cancel remote work.

New tasks use `approvalPolicy=never` and default `sandbox=read-only`. Implementation
may explicitly select `workspace-write`. The client does not auto-approve server
requests or fabricate answers to interactive questions: it replies with a JSON-RPC
error and exits 1. Existing sessions retain their policies; work that needs
interactive handling must be handled by an appropriate client. The script does not
expose a bypass-sandbox switch.

## Protocol features not wrapped

The 0.172.0 protocol also offers, among others, `thread/queue/*` (queued input),
`review/start` (native review), `thread/fork`, `thread/compact/start`,
`thread/goal/*`, `thread/search*`, named `permissions` profiles, turn `outputSchema`,
and image input. This client does not expose them; do not describe them as available
through it. Extend the client deliberately, from the generated schema, when a task
needs one.

## Recovery

- Missing `codex` CLI: stop and ask the user to install Codex CLI (`npm install -g @openai/codex`
  or `brew install --cask codex`), sign in, and start the daemon (`codex app-server daemon start`).
  Do not install global packages without user approval.
- Missing socket: start the daemon or select the correct existing server socket.
- Permission denied / `Operation not permitted` on the socket: if the host agent runs
  shell commands inside a sandbox, allow access to `$CODEX_HOME` (default `~/.codex`)
  or run the command outside the sandbox.
- Unsupported method (JSON-RPC -32601): the error names the method; compare CLI and
  daemon versions instead of retrying the task.
- Unsupported model/effort: inspect `models`; do not silently substitute the user's
  explicit choice. Access errors can still happen during inference.
- Send timeout/disconnect: the mutation may have been accepted. Check status and
  local recent user messages before retrying; the client intentionally has no automatic
  mutation retry.
- Stale turn: re-inspect current state. Never automatically change steer into a new turn.
- History visibility without runtime ownership: find the owning server. Do not use a
  second server to compete with an already active session.
