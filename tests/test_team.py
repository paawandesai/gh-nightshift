"""Features added after v0.1's first verify: per-contributor PATs, init from flags and presets, pause cancels
runs, nightly budget cap, morning digest, roadmap, take/retry, risk tiers, GitHub App auth."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakegh import BOT, FakeGitHub  # noqa: E402

from nightshift import cli  # noqa: E402
from nightshift import protocol as ns  # noqa: E402
from nightshift.config import loads  # noqa: E402
from nightshift.picker import Picker, Runtime  # noqa: E402

GREEN_MAIN = [{"status": "completed", "conclusion": "success", "event": "push", "head_sha": "def5678"}]
TEAM = """
version: 1
trusted_logins: [dana-pat]
lanes:
  - name: api
    claude_token_secret: CLAUDE_TOKEN_API
  - name: web
    claude_token_secret: CLAUDE_TOKEN_WEB
    agent_pat_secret: AGENT_PAT_WEB
"""


def picker(fake, lane="web", text=TEAM, login=BOT):
    gh = fake.gh(out=io.StringIO())
    gh.login = login
    cfg = loads(text)
    gh.trusted_extra = set(cfg.trusted_logins)
    return Picker(gh, cfg, Runtime(lane=lane, enabled=True))


class PerContributorPATs(unittest.TestCase):
    def test_markers_from_trusted_logins_count_and_others_do_not(self):
        comments = [{"id": 1, "body": ns.marker("attempt"), "user": {"login": "dana-pat"}},
                    {"id": 2, "body": ns.marker("attempt"), "user": {"login": BOT}},
                    {"id": 3, "body": ns.marker("attempt"), "user": {"login": "mallory"}}]
        self.assertEqual(ns.count(comments, frozenset({BOT, "dana-pat"}), "attempt"), 2)
        self.assertEqual(ns.count(comments, BOT, "attempt"), 1)

    def test_attempts_by_another_lanes_pat_count_toward_the_cap(self):
        f = FakeGitHub()
        f.workflow_runs = list(GREEN_MAIN)
        f.add_issue(7, title="[W1] x", labels=["lane:web", "night-safe", "wave:1"])
        for _ in range(3):
            f.add_comment(7, ns.marker("attempt", issue=7), user="dana-pat")
        decision = picker(f).decide()
        self.assertEqual(decision["issue"], "")
        self.assertIn("blocked", f.label_names(7))

    def test_gh_trusted_includes_own_login(self):
        gh = FakeGitHub().gh()
        gh.login = "me"
        gh.trusted_extra = {"dana-pat", ""}
        self.assertEqual(gh.trusted, frozenset({"me", "dana-pat"}))

    def test_cli_context_loads_trusted_logins(self):
        root = tempfile.mkdtemp()
        Path(root, ".night-shift").mkdir()
        Path(root, ".night-shift/config.yml").write_text(TEAM)
        ctx = cli.Ctx(cli.parser().parse_args(["status", "--root", root, "--repo", "o/r"]), transport=FakeGitHub())
        self.assertIn("dana-pat", ctx.gh.trusted_extra)

    def test_doctor_warns_when_lanes_have_own_pats_but_no_trusted_logins(self):
        from nightshift.doctor import local_checks

        root = tempfile.mkdtemp()
        Path(root, ".night-shift").mkdir()
        Path(root, ".night-shift/config.yml").write_text(TEAM.replace("trusted_logins: [dana-pat]\n", ""))
        _, checks = local_checks(Path(root))
        self.assertTrue(any("trusted_logins is empty" in str(c) for c in checks))

    def test_doctor_checks_every_pat(self):
        from types import SimpleNamespace

        from nightshift.doctor import run_doctor

        def run(args, **kw):
            token = (kw.get("env") or {}).get("GH_TOKEN")
            login, scopes = {"t1": ("bot", ""), "t2": ("carol", "repo, workflow")}.get(token, ("me", "workflow"))
            head = "HTTP/2.0 200 OK\n" + (f"X-Oauth-Scopes: {scopes}\n" if scopes else "")
            return SimpleNamespace(returncode=0, stdout=head + '\n' + json.dumps({"login": login}), stderr="")

        root = tempfile.mkdtemp()
        Path(root, ".night-shift").mkdir()
        Path(root, ".night-shift/config.yml").write_text(TEAM)
        checks = [str(c) for c in run_doctor(FakeGitHub().gh(), Path(root), ["t1", "t2", ""], run)]
        self.assertTrue(any("authenticates as bot" in c for c in checks))
        self.assertTrue(any("`workflow` scope" in c for c in checks))
        self.assertTrue(any("carol is not in trusted_logins" in c for c in checks))
        self.assertTrue(any("empty or unset" in c for c in checks))



class InitFromFlags(unittest.TestCase):
    def init(self, *flags, env=None, transport=None):
        root = tempfile.mkdtemp()
        out = io.StringIO()
        old = dict(os.environ)
        os.environ.update(env or {})
        try:
            rc = cli.main(["init", "--root", root, "--repo", "o/r", "--yes", *flags], transport=transport or FakeGitHub(),
                          out=out)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return rc, root, out.getvalue()

    def test_fully_non_interactive_with_presets_paths_owners_budget(self):
        rc, root, _ = self.init("--files-only", "--lanes", "api,web", "--preset", "api=python,web=node",
                                "--paths", "api=services/api/,web=web/", "--owners", "api=alice,web=bob",
                                "--merge-mode", "queue", "--budget", "5")
        self.assertEqual(rc, 0)
        from nightshift.config import load

        cfg = load(root)
        api, web = cfg.lane("api"), cfg.lane("web")
        self.assertEqual(api.paths, ["services/api/"])
        self.assertEqual(api.token_secret, "CLAUDE_TOKEN_ALICE")
        self.assertEqual(api.test_command, "cd services/api && python -m pytest -q")
        self.assertEqual(web.setup_commands.strip(), "cd web && npm ci")
        self.assertEqual((cfg.merge_mode, api.budget_usd_per_night), ("queue", 5.0))

    def test_starter_ci_is_written_when_missing_and_lints(self):
        rc, root, _ = self.init("--files-only", "--lanes", "svc", "--preset", "go")
        ci = Path(root, ".github/workflows/ci.yml").read_text()
        self.assertIn("  test:\n", ci)
        self.assertIn("go test ./...", ci)
        actionlint = os.environ.get("ACTIONLINT")
        if actionlint:
            import subprocess

            self.assertEqual(subprocess.run([actionlint, str(Path(root, ".github/workflows/ci.yml"))]).returncode, 0)

    def test_existing_ci_is_kept(self):
        root = tempfile.mkdtemp()
        Path(root, ".github/workflows").mkdir(parents=True)
        Path(root, ".github/workflows/ci.yml").write_text("name: Mine\n")
        cli.main(["init", "--root", root, "--repo", "o/r", "--yes", "--files-only", "--lanes", "a"],
                 transport=FakeGitHub(), out=io.StringIO())
        self.assertEqual(Path(root, ".github/workflows/ci.yml").read_text(), "name: Mine\n")

    def test_unknown_preset_is_an_error(self):
        rc, _, _ = self.init("--files-only", "--preset", "cobol")
        self.assertEqual(rc, 2)

    def test_secrets_from_env_sets_without_prompting(self):
        fake = FakeGitHub()
        stored = []
        from nightshift import repo_setup

        orig = repo_setup.gh_secret_set
        repo_setup.gh_secret_set = lambda repo, name, value: stored.append((name, value))
        try:
            rc, _, text = self.init("--lanes", "api", "--secrets-from-env",
                                    env={"AGENT_PAT": "pat-value", "CLAUDE_TOKEN_API": "claude-value"}, transport=fake)
        finally:
            repo_setup.gh_secret_set = orig
        self.assertEqual(rc, 0)
        self.assertIn(("AGENT_PAT", "pat-value"), stored)
        self.assertIn(("CLAUDE_TOKEN_API", "claude-value"), stored)
        self.assertNotIn("pat-value", text)


class PauseCancels(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.runs_by_workflow["night-shift.yml"] = [{"id": 11, "status": "in_progress"}, {"id": 12, "status": "queued"},
                                                      {"id": 13, "status": "completed", "conclusion": "success"}]

    def test_pause_cancels_runs_in_flight(self):
        out = io.StringIO()
        cli.main(["pause", "--repo", "o/r", "--root", tempfile.mkdtemp()], transport=self.f, out=out)
        self.assertEqual(sorted(self.f.cancelled), [11, 12])
        self.assertEqual(self.f.variables["AGENTS_ENABLED"], "false")

    def test_no_cancel_and_dry_run(self):
        cli.main(["pause", "--no-cancel", "--repo", "o/r", "--root", tempfile.mkdtemp()], transport=self.f,
                 out=io.StringIO())
        self.assertEqual(self.f.cancelled, [])
        out = io.StringIO()
        cli.main(["pause", "--dry-run", "--repo", "o/r", "--root", tempfile.mkdtemp()], transport=self.f, out=out)
        self.assertEqual(self.f.cancelled, [])
        self.assertIn("[dry-run] gh api -X POST repos/o/r/actions/runs/11/cancel", out.getvalue())


class Budget(unittest.TestCase):
    TEXT = TEAM.replace("    agent_pat_secret: AGENT_PAT_WEB\n", "    budget_usd_per_night: 2.5\n")

    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        self.f.add_issue(500, title="Night-shift log", labels=["night-log"])
        self.f.add_issue(7, title="[W1] x", labels=["lane:web", "night-safe", "wave:1"])

    def test_lane_pauses_when_its_nightly_spend_reaches_the_cap(self):
        self.f.add_comment(500, "01:00 `web` #3 [sonnet] → PR https://x (≈$1.50)")
        self.f.add_comment(500, "01:30 `api` #4 [sonnet] → PR https://x (≈$9.00)")
        self.assertEqual(picker(self.f, text=self.TEXT).decide()["issue"], "7")
        self.f.add_comment(500, "02:00 `web` #5 [sonnet] → stuck (≈$1.20)")
        d = picker(self.f, text=self.TEXT).decide()
        self.assertEqual(d["issue"], "")
        self.assertIn("nightly budget", d["reason"])
        picker(self.f, text=self.TEXT).decide()
        self.assertEqual(len(ns.trusted(self.f.comments[500], BOT, "budget")), 1)  # noted once per night

    def test_untrusted_and_old_lines_do_not_count(self):
        self.f.add_comment(500, "01:00 `web` #3 [sonnet] → PR (≈$9.00)", user="mallory")
        self.f.add_comment(500, "01:00 `web` #3 [sonnet] → PR (≈$9.00)", created_at="2020-01-01T00:00:00Z")
        self.assertEqual(picker(self.f, text=self.TEXT).decide()["issue"], "7")

    def test_night_runs_noon_to_noon_in_the_configured_zone(self):
        import datetime as dt

        from nightshift.picker import night_start

        utc = dt.timezone.utc
        self.assertEqual(night_start(dt.datetime(2026, 1, 2, 3, 0, tzinfo=utc), "UTC"),
                         dt.datetime(2026, 1, 1, 12, 0, tzinfo=utc))
        self.assertEqual(night_start(dt.datetime(2026, 1, 2, 13, 0, tzinfo=utc), "UTC"),
                         dt.datetime(2026, 1, 2, 12, 0, tzinfo=utc))

    def test_no_cap_by_default(self):
        self.f.add_comment(500, "01:00 `web` #3 [sonnet] → PR (≈$999)")
        self.assertEqual(picker(self.f).decide()["issue"], "7")

if __name__ == "__main__":
    unittest.main()
