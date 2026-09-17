# Privacy

Usage Bar is local-first. It reads usage data already written by Claude Code and Codex
on your machine.

## Local Files Read

- Codex session logs:
  `~/.codex/sessions/**/rollout-*.jsonl`
- Claude Code project logs:
  `~/.claude/projects/**/*.jsonl`
- Optional Claude Code credentials:
  - macOS Keychain item `Claude Code-credentials`
  - `~/.claude/.credentials.json`

## Local Files Written

- Settings:
  `~/.config/usage-bar/config.json`
- Claude usage cache, only when Claude windows are enabled:
  `~/.config/usage-bar/claude-usage-cache.json`
- Notification de-duplication state:
  `~/.config/usage-bar/notify-state.json`

## Network

By default, Usage Bar does not make network requests.

If you run `python3 usage.py --enable-claude`, Usage Bar can call:

`https://api.anthropic.com/api/oauth/usage`

That request uses the OAuth token Claude Code already stores on your machine. The token
is sent only to `api.anthropic.com`. Usage Bar does not print, persist, or commit the
token.

Claude windows are cached for 120 seconds to avoid unnecessary requests.

## Disable Claude Windows

```sh
python3 usage.py --disable-claude
```

You can also remove the local state manually:

```sh
rm -rf ~/.config/usage-bar
```

## Known Risk

Claude's usage endpoint is undocumented. It can change or disappear. If that happens,
Usage Bar should fail closed and show Claude spend only.
