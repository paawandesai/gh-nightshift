"""`gh nightshift status` (each lane's decision, never a write) and `gh nightshift stats`."""
import contextlib
import datetime as dt
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakegh import FakeGitHub

from nightshift import audit, cli, scaffold
from nightshift import protocol as ns
from nightshift.stats_math import fmt_rate


def write_config(root, lanes=("api", "web", "db"), **extra):
    data = {"version": 1, "main_branch": "main", "ci_workflow": "ci.yml",
            "lanes": [{"name": n, "paths": [f"{n}/"], "claude_token_secret": f"CLAUDE_TOKEN_{n.upper()}",
                       "test_command": "pytest"} for n in lanes]}
    data.update(extra)
    path = Path(root, ".night-shift", "config.yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scaffold.config_yaml(data), encoding="utf-8")


def now_iso(minutes_ago=0):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        write_config(self.root)
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_REPOSITORY", "GH_REPO", "AGENTS_ENABLED")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeGitHub()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):  # the picker prints hand-off notes to stdout
            rc = cli.main([argv[0], "--repo", "o/r", "--root", self.root, *argv[1:]], transport=self.fake, out=out)
        return rc, out.getvalue()

    def non_get_calls(self):
        return [c for c in self.fake.calls if c[0] != "GET"]


class StatusTests(CliBase):
    def build_repo(self):
        f = self.fake
        f.variables["AGENTS_ENABLED"] = "true"
        f.add_issue(number=1, title="Night-shift log", labels=["night-log"])
        f.add_comment(1, "2026-11-08 02:00 api #10 merged ≈$1.20")
        f.add_comment(1, ns.marker("plan", lane="*", complete=False) + "\nplanner ran")
        # api: a ready issue; a recently merged PR still owes its hand-off (the picker would comment).
        f.add_issue(number=10, title="[A1] Add login", labels=["lane:api", "night-safe", "wave:1"])
        f.add_issue(number=11, title="[A2] Needs login", labels=["lane:api", "night-safe", "wave:2"],
                    body="Blocked by #10")
        f.add_issue(number=9, title="[A0] Done", labels=["lane:api"], state="closed")
        f.add_pull(number=30, branch="agent/api/issue-9", sha="s30", state="closed", merged_at=now_iso(10))
        # web: an open PR still running checks.
        f.add_issue(number=20, title="[W1] Page", labels=["lane:web", "night-safe"])
        f.add_pull(number=31, branch="agent/web/issue-20", sha="s31", created_at=now_iso(5), title="web page")
        f.check_runs["s31"] = [{"name": "test", "status": "in_progress", "conclusion": None}]
        # db: an open PR with failing checks (the picker would close it) and an issue out of attempts.
        f.add_issue(number=40, title="[D1] Schema", labels=["lane:db", "night-safe"])
        f.add_pull(number=32, branch="agent/db/issue-40", sha="s32", created_at=now_iso(5), title="db schema")
        f.check_runs["s32"] = [{"name": "test", "status": "completed", "conclusion": "failure"}]
        f.add_issue(number=39, title="[D0] Index", labels=["lane:db", "night-safe"])
        for _ in range(3):
            f.add_comment(39, ns.marker("attempt", issue=39, lane="db"))
        f.add_issue(number=50, title="Stuck one", labels=["lane:web", "blocked"])

    def test_status_shows_each_lane_and_makes_no_write(self):
        self.build_repo()
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertEqual(self.non_get_calls(), [], "status must not change GitHub")
        lines = text.splitlines()
        self.assertEqual(lines[0], "Lanes (what each picker would do now):")
        api = next(ln for ln in lines if ln.startswith("  api "))
        web = next(ln for ln in lines if ln.startswith("  web "))
        db = next(ln for ln in lines if ln.startswith("  db "))
        self.assertIn("picked #10: [A1] Add login (model sonnet)", api)
        self.assertIn("PR #31 in lane web is still in review", web)
        # #40's failing PR would be closed (so #40 is free again); #39 is out of attempts and would be blocked.
        self.assertIn("picked #40: [D1] Schema (model sonnet)", db)
        self.assertNotIn("AGENTS_ENABLED is not true", text)
        self.assertIn("Queue (ready issues per lane): api 2, web 1, db 2", text)
        self.assertIn("Open agent PRs: 2", text)
        self.assertIn("#31 web page [pending]", text)
        self.assertIn("#32 db schema [failed]", text)
        self.assertIn("Blocked / needs-human / waiting: 1", text)
        self.assertIn("#50 [web] Stuck one (blocked)", text)
        self.assertIn("Night log (#1, last 1):", text)
        self.assertIn("2026-11-08 02:00 api #10 merged ≈$1.20", text)
        self.assertNotIn("planner ran", text)
        # Nothing changed in the model either.
        self.assertEqual(self.fake.comments[30], [])
        self.assertEqual(self.fake.pulls[32]["state"], "open")
        self.assertNotIn("blocked", self.fake.label_names(39))
        self.assertEqual(len(self.fake.comments[39]), 3)

    def test_status_with_dry_run_flag_also_writes_nothing(self):
        self.build_repo()
        rc, text = self.run_cli("status", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(self.non_get_calls(), [])
        self.assertNotIn("[dry-run]", text)

    def test_status_when_disabled(self):
        self.build_repo()
        self.fake.variables["AGENTS_ENABLED"] = "false"
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("(AGENTS_ENABLED is not true: no lane runs until `gh nightshift start`)", text)
        self.assertEqual(self.non_get_calls(), [])

    def test_status_without_night_log(self):
        self.fake.add_issue(number=10, title="[A1] Add login", labels=["lane:api", "night-safe"])
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("Night log: none (run `gh nightshift setup`)", text)
        self.assertIn("AGENTS_ENABLED is not true", text)  # variable missing counts as off
        self.assertEqual(self.non_get_calls(), [])

    def test_status_survives_a_lane_error(self):
        self.build_repo()
        self.fake.runs_error = 500  # CI runs unreadable: picker says so, status keeps going
        rc, text = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("cannot read CI status on main", text)
        self.assertEqual(self.non_get_calls(), [])


class StatsTests(CliBase):
    def build_repo(self):
        f = self.fake
        f.add_issue(number=1, title="Night-shift log", labels=["night-log"])
        f.add_comment(1, "02:00 api #10 merged, ≈$1.50 API-equivalent")
        f.add_comment(1, "03:00 web #20 failed, ≈$3.00")
        f.add_comment(1, "04:00 db no cost here")
        bodies = ["**Model:** `opus`\nok", "**Model:** `sonnet`", "**Model:** `sonnet`"]
        for n, body in zip((60, 61, 62), bodies):
            f.add_pull(number=n, branch=f"agent/api/issue-{n + 100}", state="closed", merged_at="2026-11-01T00:00:00Z",
                       body=body)
        f.add_pull(number=63, branch="agent/web/issue-163", state="closed")
        f.add_comment(63, ns.marker("pr", reason="verdict_failed", pr=63, issue=163) + "\nclosing")
        f.add_pull(number=64, branch="agent/web/issue-164", state="closed")
        f.add_comment(64, ns.marker("pr", reason="checks_failed", pr=64, issue=164), user="mallory")
        f.add_pull(number=65, branch="agent/db/issue-165", state="closed")
        f.add_comment(65, ns.marker("pr", reason="conflict", pr=65, issue=165))
        f.add_pull(number=66, branch="agent/db/issue-166", state="open")
        f.add_pull(number=67, branch="feature/human-work", state="closed", merged_at="2026-11-01T00:00:00Z",
                   user="alice")
        f.add_issue(number=70, title="old", labels=["blocked"], state="closed")
        f.add_issue(number=71, title="new", labels=["blocked"])

    def test_stats_lines(self):
        self.build_repo()
        rc, text = self.run_cli("stats")
        self.assertEqual(rc, 0)
        self.assertEqual(self.non_get_calls(), [])
        self.assertIn("Agent PRs: 7 (3 merged, 3 closed unmerged, 1 open)", text)
        self.assertIn(f"Merge rate: {fmt_rate(3, 6)}", text)
        self.assertRegex(text, r"Merge rate: 50% \(95% CI \d+%–\d+%, n=6\)")
        self.assertIn("Unmerged by reason (from ns markers; no marker = manual):", text)
        reasons = {ln.split()[0]: ln for ln in text.splitlines() if ln.startswith("  ")}
        self.assertIn("verdict_failed", reasons)
        self.assertIn("conflict", reasons)
        self.assertIn("manual", reasons)  # the marker on #64 was not posted by the bot
        self.assertNotIn("checks_failed", reasons)
        self.assertIn(fmt_rate(1, 3), reasons["manual"])
        self.assertIn(f"Escalation share (merged PRs built by opus): {fmt_rate(1, 3)}", text)
        self.assertIn("Est. cost per merged PR: $1.50 API-equivalent ($4.50 over 2 logged runs)", text)
        self.assertIn("Blocked issues (label, open or closed): 2", text)
        self.assertNotIn("catch rate", text.lower())

    def test_cost_na_without_log_costs(self):
        self.fake.add_pull(number=60, branch="agent/api/issue-160", state="closed", merged_at="2026-11-01T00:00:00Z")
        rc, text = self.run_cli("stats")
        self.assertEqual(rc, 0)
        self.assertIn("Est. cost per merged PR: n/a (no costs in the night log yet)", text)
        self.assertIn("Merge rate: 100%", text)
        self.assertIn("Escalation share (merged PRs built by opus): n/a (n=0)", text)
        self.assertNotIn("Unmerged by reason", text)

    def test_empty_repo(self):
        rc, text = self.run_cli("stats")
        self.assertEqual(rc, 0)
        self.assertIn("Agent PRs: 0 (0 merged, 0 closed unmerged, 0 open)", text)
        self.assertIn("Merge rate: n/a (n=0)", text)

    def audit_csv(self, labels, column="human_label"):
        rows = [{"pr": i, "issue": i + 100, "lane": "api", "title": f"PR {i}", column: label}
                for i, label in enumerate(labels, start=1)]
        path = os.path.join(self.root, "audit.csv")
        audit.write_csv(rows, path)
        return path

    def test_catch_rate_from_audit_csv(self):
        self.build_repo()
        path = self.audit_csv(["ok", "wrong", "ok", "ok"])
        rc, text = self.run_cli("stats", "--audit", path)
        self.assertEqual(rc, 0)
        self.assertEqual(self.non_get_calls(), [])
        self.assertIn(f"Review gate catch rate (from {path}, human_label):", text)
        expected = audit.catch_rate(1, 3, (1, 4))
        self.assertIn("  " + expected.splitlines()[0], text)
        self.assertIn("Catch rate: 57%", text)  # 1 / (1 + 3 * 1/4)

    def test_catch_rate_llm_labels_marked_not_ground_truth(self):
        self.build_repo()
        path = self.audit_csv(["ok", "defect_fixed_later"], column="llm_label")
        rc, text = self.run_cli("stats", "--audit", path)
        self.assertEqual(rc, 0)
        self.assertIn("llm_label, not ground truth):", text)
        self.assertIn(audit.catch_rate(1, 3, (1, 2)).splitlines()[0], text)


if __name__ == "__main__":
    unittest.main()
