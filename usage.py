#!/usr/bin/env python3
"""Usage Bar — Claude Code + Codex usage pressure in the macOS menu bar.

Local by default:
  - Claude: ~/.claude/projects/**/*.jsonl -> token spend
  - Codex:  ~/.codex/sessions/**/rollout-*.jsonl -> rate-limit windows

Claude rate-limit windows are optional because they require Claude Code's
stored OAuth token and an Anthropic usage endpoint request.

Flags:
  --full   multi-line breakdown (per-window resets, today's tokens)
  --json   machine-readable dump
  --doctor setup checks
"""
from __future__ import annotations

import base64
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

# $ per 1M tokens: (input, output). Cache read = 0.1x input; 5m write = 1.25x; 1h write = 2.0x.
# Source: claude-api skill (cached 2026-06-24). Sonnet 5 sticker; intro discount not applied.
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def rate_for(model: str) -> tuple[float, float]:
    if model in PRICING:
        return PRICING[model]
    # fallback by family substring
    m = model.lower()
    if "opus" in m:
        return (5.0, 25.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "haiku" in m:
        return (1.0, 5.0)
    if "fable" in m or "mythos" in m:
        return (10.0, 50.0)
    return (0.0, 0.0)  # unknown model -> don't guess a price


# ── Claude rate-limit windows (opt-in; reuses Claude Code's OAuth token) ──
CONFIG_DIR = HOME / ".config" / "usage-bar"
CONFIG_FILE = CONFIG_DIR / "config.json"
CLAUDE_CACHE = CONFIG_DIR / "claude-usage-cache.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_CACHE_TTL = 120  # seconds — be polite to the endpoint

CLAUDE_CONSENT_TEXT = f"""\
Usage Bar — enable Claude rate-limit windows

To show Claude's 5h and weekly usage %, Usage Bar will:
  • read the OAuth access token Claude Code already stored on this machine
    (macOS Keychain item "Claude Code-credentials", or ~/.claude/.credentials.json), and
  • send it to {CLAUDE_USAGE_URL} to fetch YOUR account's usage.

What you should know and accept:
  • It's your own account, used only to display your own numbers in your menu bar.
  • The token goes only to api.anthropic.com — nowhere else. No data leaves your machine
    except that request.
  • This is an UNDOCUMENTED endpoint Anthropic may change or remove, so the feature can
    break on a Claude Code update (it fails closed — Claude just shows spend only).
  • Results are cached locally for {CLAUDE_CACHE_TTL}s to limit requests.

Enabling records your acceptance (with a timestamp) in:
  {CONFIG_FILE}
Disable any time with:  usage.py --disable-claude
"""


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def claude_enabled() -> bool:
    return bool(_load_config().get("claude_windows", {}).get("enabled"))


def set_claude_enabled(on: bool) -> None:
    cfg = _load_config()
    cw = cfg.setdefault("claude_windows", {})
    cw["enabled"] = bool(on)
    if on:
        cw["accepted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_config(cfg)


# ── Display customization (editable live from the dropdown) ──
DISPLAY_DEFAULTS = {
    "style": "ring",                 # ring | bar | number | harvey | text
    "mark": "spark",                 # letter (C/A) | logo | spark (Claude starburst)
    "tools": ["codex", "claude"],    # which tools appear in the menu bar
    "windows": ["5h"],               # which windows in the bar: 5h and/or weekly
    "show_spend": True,
    "spend_range": "today",          # today | d7 | d30
    "digits": "large",               # digit size in number/bar styles
}
_DISPLAY_CHOICES = {
    "style": ["ring", "bar", "number", "harvey", "text"],
    "mark": ["letter", "logo", "spark"],
    "spend_range": ["today", "d7", "d30"],
    "digits": ["large", "medium", "small"],
}
_IMAGE_STYLES = ("ring", "bar", "number")
_DISPLAY_LISTS = {"tools": ["codex", "claude"], "windows": ["5h", "weekly"]}


def display_cfg() -> dict:
    cfg = {**DISPLAY_DEFAULTS, **_load_config().get("display", {})}
    return cfg


def set_display(key: str, value) -> None:
    cfg = _load_config()
    cfg.setdefault("display", {})[key] = value
    _save_config(cfg)


def cycle_display(key: str) -> None:
    """Advance a scalar setting to its next allowed value."""
    choices = _DISPLAY_CHOICES.get(key)
    if not choices:
        return
    cur = display_cfg().get(key)
    nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
    set_display(key, nxt)


def toggle_display_list(key: str, item: str) -> None:
    """Add/remove an item from a list setting (tools, windows)."""
    if key not in _DISPLAY_LISTS or item not in _DISPLAY_LISTS[key]:
        return
    cur = list(display_cfg().get(key, []))
    if item in cur:
        cur.remove(item)
    else:
        # preserve canonical order
        cur = [x for x in _DISPLAY_LISTS[key] if x in cur or x == item]
    set_display(key, cur)


def toggle_display_bool(key: str) -> None:
    set_display(key, not bool(display_cfg().get(key)))


# ── Notifications + staleness (CodexBar-style, still local-only) ──
NOTIFY_DEFAULTS = {"enabled": True, "threshold": 90}
NOTIFY_THRESHOLDS = [80, 90, 95]
NOTIFY_STATE = CONFIG_DIR / "notify-state.json"
STALE_SECS = 30 * 60  # a Codex snapshot older than this is stale -> dim the ring


def notify_cfg() -> dict:
    return {**NOTIFY_DEFAULTS, **_load_config().get("notify", {})}


def _load_notify_state() -> dict:
    try:
        return json.loads(NOTIFY_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_notify_state(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        NOTIFY_STATE.write_text(json.dumps(state))
    except OSError:
        pass


def _notify(title: str, text: str) -> None:
    """Fire a native macOS banner (no deps). Best-effort; never raises."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def check_notifications(codex: dict, cw: dict) -> None:
    """Notify once per window per reset-cycle when usage crosses the threshold.
    Dedup key is (tool:window) -> the resets_at we last alerted for, so a new
    cycle (new resets_at) re-arms the alert. Called only on the periodic run."""
    cfg = notify_cfg()
    if not cfg.get("enabled"):
        return
    thr = float(cfg.get("threshold", 90))
    checks = []  # (key, tool, window_label, pct, resets_at)
    for src, tool, field_idx in ((codex, "Codex", 0), (cw, "Claude", 1)):
        if not src.get("found"):
            continue
        for wk, spec in _WIN_MAP.items():
            w = src.get(spec[field_idx]) or {}
            if isinstance(w, dict) and w.get("used_percent") is not None:
                checks.append((f"{tool.lower()}:{wk}", tool, spec[3],
                               w["used_percent"], w.get("resets_at")))
    state = _load_notify_state()
    changed = False
    for key, tool, wl, pct, resets_at in checks:
        if pct >= thr and state.get(key) != str(resets_at):
            _notify("Usage Bar", f"{tool} {wl} at {pct:.0f}% · resets {_fmt_reset(resets_at)}")
            state[key] = str(resets_at)
            changed = True
    if changed:
        _save_notify_state(state)


_WINDOW_MINUTES = {"5h": 300, "weekly": 10080, "five_hour": 300, "seven_day": 10080}


def _project(used_percent, window_minutes, resets_at) -> dict | None:
    """Project a window's end-of-period % and ETA-to-cap from current burn,
    assuming the average rate so far this window holds. No persistence needed."""
    if used_percent is None or not window_minutes or not resets_at:
        return None
    remaining = (resets_at - time.time()) / 60
    elapsed = window_minutes - remaining
    if elapsed <= 1 or used_percent <= 0 or remaining <= 0:
        return None
    rate = used_percent / elapsed  # %/min
    proj = used_percent + rate * remaining
    eta_min = (100 - used_percent) / rate if rate > 0 else None
    return {"proj": proj, "eta_min": eta_min if eta_min and eta_min < remaining else None}


def _claude_token() -> str | None:
    """Reuse the OAuth token Claude Code stored (file first, then Keychain)."""
    p = HOME / ".claude" / ".credentials.json"
    if p.exists():
        try:
            return json.loads(p.read_text())["claudeAiOauth"]["accessToken"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)["claudeAiOauth"]["accessToken"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return None


def _iso_to_epoch(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _claude_win(d) -> dict | None:
    if not isinstance(d, dict) or d.get("utilization") is None:
        return None
    return {"used_percent": float(d["utilization"]), "resets_at": _iso_to_epoch(d.get("resets_at"))}


def claude_windows() -> dict:
    """Claude's 5h / weekly utilization via the OAuth usage endpoint (opt-in, cached)."""
    if not claude_enabled():
        return {"enabled": False}
    try:
        c = json.loads(CLAUDE_CACHE.read_text())
        if time.time() - c.get("fetched_at", 0) < CLAUDE_CACHE_TTL:
            return c["data"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    token = _claude_token()
    if not token:
        return {"enabled": True, "found": False, "error": "no token — run any claude command"}
    try:
        req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "User-Agent": "usage-bar/0.1",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            j = json.load(resp)
    except urllib.error.HTTPError as e:
        err = "auth expired — run any claude command" if e.code in (401, 403) else f"HTTP {e.code}"
        return {"enabled": True, "found": False, "error": err}
    except Exception:
        return {"enabled": True, "found": False, "error": "network error"}
    data = {
        "enabled": True, "found": True,
        "five_hour": _claude_win(j.get("five_hour")),
        "seven_day": _claude_win(j.get("seven_day")),
        "seven_day_opus": _claude_win(j.get("seven_day_opus")),
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_CACHE.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
    except OSError:
        pass
    return data


def _local_today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _ts_to_local_date(ts: str) -> str | None:
    try:
        # ISO 8601, usually trailing Z (UTC)
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _price(a: dict) -> float:
    in_rate, out_rate = rate_for(a["model"])
    return (
        a["input"] * in_rate
        + a["output"] * out_rate
        + a["cache_read"] * in_rate * 0.1
        + a["cw_5m"] * in_rate * 1.25
        + a["cw_1h"] * in_rate * 2.0
    ) / 1_000_000


def claude_spend(projects_dir: Path | None = None) -> dict:
    """Price Claude token usage over today / last 7d / last 30d, with today's
    per-model breakdown. Dedup: one record per (message.id, requestId), keeping
    the max-output line (streaming snapshots grow output; resumes copy lines)."""
    from datetime import timedelta

    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    d7 = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
    d30 = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)}

    best: dict[tuple, dict] = {}
    unkeyed: list[dict] = []
    dup_count = 0
    projects_dir = projects_dir or CLAUDE_PROJECTS
    if not projects_dir.exists():
        return {"found": False, "cost": 0.0, "models": {},
                "spend": {"today": 0.0, "d7": 0.0, "d30": 0.0}}

    for jf in projects_dir.rglob("*.jsonl"):
        try:
            with jf.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    date = _ts_to_local_date(rec.get("timestamp") or "")
                    if not date or date not in d30:
                        continue
                    cc = usage.get("cache_creation") or {}
                    cw_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
                    cw_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
                    cc_total = usage.get("cache_creation_input_tokens", 0) or 0
                    if cw_5m + cw_1h == 0 and cc_total:
                        cw_5m = cc_total
                    rec_fields = {
                        "model": msg.get("model") or "unknown",
                        "input": usage.get("input_tokens", 0) or 0,
                        "output": usage.get("output_tokens", 0) or 0,
                        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                        "cw_5m": cw_5m,
                        "cw_1h": cw_1h,
                        "date": date,
                    }
                    mid = msg.get("id")
                    rid = rec.get("requestId")
                    if not mid and not rid:
                        unkeyed.append(rec_fields)
                        continue
                    key = (mid, rid)
                    prev = best.get(key)
                    if prev is None:
                        best[key] = rec_fields
                    else:
                        dup_count += 1
                        if rec_fields["output"] > prev["output"]:
                            best[key] = rec_fields
        except OSError:
            continue

    s_today = s7 = s30 = 0.0
    by_model: dict[str, dict] = {}
    for rf in list(best.values()) + unkeyed:
        c = _price(rf)
        s30 += c
        if rf["date"] in d7:
            s7 += c
        if rf["date"] == today:
            s_today += c
            acc = by_model.setdefault(
                rf["model"], {"input": 0, "output": 0, "cache_read": 0, "cw_5m": 0, "cw_1h": 0}
            )
            for f in ("input", "output", "cache_read", "cw_5m", "cw_1h"):
                acc[f] += rf[f]

    priced = {m: {**a, "cost": _price({**a, "model": m})} for m, a in by_model.items()}
    return {
        "found": s30 > 0,
        "cost": s_today,
        "models": priced,
        "dups_skipped": dup_count,
        "spend": {"today": s_today, "d7": s7, "d30": s30},
    }


def codex_rate_limits(scan: int = 12, sessions_dir: Path | None = None) -> dict:
    """Latest rate-limit snapshot across the most recently modified Codex sessions."""
    sessions_dir = sessions_dir or CODEX_SESSIONS
    if not sessions_dir.exists():
        return {"found": False}
    files = sorted(
        sessions_dir.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:scan]

    best_ts = ""
    best = None
    for jf in files:
        try:
            with jf.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "rate_limits" not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") or {}
                    rl = payload.get("rate_limits")
                    if not isinstance(rl, dict):
                        continue
                    # Codex emits two record shapes: limit_id "codex" carries the
                    # 5h/weekly windows; "premium" carries credit info with null
                    # windows. Only the windowed snapshots are useful here.
                    if not isinstance(rl.get("primary"), dict):
                        continue
                    ts = rec.get("timestamp", "")
                    if ts >= best_ts:
                        best_ts = ts
                        best = rl
        except OSError:
            continue

    if best is None:
        return {"found": False}
    return {"found": True, "timestamp": best_ts, **best}


def _fmt_reset(epoch: int | None) -> str:
    if not epoch:
        return "?"
    secs = int(epoch - time.time())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h >= 24:
        return f"{h // 24}d{h % 24}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _ring(pct: float) -> str:
    """Harvey-ball circular-progress glyph for a 0–100 percentage."""
    if pct >= 95:
        return "●"
    if pct >= 60:
        return "◕"
    if pct >= 35:
        return "◑"
    if pct >= 12:
        return "◔"
    return "○"


def line_summary(claude: dict, codex: dict, cw: dict | None = None) -> str:
    parts = []
    spend = f"🟣 Claude ${claude['cost']:.2f}" if claude.get("found") else "🟣 Claude —"
    if cw and cw.get("found"):
        f = (cw.get("five_hour") or {}).get("used_percent")
        s = (cw.get("seven_day") or {}).get("used_percent")
        bits = []
        if f is not None:
            bits.append(f"{_ring(f)} 5h {f:.0f}%")
        if s is not None:
            bits.append(f"{_ring(s)} wk {s:.0f}%")
        if bits:
            spend += " (" + " · ".join(bits) + ")"
    parts.append(spend)
    if codex.get("found"):
        p = codex.get("primary") or {}
        s = codex.get("secondary") or {}
        seg = "Codex"
        if p:
            pp = p.get("used_percent", 0) or 0
            seg += f" {_ring(pp)} 5h {pp:.0f}%"
        if s:
            sp = s.get("used_percent", 0) or 0
            seg += f" · {_ring(sp)} wk {sp:.0f}%"
        parts.append(seg)
    else:
        parts.append("Codex —")
    return " · ".join(parts)


def full_report(claude: dict, codex: dict, cw: dict) -> str:
    def plain(line: str) -> str:  # strip SwiftBar "| ..." params for terminal
        return "  " + line.split(" | ")[0]

    out = [line_summary(claude, codex, cw), ""]
    out.append("Claude — spend")
    if claude.get("found"):
        sp = claude.get("spend", {})
        out.append(f"  today ${sp.get('today', 0):.2f} · 7d ${sp.get('d7', 0):.2f} · 30d ${sp.get('d30', 0):.2f}")
        for model, a in sorted(claude["models"].items(), key=lambda kv: -kv[1]["cost"]):
            out.append(
                f"    {model}: ${a['cost']:.3f} today  "
                f"(in {a['input']:,} / out {a['output']:,} / cache_read {a['cache_read']:,})"
            )
        if claude.get("dups_skipped"):
            out.append(f"  ({claude['dups_skipped']:,} duplicate entries skipped)")
    else:
        out.append("  no usage")
    out.append("")
    out.append("Claude — rate-limit windows")
    if not cw.get("enabled"):
        out.append("  off — enable with: usage.py --enable-claude")
    elif cw.get("found"):
        for label, key, wmin in (("5h", "five_hour", 300), ("weekly", "seven_day", 10080),
                                 ("weekly·opus", "seven_day_opus", 10080)):
            w = cw.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                out.append(plain(_window_line(label, w, wmin)))
    else:
        out.append(f"  {cw.get('error', 'unavailable')}")
    out.append("")
    out.append("Codex — rate-limit windows")
    if codex.get("found"):
        if codex.get("plan_type"):
            out.append(f"  plan: {codex['plan_type']}")
        for label, key, wmin in (("5h", "primary", 300), ("weekly", "secondary", 10080)):
            w = codex.get(key) or {}
            if w:
                out.append(plain(_window_line(label, w, wmin)))
    else:
        out.append("  no rate-limit data found")
    return "\n".join(out)


def _pct_color(pct: float) -> str:
    """Green < 70, amber 70–90, red > 90."""
    if pct >= 90:
        return "#e74c3c"
    if pct >= 70:
        return "#e67e22"
    return "#2ecc71"


# Dropdown colors as "light,dark" pairs — SwiftBar picks per appearance.
_MENU_TEXT = "#1f2933,#e5e7eb"
_MENU_MUTED = "#6b7280,#9ca3af"
_MENU_ERR = "#c2410c,#fb923c"


def _menu_pct_color(pct: float) -> str:
    """Threshold colors readable on both the light and dark dropdown."""
    if pct >= 90:
        return "#b91c1c,#f87171"
    if pct >= 70:
        return "#b45309,#fbbf24"
    return "#15803d,#4ade80"


# ── Ring gauge: an anti-aliased PNG progress ring, drawn with stdlib only ──
def _hex_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def _png_bytes(w: int, h: int, rgba: bytes) -> bytes:
    """Encode an 8-bit RGBA buffer (w*h*4 bytes) as a PNG."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0 (none) per scanline
        raw += rgba[y * w * 4 : (y + 1) * w * 4]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# 3×5 bitmap font for the tag (top) + percent (bottom) baked inside each ring.
_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("110", "010", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "100", "100"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "$": ("111", "110", "111", "011", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "A": ("010", "101", "111", "101", "101"),
}


def _blit(out: bytearray, W: int, H: int, text: str, cx: float, cy: float,
          scale: int, rgb: tuple[float, float, float]) -> None:
    """Draw `text` (opaque) centered on (cx, cy) into an RGBA buffer."""
    cw, gap = 3 * scale, scale
    total = len(text) * cw + (len(text) - 1) * gap
    x = int(round(cx - total / 2))
    y0 = int(round(cy - 5 * scale / 2))
    r, g, b = int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
    for ch in text:
        glyph = _FONT.get(ch)
        if glyph:
            for ry in range(5):
                for rx in range(3):
                    if glyph[ry][rx] == "1":
                        for yy in range(scale):
                            py = y0 + ry * scale + yy
                            if not 0 <= py < H:
                                continue
                            for xx in range(scale):
                                px = x + rx * scale + xx
                                if 0 <= px < W:
                                    i = (py * W + px) * 4
                                    out[i], out[i + 1], out[i + 2], out[i + 3] = r, g, b, 255
        x += cw + gap


_SS = 3  # supersample factor for AA'd text and bars (matches the ring's)


def _downsample(big: bytearray, w: int, h: int, ss: int = _SS) -> bytearray:
    """Box-average a (w·ss)×(h·ss) RGBA buffer down to w×h, alpha-weighted."""
    out = bytearray(w * h * 4)
    n = ss * ss
    sw = w * ss
    for y in range(h):
        for x in range(w):
            sa = sr = sg = sb = 0
            for yy in range(ss):
                base = ((y * ss + yy) * sw + x * ss) * 4
                for xx in range(ss):
                    i = base + xx * 4
                    a = big[i + 3]
                    if a:
                        sa += a
                        sr += big[i] * a
                        sg += big[i + 1] * a
                        sb += big[i + 2] * a
            if sa:
                i = (y * w + x) * 4
                out[i] = sr // sa
                out[i + 1] = sg // sa
                out[i + 2] = sb // sa
                out[i + 3] = sa // n
    return out


def _text_rgba(text: str, s: int, rgb: tuple[float, float, float]) -> tuple[int, int, bytearray]:
    """AA'd pixel-font text: glyphs drawn at subpixel scale `s` (effective
    scale s/_SS, so fractional sizes work) then box-filtered down."""
    sub_w = s * (4 * len(text) - 1)  # 3-wide glyph + 1 gap per char
    sub_h = 5 * s
    w = -(-sub_w // _SS)
    h = -(-sub_h // _SS)
    big = bytearray((w * _SS) * (h * _SS) * 4)
    _blit(big, w * _SS, h * _SS, text, w * _SS / 2, h * _SS / 2, s, rgb)
    return w, h, _downsample(big, w, h)


def _paste(dst: bytearray, W: int, src: bytearray, sw: int, sh: int,
           x0: int, y0: int) -> None:
    """Copy an RGBA block into dst (row-major, width W) where src has ink."""
    for y in range(sh):
        for x in range(sw):
            si = (y * sw + x) * 4
            if not src[si + 3]:
                continue
            di = ((y + y0) * W + x + x0) * 4
            dst[di:di + 4] = src[si:si + 4]


# 30×30 logo bitmaps rasterized from the Simple Icons SVG paths with 8×8
# supersampling; each char is a hex alpha level (0 transparent … f opaque) so
# the marks render anti-aliased like the ring, in a single generated PNG.
_LOGO = {
    # Claude/Anthropic: Simple Icons "Anthropic" A mark.
    "claude": (
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000022210012220000000000",
        "0000000005fffd003fff5000000000",
        "000000000bffff300cffb000000000",
        "000000002fffff9006fff200000000",
        "000000008ffffff101eff800000000",
        "00000000dffafff6009ffd00000000",
        "00000004fff3affc003fff40000000",
        "0000000bffc04fff300cffb0000000",
        "0000002fff600dff9006fff2000000",
        "0000008fff1008ffe101fff8000000",
        "000000dffb4446fff6009ffd000000",
        "000004fffffffffffc004fff400000",
        "00000affffffffffff300dffa00000",
        "00001fffcbbbbbbeff9007fff10000",
        "00007fff20000009ffe001fff70000",
        "0000dffb00000003fff600affd0000",
        "0004fff500000000cffc004fff4000",
        "000122200000000012220002221000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
    ),
    # Codex: OpenAI blossom symbol used with the Codex wordmark.
    "codex": (
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000007cffb50000000000000",
        "0000000001cfa88cf9442000000000",
        "000000000ce30001dffffb30000000",
        "000000005f50007ee8336de4000000",
        "0000004cfd004df9100001cd000000",
        "000006fccb05fb3005a3002f600000",
        "00002fa09b06a003ccdf910ca00000",
        "00008e109b06919f7008fe6bb00000",
        "0000ca009b06de88e7002aff800000",
        "0000d9009b06c2002cd5004ec00000",
        "0000bc009c069000099cb204f50000",
        "00005f402bd990000960cb00cb0000",
        "00000ce4004dc2002c60bb009d0000",
        "000008ffa2007e88ed60bb00ac0000",
        "00000bb6ef8107f91960bb01e80000",
        "00000ac019fdcc300a60bb0af20000",
        "000006f3002a5003bf50bdcf500000",
        "000000dc1000019fd400dfc4000000",
        "0000004ed6338ee70004f500000000",
        "00000003bffffd10003ec000000000",
        "0000000001449fc88afc1000000000",
        "00000000000005bffc700000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
    ),
    # Claude spark: Simple Icons "Claude" starburst, alternative Claude mark.
    "spark": (
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "0000000007e6000064000000000000",
        "000000000dfd0001fc000000000000",
        "0000000008ff5004fa000740000000",
        "0000000001dfc005f700afe0000000",
        "000002c9005ff406f408ffb0000000",
        "000003ffc20bfc08f15ffe20000000",
        "0000005efe53ff59e3eff400000000",
        "00000002bff9afcbcdff8000000000",
        "0000000006effffefffc0001430000",
        "00000000002bfffffff88beffc0000",
        "0000aba98864bfffffffffc9620000",
        "0000599abbbdefffffe94000000000",
        "000000000003bfffffdefffed50000",
        "00000000019fdcffffe337aefc0000",
        "000000007ef95fafeffe4000200000",
        "0000002cfd42eb5e6fcae500000000",
        "0000009f810ce18d0cf68f50000000",
        "00000011009f40bb02fe25e6000000",
        "0000000006f800ea007fb024000000",
        "000000001f8002f8000bf000000000",
        "00000000040005f700013000000000",
        "00000000000002d400000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
        "000000000000000000000000000000",
    ),
}
_TAG_TOOL = {"C": "codex", "A": "claude"}


def _logo_rows(tag: str, mark: str) -> tuple[str, ...] | None:
    """Bitmap for a tool mark, or None for letter mode. The "spark" mark swaps
    the Anthropic A for the Claude starburst; Codex keeps the blossom."""
    tool = _TAG_TOOL.get(tag)
    if not tool or mark not in ("logo", "spark"):
        return None
    if mark == "spark" and tool == "claude":
        return _LOGO["spark"]
    return _LOGO.get(tool)


def _blit_rows(out: bytearray, W: int, H: int, rows: tuple[str, ...],
               cx: float, cy: float, scale: int, rgb: tuple[float, float, float]) -> None:
    """Draw an alpha bitmap (rows of hex levels 0…f) centered on (cx, cy)."""
    gh, gw = len(rows), len(rows[0])
    x0 = int(round(cx - gw * scale / 2))
    y0 = int(round(cy - gh * scale / 2))
    r, g, b = int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)
    for ry, row in enumerate(rows):
        for rx, ch in enumerate(row):
            if ch == "0":
                continue
            a = int(ch, 16) * 255 // 15
            for yy in range(scale):
                py = y0 + ry * scale + yy
                if not 0 <= py < H:
                    continue
                for xx in range(scale):
                    px = x0 + rx * scale + xx
                    if 0 <= px < W:
                        i = (py * W + px) * 4
                        out[i], out[i + 1], out[i + 2] = r, g, b
                        out[i + 3] = max(out[i + 3], a)


_LETTER_S = 7  # letter-mark subpixel scale (effective 7/3 ≈ 2.3, AA'd)


def _mark_w(tag: str, mark: str, scale: int) -> int:
    rows = _logo_rows(tag, mark)
    if rows:
        return len(rows[0])
    return -(-3 * _LETTER_S // _SS)


def _rows_ink_center(rows: tuple[str, ...]) -> tuple[float, float]:
    xs, ys = [], []
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "0":
                xs.append(x)
                ys.append(y)
    if not xs:
        return len(rows[0]) / 2, len(rows) / 2
    return (min(xs) + max(xs) + 1) / 2, (min(ys) + max(ys) + 1) / 2


def _draw_mark(out: bytearray, W: int, H: int, tag: str, mark: str,
               cx: float, cy: float, scale: int, rgb: tuple[float, float, float]) -> None:
    """Draw the tool identifier: a C/A letter, or the drawn logo symbol."""
    if mark != "letter":
        rows = _logo_rows(tag, mark)
        if rows:
            ix, iy = _rows_ink_center(rows)
            cx += len(rows[0]) / 2 - ix
            cy += len(rows) / 2 - iy
            _blit_rows(out, W, H, rows, cx, cy, 1, rgb)
            return
    tw, th, tbuf = _text_rgba(tag, _LETTER_S, rgb)
    _paste(out, W, tbuf, tw, th, int(round(cx - tw / 2)), int(round(cy - th / 2)))


def _hbar_rgba(bw: int, bh: int, frac: float,
               rgb: tuple[float, float, float]) -> bytearray:
    """A slim capsule progress bar (bw×bh RGBA, AA'd): rounded colored fill
    over a faint rounded track."""
    track, ta = (0.55, 0.55, 0.58), 0.34
    W, H = bw * _SS, bh * _SS
    r = H / 2.0
    fx = W * max(0.0, min(1.0, frac))

    def in_capsule(px: float, dy: float, x1: float) -> bool:
        if x1 <= 0:
            return False
        rr = min(r, x1 / 2)
        cx = min(max(px, rr), x1 - rr)
        return (px - cx) ** 2 + dy * dy <= rr * rr

    big = bytearray(W * H * 4)
    for y in range(H):
        dy = y + 0.5 - H / 2.0
        for x in range(W):
            px = x + 0.5
            if not in_capsule(px, dy, W):
                continue
            col, a = (rgb, 1.0) if in_capsule(px, dy, fx) else (track, ta)
            i = (y * W + x) * 4
            big[i] = int(col[0] * 255)
            big[i + 1] = int(col[1] * 255)
            big[i + 2] = int(col[2] * 255)
            big[i + 3] = int(a * 255)
    return _downsample(big, bw, bh)


def _ring_rgba(pct: float, rgb: tuple[float, float, float], size: int, ss: int = 3,
               top: str | None = None, bottom: str | None = None,
               center: str | None = None) -> bytearray:
    """One square ring gauge (size×size RGBA): faint track + colored arc, AA'd
    by ss×ss supersampling. Arc starts at 12 o'clock, fills clockwise. Optional
    `top`/`bottom` text are baked in the threshold color (e.g. tag + percent)."""
    frac = max(0.0, min(1.0, pct / 100.0))
    sw = sh = size * ss
    cx = cy = sw / 2.0
    r_out = sw * 0.43  # slightly inset so the gauge sits lighter among menu icons
    r_in = r_out - sw * 0.10  # thin Apple-style stroke
    track = (0.55, 0.55, 0.58)
    track_a = 0.34
    out = bytearray(size * size * 4)
    n = ss * ss
    for ty in range(size):
        for tx in range(size):
            sa = sr = sg = sb = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    dx = (tx * ss + sx + 0.5) - cx
                    dy = (ty * ss + sy + 0.5) - cy
                    d = math.hypot(dx, dy)
                    if not (r_in <= d <= r_out):
                        continue
                    ang = math.atan2(dx, -dy)  # 0 at top, clockwise
                    if ang < 0:
                        ang += 2 * math.pi
                    if ang / (2 * math.pi) <= frac:
                        col, a = rgb, 1.0
                    else:
                        col, a = track, track_a
                    sa += a
                    sr += col[0] * a
                    sg += col[1] * a
                    sb += col[2] * a
            if sa > 0:
                i = (ty * size + tx) * 4
                out[i] = int(sr / sa * 255)
                out[i + 1] = int(sg / sa * 255)
                out[i + 2] = int(sb / sa * 255)
                out[i + 3] = int(sa / n * 255)
    fs = max(1, size // 22)
    if center:
        # Largest subpixel scale whose glyph block fits inside the inner circle
        # — compare the block's diagonal (its corners reach furthest) to the
        # hole diameter, with fractional effective scales for AA.
        fit = 0.66 * size * 0.96  # inner hole diameter (r_in = 0.33·sw), padded
        fit_len = max(2, len(center))
        best = _SS
        for s in range(5 * _SS, _SS - 1, -1):
            gw = s * (4 * fit_len - 1) / _SS
            gh = 5 * s / _SS
            if math.hypot(gw, gh) <= fit:
                best = s
                break
        tw, th, tbuf = _text_rgba(center, best, rgb)
        _paste(out, size, tbuf, tw, th, (size - tw) // 2, (size - th) // 2)
    if top:
        _blit(out, size, size, top, size / 2, size * 0.35, fs, rgb)
    if bottom:
        _blit(out, size, size, bottom, size / 2, size * 0.64, fs, rgb)
    return out


def _compose_h(layers: list[bytearray], size: int, gap: int = 2) -> tuple[int, int, bytes]:
    """Concatenate square RGBA layers horizontally with a transparent gap."""
    w = len(layers) * size + (len(layers) - 1) * gap
    out = bytearray(w * size * 4)
    x0 = 0
    for lay in layers:
        for y in range(size):
            src = y * size * 4
            dst = (y * w + x0) * 4
            out[dst : dst + size * 4] = lay[src : src + size * 4]
        x0 += size + gap
    return w, size, bytes(out)


_MARK_SCALE = 2  # both the C/A letter and the logo are drawn at this pixel scale


def _cell_rgb(pct: float, stale: bool) -> tuple[float, float, float]:
    rgb = _hex_rgb(_pct_color(pct))
    if stale:
        rgb = tuple(c * 0.45 + 0.275 for c in rgb)  # blend 55% toward mid-gray
    return rgb


def _text_cell(text: str, rgb: tuple[float, float, float], h: int = 30,
               s: int = 10) -> tuple[int, int, bytearray]:
    tw, th, tbuf = _text_rgba(text, s, rgb)
    cell = bytearray(tw * h * 4)
    _paste(cell, tw, tbuf, tw, th, 0, (h - th) // 2)
    return tw, h, cell


# digit-size setting → subpixel text scale per style (heights ≈ 5·s/3 px)
_DIGIT_S = {
    "number": {"large": 13, "medium": 11, "small": 9},
    "bar": {"large": 11, "medium": 9, "small": 8},
}


def _gauge_cell(tag: str, pct: float, mark: str = "letter", ring: int = 30,
                stale: bool = False, digits: str = "large") -> tuple[int, int, bytearray]:
    """Ring style: the tool mark (letter/logo) left, a ring with % centered inside."""
    rgb = _cell_rgb(pct, stale)
    ring_buf = _ring_rgba(pct, rgb, ring, center=f"{pct:.0f}")
    mark_w = _mark_w(tag, mark, _MARK_SCALE)
    gap = 2
    w, h = mark_w + gap + ring, ring
    cell = bytearray(w * h * 4)
    _draw_mark(cell, w, h, tag, mark, mark_w / 2, h / 2, _MARK_SCALE, rgb)
    x0 = mark_w + gap
    for y in range(ring):  # paste ring on the right
        src = y * ring * 4
        dst = (y * w + x0) * 4
        cell[dst:dst + ring * 4] = ring_buf[src:src + ring * 4]
    return w, h, cell


def _number_cell(tag: str, pct: float, mark: str = "letter", h: int = 30,
                 stale: bool = False, digits: str = "large") -> tuple[int, int, bytearray]:
    """Number style: the tool mark left, a big threshold-colored % right. No ring."""
    rgb = _cell_rgb(pct, stale)
    # large (s=13) ≈ 22px digits — the cap height of 13pt menu-bar text
    s = _DIGIT_S["number"].get(digits, 13)
    tw, th, tbuf = _text_rgba(f"{pct:.0f}", s, rgb)
    mark_w = _mark_w(tag, mark, _MARK_SCALE)
    gap = 4
    w = mark_w + gap + tw
    cell = bytearray(w * h * 4)
    _draw_mark(cell, w, h, tag, mark, mark_w / 2, h / 2, _MARK_SCALE, rgb)
    _paste(cell, w, tbuf, tw, th, mark_w + gap, (h - th) // 2)
    return w, h, cell


def _bar_cell(tag: str, pct: float, mark: str = "letter", h: int = 30,
              stale: bool = False, digits: str = "large") -> tuple[int, int, bytearray]:
    """Bar style: the tool mark, a slim capsule fill bar, then the %."""
    rgb = _cell_rgb(pct, stale)
    s = _DIGIT_S["bar"].get(digits, 11)
    tw, th, tbuf = _text_rgba(f"{pct:.0f}", s, rgb)
    bar_w, bar_h = 36, 7
    mark_w = _mark_w(tag, mark, _MARK_SCALE)
    gap = 4
    w = mark_w + gap + bar_w + gap + tw
    cell = bytearray(w * h * 4)
    _draw_mark(cell, w, h, tag, mark, mark_w / 2, h / 2, _MARK_SCALE, rgb)
    bx = mark_w + gap
    _paste(cell, w, _hbar_rgba(bar_w, bar_h, pct / 100.0, rgb), bar_w, bar_h,
           bx, (h - bar_h) // 2)
    _paste(cell, w, tbuf, tw, th, bx + bar_w + gap, (h - th) // 2)
    return w, h, cell


_CELL_BUILDERS = {"ring": _gauge_cell, "number": _number_cell, "bar": _bar_cell}


def _compose_cells(cells: list[tuple[int, int, bytearray]],
                   gap: int = 8, pad: int = 3) -> tuple[int, int, bytearray]:
    """Lay square/rect cells in a row, centered vertically; pad keeps the
    first/last glyph off the menu-bar edge. Returns (w, h, RGBA)."""
    h = max(c[1] for c in cells)
    w = sum(c[0] for c in cells) + gap * (len(cells) - 1) + 2 * pad
    out = bytearray(w * h * 4)
    x = pad
    for cw, ch, cbuf in cells:
        yoff = (h - ch) // 2
        for y in range(ch):
            src = y * cw * 4
            dst = ((y + yoff) * w + x) * 4
            out[dst:dst + cw * 4] = cbuf[src:src + cw * 4]
        x += cw + gap
    return w, h, out


def bar_image(items: list[dict], style: str = "ring", mark: str = "letter",
              prefix: str = "", digits: str = "large") -> str | None:
    """Base64 PNG: a row of per-tool cells in the chosen image style.
    items: [{"tag": "C", "pct": 73.0, "stale": False}, ...]"""
    build = _CELL_BUILDERS.get(style)
    if not build or not items:
        return None
    cells = [build(it["tag"], it["pct"], mark=mark, stale=it.get("stale", False),
                   digits=digits)
             for it in items]
    if prefix:
        worst = max((it.get("pct") or 0) for it in items)
        cells.insert(0, _text_cell(prefix, _hex_rgb(_pct_color(worst))))
    w, h, out = _compose_cells(cells)
    return base64.b64encode(_png_bytes(w, h, out)).decode("ascii")


# window key → (codex field, claude field, minutes, bar label)
_WIN_MAP = {
    "5h": ("primary", "five_hour", 300, "5h"),
    "weekly": ("secondary", "seven_day", 10080, "wk"),
}


def _window_line(label: str, w: dict, wmin: int) -> str:
    pct = w.get("used_percent", 0) or 0
    s = f"{_ring(pct)} {label}: {pct:.0f}% · resets {_fmt_reset(w.get('resets_at'))}"
    if pct >= 99.5:
        s += " · at cap"
    else:
        pr = _project(pct, w.get("window_minutes") or wmin, w.get("resets_at"))
        if pr:
            if pr["eta_min"] is not None:
                s += f" · ≈cap in {_fmt_reset(time.time() + pr['eta_min'] * 60)}"
            elif pr["proj"] > pct + 1:
                s += f" · proj {pr['proj']:.0f}%"
    return f"{s} | font=Menlo size=12 color={_menu_pct_color(pct)}"


def swiftbar_output(claude: dict, codex: dict, cw: dict) -> str:
    """SwiftBar/xbar plugin format: title line, '---', then dropdown items."""
    disp = display_cfg()
    self_path = str(Path(__file__).resolve())
    tags = {"codex": "C", "claude": "A"}
    labels = {"codex": "Codex", "claude": "Claude"}

    def win_pct(tool: str, wkey: str):
        ci, ai, *_ = _WIN_MAP[wkey]
        src = codex if tool == "codex" else cw
        if not src.get("found"):
            return None
        w = src.get(ci if tool == "codex" else ai)
        return w.get("used_percent") if isinstance(w, dict) else None

    active = [t for t in disp["tools"]
              if (t == "codex" and codex.get("found")) or (t == "claude" and cw.get("found"))]
    wins = disp["windows"] or ["5h"]

    # Codex windows come from the newest rollout log; if you haven't run Codex in
    # a while that snapshot is stale (Claude spend/windows are recomputed each run).
    codex_ep = _iso_to_epoch(codex.get("timestamp")) if codex.get("found") else None
    codex_age = (time.time() - codex_ep) if codex_ep else None
    codex_stale = codex_age is not None and codex_age > STALE_SECS

    worst = 0.0
    for t in active:
        for wk in wins:
            p = win_pct(t, wk)
            if p is not None:
                worst = max(worst, p)
    title_color = _pct_color(worst)

    spend_txt = ""
    spend_img_txt = ""
    if disp["show_spend"] and claude.get("found"):
        rng = disp["spend_range"]
        val = claude.get("spend", {}).get(rng, claude.get("cost", 0.0))
        spend_txt = {"today": "$", "d7": "7d $", "d30": "30d $"}.get(rng, "$") + f"{val:.0f}"
        spend_img_txt = {"today": "$", "d7": "7D$", "d30": "30D$"}.get(rng, "$") + f"{val:.0f}"

    if disp["style"] in _IMAGE_STYLES:
        # one cell per active tool (tagged C/A) for the first selected window
        items = [{"tag": tags[t], "pct": win_pct(t, wins[0]),
                  "stale": t == "codex" and codex_stale}
                 for t in active if win_pct(t, wins[0]) is not None]
        img = None
        try:
            img = bar_image(items, disp["style"], disp.get("mark", "letter"), spend_img_txt,
                            disp.get("digits", "large")) if items else None
        except Exception:
            img = None
        if img:
            # Keep image styles as image-only: text + image is flaky in SwiftBar.
            title = f" | image={img} size=13 color={title_color}"
        else:
            title = f"{spend_txt or 'Usage Bar'} | size=13 color={title_color}"
    else:
        segs = []
        for t in active:
            bits = []
            for wk in wins:
                p = win_pct(t, wk)
                if p is None:
                    continue
                wl = _WIN_MAP[wk][3]
                bits.append(f"{_ring(p)} {wl} {p:.0f}%" if disp["style"] == "harvey"
                            else f"{wl} {p:.0f}%")
            if bits:
                segs.append(f"{labels[t]} " + " ".join(bits))
        txt = " · ".join(([spend_txt] if spend_txt else []) + segs)
        title = f"{txt or 'Usage Bar'} | size=13 color={title_color}"
    lines = [title, "---"]

    def click(label: str, *args: str, term: str = "false") -> str:
        parts = [f"{label} | bash=/usr/bin/python3", f"param1={self_path}"]
        parts += [f"param{i}={a}" for i, a in enumerate(args, start=2)]
        parts.append(f"terminal={term} refresh=true")
        return " ".join(parts)

    # ── Claude spend (today / 7d / 30d) ──
    lines.append(f"Claude — spend | size=11 color={_MENU_MUTED}")
    if claude.get("found"):
        sp = claude.get("spend", {})
        lines.append(
            f"today ${sp.get('today', 0):.2f}  ·  7d ${sp.get('d7', 0):.2f}  ·  30d ${sp.get('d30', 0):.2f} "
            f"| font=Menlo size=12 color={_MENU_TEXT}"
        )
        for model, a in sorted(claude["models"].items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(f"  {model}: ${a['cost']:.2f} today | font=Menlo size=11 color={_MENU_MUTED}")
        if claude.get("dups_skipped"):
            lines.append(f"  {claude['dups_skipped']:,} duplicate lines skipped | size=10 color={_MENU_MUTED}")
    else:
        lines.append(f"no usage | size=11 color={_MENU_MUTED}")

    # ── Claude windows ──
    lines.append("---")
    lines.append(f"Claude (A) — rate-limit windows | size=11 color={_MENU_MUTED}")
    if not cw.get("enabled"):
        lines.append(f"Off — shows spend only | size=11 color={_MENU_MUTED}")
        lines.append(click("Enable… (reads your Claude token, shows disclosure)",
                           "--enable-claude", term="true"))
    elif cw.get("found"):
        for label, key, wmin in (("5h", "five_hour", 300), ("weekly", "seven_day", 10080),
                                 ("weekly·opus", "seven_day_opus", 10080)):
            w = cw.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                lines.append(_window_line(label, w, wmin))
        lines.append(click("Disable Claude windows", "--disable-claude"))
    else:
        lines.append(f"{cw.get('error', 'unavailable')} | size=11 color={_MENU_ERR}")

    # ── Codex windows ──
    lines.append("---")
    codex_hdr = "Codex (C) — rate-limit windows"
    if codex.get("found") and codex_stale:
        codex_hdr += f" · stale ({_fmt_reset(time.time() + codex_age)} old)"
    lines.append(f"{codex_hdr} | size=11 color={_MENU_MUTED}")
    if codex.get("found"):
        if codex.get("plan_type"):
            lines.append(f"plan: {codex['plan_type']} | size=11 color={_MENU_MUTED}")
        for label, key, wmin in (("5h", "primary", 300), ("weekly", "secondary", 10080)):
            w = codex.get(key) or {}
            if w:
                lines.append(_window_line(label, w, wmin))
    else:
        lines.append(f"no rate-limit data | size=11 color={_MENU_MUTED}")

    # ── Settings — one submenu; each choice is a direct-select radio.
    #    (macOS closes any menu on click — NSMenu behavior SwiftBar can't
    #    override — so every item applies and refreshes in a single click.)
    lines.append("---")
    lines.append("Settings")

    def radio(title: str, cur: str, options, *set_args: str) -> None:
        lines.append(f"-- {title}: {cur}")
        for lbl, val in options:
            dot = "●" if cur == val else "○"
            lines.append("---- " + click(f"{dot} {lbl}", *set_args, val))

    radio("Style", disp["style"], [(s, s) for s in _DISPLAY_CHOICES["style"]],
          "--set-display", "style")
    mark = disp.get("mark", "letter")
    radio("Tool mark", mark,
          [("letter — C / A", "letter"), ("logo — blossom / A", "logo"),
           ("spark — blossom / Claude spark", "spark")],
          "--set-display", "mark")
    cur_spend = disp["spend_range"] if disp["show_spend"] else "hidden"
    radio("Spend in bar", cur_spend,
          [("hidden", "hidden"), ("today", "today"), ("7d", "d7"), ("30d", "d30")],
          "--set-spend")
    radio("Digit size (number/bar)", disp.get("digits", "large"),
          [(d, d) for d in _DISPLAY_CHOICES["digits"]],
          "--set-display", "digits")

    for t in ("codex", "claude"):
        lines.append("-- " + click(
            f"Tool {labels[t]}: {'✓ shown' if t in disp['tools'] else '✗ hidden'}",
            "--toggle-tool", t))
    for wk in ("5h", "weekly"):
        lines.append("-- " + click(
            f"Bar window {wk}: {'✓' if wk in disp['windows'] else '✗'}",
            "--toggle-window", wk))

    ncfg = notify_cfg()
    ncur = f"≥{int(ncfg['threshold'])}%" if ncfg["enabled"] else "off"
    radio("Notify near cap", ncur,
          [("off", "off")] + [(f"≥{th}%", f"≥{th}%") for th in NOTIFY_THRESHOLDS],
          "--set-notify")

    lines.append("---")
    lines.append("Refresh | refresh=true")
    return "\n".join(lines)


def _arg_value(argv: list[str], flag: str, count: int = 1) -> list[str] | None:
    """Return values after a flag, or None if the command is malformed."""
    if flag not in argv:
        return None
    i = argv.index(flag) + 1
    vals = argv[i:i + count]
    if len(vals) != count or any(v.startswith("--") for v in vals):
        print(f"Usage Bar: {flag} expects {count} value(s)", file=sys.stderr)
        return None
    return vals


def doctor_report() -> str:
    """Human-readable setup diagnostics. No network requests."""
    lines = [f"Usage Bar {VERSION} doctor", ""]

    def check(label: str, ok: bool, detail: str) -> None:
        mark = "ok" if ok else "missing"
        lines.append(f"{mark:7} {label}: {detail}")

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    check("script", Path(__file__).exists(), str(Path(__file__).resolve()))
    check("config", CONFIG_FILE.exists(), str(CONFIG_FILE))
    check("codex logs", CODEX_SESSIONS.exists(), str(CODEX_SESSIONS))
    check("claude logs", CLAUDE_PROJECTS.exists(), str(CLAUDE_PROJECTS))
    check("claude windows", claude_enabled(), "enabled" if claude_enabled() else "disabled")

    plugin = HOME / "Library" / "Application Support" / "SwiftBar" / "Plugins" / "usage-bar.30s.sh"
    if plugin.exists():
        detail = str(plugin)
        try:
            if plugin.is_symlink():
                detail += f" -> {plugin.resolve()}"
        except OSError:
            pass
        check("swiftbar plugin", True, detail)
    else:
        check("swiftbar plugin", False, str(plugin))

    readable = os.access(Path(__file__), os.R_OK)
    check("readable", readable, "usage.py" if readable else "usage.py is not readable")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if "--doctor" in argv:
        print(doctor_report())
        return 0

    # Consent management for the Claude-windows feature.
    if "--enable-claude" in argv:
        print(CLAUDE_CONSENT_TEXT)
        set_claude_enabled(True)
        print(f"Enabled. Recorded acceptance in {CONFIG_FILE}.")
        return 0
    if "--disable-claude" in argv:
        set_claude_enabled(False)
        print("Claude rate-limit windows disabled. Claude shows spend only.")
        return 0
    # Display settings (used by the clickable dropdown items; also usable by hand).
    if "--cycle-style" in argv:
        cycle_display("style")
        return 0
    if "--cycle-spend-range" in argv:
        cycle_display("spend_range")
        return 0
    if "--toggle-spend" in argv:
        toggle_display_bool("show_spend")
        return 0
    if "--toggle-tool" in argv:
        vals = _arg_value(argv, "--toggle-tool")
        if vals is None:
            return 2
        toggle_display_list("tools", vals[0])
        return 0
    if "--toggle-window" in argv:
        vals = _arg_value(argv, "--toggle-window")
        if vals is None:
            return 2
        toggle_display_list("windows", vals[0])
        return 0
    if "--toggle-notify" in argv:
        cfg = _load_config()
        cfg.setdefault("notify", {})["enabled"] = not notify_cfg()["enabled"]
        _save_config(cfg)
        return 0
    if "--cycle-notify-threshold" in argv:
        cur = notify_cfg()["threshold"]
        nxt = (NOTIFY_THRESHOLDS[(NOTIFY_THRESHOLDS.index(cur) + 1) % len(NOTIFY_THRESHOLDS)]
               if cur in NOTIFY_THRESHOLDS else NOTIFY_THRESHOLDS[0])
        cfg = _load_config()
        cfg.setdefault("notify", {})["threshold"] = nxt
        _save_config(cfg)
        return 0
    # Direct-select handlers used by the radio submenus (also usable by hand).
    if "--set-display" in argv:
        vals = _arg_value(argv, "--set-display", 2)
        if vals is None:
            return 2
        key, val = vals
        if val in _DISPLAY_CHOICES.get(key, []):
            set_display(key, val)
        return 0
    if "--set-spend" in argv:
        vals = _arg_value(argv, "--set-spend")
        if vals is None:
            return 2
        v = vals[0]
        if v == "hidden":
            set_display("show_spend", False)
        elif v in _DISPLAY_CHOICES["spend_range"]:
            set_display("show_spend", True)
            set_display("spend_range", v)
        return 0
    if "--set-notify" in argv:
        vals = _arg_value(argv, "--set-notify")
        if vals is None:
            return 2
        v = vals[0]
        cfg = _load_config()
        n = cfg.setdefault("notify", {})
        if v == "off":
            n["enabled"] = False
        else:
            digits = "".join(c for c in v if c.isdigit())
            if digits:
                n["enabled"] = True
                n["threshold"] = int(digits)
        _save_config(cfg)
        return 0

    claude = claude_spend()
    codex = codex_rate_limits()
    cw = claude_windows()
    if "--json" in argv:
        print(json.dumps({"claude": claude, "codex": codex, "claude_windows": cw}, indent=2))
    elif "--swiftbar" in argv:
        check_notifications(codex, cw)  # only the periodic run alerts
        print(swiftbar_output(claude, codex, cw))
    elif "--full" in argv:
        print(full_report(claude, codex, cw))
    else:
        print(line_summary(claude, codex, cw))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
