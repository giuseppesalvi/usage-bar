# Usage Bar

A tiny macOS menu-bar pressure gauge for **Claude Code** and **Codex** usage.

Usage Bar is intentionally boring: one stdlib-only Python script, hosted by
[SwiftBar](https://github.com/swiftbar/SwiftBar), reading local logs by default. It
exists for people who want a quick answer to one question:

> Am I close to a usage limit, and should I slow down?

```
[C 38]  [A 46]  $25
```

Two compact gauges show the selected rate-limit window for Codex and Claude. The
dropdown shows both 5h and weekly windows, reset countdowns, Claude spend, and a few
settings.

## Why This Exists

There are more complete AI usage trackers. This one is deliberately smaller:

- one Python file
- no Python dependencies
- no background daemon
- no app bundle
- local logs by default
- explicit opt-in for the one networked Claude rate-limit lookup
- focused on the few numbers that affect whether you keep working now

If you want dashboards, history, sync, or a native app, this is probably not the right
tool. If you want an inspectable SwiftBar widget that does one job well, it is.

## Privacy Model

- **Codex windows** are read from local session logs:
  `~/.codex/sessions/**/rollout-*.jsonl`.
- **Claude spend** is computed from local Claude Code logs:
  `~/.claude/projects/**/*.jsonl`.
- **Claude windows** are off by default. If enabled, Usage Bar reads the OAuth token
  Claude Code already stores on your machine and calls Anthropic's usage endpoint for
  your own account.

The Claude token is read into memory at run time. It is not printed, cached, or stored
by this project. See [PRIVACY.md](PRIVACY.md) for the full disclosure.

## Requirements

- macOS
- [SwiftBar](https://github.com/swiftbar/SwiftBar): `brew install --cask swiftbar`
- Python 3.10+ (`/usr/bin/python3` is fine if it is 3.10 or newer)

## Install

```sh
git clone <this repo> ~/Documents/Projects/usage-bar
cd ~/Documents/Projects/usage-bar

PLUGDIR="$HOME/Library/Application Support/SwiftBar/Plugins"
mkdir -p "$PLUGDIR"
ln -s "$PWD/usage-bar.30s.sh" "$PLUGDIR/usage-bar.30s.sh"
```

Launch SwiftBar and set its **Plugin Folder** to that directory if needed. The widget
refreshes every 30 seconds.

## CLI

```sh
python3 usage.py            # one-line summary
python3 usage.py --full     # per-window + per-model breakdown
python3 usage.py --json     # machine-readable
python3 usage.py --doctor   # check local setup
python3 usage.py --version
```

## Claude Windows

Claude does not appear to persist its 5h/weekly utilization in local logs. Usage Bar can
fetch those windows from Anthropic's own usage endpoint, but only after you opt in:

```sh
python3 usage.py --enable-claude
python3 usage.py --disable-claude
```

Enabling prints a disclosure and records acceptance in
`~/.config/usage-bar/config.json`. Results are cached for 120 seconds in
`~/.config/usage-bar/claude-usage-cache.json`.

This endpoint is undocumented and may change. If it breaks, Usage Bar fails closed and
Claude falls back to spend-only.

## Customize

The dropdown has a **Settings** section with direct-select options:

- **Style**: `ring`, `bar`, `number`, `harvey`, or `text`
- **Tool mark**: `letter`, `logo`, or `spark`
- **Tools**: show/hide Codex or Claude in the menu bar
- **Bar windows**: show 5h and/or weekly in the bar
- **Spend**: hidden, today, 7d, or 30d
- **Notify near cap**: off, 80%, 90%, or 95%

Settings persist in `~/.config/usage-bar/config.json`.

## Limitations

- macOS only.
- Requires SwiftBar or xbar.
- Claude rate-limit windows use an undocumented endpoint.
- Pricing is a local hardcoded table and can drift.
- Local logs mean this is a single-machine view.
- This is a menu-bar indicator, not a full analytics app.

## Development

```sh
python3 -m py_compile usage.py
python3 -m unittest discover -s tests
```

The test suite uses temporary log fixtures and does not need real Claude/Codex data.
