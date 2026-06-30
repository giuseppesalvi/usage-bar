# Usage Bar

A minimal macOS menu-bar widget that shows **Claude Code** and **Codex** usage at a
glance: how close you are to your **rate-limit windows** (5h / weekly, with reset
countdowns) and today's Claude spend.

```
[C 100]  [A 7]   $95
```

Two ring gauges (C = Codex, A = Claude) show each tool's 5-hour window filling up,
colored green → amber → red as you approach the cap. The dropdown breaks out both
windows for both tools, reset times, and per-model spend.

It's a single stdlib-only Python script (`usage.py`) hosted by
[SwiftBar](https://github.com/swiftbar/SwiftBar). No dependencies, no build step.

## How it reads your data

- **Codex** — rate-limit windows are read from the local session logs
  (`~/.codex/sessions/**/rollout-*.jsonl`). No network.
- **Claude spend** — token usage is summed from the local logs
  (`~/.claude/projects/**/*.jsonl`) and priced locally. No network.
- **Claude rate-limit windows** — *opt-in only.* Claude doesn't store its 5h/weekly
  utilization locally, so this feature reuses the OAuth token Claude Code already keeps
  on your machine to call Anthropic's usage endpoint **for your own account**. It is
  **off by default**; enabling it prints a disclosure and records your acceptance.
  See [Claude windows (opt-in)](#claude-windows-opt-in).

Your token is never stored, printed, or committed — it's read into memory at run time
and sent only to `api.anthropic.com`.

## Requirements

- macOS
- [SwiftBar](https://github.com/swiftbar/SwiftBar): `brew install --cask swiftbar`
- `python3` (the system `/usr/bin/python3` is fine)

## Install

```sh
git clone <this repo> ~/Documents/Projects/usage-bar

# point SwiftBar at a plugin folder and symlink the plugin in
PLUGDIR="$HOME/Library/Application Support/SwiftBar/Plugins"
mkdir -p "$PLUGDIR"
ln -s "$PWD/usage-bar.30s.sh" "$PLUGDIR/usage-bar.30s.sh"
```

Launch SwiftBar and set its **Plugin Folder** to that directory (macOS may ask you to
pick it via the panel). The widget refreshes every 30s.

## CLI

`usage.py` also runs standalone:

```sh
python3 usage.py            # one-line summary
python3 usage.py --full     # per-window + per-model breakdown
python3 usage.py --json     # machine-readable
```

## Customize

The dropdown has a **Settings** section you can click to change things live (no config
editing, no restart):

- **Style** — `ring` (drawn gauges) · `harvey` (○◔◑◕● glyphs) · `text` (plain %).
- **Tools** — show/hide Codex or Claude in the menu bar.
- **Bar windows** — show the 5h and/or weekly window in the bar (the dropdown always
  shows both).
- **Spend** — show/hide, and switch the range shown in the bar: today · 7d · 30d.

Settings persist in `~/.config/usage-bar/config.json`. The same actions are available as
flags (`--cycle-style`, `--toggle-tool codex`, `--toggle-window weekly`, `--toggle-spend`,
`--cycle-spend-range`).

The dropdown also shows **multi-range spend** (today / 7d / 30d) and a **burn-rate
projection** per window — e.g. "≈cap in 1h51m" or "proj 84%" — computed from the current
fill, the window length, and the reset time.

## Claude windows (opt-in)

```sh
python3 usage.py --enable-claude    # prints a disclosure, records acceptance
python3 usage.py --disable-claude   # turn back off
```

Notes:
- Reads the OAuth access token Claude Code stored (macOS Keychain item
  `Claude Code-credentials`, or `~/.claude/.credentials.json`).
- Sends it only to `https://api.anthropic.com/api/oauth/usage` to fetch your own usage.
- This is an **undocumented** endpoint — it may change or break on a Claude Code update.
  The feature fails closed (Claude falls back to spend-only).
- Results are cached locally for 120s. Your acceptance is recorded in
  `~/.config/usage-bar/config.json`.

## License

Personal project. No license granted yet.
