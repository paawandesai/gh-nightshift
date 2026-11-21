"""The merge gate counts a `review / ...` check run only when the review workflow itself produced it: a real Actions
job of a completed, successful night-shift-review.yml run on the same commit. CI code with `checks: write` can
create a check run with the gate's name (and as the same GitHub Actions app), so name and app prove nothing."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakegh import BOT, FakeGitHub  # noqa: E402

from nightshift.provenance import GateVerifier  # noqa: E402
from test_picker import GREEN, GREEN_MAIN, cfg, decide, ready  # noqa: E402

TEST_OK = GREEN[0]
VERDICT_OK = GREEN[1]
FORGED_OK = {**VERDICT_OK, "forged": True}
REQ = ["test", "review / verdict"]


class PickerIgnoresForgedGates(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1")

    def decide(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = decide(self.f)
        return result, out.getvalue()

    def merged(self):
        return bool([c for c in self.f.writes("PUT") if c[1].endswith("/merge")])

    def test_real_gate_still_merges(self):
        self.f.check_runs["s1"] = list(GREEN)
        result, _ = self.decide()
        self.assertIn("merged PR #10", result["reason"])

    def test_forged_gate_alone_does_not_merge(self):
        """W-01: CI with checks: write creates `review / verdict` = success before the real review reports."""
        self.f.check_runs["s1"] = [TEST_OK, FORGED_OK]
        result, out = self.decide()
        self.assertIn("still in review", result["reason"])
        self.assertFalse(self.merged())
        self.assertEqual(self.f.pulls[10]["state"], "open")
        self.assertIn("::warning::ignored a forged or unverifiable review gate `review / verdict`", out)
        self.assertIn("is not a GitHub Actions job", out)

    def test_forged_success_does_not_hide_a_running_review(self):
        self.f.check_runs["s1"] = [TEST_OK, FORGED_OK, {**VERDICT_OK, "status": "in_progress", "conclusion": None}]
        self.assertIn("still in review", self.decide()[0]["reason"])
        self.assertFalse(self.merged())

    def test_forged_success_does_not_override_a_failed_review(self):
        self.f.check_runs["s1"] = [TEST_OK, FORGED_OK, {**VERDICT_OK, "conclusion": "failure"}]
        self.decide()
        self.assertEqual(self.f.pulls[10]["state"], "closed")

    def test_gate_patched_to_success_while_its_run_is_still_going(self):
        self.f.check_runs["s1"] = list(GREEN)
        self.f.review_run_overrides["s1"] = {"status": "in_progress", "conclusion": None}
        result, out = self.decide()
        self.assertFalse(self.merged())
        self.assertIn("has not finished yet", out)

    def test_gate_patched_to_success_after_its_run_failed(self):
        self.f.check_runs["s1"] = list(GREEN)
        self.f.review_run_overrides["s1"] = {"conclusion": "failure"}
        result, out = self.decide()
        self.assertFalse(self.merged())
        self.assertIn("concluded `failure`", out)

    def test_forged_check_filed_under_the_review_run_does_not_count(self):
        """Seen live: GitHub files a Checks API check run as a runner-less job of an Actions run on the commit. On
        ns-sandbox it landed in the CI run; if it lands in a finished, passing review run, the runner test stops it."""
        self.f.check_runs["s1"] = [TEST_OK, {**VERDICT_OK, "runner_id": None}]
        result, out = self.decide()
        self.assertFalse(self.merged())
        self.assertIn("never ran on a runner", out)

    def test_skipped_required_gate_still_closes_without_a_runner(self):
        """A skipped job has no runner either; the runner test is for passes only, so a skipped gate still fails."""
        self.f.check_runs["s1"] = [TEST_OK, {**VERDICT_OK, "conclusion": "skipped", "runner_id": None}]
        self.decide()
        self.assertEqual(self.f.pulls[10]["state"], "closed")

    def test_gate_from_another_workflow_does_not_count(self):
        """A CI job `review` calling a reusable workflow with a job `verdict` is also named `review / verdict`."""
        self.f.check_runs["s1"] = list(GREEN)
        self.f.review_run_overrides["s1"] = {"path": ".github/workflows/ci.yml"}
        result, out = self.decide()
        self.assertFalse(self.merged())
        self.assertIn("comes from .github/workflows/ci.yml", out)


class Verifier(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()

    def check(self, sha="s1"):
        gh = self.f.gh(out=io.StringIO())
        return GateVerifier(gh).filter(gh.check_runs(sha), sha, [*REQ, "review / security"])

    def test_real_gates_are_kept_with_one_run_lookup(self):
        security = {**VERDICT_OK, "name": "review / security"}
        self.f.check_runs["s1"] = [TEST_OK, VERDICT_OK, security]
        kept, dropped = self.check()
        self.assertEqual([r["name"] for r in kept], ["test", "review / verdict", "review / security"])
        self.assertEqual(dropped, [])
        gets = [p for m, p, _ in self.f.calls if m == "GET" and "/actions/" in p]
        self.assertEqual(len(gets), 3, gets)  # two jobs, one shared workflow run

    def test_ordinary_checks_are_not_looked_up(self):
        self.f.check_runs["s1"] = [TEST_OK, {"name": "lint", "status": "completed", "conclusion": "success"}]
        self.check()
        self.assertFalse([p for m, p, _ in self.f.calls if "/actions/" in p])

    def test_other_app_is_rejected_without_a_lookup(self):
        self.f.check_runs["s1"] = [{**FORGED_OK, "app": {"slug": "some-ci", "id": 9}}]
        kept, dropped = self.check()
        self.assertEqual(kept, [])
        self.assertIn("app `some-ci`", dropped[0])
        self.assertFalse([p for m, p, _ in self.f.calls if "/actions/" in p])

    def test_wrong_event_or_commit_is_rejected(self):
        for override, why in (({"event": "workflow_dispatch"}, "started by `workflow_dispatch`"),
                              ({"head_sha": "other"}, "tested other")):
            self.f = FakeGitHub()
            self.f.check_runs["s1"] = [VERDICT_OK]
            self.f.review_run_overrides["s1"] = override
            kept, dropped = self.check()
            self.assertEqual(kept, [], why)
            self.assertIn(why, dropped[0])

    def test_unreadable_jobs_fail_closed(self):
        self.f.check_runs["s1"] = [VERDICT_OK]
        self.f.forbidden.add(r"actions/jobs/\d+")
        kept, dropped = self.check()
        self.assertEqual(kept, [])
        self.assertIn("Actions: read", dropped[0])

    def test_failed_real_gate_is_kept(self):
        self.f.check_runs["s1"] = [{**VERDICT_OK, "conclusion": "failure"}]
        kept, _ = self.check()
        self.assertEqual(kept[0]["conclusion"], "failure")


class JobsFallback(unittest.TestCase):
    """Fine-grained PATs read Actions jobs instead of check runs; each job carries its workflow run already."""

    def runs(self, path, runner_id=7):
        f = FakeGitHub()
        f.forbidden.add(r"commits/[^/]+/check-runs")
        f.workflow_runs.append({"id": 501, "event": "pull_request", "head_sha": "s1", "status": "completed",
                                "conclusion": "success", "path": path})
        f.jobs[501] = [dict(TEST_OK), {**VERDICT_OK, "runner_id": runner_id}]
        gh = f.gh(out=io.StringIO())
        kept, dropped = GateVerifier(gh).filter(gh.check_runs("s1"), "s1", REQ)
        self.assertFalse([p for m, p, _ in f.calls if "/actions/jobs/" in p or p.endswith("/actions/runs/501")])
        return [r["name"] for r in kept], dropped

    def test_review_caller_job_counts(self):
        self.assertEqual(self.runs(".github/workflows/night-shift-review.yml"), (REQ, []))

    def test_runner_less_job_in_the_review_run_does_not(self):
        names, dropped = self.runs(".github/workflows/night-shift-review.yml", runner_id=None)
        self.assertEqual(names, ["test"])
        self.assertIn("never ran on a runner", dropped[0])

    def test_same_name_job_in_ci_does_not(self):
        names, dropped = self.runs(".github/workflows/ci.yml")
        self.assertEqual(names, ["test"])
        self.assertIn("comes from .github/workflows/ci.yml", dropped[0])


class StatusUsesProvenance(unittest.TestCase):
    def test_forged_gate_shows_pending(self):
        from nightshift.rules import checks_state
        f = FakeGitHub()
        f.check_runs["s1"] = [TEST_OK, FORGED_OK]
        gh = f.gh(out=io.StringIO())
        gh.login = BOT
        kept, _ = GateVerifier(gh).filter(gh.check_runs("s1"), "s1", cfg().required_checks)
        self.assertEqual(checks_state(kept, [], cfg().required_checks), "pending")


if __name__ == "__main__":
    unittest.main()
