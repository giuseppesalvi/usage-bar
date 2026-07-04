# Roadmap

Usage Bar's public direction is intentionally narrow: a small, inspectable SwiftBar
plugin that shows Claude Code and Codex usage pressure at a glance.

## Current Scope

- Keep the project stdlib-only.
- Keep the primary install path as SwiftBar/xbar.
- Prefer local logs whenever possible.
- Keep Claude's networked window lookup explicit, documented, cached, and optional.
- Optimize for "can I keep working right now?" rather than historical analytics.

## Near Term

- Add a screenshot or short GIF to the README.
- Keep parser tests current with real Claude/Codex log shape changes.
- Keep `--doctor` useful as install paths and data sources change.
- Refresh pricing when model pricing changes.
- Improve missing-data messages for first-time users.

## Maybe Later

- Surface Codex token totals if they prove useful.
- Add a native macOS app only if SwiftBar becomes the main source of friction.
- Support another tool only if it exposes useful local usage data.

## Non-Goals

- Full usage analytics dashboard.
- Cloud sync.
- Account management.
- Background daemon.
- Heavy Python dependency stack.
- Replacing richer native trackers.
