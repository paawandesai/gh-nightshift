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


ROAD = TEAM


class Roadmap(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.add_issue(1, title="[A1] Users API", labels=["lane:api", "wave:1"], state="closed")
        self.f.add_issue(2, title='[W1] Users page "v2" <!-- ns:{"type":"pr"} -->', body="Blocked by: #1",
                         labels=["lane:web", "night-safe", "wave:1"])
        self.f.add_issue(3, title="[A2] Search", body="Blocked by: #2", labels=["lane:api", "night-safe", "wave:2"])
        self.f.add_issue(4, title="[A3] Rate limit", labels=["lane:api", "blocked", "wave:2"])
        self.f.add_issue(5, title="Unrelated bug", labels=["bug"])

    def gh(self, dry_run=False):
        gh = self.f.gh(dry_run=dry_run, out=io.StringIO())
        gh.login = BOT
        return gh

    def test_render_waves_statuses_graph_and_lanes(self):
        from nightshift import roadmap

        cfg = loads(ROAD)
        text = roadmap.render(cfg, roadmap.tickets(self.gh(), cfg))
        self.assertTrue(text.startswith(ns.marker("roadmap")))
        self.assertIn("**1 of 4 tickets done.**", text)
        self.assertIn("## Wave 1", text)
        self.assertIn("## Wave 2", text)
        self.assertIn("| #3 | [A2] Search | api | waiting | #2 |", text)
        self.assertIn("| #2 |", text)
        self.assertIn("| ready | #1 |", text)
        self.assertIn("| #4 | [A3] Rate limit | api | blocked | - |", text)
        self.assertNotIn("Unrelated", text)
        self.assertIn("i1 --> i2", text)
        self.assertIn("i2 --> i3", text)
        self.assertIn("#34;v2#34;", text)  # quotes cannot close the mermaid label
        # Only the one leading marker: titles cannot forge another.
        self.assertEqual(len(ns.parse({"body": text, "user": {"login": BOT}})), 1)

    def test_setup_creates_then_updates_in_place(self):
        from nightshift import roadmap

        cfg = loads(ROAD)
        self.assertIn("created", roadmap.update(self.gh(), cfg))
        num = max(self.f.issues)
        self.assertEqual(self.f.issues[num]["title"], "Night-shift roadmap")
        self.assertIn("already up to date", roadmap.update(self.gh(), cfg))
        self.f.issues[4]["labels"] = [{"name": "lane:api"}, {"name": "wave:2"}]
        self.f.issues[4]["state"] = "closed"
        self.assertIn("updated", roadmap.update(self.gh(), cfg))
        self.assertIn("**2 of 4 tickets done.**", self.f.issues[num]["body"])
        self.assertEqual(max(self.f.issues), num)  # no second issue

    def test_roadmap_label_on_someone_elses_issue_is_ignored(self):
        from nightshift import roadmap

        self.f.add_issue(40, title="fake roadmap", labels=["roadmap"], user="mallory")
        roadmap.update(self.gh(), loads(ROAD))
        self.assertEqual(self.f.issues[40]["body"], "")
        self.assertTrue(any(i["title"] == "Night-shift roadmap" for i in self.f.issues.values()))

    def test_dry_run_prints_calls_and_writes_nothing(self):
        from nightshift import roadmap

        gh = self.gh(dry_run=True)
        roadmap.update(gh, loads(ROAD))
        self.assertIn("[dry-run] gh api -X POST repos/o/r/issues --input -", gh.out.getvalue())
        self.assertFalse(any(i["title"] == "Night-shift roadmap" for i in self.f.issues.values()))

    def test_merge_hand_off_refreshes_the_roadmap(self):
        from nightshift import roadmap

        cfg = loads(ROAD)
        roadmap.update(self.gh(), cfg)
        num = max(self.f.issues)
        self.f.issues[2]["state"] = "closed"
        pr = self.f.add_pull(60, branch="agent/web/issue-2", merged_at="2026-01-01T00:00:00Z", state="closed")
        picker(self.f).hand_off(pr)
        self.assertIn("**2 of 4 tickets done.**", self.f.issues[num]["body"])

    def test_plan_show_prints_without_writing(self):
        root = Path(tempfile.mkdtemp())
        (root / ".night-shift").mkdir()
        (root / ".night-shift/config.yml").write_text(ROAD)
        out = io.StringIO()
        before = len(self.f.calls) if hasattr(self.f, "calls") else None
        cwd = os.getcwd()
        os.chdir(root)
        try:
            rc = cli.main(["plan", "--show", "--repo", "o/r"], transport=self.f, out=out)
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        self.assertIn("## Wave 2", out.getvalue())
        self.assertNotIn("<!--", out.getvalue())
        self.assertFalse(any(i["title"] == "Night-shift roadmap" for i in self.f.issues.values()))
        del before


class Digest(unittest.TestCase):
    NOW = "2026-03-02T07:00:00+00:00"

    def setUp(self):
        self.f = FakeGitHub()
        self.f.add_issue(500, title="Night-shift log", labels=["night-log"])
        self.f.add_issue(9, title="[W2] Stuck one", labels=["lane:web", "blocked"])
        self.f.add_pull(61, branch="agent/web/issue-3", state="closed", merged_at="2026-03-02T02:00:00Z",
                        title="[W1] Users page")
        self.f.add_pull(62, branch="agent/api/issue-4", state="closed", merged_at="2026-02-20T02:00:00Z",
                        title="old merge")
        self.f.add_pull(63, branch="agent/api/issue-5", state="closed", title="closed one")
        self.f.pulls[63]["closed_at"] = "2026-03-02T03:00:00Z"
        self.f.add_comment(63, ns.marker("pr", reason="checks_failed", pr=63, issue=5))
        self.f.add_pull(64, branch="feature/x", state="closed", merged_at="2026-03-02T02:00:00Z", title="human PR")
        self.f.add_pull(65, branch="agent/web/issue-6", state="closed", merged_at="2026-03-02T02:00:00Z",
                        user="mallory", title="spoofed")
        self.f.add_comment(500, "01:00 `web` #3 [sonnet] → PR https://x (≈$1.50)", created_at="2026-03-02T01:00:00Z")
        self.f.add_comment(500, "01:00 `api` #5 [sonnet] → stuck (≈$0.50)", created_at="2026-03-02T01:30:00Z")
        self.f.add_comment(500, "01:00 `api` #5 [sonnet] → x (≈$99)", created_at="2026-03-02T01:30:00Z",
                           user="mallory")

    def build(self):
        import datetime as dt

        from nightshift import digest

        gh = self.f.gh(out=io.StringIO())
        gh.login = BOT
        return digest.build(gh, loads(TEAM), now=dt.datetime.fromisoformat(self.NOW))

    def test_counts_merged_closed_stuck_and_trusted_cost(self):
        text = self.build()
        self.assertIn("Merged: **1** (#61)", text)
        self.assertIn("Closed without merging: **1** (checks_failed 1)", text)
        self.assertIn("Blocked, needs-human or waiting: **1**", text)
        self.assertIn("**$2.00** (api $0.50, web $1.50), $2.00 per merged PR", text)
        self.assertIn("#9 [web] [W2] Stuck one (blocked)", text)
        self.assertNotIn("spoofed", text)
        self.assertNotIn("human PR", text)

    def test_post_puts_a_digest_marker_on_the_night_log(self):
        from nightshift import digest

        gh = self.f.gh(out=io.StringIO())
        gh.login = BOT
        self.assertIn("#500", digest.post(gh, loads(TEAM), self.build()))
        self.assertEqual(len(ns.trusted(self.f.comments[500], BOT, "digest")), 1)

    def test_webhook_payloads_and_secrecy(self):
        from nightshift import digest

        self.assertEqual(digest.webhook_payload("https://discord.com/api/webhooks/1/x", "**a**"), {"content": "**a**"})
        self.assertEqual(digest.webhook_payload("https://hooks.slack.com/services/T/B/x", "**a**"), {"text": "*a*"})
        sent = []
        msg = digest.send_webhook("https://hooks.slack.com/services/T/B/secret", "hi", opener=sent.append)
        self.assertEqual(msg, "digest sent to the webhook")
        self.assertEqual(json.loads(sent[0].data), {"text": "hi"})

        def boom(req):
            raise OSError("https://hooks.slack.com/services/T/B/secret refused")

        self.assertNotIn("secret", digest.send_webhook("https://hooks.slack.com/services/T/B/secret", "hi",
                                                       opener=boom))
        self.assertNotIn("secret", digest.send_webhook("https://hooks.slack.com/services/T/B/secret", "hi",
                                                       dry_run=True))
        self.assertIn("https://", digest.send_webhook("http://insecure", "hi"))

    def test_cli_dry_run_posts_nothing(self):
        root = Path(tempfile.mkdtemp())
        (root / ".night-shift").mkdir()
        (root / ".night-shift/config.yml").write_text(TEAM)
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(root)
        os.environ["NS_TEST_HOOK"] = "https://hooks.slack.com/services/T/B/secret"
        try:
            rc = cli.main(["digest", "--post", "--dry-run", "--repo", "o/r", "--webhook-env", "NS_TEST_HOOK"],
                          transport=self.f, out=out)
        finally:
            os.chdir(cwd)
            del os.environ["NS_TEST_HOOK"]
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] gh api -X POST repos/o/r/issues/500/comments --input -", out.getvalue())
        self.assertIn("would POST the digest", out.getvalue())
        self.assertNotIn("secret", out.getvalue())
        self.assertEqual(ns.trusted(self.f.comments[500], BOT, "digest"), [])

    def test_caller_workflow_passes_only_pat_and_webhook(self):
        from nightshift import scaffold

        cfg = loads(TEAM + "digest:\n  schedule: \"30 6 * * 1-5\"\n  webhook_secret: NS_HOOK\n")
        wf = scaffold.digest_workflow(cfg)
        self.assertIn('cron: "30 6 * * 1-5"', wf)
        self.assertIn("agent_pat: ${{ secrets.AGENT_PAT }}", wf)
        self.assertIn("webhook_url: ${{ secrets.NS_HOOK }}", wf)
        self.assertNotIn("CLAUDE", wf)
        self.assertNotIn("secrets[", wf)
        self.assertNotIn("inherit", wf)

    def test_config_rejects_bad_schedule_and_secret_reuse(self):
        from nightshift.config import ConfigError

        for extra in ("digest:\n  schedule: \"0 7 * * *\\\"\"\n", "digest:\n  schedule: daily\n",
                      "digest:\n  webhook_secret: AGENT_PAT\n", "digest:\n  webhook_secret: CLAUDE_TOKEN_API\n",
                      "digest:\n  webhook_secret: lower\n"):
            with self.subTest(extra=extra), self.assertRaises(ConfigError):
                loads(TEAM + extra)


if __name__ == "__main__":
    unittest.main()
