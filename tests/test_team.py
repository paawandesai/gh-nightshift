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


GREEN = [{"name": "test", "status": "completed", "conclusion": "success"},
         {"name": "review / verdict", "status": "completed", "conclusion": "success"}]


class Commands(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.permissions = {"dana": "write", "rita": "read"}
        self.f.add_issue(7, title="[W1] x", labels=["lane:web", "night-safe", "wave:1"])
        self.f.add_issue(8, title="bug", labels=["bug"])

    def handle(self, author, body, number=7, dry_run=False):
        from nightshift import commands

        gh = self.f.gh(dry_run=dry_run, out=io.StringIO())
        gh.login = BOT
        return commands.handle(gh, loads(TEAM), author, body, number)

    def test_take_assigns_labels_human_and_agents_skip_it(self):
        self.assertIn("taken by dana", self.handle("dana", "/night-shift take"))
        self.assertEqual(self.f.label_names(7) & {"human", "night-safe"}, {"human"})
        self.assertEqual(self.f.issues[7]["assignees"], [{"login": "dana"}])
        self.assertEqual(len(ns.trusted(self.f.comments[7], BOT, "take")), 1)
        self.f.workflow_runs = list(GREEN_MAIN)
        self.assertEqual(picker(self.f).decide()["issue"], "")

    def test_retry_requeues_with_fresh_attempts_and_undoes_take(self):
        self.f.workflow_runs = list(GREEN_MAIN)
        self.handle("dana", "/night-shift take")
        for _ in range(3):
            self.f.add_comment(7, ns.marker("attempt", issue=7))
        self.f.issues[7]["labels"].append({"name": "blocked"})
        self.assertIn("re-queued by dana", self.handle("dana", "/night-shift retry try the cache"))
        self.assertIn("night-safe", self.f.label_names(7))
        self.assertFalse(self.f.label_names(7) & {"human", "blocked"})
        self.assertEqual(self.f.issues[7]["assignees"], [])
        self.assertEqual(ns.count(self.f.comments[7], BOT, "attempt"), 0)
        self.assertEqual(picker(self.f).decide()["issue"], "7")

    def test_retry_on_an_agent_pr_targets_its_issue_and_reopens_it(self):
        self.f.issues[7]["state"] = "closed"
        self.f.add_pull(20, branch="agent/web/issue-7", state="closed")
        self.handle("dana", "/night-shift retry", number=20)
        self.assertEqual(self.f.issues[7]["state"], "open")
        self.assertEqual(len(ns.trusted(self.f.comments[7], BOT, "retry")), 1)

    def test_readers_outsiders_and_non_tickets_are_refused(self):
        self.assertIn("write access", self.handle("rita", "/night-shift take"))
        self.assertIn("write access", self.handle("mallory", "/night-shift retry"))
        self.assertIn("write access", self.handle("x/../y", "/night-shift take"))
        self.assertEqual(self.f.label_names(7), {"lane:web", "night-safe", "wave:1"})
        self.assertIn("no lane label", self.handle("dana", "/night-shift take", number=8))
        self.assertIn("not a /night-shift", self.handle("dana", "please /night-shift take"))

    def test_named_issue_and_dry_run(self):
        out = io.StringIO()
        from nightshift import commands

        gh = self.f.gh(dry_run=True, out=out)
        gh.login = BOT
        commands.handle(gh, loads(TEAM), "dana", "/night-shift take #7", 8)
        self.assertIn("[dry-run] gh api -X POST repos/o/r/issues/7/assignees --input -", out.getvalue())
        self.assertEqual(self.f.issues[7]["assignees"], [])

    def test_untrusted_take_marker_does_not_unassign(self):
        self.f.issues[7]["assignees"] = [{"login": "erin"}]
        self.f.add_comment(7, ns.marker("take", issue=7, by="erin"), user="mallory")
        self.handle("dana", "/night-shift retry")
        self.assertEqual(self.f.issues[7]["assignees"], [{"login": "erin"}])

    def test_commands_caller_passes_only_the_pat_and_uses_env_for_the_body(self):
        from nightshift import scaffold

        wf = scaffold.commands_workflow(loads(TEAM))
        self.assertIn("agent_pat: ${{ secrets.AGENT_PAT }}", wf)
        self.assertNotIn("CLAUDE", wf)
        self.assertIn("github.event.comment.user.type == 'User'", wf)
        reusable = (Path(__file__).parent.parent / ".github/workflows/commands.yml").read_text()
        self.assertNotIn("run: python3 -m nightshift command ${{", reusable)
        self.assertIn("COMMENT_BODY: ${{ github.event.comment.body }}", reusable)


RISK = TEAM + """risk:
  approve_labels: [risk:high]
  approve_paths: [migrations/]
"""


class RiskTiers(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        self.f.permissions = {"dana": "write", "rita": "read"}
        self.f.add_issue(1, title="[A1] x", labels=["lane:api", "night-safe", "wave:1"])
        self.f.add_issue(2, title="[A2] y", labels=["lane:api", "night-safe", "wave:1"])
        self.f.add_pull(10, sha="s1", branch="agent/api/issue-1")
        self.f.check_runs["s1"] = list(GREEN)

    def decide(self, text=RISK):
        return picker(self.f, lane="api", text=text).decide()

    def test_low_risk_green_pr_merges_as_before(self):
        self.f.pull_files[10] = ["api/x.py"]
        self.decide()
        self.assertTrue(self.f.pulls[10]["merged_at"])

    def test_sensitive_path_waits_for_approval_without_holding_the_lane(self):
        self.f.pull_files[10] = ["api/x.py", "migrations/0002.sql"]
        d = self.decide()
        self.assertFalse(self.f.pulls[10]["merged_at"])
        self.assertIn("needs-approval", {x["name"] for x in self.f.pulls[10]["labels"]})
        self.assertEqual(d["issue"], "2")  # issue 1 is not re-picked; the lane keeps going
        self.decide()
        self.assertEqual(len(ns.trusted(self.f.comments[10], BOT, "approval")), 1)  # asked once

    def test_label_on_the_issue_needs_approval(self):
        self.f.issues[1]["labels"].append({"name": "risk:high"})
        self.decide()
        self.assertFalse(self.f.pulls[10]["merged_at"])

    def test_only_a_writer_approving_the_current_head_counts(self):
        self.f.pull_files[10] = ["migrations/1.sql"]
        bad = [{"user": {"login": "rita"}, "state": "APPROVED", "commit_id": "s1"},
               {"user": {"login": BOT}, "state": "APPROVED", "commit_id": "s1"},
               {"user": {"login": "dana"}, "state": "APPROVED", "commit_id": "old"},
               {"user": {"login": "x[bot]"}, "state": "APPROVED", "commit_id": "s1"}]
        self.f.reviews[10] = bad
        self.decide()
        self.assertFalse(self.f.pulls[10]["merged_at"])
        self.f.reviews[10] = [{"user": {"login": "dana"}, "state": "APPROVED", "commit_id": "s1"},
                              {"user": {"login": "dana"}, "state": "CHANGES_REQUESTED", "commit_id": "s1"}]
        self.decide()
        self.assertFalse(self.f.pulls[10]["merged_at"])  # latest review wins
        self.f.reviews[10] = [{"user": {"login": "dana"}, "state": "APPROVED", "commit_id": "s1"}]
        self.decide()
        self.assertTrue(self.f.pulls[10]["merged_at"])

    def test_default_approve_with_auto_labels(self):
        text = TEAM + "risk:\n  default: approve\n  auto_labels: [risk:low]\n"
        self.f.add_issue(3, title="[A3] z", labels=["lane:api", "night-safe", "wave:1", "risk:low"])
        self.f.add_pull(11, sha="s2", branch="agent/api/issue-3")
        self.f.check_runs["s2"] = list(GREEN)
        self.decide(text)
        self.assertFalse(self.f.pulls[10]["merged_at"])
        self.assertTrue(self.f.pulls[11]["merged_at"])

    def test_config_rejects_unknown_default(self):
        from nightshift.config import ConfigError

        with self.assertRaises(ConfigError):
            loads(TEAM + "risk:\n  default: yolo\n")


class FixRound1(unittest.TestCase):
    """Verifier findings on batches 1-2."""

    def test_workflow_pat_finds_the_roadmap_the_maintainer_created(self):
        from nightshift import roadmap

        f = FakeGitHub()
        f.add_issue(1, title="[A1] x", labels=["lane:api", "wave:1"])
        admin = f.gh(out=io.StringIO())
        admin.login = "maintainer"
        f.login = "maintainer"
        roadmap.update(admin, loads(TEAM))
        num = max(f.issues)
        f.issues[num]["author_association"] = "OWNER"
        f.login = BOT
        bot = f.gh(out=io.StringIO())
        bot.login = BOT
        f.issues[1]["state"] = "closed"
        self.assertIn("updated", roadmap.safe_update(bot, loads(TEAM)))
        self.assertEqual(max(f.issues), num)
        # A writer's issue without the marker at the start is still not taken over.
        f.add_issue(50, title="mine", labels=["roadmap"], body="notes", user="erin")
        f.issues[50]["author_association"] = "COLLABORATOR"
        roadmap.update(bot, loads(TEAM))
        self.assertEqual(f.issues[50]["body"], "notes")

    def test_bot_login_is_trusted_by_lanes_on_their_own_pat(self):
        root = Path(tempfile.mkdtemp())
        (root / ".night-shift").mkdir()
        (root / ".night-shift/config.yml").write_text(TEAM.replace("version: 1\n", "version: 1\nbot_login: night-bot\n"))
        args = cli.parser().parse_args(["status", "--root", str(root), "--repo", "o/r"])
        ctx = cli.Ctx(args, transport=FakeGitHub(), out=io.StringIO())
        ctx.gh.login = "bob"
        self.assertEqual(ctx.gh.trusted, frozenset({"bob", "night-bot", "dana-pat"}))

    def test_titles_and_agent_outcomes_cannot_fake_cost(self):
        from nightshift import digest, roadmap

        f = FakeGitHub()
        f.add_issue(500, title="Night-shift log", labels=["night-log"])
        f.add_issue(9, title="Fix ≈$9999 rounding", labels=["lane:web", "blocked", "wave:1"])
        gh = f.gh(out=io.StringIO())
        gh.login = BOT
        text = digest.build(gh, loads(TEAM))
        self.assertNotIn("≈", text)
        self.assertNotIn("≈", roadmap.render(loads(TEAM), roadmap.tickets(gh, loads(TEAM))))


APP = TEAM.replace("    agent_pat_secret: AGENT_PAT_WEB\n", "") + "auth:\n  mode: app\n  app_id: 4242\n"


class FakeAppAPI:
    def __init__(self, perms=None, installed=True):
        self.calls = []
        self.perms = perms or {"contents": "write", "issues": "write", "pull_requests": "write",
                               "actions": "read", "metadata": "read"}
        self.installed = installed

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        assert token.count(".") == 2
        if path == "/app":
            return {"slug": "night-shift-o"}
        if path.endswith("/installation"):
            return {"id": 77} if self.installed else {}
        return {"token": "ghs_secret", "permissions": self.perms, "expires_at": "2026-01-01T01:00:00Z"}


def fake_sign(message, key):
    return b"sig"


class GitHubApp(unittest.TestCase):
    def test_jwt_claims(self):
        import base64

        from nightshift import app

        token = app.jwt("4242", "KEY", now=1_000_000, sign=fake_sign)
        header, payload, sig = token.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(header))), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(payload))),
                         {"iat": 999_940, "exp": 1_000_540, "iss": "4242"})

    def test_openssl_signature_verifies(self):
        import shutil
        import subprocess

        from nightshift import app

        if not shutil.which("openssl"):
            self.skipTest("no openssl")
        d = Path(tempfile.mkdtemp())
        subprocess.run(["openssl", "genrsa", "-out", str(d / "k.pem"), "2048"], capture_output=True, check=True)
        subprocess.run(["openssl", "rsa", "-in", str(d / "k.pem"), "-pubout", "-out", str(d / "p.pem")],
                       capture_output=True, check=True)
        token = app.jwt("1", (d / "k.pem").read_text())
        import base64

        head, payload, sig = token.split(".")
        (d / "sig").write_bytes(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(d / "p.pem"), "-signature", str(d / "sig")],
                           input=f"{head}.{payload}".encode(), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_mint_downscopes_to_this_repo_and_never_workflows(self):
        from nightshift import app

        api = FakeAppAPI()
        result = app.mint("o/r", "4242", "KEY", request=api, sign=fake_sign)
        self.assertEqual(result["login"], "night-shift-o[bot]")
        post = api.calls[-1]
        self.assertEqual(post[1], "/app/installations/77/access_tokens")
        self.assertEqual(post[2]["repositories"], ["r"])
        self.assertNotIn("workflows", post[2]["permissions"])
        with self.assertRaises(app.AppError):
            app.mint("o/r", "4242", "KEY", request=FakeAppAPI(perms={"workflows": "write"}), sign=fake_sign)
        with self.assertRaises(app.AppError):
            app.mint("o/r", "4242", "KEY", request=FakeAppAPI(installed=False), sign=fake_sign)
        with self.assertRaises(app.AppError):
            app.mint("o/r", "", "KEY", request=api, sign=fake_sign)

    def test_github_env_masks_the_token(self):
        from contextlib import redirect_stdout

        from nightshift import app

        env = Path(tempfile.mkdtemp()) / "env"
        buf = io.StringIO()
        with redirect_stdout(buf):
            app.write_github_env(str(env), {"token": "ghs_secret", "login": "x[bot]"})
        self.assertEqual(buf.getvalue(), "::add-mask::ghs_secret\n")
        self.assertEqual(env.read_text(), "NIGHT_SHIFT_APP_TOKEN=ghs_secret\nNIGHT_SHIFT_LOGIN=x[bot]\n")

    def test_app_mode_callers_pass_exactly_two_secrets_per_lane_and_no_pat(self):
        import re

        from nightshift import scaffold

        cfg = loads(APP)
        lane_wf = scaffold.lane_workflow(cfg)
        self.assertNotIn("AGENT_PAT", lane_wf)
        for job in re.split(r"\n  lane-", lane_wf)[1:]:
            secrets = re.findall(r"\$\{\{ secrets\.([A-Z0-9_]+) \}\}", job)
            self.assertEqual(len(secrets), 2, job)
            self.assertIn("NIGHT_SHIFT_APP_KEY", secrets)
        self.assertIn("app_id: '4242'", lane_wf)
        for wf in (scaffold.planner_workflow(cfg), scaffold.commands_workflow(cfg), scaffold.digest_workflow(cfg)):
            self.assertNotIn("AGENT_PAT", wf)
            self.assertIn("app_private_key: ${{ secrets.NIGHT_SHIFT_APP_KEY }}", wf)
        self.assertIn("NIGHT_SHIFT_APP_KEY", cfg.secret_names())
        self.assertNotIn("AGENT_PAT", cfg.secret_names())

    def test_reusable_workflows_mint_only_in_pat_jobs(self):
        import re

        lane = (Path(__file__).parent.parent / ".github/workflows/lane.yml").read_text()
        jobs = re.split(r"\n  (pick|agent|publish):\n", lane)
        body = dict(zip(jobs[1::2], jobs[2::2]))
        self.assertIn("app-token", body["pick"])
        self.assertIn("app-token", body["publish"])
        self.assertNotIn("app_private_key", body["agent"])
        self.assertNotIn("NIGHT_SHIFT_APP_TOKEN", body["agent"])
        self.assertNotIn("agent_pat", body["agent"])

    def test_config_validation(self):
        from nightshift.config import ConfigError

        sso = TEAM.replace("version: 1", "version: 1\nauth:\n  mode: sso")
        for text in (APP.replace("app_id: 4242", "app_id: abc"), TEAM + "auth:\n  mode: app\n  app_id: 1\n",
                     APP + "  private_key_secret: CLAUDE_TOKEN_API\n", sso):
            with self.subTest(text=text[-60:]), self.assertRaises(ConfigError):
                loads(text)

    def test_app_token_command_and_login_from_env(self):
        from unittest import mock

        from nightshift import app

        root = Path(tempfile.mkdtemp())
        (root / ".night-shift").mkdir()
        (root / ".night-shift/config.yml").write_text(APP)
        env_file = root / "github_env"
        with mock.patch.object(app, "http", FakeAppAPI()), mock.patch.object(app, "openssl_sign", fake_sign), \
                mock.patch.dict(os.environ, {"APP_ID": "4242", "APP_PRIVATE_KEY": "KEY"}):
            out = io.StringIO()
            rc = cli.main(["app-token", "--root", str(root), "--repo", "o/r", "--github-env", str(env_file)], out=out)
        self.assertEqual(rc, 0)
        self.assertNotIn("ghs_secret", out.getvalue())
        self.assertIn("NIGHT_SHIFT_APP_TOKEN=ghs_secret", env_file.read_text())
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghs_secret", "NIGHT_SHIFT_APP_TOKEN": "ghs_secret",
                                          "NIGHT_SHIFT_LOGIN": "night-shift-o[bot]"}):
            args = cli.parser().parse_args(["status", "--root", str(root), "--repo", "o/r"])
            ctx = cli.Ctx(args, transport=FakeGitHub(), out=io.StringIO())
            self.assertEqual(ctx.gh.login, "night-shift-o[bot]")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "other", "NIGHT_SHIFT_APP_TOKEN": "ghs_secret",
                                          "NIGHT_SHIFT_LOGIN": "spoof[bot]"}):
            ctx = cli.Ctx(args, transport=FakeGitHub(), out=io.StringIO())
            self.assertEqual(ctx.gh.login, BOT)

    def test_init_with_app_flags_dry_run(self):
        root = Path(tempfile.mkdtemp())
        key = root / "k.pem"
        key.write_text("-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n")
        f = FakeGitHub()
        out = io.StringIO()
        rc = cli.main(["init", "--root", str(root), "--repo", "o/r", "--yes", "--lanes", "api", "--app-id", "4242",
                       "--app-key-file", str(key), "--dry-run"], transport=f, out=out)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("[dry-run] gh secret set NIGHT_SHIFT_APP_KEY -R o/r   # value read from", text)
        self.assertNotIn("BEGIN RSA", text)
        self.assertNotIn("gh secret set AGENT_PAT", text)
        data = cli.build_config(root, None, "api", True, io.StringIO(), "main",
                                cli.parser().parse_args(["init", "--app-id", "4242"]))
        self.assertEqual(data["auth"], {"mode": "app", "app_id": "4242"})

    def test_manifest_has_minimum_permissions(self):
        from nightshift import app

        m = app.manifest()
        self.assertNotIn("workflows", m["default_permissions"])
        self.assertEqual(m["default_permissions"]["actions"], "read")
        self.assertFalse(m["hook_attributes"]["active"])


if __name__ == "__main__":
    unittest.main()
