"""Every `gh nightshift` command against the in-memory GitHub (tests/fakegh.py).

Product rule under test: every command that changes GitHub supports --dry-run, which makes no write and
prints the exact `gh api` call for each write the live command makes.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakegh import FakeGitHub

from nightshift import audit, cli, prompts, scaffold
from nightshift import protocol as ns
from nightshift.config import CONFIG_PATH, load
from nightshift.gh import command_for

ENV_DROP = ("GITHUB_REPOSITORY", "GH_REPO", "AGENTS_ENABLED", "GH_TOKEN", "GITHUB_TOKEN", "COMMENT_AUTHOR",
            "COMMENT_BODY", "ISSUE_NUMBER", "ONLY_LANE", "FIX_LANE", "LANE_MODEL", "ESCALATE_MODEL")
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main"]


def write_config(root, lanes=("api", "web"), lane_extra=None, **extra):
    data = {"version": 1, "main_branch": "main", "ci_workflow": "ci.yml", "lanes": []}
    for n in lanes:
        lane = {"name": n, "paths": [f"{n}/"], "claude_token_secret": f"CLAUDE_TOKEN_{n.upper()}",
                "test_command": "pytest"}
        lane.update((lane_extra or {}).get(n, {}))
        data["lanes"].append(lane)
    data.update(extra)
    path = Path(root, CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scaffold.config_yaml(data), encoding="utf-8")
    return load(root)


def all_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


def ticket_body(lane_paths="api/login.py"):
    return (f"## Goal\nUsers can log in.\n\n## Do\n- add the endpoint\n\n## Acceptance\n- pytest passes\n\n"
            f"## Files\n{lane_paths}\n")


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeGitHub()
        self.stdout = ""

    def tempdir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def run_cli(self, *argv, fake=None, root=None, repo="o/r"):
        """Run `gh nightshift ARGV --repo o/r --root ROOT` and return (exit code, text written to `out`)."""
        args = list(argv) + ["--root", root or self.root] + (["--repo", repo] if repo else [])
        out, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli.main(args, transport=fake or self.fake, out=out)
        self.stdout = stdout.getvalue()
        return rc, out.getvalue()

    def ctx(self, *argv, fake=None, root=None, repo="o/r"):
        args = list(argv) + ["--root", root or self.root] + (["--repo", repo] if repo else [])
        out = io.StringIO()
        return cli.Ctx(cli.parser().parse_args(args), transport=fake or self.fake, out=out), out

    def assert_dry_run(self, argv, seed, exact=True, root=None):
        """Run ARGV live on one fake and with --dry-run on an identical one. The dry run makes no write
        and prints one `[dry-run] gh api ...` line per live write; with exact=True, the exact command."""
        live, dry = FakeGitHub(), FakeGitHub()
        seed(live)
        seed(dry)
        rc_live, _ = self.run_cli(*argv, fake=live, root=root)
        writes = live.writes()
        self.assertTrue(writes, "fixture: the live command should write something")
        rc_dry, text = self.run_cli(*argv, "--dry-run", fake=dry, root=root)
        self.assertEqual(rc_dry, rc_live)
        self.assertEqual(dry.writes(), [], "--dry-run must not write")
        printed = [ln for ln in text.splitlines() if ln.startswith("[dry-run] gh api ")]
        self.assertEqual(len(printed), len(writes), text)
        for method, path, body in writes:
            if exact:
                self.assertIn("[dry-run] " + command_for(method, path, body), text)
            else:
                self.assertIn("[dry-run] " + command_for(method, path, None).split(" --input")[0], text)
        return text, dry


# --- plumbing -------------------------------------------------------------------------

class PlumbingTests(CliTestCase):
    def test_detect_repo_from_git_remote(self):
        subprocess.run(["git", *GIT_ID, "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin", "git@github.com:acme/widgets.git"], check=True)
        self.assertEqual(cli.detect_repo(Path(self.root)), "acme/widgets")
        subprocess.run(["git", "-C", self.root, "remote", "set-url", "origin", "https://github.com/acme/w2"],
                       check=True)
        self.assertEqual(cli.detect_repo(Path(self.root)), "acme/w2")

    def test_detect_repo_env_wins(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "env/repo"}):
            self.assertEqual(cli.detect_repo(Path(self.root)), "env/repo")
        with mock.patch.dict(os.environ, {"GH_REPO": "gh/repo"}):
            self.assertEqual(cli.detect_repo(Path(self.root)), "gh/repo")

    def test_detect_repo_without_remote_exits(self):
        with self.assertRaises(SystemExit) as cm:
            cli.detect_repo(Path(self.root))
        self.assertIn("--repo", str(cm.exception))

    def test_write_outputs_single_and_multiline(self):
        path = os.path.join(self.root, "out")
        cli.write_outputs(path, {"a": "1", "b": "two\nlines"})
        cli.write_outputs(None, {"ignored": "x"})
        text = Path(path).read_text()
        self.assertTrue(text.startswith("a=1\nb<<NS_"))
        self.assertIn("\ntwo\nlines\nNS_", text)

    def test_config_error_exits_2(self):
        rc, _ = self.run_cli("status")
        self.assertEqual(rc, 2)

    def test_github_error_exits_3(self):
        write_config(self.root)

        class Broken(FakeGitHub):
            def __call__(self, method, path, body=None):
                if method == "PATCH":
                    from nightshift.gh import GHError
                    raise GHError("boom (HTTP 500)", 500)
                return super().__call__(method, path, body)

        rc, _ = self.run_cli("setup", fake=Broken())
        self.assertEqual(rc, 3)

    def test_every_writing_command_accepts_dry_run(self):
        p = cli.parser()
        for argv in (["init"], ["setup"], ["join", "--lane", "api"], ["start"], ["pause"], ["freeze"], ["resume"],
                     ["plan", "publish", "f.json"], ["plan", "--plan-file", "f.json"], ["pick", "--lane", "api"],
                     ["task", "--lane", "api", "--mode", "issue", "--out-dir", "o"],
                     ["publish", "--lane", "a", "--mode", "issue", "--branch", "b", "--title", "t", "--model", "m"],
                     ["audit"], ["plan", "context", "--mode", "gap", "--out-dir", "o"]):
            self.assertTrue(p.parse_args(argv + ["--dry-run"]).dry_run, argv)


# --- init -----------------------------------------------------------------------------

class InitToolRef(unittest.TestCase):
    def test_tool_ref_flag_pins_the_workflows(self):
        import tempfile

        root = tempfile.mkdtemp()
        rc = cli.main(["init", "--root", root, "--repo", "o/r", "--lanes", "api", "--yes", "--files-only",
                       "--tool-ref", "v0.1.0"], transport=FakeGitHub(), out=io.StringIO())
        self.assertEqual(rc, 0)
        text = Path(root, ".github/workflows/night-shift.yml").read_text()
        self.assertIn("lane.yml@v0.1.0", text)
        self.assertEqual(load(root).tool_ref, "v0.1.0")


class InitTests(CliTestCase):
    def test_init_yes_lanes_with_git_remote(self):
        subprocess.run(["git", *GIT_ID, "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                       check=True)
        Path(self.root, "api").mkdir()
        Path(self.root, "api", "requirements.txt").write_text("", encoding="utf-8")
        fake = FakeGitHub(repo="acme/widgets")
        rc, text = self.run_cli("init", "--yes", "--lanes", "api,web", "--skip-secrets", fake=fake, repo=None)
        self.assertEqual(rc, 0, text)
        for rel in (CONFIG_PATH, ".github/workflows/night-shift.yml", ".github/workflows/night-shift-review.yml",
                    ".github/workflows/night-shift-planner.yml", "AGENT_RULES.md", "docs/NIGHT_SHIFT_ISSUES.md",
                    ".github/ISSUE_TEMPLATE/night-shift-ticket.md"):
            self.assertTrue(Path(self.root, rel).is_file(), rel)
            self.assertIn(f"wrote {rel}", text)
        cfg = load(self.root)
        self.assertEqual(cfg.lane_names, ["api", "web"])
        self.assertEqual(cfg.lane("api").token_secret, "CLAUDE_TOKEN_API")
        self.assertEqual(cfg.lane("api").test_command, "cd api && pytest")  # from the detected stack
        self.assertEqual(cfg.lane("web").paths, ["web/"])
        self.assertEqual(cfg.fix_lane, "api")
        # GitHub side: labels, settings, variables, protection, night log.
        self.assertIn("lane:api", fake.labels)
        self.assertIn("night-safe", fake.labels)
        self.assertEqual(fake.variables, {"AGENTS_ENABLED": "false", "FIX_LANE": "api"})
        # Protection waits for `setup`, so pushing the generated files to main is not refused.
        self.assertIsNone(fake.protection)
        self.assertIn("gh nightshift setup", text)
        self.assertTrue(fake.repo_settings["allow_auto_merge"])
        self.assertEqual([i["title"] for i in fake.issues.values()], ["Night-shift log"])
        self.assertIn("night-log issue #1 created", text)
        self.assertNotIn("would set", text)
        self.assertIn("gh nightshift doctor", text)

    def test_init_with_repo_flag_and_secrets(self):
        self.fake.secrets.add("AGENT_PAT")
        values = {"CLAUDE_TOKEN_API": "sk-ant-api-SECRET1", "CLAUDE_TOKEN_WEB": "sk-ant-web-SECRET2",
                  "CLAUDE_TOKEN_REVIEW": ""}
        asked, stored = [], []

        def secret_ask(prompt):
            asked.append(prompt)
            name = prompt.split(" ", 1)[0]
            return values[name]

        ctx, out = self.ctx("init", "--yes", "--lanes", "api,web")
        rc = cli.cmd_init(ctx, secret_ask=secret_ask, secret_setter=lambda repo, n, v: stored.append((repo, n, v)))
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.split(" ", 1)[0] for p in asked), sorted(values))  # AGENT_PAT already exists
        for p in asked:
            self.assertIn("input hidden", p)
        self.assertEqual(sorted(stored), [("o/r", "CLAUDE_TOKEN_API", "sk-ant-api-SECRET1"),
                                          ("o/r", "CLAUDE_TOKEN_WEB", "sk-ant-web-SECRET2")])
        self.assertIn("secret CLAUDE_TOKEN_API set", text)
        self.assertIn("skipped CLAUDE_TOKEN_REVIEW", text)
        for secret in ("SECRET1", "SECRET2"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, json.dumps(self.fake.calls))

    def test_init_interactive_ask(self):
        answers = {"Lanes": "backend", "owner's name": "Pat Lee", "folders it owns": "server/, lib/",
                   "setup command": "make deps", "test command": "make test", "CI workflow": "build.yml",
                   "Merge mode": "queue"}
        prompts_seen = []

        def ask(prompt):
            prompts_seen.append(prompt)
            return next((v for k, v in answers.items() if k in prompt), "")

        ctx, out = self.ctx("init", "--files-only")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_init(ctx, ask=ask)
        self.assertEqual(rc, 0)
        self.assertTrue(all(p.endswith("]: ") for p in prompts_seen), prompts_seen)
        self.assertTrue(prompts_seen[0].startswith("Lanes (comma-separated) [app]"))
        cfg = load(self.root)
        lane = cfg.lane("backend")
        self.assertEqual(lane.token_secret, "CLAUDE_TOKEN_PAT_LEE")
        self.assertEqual(lane.paths, ["server/", "lib/"])
        self.assertEqual(lane.setup_commands.strip(), "make deps")
        self.assertEqual(lane.test_command, "make test")
        self.assertEqual(cfg.ci_workflow, "build.yml")
        self.assertEqual(cfg.merge_mode, "queue")
        self.assertIn("merge_group", Path(self.root, ".github/workflows/night-shift-review.yml").read_text())
        self.assertEqual(self.fake.calls, [])  # --files-only touches no GitHub at all

    def test_init_dry_run_writes_nothing_and_prints_exact_calls(self):
        live_root = self.tempdir()
        live = FakeGitHub()
        rc, _ = self.run_cli("init", "--yes", "--lanes", "api,web", "--skip-secrets", fake=live, root=live_root)
        self.assertEqual(rc, 0)
        writes = live.writes()
        self.assertTrue(writes)

        rc, text = self.run_cli("init", "--yes", "--lanes", "api,web", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(all_files(self.root), [], "--dry-run must not write files")
        self.assertEqual(self.fake.writes(), [], "--dry-run must not write to GitHub")
        for method, path, body in writes:
            self.assertIn("[dry-run] " + command_for(method, path, body), text)
        self.assertEqual(text.count("[dry-run] gh api "), len(writes))
        self.assertIn(f"[dry-run] would write {CONFIG_PATH}", text)
        self.assertIn("[dry-run] would write .github/workflows/night-shift.yml", text)
        # Secrets: the command is printed, nothing is asked or set.
        for name in ("AGENT_PAT", "CLAUDE_TOKEN_API", "CLAUDE_TOKEN_WEB", "CLAUDE_TOKEN_REVIEW"):
            self.assertIn(f"[dry-run] gh secret set {name} -R o/r", text)

    def test_init_dry_run_never_asks_for_secrets(self):
        ctx, out = self.ctx("init", "--yes", "--lanes", "api", "--dry-run")

        def no(*_):
            raise AssertionError("dry-run must not ask for or set a secret")

        self.assertEqual(cli.cmd_init(ctx, secret_ask=no, secret_setter=no), 0)
        self.assertEqual(self.fake.writes(), [])

    def test_files_only(self):
        rc, text = self.run_cli("init", "--yes", "--lanes", "api", "--files-only")
        self.assertEqual(rc, 0)
        self.assertTrue(Path(self.root, CONFIG_PATH).is_file())
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("label created", text)

    def test_existing_config_is_reused_and_files_kept(self):
        write_config(self.root, lanes=("core",))
        Path(self.root, "AGENT_RULES.md").write_text("my rules\n", encoding="utf-8")
        rc, text = self.run_cli("init", "--yes", "--lanes", "ignored", "--files-only")
        self.assertEqual(rc, 0)
        self.assertIn("exists; using it", text)
        self.assertIn("kept AGENT_RULES.md", text)
        self.assertEqual(Path(self.root, "AGENT_RULES.md").read_text(), "my rules\n")
        self.assertEqual(load(self.root).lane_names, ["core"])
        self.assertIn("lane-core:", Path(self.root, ".github/workflows/night-shift.yml").read_text())

    def test_force_starts_over(self):
        write_config(self.root, lanes=("core",))
        Path(self.root, "AGENT_RULES.md").write_text("my rules\n", encoding="utf-8")
        rc, _ = self.run_cli("init", "--yes", "--lanes", "api", "--files-only", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(load(self.root).lane_names, ["api"])
        self.assertNotEqual(Path(self.root, "AGENT_RULES.md").read_text(), "my rules\n")

    def test_regenerate_rewrites_only_workflows(self):
        self.run_cli("init", "--yes", "--lanes", "api", "--files-only")
        Path(self.root, "AGENT_RULES.md").write_text("edited\n", encoding="utf-8")
        write_config(self.root, lanes=("api", "db"))
        rc, text = self.run_cli("init", "--regenerate")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(sorted(ln for ln in text.splitlines() if ln),
                         ["wrote .github/workflows/night-shift-planner.yml",
                          "wrote .github/workflows/night-shift-review.yml", "wrote .github/workflows/night-shift.yml"])
        wf = Path(self.root, ".github/workflows/night-shift.yml").read_text()
        self.assertIn("lane-db:", wf)
        self.assertIn("secrets.CLAUDE_TOKEN_DB", wf)
        self.assertEqual(Path(self.root, "AGENT_RULES.md").read_text(), "edited\n")

    def test_regenerate_dry_run(self):
        write_config(self.root, lanes=("api",))
        rc, text = self.run_cli("init", "--regenerate", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] would write .github/workflows/night-shift.yml", text)
        self.assertEqual(all_files(self.root), [CONFIG_PATH])

    def test_regenerate_without_config(self):
        rc, _ = self.run_cli("init", "--regenerate")
        self.assertEqual(rc, 2)


# --- setup / join ---------------------------------------------------------------------

class SetupJoinTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = write_config(self.root, lane_extra={"web": {"agent_pat_secret": "WEB_PAT"}})

    def test_setup(self):
        rc, text = self.run_cli("setup")
        self.assertEqual(rc, 0)
        names = {n for n, _, _ in scaffold.labels(self.cfg)}
        self.assertEqual(set(self.fake.labels), names)
        self.assertEqual(self.fake.variables["AGENTS_ENABLED"], "false")
        self.assertIn("protected main", text)
        self.assertEqual(len(self.fake.issues), 1)

    def test_setup_is_idempotent_and_keeps_switch_on(self):
        self.run_cli("setup")
        self.fake.variables["AGENTS_ENABLED"] = "true"
        self.fake.calls.clear()
        rc, text = self.run_cli("setup")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.variables["AGENTS_ENABLED"], "true")
        self.assertEqual(self.fake.writes("POST", r"labels$"), [])
        self.assertEqual(self.fake.writes("POST", r"issues$"), [])
        self.assertNotIn("label created", text)

    def test_setup_dry_run_exact(self):
        self.assert_dry_run(["setup"], lambda f: None)

    def test_setup_dry_run_on_partly_set_up_repo(self):
        def seed(f):
            f.labels["lane:api"] = {"name": "lane:api", "color": "000000", "description": "old"}
            f.variables["AGENTS_ENABLED"] = "true"
        text, dry = self.assert_dry_run(["setup"], seed)
        self.assertIn("[dry-run] gh api -X PATCH repos/o/r/labels/lane%3Aapi --input -", text)
        self.assertEqual(dry.labels["lane:api"]["color"], "000000")

    def test_setup_queue_mode_creates_ruleset(self):
        write_config(self.root, merge={"mode": "queue"})
        rc, text = self.run_cli("setup")
        self.assertEqual(rc, 0)
        self.assertEqual([r["name"] for r in self.fake.rulesets], ["night-shift merge queue"])
        self.assertFalse(self.fake.protection["required_status_checks"]["strict"])

    def test_join_sets_secret_without_printing_it(self):
        stored = []
        ctx, out = self.ctx("join", "--lane", "web", "--pat")
        answers = {"CLAUDE_TOKEN_WEB": "sk-ant-oat-VERY-SECRET", "WEB_PAT": "github_pat_ALSO_SECRET"}
        rc = cli.cmd_join(ctx, secret_ask=lambda p: answers[p.split(" ", 1)[0]],
                          secret_setter=lambda repo, n, v: stored.append((repo, n, v)))
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertEqual(stored, [("o/r", "CLAUDE_TOKEN_WEB", "sk-ant-oat-VERY-SECRET"),
                                  ("o/r", "WEB_PAT", "github_pat_ALSO_SECRET")])
        self.assertIn("claude setup-token", text)
        self.assertIn("secret CLAUDE_TOKEN_WEB set", text)
        self.assertIn("secret WEB_PAT set", text)
        self.assertNotIn("VERY-SECRET", text)
        self.assertNotIn("ALSO_SECRET", text)
        self.assertEqual(self.fake.writes(), [])

    def test_join_empty_answer_skips(self):
        ctx, out = self.ctx("join", "--lane", "api")
        rc = cli.cmd_join(ctx, secret_ask=lambda p: "  ", secret_setter=lambda *a: self.fail("must not set"))
        self.assertEqual(rc, 0)
        self.assertIn("skipped CLAUDE_TOKEN_API", out.getvalue())

    def test_join_dry_run(self):
        ctx, out = self.ctx("join", "--lane", "api", "--dry-run")
        rc = cli.cmd_join(ctx, secret_ask=lambda p: self.fail("must not ask"),
                          secret_setter=lambda *a: self.fail("must not set"))
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] gh secret set CLAUDE_TOKEN_API -R o/r", out.getvalue())

    def test_join_unknown_lane(self):
        rc, _ = self.run_cli("join", "--lane", "nope")
        self.assertEqual(rc, 2)


# --- switches -------------------------------------------------------------------------

class SwitchTests(CliTestCase):
    def setUp(self):
        super().setUp()
        write_config(self.root)

    def seed(self, f):
        f.variables["AGENTS_ENABLED"] = "true"
        f.add_issue(number=1, title="a", labels=["lane:api", "night-safe"])
        f.add_issue(number=2, title="b", labels=["lane:web", "night-safe", "human"])
        f.add_issue(number=3, title="c", labels=["lane:web", "night-safe"])
        f.add_issue(number=4, title="d", labels=["lane:api"])

    def test_start_and_pause(self):
        rc, text = self.run_cli("start")
        self.assertEqual((rc, text.strip()), (0, "variable AGENTS_ENABLED=true"))
        self.assertEqual(self.fake.writes(), [("POST", "repos/o/r/actions/variables",
                                               {"name": "AGENTS_ENABLED", "value": "true"})])
        rc, text = self.run_cli("pause")
        self.assertEqual(self.fake.variables["AGENTS_ENABLED"], "false")
        self.assertEqual(self.fake.writes("PATCH")[-1][1], "repos/o/r/actions/variables/AGENTS_ENABLED")

    def test_start_pause_dry_run(self):
        for name in ("start", "pause"):
            text, dry = self.assert_dry_run([name], self.seed)
            self.assertEqual(dry.variables["AGENTS_ENABLED"], "true")
        text, dry = self.assert_dry_run(["start"], lambda f: None)  # variable missing: POST
        self.assertIn("gh api -X POST repos/o/r/actions/variables", text)
        self.assertNotIn("AGENTS_ENABLED", dry.variables)

    def test_freeze_then_resume(self):
        self.seed(self.fake)
        rc, text = self.run_cli("freeze")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.variables["AGENTS_ENABLED"], "false")
        self.assertEqual(self.fake.label_names(1), {"lane:api", "human", "frozen"})
        self.assertEqual(self.fake.label_names(3), {"lane:web", "human", "frozen"})
        self.assertEqual(self.fake.label_names(2), {"lane:web", "night-safe", "human"})  # already a person's
        self.assertEqual(self.fake.label_names(4), {"lane:api"})
        self.assertIn("#1 frozen (labelled human)", text)
        self.assertNotIn("#2 frozen", text)

        rc, text = self.run_cli("resume")
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.variables["AGENTS_ENABLED"], "true")
        self.assertEqual(self.fake.label_names(1), {"lane:api", "night-safe"})
        self.assertEqual(self.fake.label_names(3), {"lane:web", "night-safe"})
        self.assertEqual(self.fake.label_names(2), {"lane:web", "night-safe", "human"})
        self.assertIn("#1 back on the night shift", text)
        self.assertTrue(text.strip().endswith("variable AGENTS_ENABLED=true"))

    def test_freeze_dry_run_exact(self):
        text, dry = self.assert_dry_run(["freeze"], self.seed)
        self.assertEqual(dry.label_names(1), {"lane:api", "night-safe"})
        self.assertEqual(dry.variables["AGENTS_ENABLED"], "true")
        self.assertIn("gh api -X DELETE repos/o/r/issues/1/labels/night-safe", text)

    def test_resume_dry_run_exact(self):
        def seed(f):
            self.seed(f)
            f.variables["AGENTS_ENABLED"] = "false"
            f.add_issue(number=5, title="e", labels=["lane:api", "human", "frozen"])
        text, dry = self.assert_dry_run(["resume"], seed)
        self.assertEqual(dry.label_names(5), {"lane:api", "human", "frozen"})
        self.assertEqual(dry.variables["AGENTS_ENABLED"], "false")


# --- lint -----------------------------------------------------------------------------

class LintTests(CliTestCase):
    def setUp(self):
        super().setUp()
        write_config(self.root)

    def test_lint_reports_problems(self):
        self.fake.add_issue(number=1, title="[A1] Good ticket", body=ticket_body(), labels=["lane:api", "night-safe",
                                                                                            "wave:1"])
        self.fake.add_issue(number=2, title="vague", body="do stuff", labels=["night-safe"], assignees=["bob"])
        self.fake.add_issue(number=3, title="[W1] Not ready", body="", labels=["lane:web"])
        rc, text = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertNotIn("#1 ", text)
        self.assertNotIn("#3 ", text)
        self.assertIn("#2 vague", text)
        self.assertIn("needs exactly one lane label (has 0)", text)
        self.assertIn("assigned to someone", text)
        self.assertIn("missing a Goal section", text)
        self.assertIn("2 ready issue(s), 1 with problems", text)
        self.assertEqual(self.fake.writes(), [])

    def test_lint_needs_interface_when_depended_on(self):
        self.fake.add_issue(number=1, title="[A1] Provider", body=ticket_body(), labels=["lane:api", "night-safe",
                                                                                         "wave:1"])
        self.fake.add_issue(number=2, title="[W1] Consumer", body=ticket_body() + "\n## Interface\nPOST /x\n\n"
                            "Blocked by: #1\n", labels=["lane:web", "night-safe", "wave:2"])
        rc, text = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertIn("#1 [A1] Provider", text)
        self.assertIn("no Interface section", text)
        self.assertNotIn("#2 ", text)

    def test_lint_clean(self):
        self.fake.add_issue(number=1, title="[A1] Good", body=ticket_body(), labels=["lane:api", "night-safe",
                                                                                     "wave:0"])
        rc, text = self.run_cli("lint")
        self.assertEqual(rc, 0)
        self.assertIn("1 ready issue(s), 0 with problems", text)


# --- plan -----------------------------------------------------------------------------

def plan_data(with_deps=True):
    tickets = [
        {"key": "a", "title": "Add login endpoint", "lane": "api", "wave": 1, "goal": "log in", "do": ["route"],
         "acceptance": ["pytest api"], "files": ["api/login.py"], "interface": "POST /login -> 200 {token}",
         "requirements": ["R1"]},
        {"key": "c", "title": "Change the login contract", "lane": "api", "wave": 1, "goal": "contract",
         "do": ["edit"], "acceptance": ["contract test"], "files": ["contracts/login.yml"], "requirements": ["R3"]},
        {"key": "d", "title": "Something for a lane that does not exist", "lane": "mobile", "wave": 1, "goal": "g",
         "do": ["x"], "acceptance": ["y"], "files": ["m/x"]},
    ]
    coverage = [{"requirement": "R1", "status": "ticketed", "tickets": ["a"]},
                {"requirement": "R3", "status": "ticketed", "tickets": ["c"]}]
    if with_deps:
        tickets.insert(1, {"key": "b", "title": "Login page", "lane": "web", "wave": 2, "goal": "page",
                           "do": ["form"], "acceptance": ["npm test"], "files": ["web/login.tsx"],
                           "interface": "calls POST /login", "blocked_by": ["a"], "requirements": ["R2"]})
        coverage.append({"requirement": "R2", "status": "ticketed", "tickets": ["b"]})
    return {"tickets": tickets, "coverage": coverage, "notes": "two lanes"}


class PlanTests(CliTestCase):
    def setUp(self):
        super().setUp()
        write_config(self.root, planner={"docs": ["docs/PRD.md"], "refill_below": 2})
        self.plan_path = os.path.join(self.root, "plan.json")
        Path(self.plan_path).write_text(json.dumps(plan_data()), encoding="utf-8")

    def seed(self, f):
        f.add_issue(number=1, title="Night-shift log", labels=["night-log"])
        f.commits_by_path["docs/PRD.md"] = [{"sha": "abcdef1234567890"}]

    def created(self, fake=None):
        fake = fake or self.fake
        return {i["title"]: i for i in fake.issues.values() if i["number"] != 1}

    def test_plan_publish_creates_issues(self):
        self.seed(self.fake)
        rc, text = self.run_cli("plan", "publish", self.plan_path)
        self.assertEqual(rc, 0, text)
        issues = self.created()
        self.assertEqual(sorted(issues), ["[A1] Add login endpoint", "[A2] Change the login contract",
                                          "[W1] Login page"])
        a, b, c = issues["[A1] Add login endpoint"], issues["[W1] Login page"], issues["[A2] Change the login contract"]
        self.assertEqual({x["name"] for x in a["labels"]}, {"lane:api", "wave:1", "night-safe"})
        self.assertEqual({x["name"] for x in c["labels"]}, {"lane:api", "wave:1", "human"})
        self.assertIn(f"Blocked by: #{a['number']}", b["body"])
        self.assertLess(a["number"], b["number"])  # dependency created first
        self.assertIn("## Interface\nPOST /login", a["body"])
        self.assertIn("rejected 'd'", text)
        self.assertIn("unknown lane 'mobile'", text)
        self.assertIn("coverage: complete", text)
        self.assertIn("planner notes: two lanes", text)
        log = ns.trusted([{"id": c["id"], **c} for c in self.fake.comments[1]], "night-bot", "plan")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0].data, {"type": "plan", "lane": "*", "complete": True, "docs_sha": "abcdef123456",
                                       "created": 3})

    def test_plan_publish_reply_to_and_lane_filter(self):
        self.seed(self.fake)
        self.fake.add_issue(number=5, title="please plan")
        rc, text = self.run_cli("plan", "publish", self.plan_path, "--lane", "api", "--reply-to", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(self.created()), ["[A1] Add login endpoint", "[A2] Change the login contract",
                                                  "please plan"])
        self.assertIn("rejected 'b': outside the requested lane 'api'", text)
        self.assertIn("**Night shift planner**", self.fake.bodies(5)[0])

    def test_plan_publish_dry_run(self):
        Path(self.plan_path).write_text(json.dumps(plan_data(with_deps=False)), encoding="utf-8")
        text, dry = self.assert_dry_run(["plan", "publish", self.plan_path], self.seed, exact=False)
        self.assertEqual(self.created(dry), {})
        self.assertIn("gh api -X POST repos/o/r/issues --input - <<'JSON'", text)
        self.assertIn('"title": "[A1] Add login endpoint"', text)

    def test_plan_with_plan_file(self):
        self.seed(self.fake)
        rc, text = self.run_cli("plan", "--plan-file", self.plan_path)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.created()), 3)
        self.assertIn("created #", text)

    def test_plan_local_run_with_claude(self):
        self.seed(self.fake)
        work = os.path.join(self.root, "work")
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": plan_data()}), stderr="")

        ctx, out = self.ctx("plan", "--gap", "--out-dir", work)
        with mock.patch("nightshift.cli.shutil.which", return_value="/usr/bin/claude"):
            rc = cli.cmd_plan(ctx, run=run)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][:2], ["claude", "-p"])
        self.assertIn("Read,Grep,Glob", calls[0])
        self.assertIn("Mode: **gap**", Path(work, "context.md").read_text())
        self.assertTrue(Path(work, "plan.json").is_file())
        self.assertIn(f"draft saved to {work}/plan.json", out.getvalue())
        self.assertEqual(len(self.created()), 3)

    def test_plan_without_claude_cli(self):
        ctx, _ = self.ctx("plan", "--gap", "--out-dir", os.path.join(self.root, "work"))
        with mock.patch("nightshift.cli.shutil.which", return_value=None), self.assertRaises(SystemExit) as cm:
            cli.cmd_plan(ctx)
        self.assertIn("--plan-file", str(cm.exception))

    # Regression: `plan context|publish` sub-subparsers used `parents=[common]`,
    # so their defaults (dry_run=False, repo=None, root=".") overwrite the same flags given before the
    # sub-command. `plan --dry-run publish FILE` therefore creates real issues.
    def test_plan_dry_run_before_publish_subcommand(self):
        self.seed(self.fake)
        out = io.StringIO()
        rc = cli.main(["plan", "--dry-run", "publish", self.plan_path, "--repo", "o/r", "--root", self.root],
                      transport=self.fake, out=out)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.writes(), [], "plan --dry-run publish must not create issues")

    # Regression (fixed): nightshift/cli.py:374 (publish_plan_file) treats a missing plan file as "{}": a typo or a planner
    # run that produced no plan.json exits 0 ("coverage: incomplete") and still posts a plan marker.
    def test_plan_publish_missing_file_is_an_error(self):
        self.seed(self.fake)
        rc, _ = self.run_cli("plan", "publish", os.path.join(self.root, "missing.json"))
        self.assertNotEqual(rc, 0)

    def test_plan_publish_missing_file_creates_nothing(self):
        self.seed(self.fake)
        rc, _ = self.run_cli("plan", "publish", os.path.join(self.root, "missing.json"))
        self.assertEqual(rc, 2)
        self.assertEqual(self.created(), {})
        self.assertEqual(self.fake.writes(), [])

    # --- plan context (workflow) ---
    def context(self, mode, *extra):
        out_dir = os.path.join(self.root, "ctx")
        gh_out = os.path.join(self.root, "gh_output")
        rc, text = self.run_cli("plan", "context", "--mode", mode, "--out-dir", out_dir, "--github-output", gh_out,
                                *extra)
        outputs = Path(gh_out).read_text() if os.path.exists(gh_out) else ""
        return rc, text, out_dir, outputs

    def test_refill_skips_when_lanes_full(self):
        for n, lane in ((10, "api"), (11, "api"), (12, "web"), (13, "web")):
            self.fake.add_issue(number=n, title=f"t{n}", labels=[f"lane:{lane}", "night-safe"])
        rc, text, out_dir, outputs = self.context("refill")
        self.assertEqual(rc, 0)
        self.assertIn("skip: every lane has at least 2 ready issues", text)
        self.assertEqual(outputs, "run=false\n")
        self.assertFalse(os.path.exists(out_dir))
        self.assertEqual(self.fake.writes(), [])

    def test_refill_skips_when_disabled(self):
        os.environ["AGENTS_ENABLED"] = "false"
        rc, text, _, outputs = self.context("refill")
        self.assertIn("skip: AGENTS_ENABLED is not true", text)
        self.assertEqual(outputs, "run=false\n")

    def test_refill_picks_emptiest_lane(self):
        self.seed(self.fake)
        for n, lane in ((10, "api"), (11, "api"), (12, "web")):
            self.fake.add_issue(number=n, title=f"t{n}", labels=[f"lane:{lane}", "night-safe"])
        rc, text, out_dir, outputs = self.context("refill")
        self.assertEqual(rc, 0)
        self.assertIn("lane web has 1 ready issues (< 2)", text)
        self.assertIn("run=true\nmode=gap\nlane=web\nissue=\n", outputs)
        self.assertIn("Mode: **gap**, lane `web` only", Path(out_dir, "context.md").read_text())
        self.assertEqual(json.loads(Path(out_dir, "schema.json").read_text()), prompts.SCHEMAS["planner"])
        self.assertIn(str(Path(out_dir, "context.md")), Path(out_dir, "prompt.md").read_text())

    def test_refill_stops_when_coverage_complete_for_current_docs(self):
        self.seed(self.fake)
        self.fake.add_comment(1, ns.marker("plan", lane="*", complete=True, docs_sha="abcdef123456"))
        rc, text, _, outputs = self.context("refill")
        self.assertIn("coverage is complete for the current docs", text)
        self.assertEqual(outputs, "run=false\n")

    def test_review_mode_refuses_commenter_without_write_access(self):
        self.fake.add_issue(number=5, title="Plan auth")
        with mock.patch.dict(os.environ, {"COMMENT_AUTHOR": "mallory", "COMMENT_BODY": "/night-shift plan do it",
                                          "ISSUE_NUMBER": "5"}):
            rc, text, out_dir, outputs = self.context("review")
        self.assertEqual(rc, 0)
        self.assertIn("needs a comment from a user with write access (got 'mallory')", text)
        self.assertEqual(outputs, "run=false\n")
        self.assertFalse(os.path.exists(out_dir))
        self.assertEqual(self.fake.writes(), [])

    def test_review_mode_refuses_other_comments(self):
        self.fake.permissions["alice"] = "admin"
        with mock.patch.dict(os.environ, {"COMMENT_AUTHOR": "alice", "COMMENT_BODY": "nice issue",
                                          "ISSUE_NUMBER": "5"}):
            rc, text, _, outputs = self.context("review")
        self.assertIn("skip:", text)
        self.assertEqual(outputs, "run=false\n")

    def test_review_mode_accepts_writer(self):
        self.fake.add_issue(number=5, title="Plan auth", body="We need SSO.")
        self.fake.permissions["alice"] = "write"
        with mock.patch.dict(os.environ, {"COMMENT_AUTHOR": "alice", "COMMENT_BODY": "/night-shift plan split it up",
                                          "ISSUE_NUMBER": "5"}):
            rc, text, out_dir, outputs = self.context("review")
        self.assertEqual(rc, 0)
        self.assertIn("run=true\nmode=review\nlane=\nissue=5\n", outputs)
        ctx_md = Path(out_dir, "context.md").read_text()
        self.assertIn("Issue #5: Plan auth", ctx_md)
        self.assertIn("We need SSO.", ctx_md)
        self.assertIn("Request from alice: split it up", ctx_md)

    def test_unknown_mode(self):
        with self.assertRaises(SystemExit):
            self.context("bogus")


# --- audit ----------------------------------------------------------------------------

class AuditTests(CliTestCase):
    def test_audit_report_on_csv(self):
        path = os.path.join(self.root, "audit.csv")
        rows = [{"pr": i, "issue": i + 10, "lane": "api", "title": f"PR {i}", "human_label": label,
                 "human_tests_meaningful": "yes"} for i, label in enumerate(["ok", "ok", "wrong", "trivial"], 1)]
        audit.write_csv(rows, path)
        rc, text = self.run_cli("audit", "--report", path)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls, [])
        self.assertIn(f"Audit of 4 merged agent PRs ({path})", text)
        self.assertIn("Labels: human (4 of 4 rows labelled)", text)
        self.assertIn("ok rate: 50%", text)

    def test_audit_report_bad_label_is_reported(self):
        path = os.path.join(self.root, "audit.csv")
        audit.write_csv([{"pr": 1, "human_label": "great"}], path)
        with self.assertRaises(ValueError):
            self.run_cli("audit", "--report", path)


# --- workflow-internal commands -------------------------------------------------------

class WorkflowCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = write_config(self.root, lane_extra={"api": {"setup": {"commands": ["pip install -e .", "make db"],
                                                                         "action": "./.github/actions/api-setup"},
                                                               "postgres_image": "postgres:16"}},
                                limits={"max_turns": 120})
        self.gh_out = os.path.join(self.root, "gh_output")

    def outputs(self):
        lines = Path(self.gh_out).read_text().splitlines()
        return dict(ln.split("=", 1) for ln in lines if "=" in ln)

    def test_lane_config(self):
        rc, text = self.run_cli("lane-config", "--lane", "api", "--github-output", self.gh_out)
        self.assertEqual(rc, 0)
        expected = {"postgres_image": "postgres:16", "setup_action": ".github/actions/api-setup", "max_turns": "120",
                    "test_command": "pytest"}
        self.assertEqual(self.outputs(), expected)
        self.assertEqual(json.loads(text), expected)
        self.assertEqual(self.fake.calls, [])

    def test_lane_config_unknown_lane(self):
        rc, _ = self.run_cli("lane-config", "--lane", "nope")
        self.assertEqual(rc, 2)

    def test_pick_writes_github_output(self):
        os.environ["AGENTS_ENABLED"] = "true"
        self.fake.add_issue(number=7, title="[A1] Do it\nnow", labels=["lane:api", "night-safe"])
        rc, text = self.run_cli("pick", "--lane", "api", "--github-output", self.gh_out)
        self.assertEqual(rc, 0)
        out = self.outputs()
        self.assertEqual(out["mode"], "issue")
        self.assertEqual(out["issue"], "7")
        self.assertEqual(out["model"], "sonnet")
        self.assertNotIn("\n", out["reason"])
        self.assertEqual(json.loads(text)["issue"], "7")

    def test_pick_kill_switch(self):
        self.fake.add_issue(number=7, title="x", labels=["lane:api", "night-safe"])
        rc, _ = self.run_cli("pick", "--lane", "api", "--github-output", self.gh_out)
        self.assertEqual(rc, 0)
        self.assertEqual(self.outputs()["mode"], "skip")
        self.assertIn("AGENTS_ENABLED is not true", self.outputs()["reason"])
        self.assertEqual(self.fake.writes(), [])

    def test_pick_dry_run_prints_picker_writes(self):
        os.environ["AGENTS_ENABLED"] = "true"

        def seed(f):
            f.add_issue(number=7, title="x", labels=["lane:api", "night-safe"])
            for _ in range(3):
                f.add_comment(7, ns.marker("attempt", issue=7, lane="api"))
        text, dry = self.assert_dry_run(["pick", "--lane", "api"], seed)
        self.assertNotIn("blocked", dry.label_names(7))

    def test_task_issue_mode_writes_files(self):
        self.fake.add_issue(number=7, title="[A1] Login", body="## Goal\nlog in", labels=["lane:api", "night-safe"])
        self.fake.add_comment(7, "Please use bcrypt", user="alice", association="MEMBER")
        out_dir = os.path.join(self.root, "task")
        rc, text = self.run_cli("task", "--lane", "api", "--mode", "issue", "--issue", "7", "--out-dir", out_dir,
                                "--run-url", "https://run/1", "--github-output", self.gh_out)
        self.assertEqual(rc, 0)
        task = Path(out_dir, "task.md").read_text()
        self.assertIn("# Issue #7: [A1] Login", task)
        self.assertIn("Please use bcrypt", task)
        prompt = Path(out_dir, "prompt.md").read_text()
        self.assertIn("lane `api`", prompt)
        self.assertIn("__TASK_FILE__", prompt)  # filled in by the workflow
        self.assertEqual(json.loads(Path(out_dir, "schema.json").read_text()), prompts.SCHEMAS["worker"])
        self.assertEqual(Path(out_dir, "setup.sh").read_text(), "pip install -e .\nmake db")
        out = self.outputs()
        self.assertEqual(out["branch"], "agent/api/issue-7")
        self.assertEqual(out["title"], "[A1] Login")
        self.assertTrue(out["attempt_comment"].isdigit())
        self.assertIn("agent:in-progress", self.fake.label_names(7))
        attempt = ns.trusted(self.fake.comments[7], "night-bot", "attempt")
        self.assertEqual(len(attempt), 1)
        self.assertIn("https://run/1", attempt[0].body)

    def test_task_dry_run_exact(self):
        out_dir = os.path.join(self.root, "task")

        def seed(f):
            f.add_issue(number=7, title="[A1] Login", labels=["lane:api", "night-safe", "waiting"])
        text, dry = self.assert_dry_run(["task", "--lane", "api", "--mode", "issue", "--issue", "7", "--out-dir",
                                         out_dir], seed)
        self.assertEqual(dry.label_names(7), {"lane:api", "night-safe", "waiting"})
        self.assertEqual(dry.comments[7], [])

    def test_task_fix_main_mode(self):
        self.fake.add_issue(number=1, title="Night-shift log", labels=["night-log"])
        self.fake.workflow_runs = [{"id": 99, "conclusion": "failure", "head_sha": "deadbeef"}]
        self.fake.jobs[99] = [{"id": 5, "name": "test", "conclusion": "failure"}]
        self.fake.job_logs[5] = "line\nAssertionError: boom"
        out_dir = os.path.join(self.root, "task")
        rc, _ = self.run_cli("task", "--lane", "api", "--mode", "fix-main", "--fail-sha", "deadbeefcafe",
                             "--out-dir", out_dir, "--github-output", self.gh_out)
        self.assertEqual(rc, 0)
        self.assertEqual(self.outputs()["branch"], "agent/api/fix-main-deadbee")
        self.assertIn("AssertionError: boom", Path(out_dir, "task.md").read_text())
        self.assertEqual(len(ns.trusted(self.fake.comments[1], "night-bot", "fix-attempt")), 1)

    def run_stdout(self, *argv):
        buf = io.StringIO()
        rc = cli.main(list(argv) + ["--root", self.root], transport=self.fake, out=buf)
        return rc, buf.getvalue()

    def test_prompt(self):
        rc, text = self.run_stdout("prompt", "review", "--set", "DIFF_FILE=/w/diff.patch", "--set",
                                   "ISSUE_FILE=/w/issue.md")
        self.assertEqual(rc, 0)
        self.assertIn("/w/diff.patch", text)
        self.assertIn("/w/issue.md", text)
        self.assertNotIn("__DIFF_FILE__", text)
        self.assertEqual(self.fake.calls, [])

    def test_prompt_override_from_repo(self):
        path = Path(self.root, ".night-shift", "prompts", "worker.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("custom for __LANE__\n", encoding="utf-8")
        rc, text = self.run_stdout("prompt", "worker", "--set", "LANE=api")
        self.assertEqual(text, "custom for api\n")

    def test_schema(self):
        for name in ("worker", "review", "planner"):
            rc, text = self.run_stdout("schema", name)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text), prompts.SCHEMAS[name])

    # Regression (fixed): nightshift/cli.py:451 (cmd_prompt) and :456 (cmd_schema) write to sys.stdout instead of ctx.out,
    # so `main(..., out=buf)` cannot capture them, unlike every other command.
    def test_schema_and_prompt_honour_out(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["schema", "worker", "--root", self.root], transport=self.fake, out=out)
            cli.main(["prompt", "worker", "--root", self.root], transport=self.fake, out=out)
        self.assertIn('"status"', out.getvalue())
        self.assertIn("night-shift agent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
