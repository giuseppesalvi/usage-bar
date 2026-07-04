import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import usage


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


class ClaudeSpendTests(unittest.TestCase):
    def test_dedup_keeps_max_output_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            projects = Path(td)
            ts = datetime.now(timezone.utc).isoformat()
            write_jsonl(projects / "proj" / "session.jsonl", [
                {
                    "timestamp": ts,
                    "requestId": "req-1",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                    },
                },
                {
                    "timestamp": ts,
                    "requestId": "req-1",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 10, "output_tokens": 100},
                    },
                },
            ])

            result = usage.claude_spend(projects)

            self.assertTrue(result["found"])
            self.assertEqual(result["dups_skipped"], 1)
            model = result["models"]["claude-sonnet-4-5"]
            self.assertEqual(model["input"], 10)
            self.assertEqual(model["output"], 100)
            self.assertAlmostEqual(result["cost"], (10 * 3.0 + 100 * 15.0) / 1_000_000)

    def test_missing_claude_logs_returns_empty_spend(self):
        with tempfile.TemporaryDirectory() as td:
            result = usage.claude_spend(Path(td) / "missing")

        self.assertFalse(result["found"])
        self.assertEqual(result["cost"], 0.0)
        self.assertEqual(result["spend"], {"today": 0.0, "d7": 0.0, "d30": 0.0})


class CodexRateLimitTests(unittest.TestCase):
    def test_ignores_premium_records_and_reads_windowed_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            write_jsonl(sessions / "2026" / "07" / "05" / "rollout-test.jsonl", [
                {
                    "timestamp": "2026-07-05T09:00:00.000Z",
                    "payload": {
                        "rate_limits": {
                            "limit_id": "premium",
                            "primary": None,
                            "secondary": None,
                        }
                    },
                },
                {
                    "timestamp": "2026-07-05T09:01:00.000Z",
                    "payload": {
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {
                                "used_percent": 42.0,
                                "window_minutes": 300,
                                "resets_at": 1783213386,
                            },
                            "secondary": {
                                "used_percent": 73.0,
                                "window_minutes": 10080,
                                "resets_at": 1783501303,
                            },
                            "plan_type": "prolite",
                        }
                    },
                },
            ])

            result = usage.codex_rate_limits(sessions_dir=sessions)

            self.assertTrue(result["found"])
            self.assertEqual(result["limit_id"], "codex")
            self.assertEqual(result["primary"]["used_percent"], 42.0)
            self.assertEqual(result["secondary"]["used_percent"], 73.0)


class CliAndOutputTests(unittest.TestCase):
    def test_malformed_setting_command_returns_usage_error(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(usage.main(["--set-display", "style"]), 2)

    def test_swiftbar_output_contains_expected_sections(self):
        claude = {
            "found": True,
            "cost": 1.23,
            "models": {
                "claude-sonnet-4-5": {
                    "input": 1,
                    "output": 2,
                    "cache_read": 3,
                    "cw_5m": 0,
                    "cw_1h": 0,
                    "cost": 1.23,
                }
            },
            "dups_skipped": 0,
            "spend": {"today": 1.23, "d7": 4.56, "d30": 7.89},
        }
        codex = {
            "found": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "plan_type": "prolite",
            "primary": {"used_percent": 38.0, "window_minutes": 300, "resets_at": 1783213386},
            "secondary": {"used_percent": 73.0, "window_minutes": 10080, "resets_at": 1783501303},
        }
        cw = {
            "enabled": True,
            "found": True,
            "five_hour": {"used_percent": 46.0, "window_minutes": 300, "resets_at": 1783213386},
            "seven_day": {"used_percent": 13.0, "window_minutes": 10080, "resets_at": 1783501303},
        }
        display = {
            "style": "text",
            "mark": "letter",
            "tools": ["codex", "claude"],
            "windows": ["5h"],
            "show_spend": True,
            "spend_range": "today",
        }

        with mock.patch.object(usage, "display_cfg", return_value=display):
            out = usage.swiftbar_output(claude, codex, cw)

        self.assertIn("Claude — spend", out)
        self.assertIn("Claude (A) — rate-limit windows", out)
        self.assertIn("Codex (C) — rate-limit windows", out)
        self.assertIn("Settings", out)


if __name__ == "__main__":
    unittest.main()
