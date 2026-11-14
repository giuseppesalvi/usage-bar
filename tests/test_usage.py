import contextlib
import io
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


class ClaudeSpendCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "cache" / "spend.json"
        self.data = {
            "found": True, "cost": 1.23,
            "models": {"fixture-model": {"cost": 1.23, "input": 10,
                                          "output": 20, "cache_read": 0}},
            "spend": {"today": 1.23, "d7": 4.56, "d30": 7.89},
        }
        for patcher in (
            mock.patch.object(usage, "CLAUDE_SPEND_CACHE", self.cache),
            mock.patch.object(usage, "_local_today", return_value="2026-11-14"),
            mock.patch.object(usage.time, "time", return_value=1000),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hidden_spend_survives_cached_swiftbar_refresh(self):
        display = {**usage.DISPLAY_DEFAULTS, "style": "text", "show_spend": False}
        with mock.patch.object(usage, "claude_spend", return_value=self.data) as scan, \
             mock.patch.object(usage, "display_cfg", return_value=display), \
             mock.patch.object(usage, "codex_rate_limits", return_value={"found": False}), \
             mock.patch.object(usage, "claude_windows", return_value={"enabled": False}), \
             mock.patch.object(usage, "check_notifications"), \
             mock.patch.object(usage, "notify_cfg", return_value={"enabled": False, "threshold": 90}):
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(usage.main(["--swiftbar"]), 0)
                title, dropdown = output.getvalue().split("\n", 1)
                self.assertNotIn("$", title)
                self.assertIn("today $1.23  ·  7d $4.56  ·  30d $7.89", dropdown)
                self.assertIn("fixture-model: $1.23 today", dropdown)
            scan.assert_called_once_with()

    def test_expired_midnight_future_and_old_version_caches_refresh(self):
        with mock.patch.object(usage, "claude_spend", return_value=self.data) as scan:
            usage.cached_claude_spend()
            original = json.loads(self.cache.read_text())
            for changes in ({"fetched_at": 1000 - usage.CLAUDE_SPEND_CACHE_TTL},
                            {"date": "2026-11-13"}, {"fetched_at": 1001},
                            {"version": "old"}):
                with self.subTest(changes=changes):
                    self.cache.write_text(json.dumps({**original, **changes}))
                    scan.reset_mock()
                    self.assertEqual(usage.cached_claude_spend(), self.data)
                    scan.assert_called_once_with()

    def test_corrupt_caches_refresh(self):
        self.cache.parent.mkdir()
        for contents in ("{", "null", "[]", '{}',
                         json.dumps({"version": usage.VERSION, "date": "2026-11-14",
                                     "fetched_at": 1000, "data": {"found": True}})):
            with self.subTest(contents=contents), \
                 mock.patch.object(usage, "claude_spend", return_value=self.data) as scan:
                self.cache.write_text(contents)
                self.assertEqual(usage.cached_claude_spend(), self.data)
                scan.assert_called_once_with()

    def test_unwritable_cache_still_returns_fresh_spend(self):
        self.cache.parent.write_text("blocks directory creation")
        with mock.patch.object(usage, "claude_spend", return_value=self.data):
            self.assertEqual(usage.cached_claude_spend(), self.data)

    def test_cli_reports_bypass_cache(self):
        with mock.patch.object(usage, "cached_claude_spend", side_effect=AssertionError("cache used")), \
             mock.patch.object(usage, "claude_spend", return_value=self.data) as scan, \
             mock.patch.object(usage, "codex_rate_limits", return_value={"found": False}), \
             mock.patch.object(usage, "claude_windows", return_value={"enabled": False}):
            for args in ([], ["--full"], ["--json"], ["--swiftbar", "--json"]):
                with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(usage.main(args), 0)
            self.assertEqual(scan.call_count, 4)


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
