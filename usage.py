#!/usr/bin/env python3
"""Usage Bar v0 — Claude Code + Codex usage at a glance.

Reads local logs only (no network):
  - Claude: ~/.claude/projects/**/*.jsonl  -> today's token spend in $
  - Codex:  ~/.codex/sessions/**/rollout-*.jsonl -> live rate-limit windows

Output (default): one line for a menu bar.
  🟣 Claude $4.20 · 🟢 Codex 5h 12% · wk 41%

Flags:
  --full   multi-line breakdown (per-window resets, today's tokens)
  --json   machine-readable dump
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def claude_today() -> dict:
    """Sum today's Claude token usage and price it. Returns {} on no data."""
    today = _local_today()
    # One assistant response can be written as many JSONL lines that share
    # (message.id, requestId): multiple content blocks repeat the usage, and
    # streaming snapshots grow output_tokens 2 -> 2 -> N. It's also copied
    # verbatim across files on resume/branch. So collapse to one record per key
    # and keep the line with the MAX output (the final cumulative snapshot) —
    # first-wins would keep a partial and undercount; summing would over-count.
    best: dict[tuple, dict] = {}  # (mid, rid) -> {model, fields...}
    unkeyed: list[dict] = []      # lines with no id at all -> can't dedup, keep each
    dup_count = 0
    if not CLAUDE_PROJECTS.exists():
        return {"cost": 0.0, "models": {}, "found": False}

    for jf in CLAUDE_PROJECTS.rglob("*.jsonl"):
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
                    ts = rec.get("timestamp")
                    if not ts or _ts_to_local_date(ts) != today:
                        continue
                    cc = usage.get("cache_creation") or {}
                    cw_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
                    cw_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
                    cc_total = usage.get("cache_creation_input_tokens", 0) or 0
                    if cw_5m + cw_1h == 0 and cc_total:
                        cw_5m = cc_total  # no breakdown -> assume 5m
                    rec_fields = {
                        "model": msg.get("model") or "unknown",
                        "input": usage.get("input_tokens", 0) or 0,
                        "output": usage.get("output_tokens", 0) or 0,
                        "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                        "cw_5m": cw_5m,
                        "cw_1h": cw_1h,
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

    by_model: dict[str, dict] = {}
    for rec_fields in list(best.values()) + unkeyed:
        acc = by_model.setdefault(
            rec_fields["model"],
            {"input": 0, "output": 0, "cache_read": 0, "cw_5m": 0, "cw_1h": 0},
        )
        for f in ("input", "output", "cache_read", "cw_5m", "cw_1h"):
            acc[f] += rec_fields[f]

    total = 0.0
    priced = {}
    for model, a in by_model.items():
        in_rate, out_rate = rate_for(model)
        cost = (
            a["input"] * in_rate
            + a["output"] * out_rate
            + a["cache_read"] * in_rate * 0.1
            + a["cw_5m"] * in_rate * 1.25
            + a["cw_1h"] * in_rate * 2.0
        ) / 1_000_000
        total += cost
        priced[model] = {**a, "cost": cost}
    return {"cost": total, "models": priced, "found": bool(by_model), "dups_skipped": dup_count}


def codex_rate_limits(scan: int = 12) -> dict:
    """Latest rate-limit snapshot across the most recently modified Codex sessions."""
    if not CODEX_SESSIONS.exists():
        return {"found": False}
    files = sorted(
        CODEX_SESSIONS.rglob("rollout-*.jsonl"),
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
    out = [line_summary(claude, codex, cw), ""]
    out.append("Claude — today's spend")
    if claude.get("found"):
        for model, a in sorted(claude["models"].items(), key=lambda kv: -kv[1]["cost"]):
            out.append(
                f"  {model}: ${a['cost']:.3f}  "
                f"(in {a['input']:,} / out {a['output']:,} / cache_read {a['cache_read']:,})"
            )
        out.append(f"  total: ${claude['cost']:.2f}")
        if claude.get("dups_skipped"):
            out.append(f"  ({claude['dups_skipped']:,} duplicate entries skipped)")
    else:
        out.append("  no usage today")
    out.append("")
    out.append("Claude — rate-limit windows")
    if not cw.get("enabled"):
        out.append("  off — enable with: usage.py --enable-claude")
    elif cw.get("found"):
        for label, key in (("5h", "five_hour"), ("weekly", "seven_day"), ("weekly·opus", "seven_day_opus")):
            w = cw.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                out.append(
                    f"  {label}: {w['used_percent']:.0f}% used"
                    f"  · resets in {_fmt_reset(w.get('resets_at'))}"
                )
    else:
        out.append(f"  {cw.get('error', 'unavailable')}")
    out.append("")
    out.append("Codex — rate-limit windows")
    if codex.get("found"):
        plan = codex.get("plan_type")
        if plan:
            out.append(f"  plan: {plan}")
        for label, key in (("5h", "primary"), ("weekly", "secondary")):
            w = codex.get(key) or {}
            if w:
                out.append(
                    f"  {label}: {w.get('used_percent', 0):.0f}% used"
                    f"  · resets in {_fmt_reset(w.get('resets_at'))}"
                )
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


# ── Ring gauge: an anti-aliased PNG progress ring, drawn with stdlib only ──
import base64
import math
import struct
import subprocess
import urllib.error
import urllib.request
import zlib


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


def _ring_rgba(pct: float, rgb: tuple[float, float, float], size: int, ss: int = 3,
               top: str | None = None, bottom: str | None = None) -> bytearray:
    """One square ring gauge (size×size RGBA): faint track + colored arc, AA'd
    by ss×ss supersampling. Arc starts at 12 o'clock, fills clockwise. Optional
    `top`/`bottom` text are baked in the threshold color (e.g. tag + percent)."""
    frac = max(0.0, min(1.0, pct / 100.0))
    sw = sh = size * ss
    cx = cy = sw / 2.0
    r_out = sw * 0.46
    r_in = r_out - sw * 0.17
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


def gauges_image(items: list[dict], size: int = 26) -> str | None:
    """Base64 PNG: a row of ring gauges, each baked with a tag + percent.
    items: [{"tag": "C", "pct": 73.0}, ...]"""
    layers = []
    for it in items:
        pct = it["pct"]
        layers.append(_ring_rgba(
            pct, _hex_rgb(_pct_color(pct)), size,
            top=it.get("tag"), bottom=f"{pct:.0f}",
        ))
    if not layers:
        return None
    w, h, rgba = _compose_h(layers, size, gap=3)
    return base64.b64encode(_png_bytes(w, h, rgba)).decode("ascii")


def swiftbar_output(claude: dict, codex: dict, cw: dict) -> str:
    """SwiftBar/xbar plugin format: title line, '---', then dropdown items."""
    # Title color = the single hottest window across both tools.
    worst = 0.0
    for w in (codex.get("primary"), codex.get("secondary")):
        if isinstance(w, dict):
            worst = max(worst, w.get("used_percent", 0) or 0)
    if cw.get("found"):
        for w in (cw.get("five_hour"), cw.get("seven_day")):
            if isinstance(w, dict):
                worst = max(worst, w.get("used_percent", 0) or 0)
    title_color = _pct_color(worst)

    # Menu-bar gauges: one labeled 5h ring per tool (C = Codex, A = Claude).
    items = []
    cp = (codex.get("primary") or {}).get("used_percent") if codex.get("found") else None
    if cp is not None:
        items.append({"tag": "C", "pct": cp})
    af = (cw.get("five_hour") or {}).get("used_percent") if cw.get("found") else None
    if af is not None:
        items.append({"tag": "A", "pct": af})
    img = None
    try:
        img = gauges_image(items) if items else None
    except Exception:
        img = None

    spend = f"${claude['cost']:.0f}" if claude.get("found") else "—"
    if img:
        title = f"{spend} | image={img} size=13 color={title_color}"
    else:
        title = f"{line_summary(claude, codex, cw)} | size=13 color={title_color}"
    lines = [title, "---"]

    lines.append("Claude — today's spend | size=11 color=#888888")
    if claude.get("found"):
        for model, a in sorted(claude["models"].items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(f"{model}: ${a['cost']:.2f} | font=Menlo size=12")
        lines.append(f"total: ${claude['cost']:.2f} | font=Menlo size=12 color=#ffffff")
        if claude.get("dups_skipped"):
            lines.append(f"{claude['dups_skipped']:,} duplicate lines skipped | size=10 color=#888888")
    else:
        lines.append("no usage today | size=11 color=#888888")

    lines.append("---")
    lines.append("Claude (A) — rate-limit windows | size=11 color=#888888")
    if not cw.get("enabled"):
        lines.append("Off — shows spend only | size=11 color=#888888")
        lines.append("Enable (reads your Claude token): | size=11 color=#888888")
        lines.append("usage.py --enable-claude | font=Menlo size=11 color=#888888")
    elif cw.get("found"):
        for label, key in (("5h", "five_hour"), ("weekly", "seven_day"), ("weekly·opus", "seven_day_opus")):
            w = cw.get(key)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                pct = w["used_percent"]
                lines.append(
                    f"{_ring(pct)} {label}: {pct:.0f}% · resets in {_fmt_reset(w.get('resets_at'))} "
                    f"| font=Menlo size=12 color={_pct_color(pct)}"
                )
    else:
        lines.append(f"{cw.get('error', 'unavailable')} | size=11 color=#e67e22")

    lines.append("---")
    lines.append("Codex (C) — rate-limit windows | size=11 color=#888888")
    if codex.get("found"):
        plan = codex.get("plan_type")
        if plan:
            lines.append(f"plan: {plan} | size=11 color=#888888")
        for label, key in (("5h", "primary"), ("weekly", "secondary")):
            w = codex.get(key) or {}
            if w:
                pct = w.get("used_percent", 0) or 0
                lines.append(
                    f"{_ring(pct)} {label}: {pct:.0f}% · resets in {_fmt_reset(w.get('resets_at'))} "
                    f"| font=Menlo size=12 color={_pct_color(pct)}"
                )
    else:
        lines.append("no rate-limit data | size=11 color=#888888")

    lines.append("---")
    lines.append("Refresh | refresh=true")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
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

    claude = claude_today()
    codex = codex_rate_limits()
    cw = claude_windows()
    if "--json" in argv:
        print(json.dumps({"claude": claude, "codex": codex, "claude_windows": cw}, indent=2))
    elif "--swiftbar" in argv:
        print(swiftbar_output(claude, codex, cw))
    elif "--full" in argv:
        print(full_report(claude, codex, cw))
    else:
        print(line_summary(claude, codex, cw))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
