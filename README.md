# error-recall

A memory for failures. When a Bash command fails, error-recall pulls out the meaningful error lines
(exception lines, `error:` / `error[E…]` lines, and so on) and normalizes away paths, line numbers,
addresses and UUIDs to get a stable fingerprint. When a later command of the same kind succeeds in the
same session (`pytest` after `pytest`), it records what happened in between: the files edited and the
other commands run.

When the same error shows up again, even weeks later in a new session, Claude is told:

> error-recall: this error (fingerprint 3f9a…) has been seen 2 time(s) before in this repo, last on
> 2026-10-02. On 2026-10-02 it was resolved by editing pyproject.toml and running `uv add pyyaml`;
> `python app.py` then succeeded.

**Hooks:** `PostToolUseFailure(Bash)`, `PostToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit)`.
**CLI:** `error-recall list`, `error-recall show <fingerprint>`, `error-recall forget`.
Data stays local, in SQLite under `~/.claude/error-recall/`.

## Install

From Anthropic's plugin directory, once listed: search for `error-recall` in `/plugin directory` in Claude Code, or under **Customize > Plugins** on claude.ai.

To try it from a clone for one session: `claude --plugin-dir /path/to/error-recall`.

This plugin is for Claude Code: it is built on hooks and a command-line tool in `bin/`, which claude.ai chat does not load.

## Configuration

Settings are read from `~/.claude/error-recall.json` and then `<project>/.claude/error-recall.json`. The project file wins, objects merge by key, and a broken file is skipped and logged.

## What it runs and stores

It is Python 3.9+ standard library only, makes no network requests, and sends nothing anywhere. Every hook fails open: an internal error is logged to `~/.claude/error-recall/errors.log` and the hook exits 0.

- Stores normalized error lines, commands and the names of edited files in `~/.claude/error-recall/recall.sqlite`.
- Runs no commands of its own.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

`docs/HOOK_SCHEMA.md` records the hook payloads this plugin depends on.

## License

MIT. See `LICENSE`.
