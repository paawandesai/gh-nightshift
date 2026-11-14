"""`gh nightshift doctor` and `check-pat` against the in-memory GitHub, with an injected `gh api -i user`."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakegh import FakeGitHub

from nightshift import cli, repo_setup, scaffold
from nightshift.config import loads

PAT = "github_pat_DO_NOT_PRINT_123"
PAT_ENV = "NS_TEST_PAT"


def config_text(lanes=("api", "web"), mode="strict", **extra):
    data = {"version": 1, "main_branch": "main", "ci_workflow": "ci.yml", "merge": {"mode": mode},
            "tool_ref": "v1.0.0",
            "lanes": [{"name": n, "paths": [f"{n}/"], "claude_token_secret": f"CLAUDE_TOKEN_{n.upper()}",
                       "test_command": "pytest"} for n in lanes]}
    data.update(extra)
    return scaffold.config_yaml(data)


def gh_user_output(login="alice", scopes=None):
    head = "HTTP/2.0 200 OK\nContent-Type: application/json; charset=utf-8\n"
    if scopes is not None:
        head += f"X-Oauth-Scopes: {scopes}\n"
    return head + "\n" + json.dumps({"login": login, "id": 1})


class FakeRun:
    """Stands in for subprocess.run(["gh", "api", "-i", "user"], ...): one answer for gh's own token,
    another for the PAT (recognised by GH_TOKEN in the env it is given)."""

    def __init__(self, own=("me", "repo, workflow"), pat=("bot", ""), pat_rc=0):
        self.own, self.pat, self.pat_rc = own, pat, pat_rc
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert args[:4] == ["gh", "api", "-i", "user"], args
        is_pat = (kwargs.get("env") or {}).get("GH_TOKEN") == PAT
        if is_pat and self.pat_rc:
            return SimpleNamespace(returncode=self.pat_rc, stdout="", stderr="HTTP 401: Bad credentials")
        login, scopes = self.pat if is_pat else self.own
        return SimpleNamespace(returncode=0, stdout=gh_user_output(login, scopes), stderr="")


class DoctorBase(unittest.TestCase):
    lanes = ("api", "web")
    mode = "strict"
    strip_header = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY",
                                                                  "GH_REPO", PAT_ENV)}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_repo(config_text(self.lanes, self.mode))
        self.fake = self.good_fake()

    def write_repo(self, text):
        cfg = loads(text)
        for rel, content in scaffold.files(cfg, text, self.root).items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if self.strip_header:
            # Some tests drop the header comment lines that mention `secrets[...]` / `secrets: inherit`, so their
            # own fixtures are the only source of those strings (doctor ignores comments; see the regression test).
            wf = self.root / ".github/workflows/night-shift.yml"
            wf.write_text("".join(ln for ln in wf.read_text().splitlines(True)
                                  if not (ln.startswith("#") and ("secrets[" in ln or "secrets: inherit" in ln))))
        ci = "name: CI\non:\n  push:\n  pull_request:\n" + ("  merge_group:\n" if self.mode == "queue" else "")
        ci += "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
        (self.root / ".github/workflows/ci.yml").write_text(ci, encoding="utf-8")
        self.cfg = cfg

    def good_fake(self):
        fake = FakeGitHub()
        repo_setup.setup(fake.gh(), self.cfg)
        fake.variables["AGENTS_ENABLED"] = "true"
        fake.secrets |= set(self.cfg.secret_names())
        fake.calls.clear()
        return fake

    def doctor(self, run=None, pat=True):
        argv = ["doctor", "--repo", "o/r", "--root", str(self.root)]
        if pat:
            argv += ["--pat-env", PAT_ENV]
            os.environ[PAT_ENV] = PAT
        out = io.StringIO()
        ctx = cli.Ctx(cli.parser().parse_args(argv), transport=self.fake, out=out)
        rc = cli.cmd_doctor(ctx, run=run or FakeRun())
        text = out.getvalue()
        self.assertNotIn(PAT, text, "doctor must never print the PAT")
        self.assertEqual(self.fake.writes(), [], "doctor is read-only")
        return rc, text

    def fail_lines(self, text):
        return [ln for ln in text.splitlines() if ln.startswith("✗")]

    def fix_after(self, text, needle):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("✗") and needle in ln:
                self.assertLess(i + 1, len(lines), f"no fix line after {ln!r}")
                self.assertTrue(lines[i + 1].strip().startswith("fix:"), f"no fix after {ln!r}")
                return lines[i + 1]
        self.fail(f"no FAIL line containing {needle!r} in:\n{text}")


class DoctorGoodTests(DoctorBase):
    def test_all_good_exits_zero(self):
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.fail_lines(text), [])
        self.assertIn("No problems found.", text)
        self.assertIn("✓ .night-shift/config.yml is valid (2 lanes: api, web)", text)
        self.assertIn("✓ all 4 secrets exist", text)
        self.assertIn("✓ labels exist", text)
        self.assertIn("✓ branch protection is strict", text)
        self.assertIn("✓ AGENTS_ENABLED=true", text)
        self.assertIn("✓ PAT authenticates as bot", text)
        self.assertIn("✓ gh authenticated as me", text)

    def test_without_pat_env_warns_only(self):
        rc, text = self.doctor(pat=False)
        self.assertEqual(rc, 0, text)
        self.assertIn("! PAT permissions not checked", text)

    def test_agents_disabled_is_a_warning(self):
        self.fake.variables["AGENTS_ENABLED"] = "false"
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("! AGENTS_ENABLED=false", text)

    def test_moving_tool_ref_is_a_warning(self):
        self.write_repo(config_text(self.lanes, self.mode, tool_ref="main"))
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("a moving branch", text)

    def test_own_token_without_workflow_scope_warns(self):
        rc, text = self.doctor(run=FakeRun(own=("me", "repo")))
        self.assertEqual(rc, 0, text)
        self.assertIn("lacks the `workflow` scope", text)
        self.assertIn("gh auth refresh -s workflow", text)


class DoctorGeneratedWorkflowTests(DoctorBase):
    strip_header = False

    # Regression (fixed): nightshift/doctor.py:81 searches the whole caller workflow, comments included, for `secrets[` and
    # `secrets: inherit`; the header scaffold.lane_workflow writes (nightshift/scaffold.py:78) contains both
    # phrases, so doctor reports FAIL "uses dynamic secrets" on the workflow init just generated.
    def test_freshly_generated_workflow_passes_doctor(self):
        rc, text = self.doctor()
        self.assertEqual(self.fail_lines(text), [])
        self.assertEqual(rc, 0, text)


class DoctorProblemTests(DoctorBase):
    def test_strict_false_protection_fails_with_fix(self):
        self.fake.protection["required_status_checks"]["strict"] = False
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        fails = [ln for ln in self.fail_lines(text) if "strict" in ln]
        self.assertEqual(len(fails), 1, text)
        fix = self.fix_after(text, "strict=false")
        self.assertIn("gh nightshift setup", fix)
        self.assertIn("strict=true", fix)
        self.assertIn("1 problem(s)", text)

    def test_unprotected_main_fails(self):
        self.fake.protection = None
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("run `gh nightshift setup`", self.fix_after(text, "main is not protected"))

    def test_required_check_missing(self):
        self.fake.protection["required_status_checks"]["checks"] = [{"context": "test", "app_id": 15368}]
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "required checks missing: review / verdict")

    def test_required_checks_not_bound_to_actions_warn(self):
        rsc = self.fake.protection["required_status_checks"]
        rsc["contexts"], rsc["checks"] = [c["context"] for c in rsc.pop("checks")], []
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("! required checks test, review / verdict can be passed by any app or commit status", text)

    def test_missing_secret_fails(self):
        self.fake.secrets.discard("CLAUDE_TOKEN_WEB")
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        fix = self.fix_after(text, "secret CLAUDE_TOKEN_WEB is missing")
        self.assertIn("gh secret set CLAUDE_TOKEN_WEB -R o/r", fix)
        self.assertNotIn("all 4 secrets exist", text)

    def test_unlistable_secrets_warn(self):
        self.fake.secrets.clear()
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("cannot list Actions secrets", text)

    def test_missing_labels_fail(self):
        del self.fake.labels["lane:web"]
        del self.fake.labels["night-safe"]
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        line = next(ln for ln in self.fail_lines(text) if "labels missing" in ln)
        self.assertIn("lane:web", line)
        self.assertIn("night-safe", line)
        self.assertIn("gh nightshift setup", self.fix_after(text, "labels missing"))

    def test_missing_variable_fails(self):
        del self.fake.variables["AGENTS_ENABLED"]
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "variable AGENTS_ENABLED is missing")

    def test_missing_night_log_fails(self):
        for issue in self.fake.issues.values():
            issue["state"] = "closed"
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "no open night-log issue")

    def test_caller_with_secrets_lookup_fails(self):
        wf = self.root / ".github/workflows/night-shift.yml"
        wf.write_text(wf.read_text().replace("${{ secrets.CLAUDE_TOKEN_API }}",
                                             "${{ secrets[format('CLAUDE_TOKEN_{0}', 'API')] }}"))
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("--regenerate", self.fix_after(text, "dynamic secrets"))

    def test_caller_with_secrets_inherit_fails(self):
        wf = self.root / ".github/workflows/night-shift.yml"
        text = wf.read_text()
        head, _, rest = text.partition("    secrets:\n      claude_token: ${{ secrets.CLAUDE_TOKEN_API }}\n")
        self.assertTrue(rest, "fixture: generated caller workflow changed shape")
        rest = rest.split("\n", 1)[1]  # drop the agent_pat line of lane-api
        wf.write_text(head + "    secrets: inherit\n" + rest)
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(out, "dynamic secrets")
        self.fix_after(out, "lane api's job does not pass secrets.CLAUDE_TOKEN_API")

    # Regression (fixed): nightshift/doctor.py:99 appends "✓ caller workflow has one job per lane with explicit secrets" in the
    # else branch of the tool_ref check, even when the same file just failed the dynamic-secrets or per-lane checks.
    def test_no_explicit_secrets_ok_line_when_caller_workflow_failed(self):
        wf = self.root / ".github/workflows/night-shift.yml"
        wf.write_text(wf.read_text().replace("${{ secrets.CLAUDE_TOKEN_API }}", "${{ secrets[inputs.token] }}"))
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertNotIn("explicit secrets", "\n".join(ln for ln in text.splitlines() if ln.startswith("✓")))

    def test_missing_caller_workflow_fails(self):
        (self.root / ".github/workflows/night-shift.yml").unlink()
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "night-shift.yml is missing")

    def test_lane_without_job_fails(self):
        cfg_path = self.root / ".night-shift/config.yml"
        text = config_text(("api", "web", "db"), self.mode)
        cfg_path.write_text(text, encoding="utf-8")  # config edited, workflows not regenerated
        rc, out = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("--regenerate", self.fix_after(out, "lane db has no job"))

    def test_missing_ci_workflow_fails(self):
        (self.root / ".github/workflows/ci.yml").unlink()
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "ci.yml not found")

    def test_missing_config_fails(self):
        (self.root / ".night-shift/config.yml").unlink()
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("gh nightshift init", self.fix_after(text, "not found"))

    def test_pat_with_workflow_scope_fails(self):
        rc, text = self.doctor(run=FakeRun(pat=("bot", "repo, workflow")))
        self.assertEqual(rc, 1)
        fix = self.fix_after(text, "PAT has the `workflow` scope")
        self.assertIn("without the Workflows permission", fix)

    def test_no_environment_warns_that_the_pat_is_a_repo_secret(self):
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("! the PAT is a repository secret", text)

    def test_environment_checks(self):
        self.write_repo(config_text(self.lanes, self.mode, environment="night-shift"))
        lane = (self.root / ".github/workflows/night-shift.yml").read_text()
        self.assertEqual(lane.count("environment: 'night-shift'"), len(self.lanes))
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "environment night-shift does not exist")
        self.fake.environments["night-shift"] = {"policy": None, "secrets": ["AGENT_PAT"]}
        rc, text = self.doctor()
        self.assertIn("! environment night-shift accepts every branch", text)
        self.assertIn("! repository secret AGENT_PAT still exists", text)
        self.assertNotIn("the PAT is a repository secret", text)
        self.fake.environments["night-shift"]["policy"] = {"protected_branches": True, "custom_branch_policies": False}
        self.fake.secrets.discard("AGENT_PAT")
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)
        self.assertIn("✓ the GitHub credential is environment night-shift's secret AGENT_PAT", text)
        self.assertNotIn("secret AGENT_PAT is missing", text)
        self.fake.environments["night-shift"]["secrets"] = ["OTHER"]
        rc, text = self.doctor()
        self.fix_after(text, "environment night-shift has no AGENT_PAT secret")

    def test_classic_pat_fails(self):
        rc, text = self.doctor(run=FakeRun(pat=("bot", "repo")))
        self.assertEqual(rc, 1, text)
        self.fix_after(text, "classic PAT with scopes repo")

    def test_pat_that_does_not_authenticate_fails(self):
        rc, text = self.doctor(run=FakeRun(pat_rc=1))
        self.assertEqual(rc, 1)
        self.fix_after(text, "the PAT does not authenticate")

    def test_pat_is_passed_only_through_env(self):
        run = FakeRun()
        self.doctor(run=run)
        pat_calls = [kw for args, kw in run.calls if (kw.get("env") or {}).get("GH_TOKEN") == PAT]
        self.assertEqual(len(pat_calls), 1)
        for args, _ in run.calls:
            self.assertNotIn(PAT, " ".join(args))


class DoctorQueueTests(DoctorBase):
    mode = "queue"

    def test_queue_mode_all_good(self):
        rc, text = self.doctor()
        self.assertEqual(rc, 0, text)

    def test_queue_without_ruleset_fails(self):
        self.fake.rulesets.clear()
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.fix_after(text, "no merge queue ruleset")

    def test_queue_without_merge_group_in_ci_fails(self):
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text(ci.read_text().replace("  merge_group:\n", ""))
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("merge_group", self.fix_after(text, "does not run on merge_group"))


class CheckPatTests(unittest.TestCase):
    def check(self, run):
        out = io.StringIO()
        ctx = cli.Ctx(cli.parser().parse_args(["check-pat", "--repo", "o/r"]), transport=FakeGitHub(), out=out)
        with mock.patch.dict(os.environ, {"GH_TOKEN": PAT}):
            rc = cli.cmd_check_pat(ctx, run=run)
        self.assertNotIn(PAT, out.getvalue())
        return rc, out.getvalue()

    def test_workflow_scope_fails(self):
        rc, text = self.check(FakeRun(pat=("bot", "repo, workflow")))
        self.assertEqual(rc, 1)
        self.assertIn("::error::", text)
        self.assertIn("`workflow` scope", text)

    def test_unauthenticated_fails(self):
        rc, text = self.check(FakeRun(pat_rc=1))
        self.assertEqual(rc, 1)
        self.assertIn("does not authenticate", text)

    def test_fine_grained_passes(self):
        rc, text = self.check(FakeRun(pat=("bot", None)))
        self.assertEqual(rc, 0, text)
        self.assertIn("✓ PAT authenticates as bot", text)
        self.assertIn("fine-grained PAT", text)
        self.assertIn("PAT can read the repo, contents, issues, pull requests, Actions and commit statuses", text)

    def check_denied(self, *paths):
        fake = FakeGitHub()
        fake.denied.update(paths)
        out = io.StringIO()
        ctx = cli.Ctx(cli.parser().parse_args(["check-pat", "--repo", "o/r"]), transport=fake, out=out)
        with mock.patch.dict(os.environ, {"GH_TOKEN": PAT}):
            rc = cli.cmd_check_pat(ctx, run=FakeRun(pat=("bot", None)))
        return rc, out.getvalue()

    def test_missing_pull_requests_permission_fails(self):
        # Seen live: a fine-grained PAT without Pull requests made `pick` die on a bare 404 listing closed PRs.
        rc, text = self.check_denied("pulls")
        self.assertEqual(rc, 1)
        self.assertIn("cannot read Pull requests on o/r (HTTP 404)", text)
        self.assertIn("Pull requests: Read and write", text)
        self.assertNotIn("cannot read Issues", text)

    def test_missing_statuses_permission_fails(self):
        rc, text = self.check_denied("commits/main/status")
        self.assertEqual(rc, 1)
        self.assertIn("cannot read Commit statuses on o/r", text)
        self.assertIn("Commit statuses: Read", text)

    def test_check_runs_are_not_probed(self):
        # Fine-grained PATs cannot be given Checks; GH.check_runs falls back to Actions jobs, so no FAIL here.
        rc, text = self.check_denied("commits/main/check-runs")
        self.assertEqual(rc, 0, text)

    def test_repo_not_selected_fails_once(self):
        rc, text = self.check_denied("")
        self.assertEqual(rc, 1)
        self.assertIn("the PAT cannot see o/r", text)
        self.assertEqual(text.count("cannot read"), 0)




class DoctorToolRepoTests(unittest.TestCase):
    def checks(self, fake, ref="main"):
        from nightshift.config import loads
        from nightshift.doctor import tool_checks

        cfg = loads(f"version: 1\ntool_ref: {ref}\nlanes:\n  - name: api\n    claude_token_secret: CLAUDE_TOKEN_A\n")
        gh = fake.gh()
        return [str(c) for c in tool_checks(gh, cfg)]

    def test_reachable(self):
        self.assertIn("reachable", self.checks(FakeGitHub())[0])

    def test_private_tool_repo_fails(self):
        f = FakeGitHub()
        f.tool_repo["private"] = True
        self.assertIn("is private", self.checks(f)[0])

    def test_missing_ref_fails(self):
        self.assertIn("does not exist", self.checks(FakeGitHub(), ref="v9.9.9")[0])

if __name__ == "__main__":
    unittest.main()


class DoctorMergeGateTests(DoctorBase):
    """W-01: CI that gives agent-written code a token able to fake the gate; W-02: a dropped review gate."""

    def write_wf(self, name, head, perms):
        (self.root / f".github/workflows/{name}").write_text(
            f"name: X\n{head}{perms}jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n",
            encoding="utf-8")

    def gate_warnings(self):
        from nightshift.doctor import WARN, workflow_checks
        return [c for c in workflow_checks(self.root, self.cfg) if c.level == WARN and "agent-written code" in c.what]

    def test_ci_granting_gate_writes_warns(self):
        self.write_wf("ci.yml", "on:\n  pull_request:\n",
                      "permissions:\n  contents: read\n  statuses: write\n  checks: write\n  pull-requests: write\n")
        (warning,) = self.gate_warnings()
        self.assertIn("checks, pull-requests, statuses", warning.what)
        self.assertIn("ci.yml", warning.what)
        rc, text = self.doctor()
        self.assertIn("! .github/workflows/ci.yml runs agent-written code", text)

    def test_each_gate_write_and_write_all_warn(self):
        for perms in ("permissions:\n  statuses: write\n", "permissions: write-all\n",
                      "permissions:\n  checks: 'write'\n", "permissions:\n  pull-requests: write\n"):
            self.write_wf("ci.yml", "on: [push, pull_request]\n", perms)
            self.assertEqual(len(self.gate_warnings()), 1, perms)

    def test_job_level_permissions_count(self):
        (self.root / ".github/workflows/ci.yml").write_text(
            "on:\n  pull_request:\njobs:\n  test:\n    permissions:\n      statuses: write\n    runs-on: x\n"
            "    steps:\n      - run: pytest\n", encoding="utf-8")
        self.assertEqual(len(self.gate_warnings()), 1)

    def test_other_pull_request_workflow_warns(self):
        self.write_wf("lint.yml", "on:\n  - pull_request\n", "permissions:\n  pull-requests: write\n")
        (warning,) = self.gate_warnings()
        self.assertIn("lint.yml", warning.what)

    def test_read_only_or_non_pr_workflows_do_not_warn(self):
        self.write_wf("ci.yml", "on:\n  pull_request:\n", "permissions:\n  contents: read\n  checks: read\n")
        self.write_wf("release.yml", "on:\n  push:\n    tags: ['v*']\n", "permissions:\n  contents: write\n  checks: write\n")
        self.write_wf("label.yml", "on:\n  issues:\n", "permissions:\n  pull-requests: write\n"
                      "# reads github.event.pull_request.number in a step\n")
        self.assertEqual(self.gate_warnings(), [])

    def test_generated_night_shift_workflows_are_not_flagged(self):
        # night-shift-review.yml grants pull-requests: write by design and runs no agent code.
        self.assertEqual(self.gate_warnings(), [])

    def test_review_gate_not_required_fails(self):
        self.write_repo(config_text(self.lanes, self.mode, required_checks=["test"]))
        self.fake = self.good_fake()
        rc, text = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("review / verdict", self.fix_after(text, "does not include `review / verdict`"))

    def test_security_gate_not_required_fails(self):
        from nightshift.doctor import FAIL, rules_checks
        self.cfg.security_enabled = True
        self.cfg.required_checks = ["test", "review / verdict"]  # load() adds it; a hand-built config may not
        fails = [c.what for c in rules_checks(self.root, self.cfg) if c.level == FAIL]
        self.assertTrue(any("`review / security`" in w for w in fails), fails)

    def test_default_and_security_configs_pass_the_gate_checks(self):
        from nightshift.doctor import FAIL, rules_checks
        for extra in ({}, {"security": {"enabled": True}}):
            cfg = loads(config_text(self.lanes, self.mode, **extra))
            self.assertFalse([c for c in rules_checks(self.root, cfg) if c.level == FAIL and "required_checks" in c.what])
