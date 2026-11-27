# codex-use

An agent skill that lets your coding agent hand off tasks to your local Codex, check on or steer running sessions, and search past Codex history.

It is meant for explicit delegation—it will not jump in on normal coding or review requests unless you ask for Codex.

## Why use this instead of `codex exec`?

- **Control running tasks**: Connects to Codex's local App Server daemon, so work keeps running in the background. You can check status, steer mid-turn, interrupt, or send follow-ups on the same thread.
- **No dependencies**: Uses only the Python 3.8+ standard library and your existing Codex login. No `npm install`, no `pip install`.
- **Read-only local history**: Search past threads, turns, commands, and zstd-compressed rollout logs without modifying Codex's local databases.
- **Safe defaults**: New tasks start in a `read-only` sandbox unless `workspace-write` is requested, and the client never auto-approves interactive permission prompts.

## Requirements

- macOS or Linux (talks to Codex's local Unix socket; Windows is not supported)
- [Codex CLI](https://github.com/openai/codex) installed and signed in (`npm install -g @openai/codex` or `brew install --cask codex` on macOS)
- Python 3.8+
- Optional: Python 3.14+ or the `zstd` command if you need to read compressed older rollout files

Start the shared Codex daemon once before using the skill:

```bash
codex app-server daemon start
```

## Install

Works with agents that follow the [Agent Skills](https://agentskills.io) format (Claude Code, Codex, Cursor, VS Code / GitHub Copilot, Antigravity, Gemini CLI, OpenCode, and others).

```bash
npx skills add zhm9484/codex-use
```

Or just tell your agent:

> Install the skill from https://github.com/zhm9484/codex-use

## Usage

Ask your agent explicitly to use Codex or `codex-use` (in Codex itself, invoke it with `$codex-use`):

- *"Use codex-use to review my changes on the `auth-fix` branch and list concrete bugs."*
- *"Ask Codex with codex-use to implement the retry fix in `src/client.py`, then inspect the diff."*
- *"Use codex-use to find the Codex session from last week where we debugged the cache timeout."*

The skill includes two standalone CLI scripts under `scripts/` (both support `--help` on the root command and every subcommand):

- `scripts/codex_use.py` — list available models, start tasks, poll with `wait`, send `follow-up` turns, `steer`, or `interrupt`.
- `scripts/codex_history.py` — read-only search across local Codex SQLite databases and rollout JSONL files.

## Notes

- **Models**: Built-in `--kind` presets (`research`, `simple`, `complex-review`) are optional shortcuts. You can pass `--model` (and optional `--effort`) directly without `--kind`. Run `python3 scripts/codex_use.py models` to see what your account supports.
- **Host sandboxes**: If your coding agent runs shell commands inside a restricted sandbox, allow access to `$CODEX_HOME` (default `~/.codex`) so the scripts can reach the App Server socket and read local history.
- **Compatibility**: Verified with Codex CLI `0.167.2` and App Server daemon `0.172.0`. The App Server protocol is still experimental, so newer Codex releases may require updates. See [references/app-server.md](references/app-server.md) and [references/local-history.md](references/local-history.md) for protocol, schema, and troubleshooting details.

## License

[MIT](LICENSE)
