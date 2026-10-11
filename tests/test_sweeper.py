"""Smoke tests for routine-sweeper. All fixture-based; stdlib only."""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from routine_sweeper import cli, config, scan, sweep  # noqa: E402


def line(rec):
    return json.dumps(rec)


def user_msg(content, **extra):
    rec = {"type": "user", "message": {"content": content}}
    rec.update(extra)
    return line(rec)


def kickoff(name):
    return user_msg('<scheduled-task name="%s" file="/tmp/x">' % name)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="routine-sweeper-test-")
        self.claude_dir = os.path.join(self.tmp, ".claude")
        self.proj = os.path.join(self.claude_dir, "projects", "-tmp-proj")
        os.makedirs(self.proj)
        os.makedirs(os.path.join(self.claude_dir, "sessions"))
        self._now = time.time()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_session(self, sid, lines, age_days=10, sidecar=False):
        path = os.path.join(self.proj, sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        mtime = self._now - age_days * 86400
        os.utime(path, (mtime, mtime))
        if sidecar:
            os.makedirs(os.path.join(self.proj, sid))
            with open(os.path.join(self.proj, sid, "tool-output.txt"), "w") as fh:
                fh.write("x")
        return path

    def add_registry(self, sid, pid=None, entrypoint="claude-desktop"):
        path = os.path.join(self.claude_dir, "sessions", "%s.json" % sid)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "pid": pid,
                       "entrypoint": entrypoint}, fh)
        return path

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.main(["--claude-dir", self.claude_dir] + argv)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, out.getvalue(), err.getvalue()


class DetectionTests(FixtureCase):
    def test_detects_routine_by_scheduled_task_marker(self):
        self.write_session("aaa", [kickoff("nightly-report"),
                                   user_msg("status update")])
        sessions = list(scan.iter_sessions(self.claude_dir))
        routines = [s for s in sessions if s.is_routine_run]
        self.assertEqual(len(routines), 1)
        self.assertEqual(routines[0].routine_name, "nightly-report")

    def test_non_routine_session_never_detected(self):
        self.write_session("bbb", [user_msg("help me debug this"),
                                   user_msg("thanks")])
        sessions = list(scan.iter_sessions(self.claude_dir))
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0].is_routine_run)

    def test_corrupt_and_empty_files_do_not_crash(self):
        path = os.path.join(self.proj, "bad.jsonl")
        with open(path, "w") as fh:
            fh.write("not json at all\n{broken\n")
        sessions = list(scan.iter_sessions(self.claude_dir))
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0].is_routine_run)


class PlanningTests(FixtureCase):
    def _three_routine_runs(self):
        # ages: 30d, 20d, 10d -> with keep=2, the 30d one is the candidate
        for sid, age in (("s1", 30), ("s2", 20), ("s3", 10)):
            self.write_session(sid, [kickoff("hourly-check")], age_days=age)

    def test_keep_newest_n_per_routine(self):
        self._three_routine_runs()
        candidates, protected = sweep.plan(self.claude_dir, keep=2)
        self.assertEqual([s.session_id for s, _b, _r in candidates], ["s1"])
        kept = {s.session_id for s, _b, _r in protected}
        self.assertEqual(kept, {"s2", "s3"})

    def test_engaged_run_protected_by_default(self):
        self.write_session("e1", [kickoff("hourly-check")], age_days=30)
        self.write_session("e2", [kickoff("hourly-check"),
                                  user_msg("actually also check prod")],
                           age_days=30)
        candidates, protected = sweep.plan(self.claude_dir, keep=0)
        swept = {s.session_id for s, _b, _r in candidates}
        self.assertIn("e1", swept)
        self.assertNotIn("e2", swept)
        buckets = {s.session_id: b for s, b, _r in protected}
        self.assertEqual(buckets["e2"], sweep.KEEP_ENGAGED)

    def test_include_engaged_allows_engaged_deletion(self):
        self.write_session("e2", [kickoff("hourly-check"),
                                  user_msg("actually also check prod")],
                           age_days=30)
        candidates, _ = sweep.plan(self.claude_dir, keep=0,
                                   include_engaged=True)
        self.assertEqual([s.session_id for s, _b, _r in candidates], ["e2"])

    def test_tool_results_and_meta_do_not_count_as_replies(self):
        self.write_session("t1", [
            kickoff("hourly-check"),
            user_msg([{"type": "tool_result", "content": "ok"}],
                     toolUseResult={"stdout": "ok"}),
            line({"type": "user", "message": {"content": "skill loaded"},
                  "isMeta": True}),
        ], age_days=30)
        candidates, _ = sweep.plan(self.claude_dir, keep=0)
        self.assertEqual([s.session_id for s, _b, _r in candidates], ["t1"])

    def test_whitelist_excludes_by_pattern_and_session_id(self):
        self.write_session("w1", [kickoff("nightly-report")], age_days=30)
        self.write_session("w2", [kickoff("hourly-check")], age_days=30)
        candidates, _ = sweep.plan(self.claude_dir, keep=0,
                                   whitelist=["nightly-*", "w2"])
        self.assertEqual(candidates, [])

    def test_live_session_never_selected(self):
        self.write_session("l1", [kickoff("hourly-check")], age_days=30)
        self.add_registry("l1", pid=os.getpid())
        candidates, protected = sweep.plan(self.claude_dir, keep=0)
        self.assertEqual(candidates, [])
        self.assertEqual(protected[0][1], sweep.KEEP_LIVE)

    def test_recent_activity_skipped_within_live_window(self):
        self.write_session("r1", [kickoff("hourly-check")], age_days=0.001)
        candidates, protected = sweep.plan(self.claude_dir, keep=0)
        self.assertEqual(candidates, [])
        self.assertEqual(protected[0][1], sweep.KEEP_RECENT)

    def test_older_than_filter(self):
        self.write_session("o1", [kickoff("hourly-check")], age_days=3)
        self.write_session("o2", [kickoff("hourly-check")], age_days=30)
        candidates, _ = sweep.plan(self.claude_dir, keep=0,
                                   older_than_days=7)
        self.assertEqual([s.session_id for s, _b, _r in candidates], ["o2"])

    def test_ordinary_sessions_invisible_even_with_aggressive_flags(self):
        self.write_session("n1", [user_msg("hello")], age_days=90)
        candidates, protected = sweep.plan(
            self.claude_dir, keep=0, include_engaged=True,
            live_window_minutes=0, whitelist=[])
        self.assertEqual(candidates, [])
        self.assertEqual(protected, [])


class DeletionTests(FixtureCase):
    def test_dry_run_deletes_nothing(self):
        path = self.write_session("d1", [kickoff("hourly-check")],
                                  age_days=30, sidecar=True)
        code, out, _ = self.run_cli(["sweep", "--keep", "0"])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isdir(os.path.join(self.proj, "d1")))

    def test_sweep_deletes_jsonl_sidecar_and_registry(self):
        path = self.write_session("d2", [kickoff("hourly-check")],
                                  age_days=30, sidecar=True)
        reg = self.add_registry("d2", pid=999999999)  # dead pid
        code, out, _ = self.run_cli(["sweep", "--keep", "0", "--yes"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "d2")))
        self.assertFalse(os.path.exists(reg))
        self.assertIn("deleted d2", out)

    def test_refuses_while_desktop_app_running_unless_forced(self):
        self.write_session("d3", [kickoff("hourly-check")], age_days=30)
        self.add_registry("other", pid=os.getpid(),
                         entrypoint="claude-desktop")
        code, _out, err = self.run_cli(["sweep", "--keep", "0", "--yes"])
        self.assertEqual(code, 3)
        self.assertIn("refusing", err)
        # --force overrides
        code, out, _ = self.run_cli(["sweep", "--keep", "0", "--yes",
                                     "--force"])
        self.assertEqual(code, 0)
        self.assertIn("deleted d3", out)


class ConfigTests(FixtureCase):
    def test_defaults_when_no_config_file(self):
        cfg = config.load(self.claude_dir)
        self.assertEqual(cfg["keep"], 10)
        self.assertEqual(cfg["include_engaged"], False)
        self.assertEqual(cfg["whitelist"], [])

    def test_config_file_overrides_defaults(self):
        with open(os.path.join(self.claude_dir,
                               "routine-sweeper.json"), "w") as fh:
            json.dump({"keep": 3, "whitelist": ["vip-*"]}, fh)
        cfg = config.load(self.claude_dir)
        self.assertEqual(cfg["keep"], 3)
        self.assertEqual(cfg["whitelist"], ["vip-*"])
        self.assertEqual(cfg["live_window_minutes"], 15)  # default kept


if __name__ == "__main__":
    unittest.main()
