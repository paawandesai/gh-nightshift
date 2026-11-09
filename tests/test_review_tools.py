"""Review gate inputs (drift report over a real git repo) and the review comment renderer."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fakegh import FakeGitHub

from nightshift import cli
from nightshift.review_tools import drift_report, git_in, render_report, write_inputs

GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
          "-c", "init.defaultBranch=main"]


def git(repo, *args):
    r = subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def write(repo, rel, text):
    path = Path(repo, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


class DriftRepo:
    """main with app.py and util.py; branch agent/api/issue-7 edits app.py; main then moves."""

    def __init__(self, root, overlap=True):
        self.root = root
        git(root, "init", "-q")
        git(root, "checkout", "-q", "-B", "main")
        write(root, "app.py", "def handler():\n    return 1\n")
        write(root, "util.py", "X = 1\n")
        commit_all(root, "base")
        git(root, "checkout", "-q", "-b", "agent/api/issue-7")
        write(root, "app.py", "def handler():\n    return 2  # PR change\n")
        write(root, "pr_only.py", "NEW = True\n")
        commit_all(root, "pr work")
        git(root, "checkout", "-q", "main")
        if overlap:
            write(root, "app.py", "def handler():\n    return 1\n\n\ndef main_side_helper():\n    pass\n")
        write(root, "util.py", "X = 2\n")
        commit_all(root, "main moves")
        git(root, "checkout", "-q", "agent/api/issue-7")


class DriftReportTests(unittest.TestCase):
    def test_overlap_shows_stat_and_main_side_diff(self):
        with tempfile.TemporaryDirectory() as d:
            DriftRepo(d, overlap=True)
            pr_diff, drift, overlap = drift_report(git_in(d), "main")
            self.assertEqual(overlap, ["app.py"])
            self.assertIn("return 2  # PR change", pr_diff)
            self.assertIn("pr_only.py", pr_diff)
            self.assertIn("--stat", drift)
            # The stat lists both files main changed.
            stat = drift.split("```")[1]
            self.assertIn("app.py", stat)
            self.assertIn("util.py", stat)
            self.assertIn("2 files changed", stat)
            self.assertIn("## Files changed both by this PR and on main", drift)
            self.assertIn("### app.py", drift)
            self.assertIn("+def main_side_helper():", drift)
            # Only the main-side diff of the overlapping file, not the PR's own change, not util.py's diff.
            overlap_part = drift.split("### app.py", 1)[1]
            self.assertNotIn("PR change", overlap_part)
            self.assertNotIn("X = 2", overlap_part)
            self.assertNotIn("No file is changed both", drift)

    def test_no_overlap_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            DriftRepo(d, overlap=False)
            _, drift, overlap = drift_report(git_in(d), "main")
            self.assertEqual(overlap, [])
            self.assertIn("util.py", drift)
            self.assertIn("No file is changed both by this PR and on main", drift)
            self.assertNotIn("### ", drift)

    def test_main_not_moved(self):
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            git(d, "checkout", "-q", "-B", "main")
            write(d, "a.txt", "a\n")
            commit_all(d, "base")
            git(d, "checkout", "-q", "-b", "agent/api/issue-1")
            write(d, "a.txt", "b\n")
            commit_all(d, "pr")
            _, drift, overlap = drift_report(git_in(d), "main")
            self.assertEqual(overlap, [])
            self.assertIn("(nothing: main has not moved)", drift)

    def test_git_in_raises_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                git_in(d)(["rev-parse", "HEAD"])


class WriteInputsTests(unittest.TestCase):
    def test_writes_diff_drift_and_issue(self):
        fake = FakeGitHub()
        fake.add_issue(number=7, title="Add handler", body="## Goal\nreturn 2\n")
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            DriftRepo(d, overlap=True)
            values = write_inputs(fake.gh(), git_in(d), "main", "agent/api/issue-7", out)
            self.assertEqual(values, {"issue": "7", "overlap": "1"})
            self.assertIn("PR change", Path(out, "diff.patch").read_text())
            self.assertIn("### app.py", Path(out, "drift.md").read_text())
            self.assertIn("# Issue #7: Add handler", Path(out, "issue.md").read_text())
            self.assertEqual(fake.writes(), [])

    def test_fix_main_branch_has_no_issue(self):
        fake = FakeGitHub()
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            DriftRepo(d, overlap=False)
            values = write_inputs(fake.gh(), git_in(d), "main", "agent/api/fix-main-abc1234", out)
            self.assertEqual(values["issue"], "")
            self.assertIn("fixes failing CI on main", Path(out, "issue.md").read_text())

    def test_review_inputs_command(self):
        fake = FakeGitHub()
        fake.add_issue(number=7, title="Add handler")
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            DriftRepo(d, overlap=True)
            gh_out = Path(out, "gh_output")
            buf = io.StringIO()
            rc = cli.main(["review-inputs", "--repo", "o/r", "--pr-dir", d, "--base-ref", "main",
                           "--head-ref", "agent/api/issue-7", "--out-dir", str(Path(out, "work")),
                           "--github-output", str(gh_out)], transport=fake, out=buf)
            self.assertEqual(rc, 0)
            self.assertIn("issue=7\n", gh_out.read_text())
            self.assertIn("overlap=1\n", gh_out.read_text())
            self.assertTrue(Path(out, "work", "drift.md").is_file())


def result_file(d, data, name="review.json"):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    return path


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_pass(self):
        path = result_file(self.d, {"structured_output": {
            "verdict": "pass", "main_changed_overlap": False,
            "answers": [{"question": "Tests cover the acceptance criteria", "answer": "yes", "evidence": "test_x"},
                        {"question": "main_drift: did a main change break this PR", "answer": "no",
                         "evidence": "no overlap"}]}})
        verdict, comment = render_report(path)
        self.assertEqual(verdict, "pass")
        self.assertIn("### Night-shift review: **pass**", comment)
        self.assertIn("- ✅ **Tests cover the acceptance criteria**: test_x", comment)
        self.assertIn("- ✅ **main_drift: did a main change break this PR**: no overlap", comment)
        self.assertNotIn("⚠️", comment)
        self.assertNotIn("Main changed under this PR", comment)

    def test_fail_with_fixes(self):
        path = result_file(self.d, {"structured_output": {
            "verdict": "fail", "main_changed_overlap": False, "fixes": "add a test for the empty case",
            "answers": [{"question": "Edge cases handled", "answer": "no", "evidence": "empty list crashes"}]}})
        verdict, comment = render_report(path)
        self.assertEqual(verdict, "fail")
        self.assertIn("**fail**", comment)
        self.assertIn("- ❌ **Edge cases handled**: empty list crashes", comment)
        self.assertIn("**Fixes needed:** add a test for the empty case", comment)

    def test_main_drift_yes_shows_warning(self):
        path = result_file(self.d, {"structured_output": {
            "verdict": "pass", "main_changed_overlap": True, "still_correct_against_main": "yes, rebased logic ok",
            "answers": [{"question": "main_drift: did a main change touch the same code", "answer": "yes",
                         "evidence": "app.py changed on main"}]}})
        verdict, comment = render_report(path)
        self.assertEqual(verdict, "pass")
        self.assertIn("- ⚠️ **main_drift: did a main change touch the same code**: app.py changed on main", comment)
        self.assertIn("**Main changed under this PR.** Still correct against current main: yes, rebased logic ok",
                      comment)

    def test_missing_file_is_error(self):
        verdict, comment = render_report(os.path.join(self.d, "nope.json"))
        self.assertEqual(verdict, "error")
        self.assertIn("failed to produce a verdict", comment)

    def test_garbled_file_is_error(self):
        verdict, _ = render_report(result_file(self.d, "{not json"))
        self.assertEqual(verdict, "error")

    def test_unknown_verdict_or_wrong_shape_is_error(self):
        self.assertEqual(render_report(result_file(self.d, {"structured_output": {"verdict": "maybe"}}))[0], "error")
        self.assertEqual(render_report(result_file(self.d, {"structured_output": None}))[0], "error")
        self.assertEqual(render_report(result_file(self.d, "[1, 2]"))[0], "error")

    def test_review_report_command(self):
        path = result_file(self.d, {"structured_output": {"verdict": "fail", "answers": [],
                                                          "main_changed_overlap": False}})
        out_md, gh_out = os.path.join(self.d, "comment.md"), os.path.join(self.d, "gh_output")
        buf = io.StringIO()
        rc = cli.main(["review-report", "--repo", "o/r", "--result", path, "--out", out_md,
                       "--github-output", gh_out], transport=FakeGitHub(), out=buf)
        self.assertEqual(rc, 0)
        self.assertIn("verdict=fail", buf.getvalue())
        self.assertEqual(Path(gh_out).read_text(), "verdict=fail\n")
        self.assertIn("**fail**", Path(out_md).read_text())

    def test_review_report_command_error(self):
        out_md, gh_out = os.path.join(self.d, "comment.md"), os.path.join(self.d, "gh_output")
        rc = cli.main(["review-report", "--repo", "o/r", "--result", os.path.join(self.d, "missing.json"),
                       "--out", out_md, "--github-output", gh_out], transport=FakeGitHub(), out=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(Path(gh_out).read_text(), "verdict=error\n")




class DriftEnforcement(unittest.TestCase):
    def write(self, out):
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"structured_output": out}, fh)
        fh.close()
        return fh.name

    def test_pass_without_still_correct_fails_when_files_overlap(self):
        from nightshift.review_tools import render_report

        path = self.write({"verdict": "pass", "answers": [], "main_changed_overlap": False})
        self.assertEqual(render_report(path, overlap=0)[0], "pass")
        verdict, comment = render_report(path, overlap=2)
        self.assertEqual(verdict, "fail")
        self.assertIn("2 overlapping file(s)", comment)

    def test_pass_with_still_correct_stays_pass(self):
        from nightshift.review_tools import render_report

        path = self.write({"verdict": "pass", "answers": [], "main_changed_overlap": True,
                           "still_correct_against_main": "yes: main only renamed a helper this PR does not use"})
        self.assertEqual(render_report(path, overlap=1)[0], "pass")

if __name__ == "__main__":
    unittest.main()
