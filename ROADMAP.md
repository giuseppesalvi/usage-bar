# Usage Bar — Roadmap

A minimal macOS menu-bar app that shows **Claude Code** + **Codex** usage at a glance:
spend, token burn, and how close you are to your **rate-limit windows** and when they reset.

> Working name: **Usage Bar**.

---

## What it does

Shows, in the macOS menu bar: how close you are to your **5h / weekly rate-limit
windows** on **both** Claude and Codex (with reset countdowns), plus today's Claude spend.
All from local data, plus an opt-in read of Claude's own usage endpoint.

---

## Feasibility check — what the local files actually give us

Probed on this machine (2026-06-30). The two tools are **asymmetric**, and that shapes the whole plan.

### Codex — ✅ rate limits are LOCAL (the happy path)

Session logs at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` contain `token_count`
events whose `rate_limits` block is exactly what we want:

```jsonc
"rate_limits": {
  "limit_id": "codex",
  "primary":   { "used_percent": 1.0,  "window_minutes": 300,   "resets_at": 1782414588 },  // 5h window
  "secondary": { "used_percent": 4.0,  "window_minutes": 10080, "resets_at": 1782981892 },  // weekly window
  "plan_type": "plus"
}
```

The same event also carries `total_token_usage` (input / cached / output / reasoning) and
`model_context_window`. So for Codex we get **% used, window length, and reset epoch for free** —
just read the newest `token_count` event across all session files.

### Claude — ⚠️ only TOKENS are local; rate-limit % is NOT

Logs at `~/.claude/projects/**/*.jsonl`. Assistant lines carry:

```jsonc
"message": {
  "model": "claude-opus-4-8",
  "usage": { "input_tokens": 2784, "cache_creation_input_tokens": 2810,
             "cache_read_input_tokens": 16128, "output_tokens": 115, ... }
}
```

Enough to compute **tokens + $ spend** (this is the ccusage approach). But a sweep of
`~/.claude` found **no local cache** of the 5h/weekly utilization %. That number is fetched
live by the CLI (`/status`) and not persisted. So Claude's rate-limit window requires either:
- (a) calling Anthropic's usage endpoint with the cached OAuth creds in `~/.claude`, or
- (b) scraping it from a running session's output.

Both are fiddlier and more breakage-prone than Codex. **This asymmetry is the main risk.**

### App shell — trivial

No need to build a real Swift app to start. A menu-bar line can be a script that prints text,
hosted by [SwiftBar](https://github.com/swiftbar/SwiftBar) or [xbar](https://github.com/matryer/xbar).
Neither is installed here yet (`brew install --cask swiftbar`). Go native (`MenuBarExtra`, SwiftUI,
macOS 13+) only once the data layer is proven.

---

## Milestones

### v0 — Prove the data (no UI)  ·  DONE (2026-06-30)
Goal: one CLI script that prints a correct one-line summary. If this works, the project works.

- [x] `usage.py` (stdlib only) outputs `🟣 Claude $X · 🟢 Codex 5h Y% · wk Z%`,
      plus `--full` (per-window resets + per-model breakdown) and `--json`.
- [x] Codex reader: scan the most-recently-modified `rollout-*.jsonl`, take the latest
      `rate_limits` snapshot, emit primary (5h) / secondary (weekly) `used_percent` +
      `resets_at` countdown + `plan_type`. **Working** (live: 5h 77%, wk 12%, plus).
- [x] Claude reader: walk `~/.claude/projects/**/*.jsonl`, filter to today (local tz),
      sum tokens by model, price input/output/cache_read/cache_write(5m·1h). **Working.**
- [x] Sanity-check vs `ccusage`: same order of magnitude ($103 ours vs $83 ccusage).

**v0 polish:**
- [x] **Dedup — done, and it was subtler than "skip resume copies".** A single assistant
      response is written as *many* JSONL lines sharing `(message.id, requestId)`:
      (a) one line per content block (usage repeated), (b) streaming snapshots where
      `output_tokens` grows `2 → 2 → 1709`, and (c) verbatim copies across files on resume.
      Verified on real data: 1427 usage lines today → 662 distinct keys; 201 keys had
      *differing* usage across copies (the streaming case). → **Keep the max-output line
      per key**, not first-wins (which undercounts by dropping the final snapshot) and not
      sum (which overcounts every snapshot). Result: $103 (sum) → $48 (first-wins) →
      **$55 (max-per-key, correct)**; output 324k → 482k tokens recovered.
- [ ] Reconcile cache-write pricing model (1h vs 5m mix) — minor; numbers now sane.
- [ ] Codex: optionally surface today's token totals (cost is plan-based, not per-token).

> Note: ccusage was not a usable oracle here — this build emits `date: null` and folds in
> non-Anthropic models (`gpt-5.5`, `gpt-5.4-mini`). Correctness was established directly from
> the log structure instead.

### v1 — Menu bar via SwiftBar  ·  DONE (2026-06-30)
- [x] `usage.py --swiftbar` emits SwiftBar format (title · `---` · dropdown).
- [x] `usage-bar.30s.sh` plugin (30s refresh) execs `/usr/bin/python3 usage.py --swiftbar`
      — system python3 + stdlib-only script ⇒ works in SwiftBar's minimal PATH.
- [x] Compact title; dropdown = per-model $ + per-window % with reset countdowns + plan.
- [x] Threshold colors: green <70%, amber 70–90%, red >90% (title colored by hotter window).
- [x] Installed SwiftBar (`brew install --cask swiftbar`); plugin symlinked into
      `~/Library/Application Support/SwiftBar/Plugins/` (symlink ⇒ repo edits stay live);
      `PluginDirectory` set via `defaults`; SwiftBar launched.

**v1 is the usable product.** Codex rate-limits + Claude spend, both in the bar.

**v1 polish (done 2026-06-30):**
- [x] Circular % indicator — Harvey balls `○ ◔ ◑ ◕ ●` (`_ring(pct)`) replace the Codex 🟢,
      one per window, both inline in the title; dropbox lines colored by threshold.
      Claude keeps 🟣 until it has a % (v1.5).
- [x] Fix: Codex writes two `rate_limits` shapes — `limit_id:"codex"` (5h/weekly windows)
      and `limit_id:"premium"` (credit balance, **null** windows). Reader now only accepts
      windowed snapshots (`primary` is a dict), else a stray premium record blanks the bar.

Manual check if the bar is empty: SwiftBar → Preferences → set Plugin Folder to
`~/Library/Application Support/SwiftBar/Plugins` (macOS may require picking it via the
panel to grant a security-scoped bookmark, which `defaults` can't create).

### v1.5 — Claude rate-limit windows  ·  DONE (2026-06-30)
- [x] Source: `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token Claude
      Code stores (Keychain `Claude Code-credentials` / `~/.claude/.credentials.json`,
      `claudeAiOauth.accessToken`); headers `Authorization: Bearer …` + `anthropic-beta:
      oauth-2025-04-20`. Response: `five_hour` / `seven_day` / `seven_day_opus`, each
      `{utilization 0–100, resets_at ISO}`. (`/status` has no non-interactive surface.)
- [x] **Opt-in + consent**: off by default; `usage.py --enable-claude` prints a disclosure
      and records acceptance in `~/.config/usage-bar/config.json`; `--disable-claude` reverts.
      Fails closed (Claude → spend-only on any error). Cached 120s.
- [x] Isolated in `claude_windows()` so an endpoint change degrades gracefully.

**v1.5 UI — labeled ring gauges:**
- [x] Anti-aliased PNG rings (stdlib `zlib`/`struct`, supersampled) with a tool tag (C/A) +
      percent baked inside via a 3×5 bitmap font; one per tool's 5h window, threshold-colored.
      Sized to menu-bar height. Both tools' full windows + resets live in the dropdown.

### v2 — Native app (optional)  ·  if it earns a permanent slot
- [ ] SwiftUI `MenuBarExtra`, no SwiftBar dependency.
- [ ] Launch-at-login, preferences (which tools, thresholds, plan type).
- [ ] Notifications at 80% / 95% of a window.
- [ ] Maybe: sparkline of the day's burn.

---

## Open questions
- Claude weekly cap %: is there a supported endpoint, or only `/status` scraping? (decides v1.5 viability)
- Codex `plan_type` is in the log — surface plan-aware messaging?
- Multiple machines / cloud sessions: local-only means single-machine view. Acceptable for v1.
- Pricing drift: hardcoded $/token table needs a refresh path (or pull from a maintained source).

## Decision log
- **2026-06-30** — Project created. Confirmed Codex rate-limits are local & rich; Claude only exposes
  tokens locally. → Lead with Codex for the rate-limit feature; Claude starts as spend-only.
  Start at v0 CLI to de-risk before any UI.
- **2026-06-30** — v0 `usage.py` built & verified on real data. Both readers work end-to-end:
  Codex rate-limit windows + Claude $ spend. Pricing table from the claude-api skill
  (Opus 5/25, Sonnet 3/15, Haiku 1/5, Fable 10/50 per 1M; cache read 0.1x, write 1.25x/2x).
- **2026-06-30** — Dedup landed after a real investigation (see v0 polish above). The key
  insight: Claude Code writes one assistant response across many `(message.id, requestId)`
  lines including growing streaming snapshots, so the correct collapse is **max-output per
  key**, not first-wins or sum. Claude $ now trustworthy ($55/day).
- **2026-06-30** — v1 shipped. SwiftBar installed; `usage.py --swiftbar` + `usage-bar.30s.sh`
  plugin live in the menu bar with threshold colors. The usable product exists. → Next is
  v1.5 (Claude rate-limit windows — the hard part) or v2 (native app); both optional.

**v1 polish — drawn ring gauge (done 2026-06-30):**
- [x] `usage.py` renders the Codex windows as anti-aliased PNG ring gauges
      (pure stdlib: `zlib`+`struct`, ss×ss supersampling), composited side-by-side and
      embedded via SwiftBar `| image=`. Title = rings + `$X  5h N% wk M%`. Glyph fallback
      kept if rendering throws. Verified visually (full/partial fills, threshold colors).

**v1.5 — Claude rate-limit windows (blocked on a decision):**
- Confirmed earlier: Claude's 5h/weekly utilization % is NOT in the local logs (only tokens are).
- Getting it requires one of: (a) reuse Claude Code's OAuth token to call Anthropic's
  usage endpoint, (b) make one tiny API request and read `anthropic-ratelimit-*` response
  headers, or (c) find/parse a local usage cache Claude writes after `/status`.
- (a) and (b) touch credentials / make network calls → need explicit user sign-off.

### v1.6 — customization + ccusage-inspired (done 2026-07-01)
- [x] **Live customization** via a clickable Settings section in the dropdown (SwiftBar
      `bash=`/`param`/`refresh=true`): style (ring/harvey/text), tools on/off, which windows
      in the bar, spend show/hide + range. Persists in `~/.config/usage-bar/config.json`;
      same actions exposed as `--cycle-*`/`--toggle-*` flags.
- [x] **Multi-range spend** (today / 7d / 30d) — Claude reader buckets by date in one pass;
      shown in the dropdown and selectable for the bar.
- [x] **Window projection** (ccusage `blocks --active` idea, zero-persistence): from current
      %, window length, and reset time → "≈cap in Xm" or "proj N%"; "at cap" at 100%.
