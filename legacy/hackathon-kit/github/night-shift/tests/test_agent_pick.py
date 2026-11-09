import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agent_pick as ap  # noqa: E402

BOT = "night-bot"
LOG = 500  # night-log issue number


class FakeGH:
    """Minimal fake of the gh calls agent_pick makes. comments: {issue: [(login, body), ...]}"""

    def __init__(self, prs=None, runs=None, issues=None, states=None, comments=None,
                 night_log=True, runs_error=False):
        self.prs = prs or []
        self.runs = runs or []
        self.issues = issues or []
        self.states = states or {}
        self.comments = comments or {}
        self.night_log = night_log
        self.runs_error = runs_error
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[:2] == ["run", "list"]:
            if self.runs_error:
                raise RuntimeError("HTTP 403")
            return json.dumps(self.runs)
        if args[:2] == ["issue", "list"]:
            if "night-log" in args:
                return json.dumps([{"number": LOG}] if self.night_log else [])
            return json.dumps(self.issues)
        if args[:2] == ["api", "user"]:
            return BOT + "\n"
        if args[0] == "api" and args[1] == "--paginate":
            num = int(args[2].split("/")[-2])
            jq = args[4]
            login = re.search(r'user\.login == "(.*?)"', jq).group(1)
            marker = re.search(r'contains\("(.*?)"\)', jq).group(1)
            ids = [str(1000 * num + i) for i, (who, body) in enumerate(self.comments.get(num, []))
                   if who == login and marker in body]
            return "\n".join(ids) + "\n"
        if args[0] == "api" and args[1] == "-X":
            return ""
        if args[0] == "api":
            num = int(args[1].split("/")[-1])
            if num not in self.states:
                raise RuntimeError("404")
            return json.dumps({"state": self.states[num]})
        return ""  # writes: close, comment, edit, merge

    def wrote(self, verb):
        return [c for c in self.calls if c[:2] == verb]


def cfg(**over):
    env = {"LANE": "api", "REPO": "o/r", "AGENTS_ENABLED": "true", "FIX_LANE": "api", "AGENT_LOGIN": BOT}
    env.update(over)
    return ap.Config(env)


def issue(n, wave=1, body="", labels=(), assignees=()):
    names = ["lane:api", "night-safe", f"wave:{wave}", *labels]
    return {"number": n, "title": f"t{n}", "body": body, "labels": [{"name": x} for x in names],
            "assignees": [{"login": a} for a in assignees]}


def pr(checks, mergeable="MERGEABLE", created="2099-01-01T00:00:00Z", labels=(), branch="agent/api/issue-1"):
    return {"number": 10, "headRefName": branch, "mergeable": mergeable, "statusCheckRollup": checks,
            "createdAt": created, "labels": [{"name": x} for x in labels]}


RUN_OK = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RUN_FAIL = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
RUN_PENDING = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]
RED_MAIN = [{"status": "completed", "conclusion": "failure", "event": "push", "headSha": "abc1234"}]


class PickTests(unittest.TestCase):
    def use(self, fake):
        ap.run_gh = fake
        return fake

    # --- gating -----------------------------------------------------------------------
    def test_kill_switch(self):
        fake = self.use(FakeGH(issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg(AGENTS_ENABLED="false"))["mode"], "skip")
        self.assertEqual(fake.calls, [])

    def test_only_lane_filter(self):
        fake = self.use(FakeGH(issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg(ONLY_LANE="web"))["mode"], "skip")
        self.assertEqual(fake.calls, [])

    # --- choosing issues --------------------------------------------------------------
    def test_picks_lowest_wave_then_number(self):
        self.use(FakeGH(issues=[issue(5, wave=2), issue(9, wave=1), issue(3, wave=1)]))
        self.assertEqual(ap.decide(cfg()), {"mode": "issue", "issue": "3", "reason": "picked #3: t3 (model sonnet)",
                                            "fail_sha": "", "model": "sonnet"})

    def test_respects_dependencies(self):
        self.use(FakeGH(issues=[issue(2, body="Blocked by #1, #7"), issue(4, body="depends on #1")],
                        states={1: "closed", 7: "open"}))
        self.assertEqual(ap.decide(cfg())["issue"], "4")

    def test_unknown_dependency_counts_as_unmet(self):
        self.use(FakeGH(issues=[issue(2, body="Blocked by #99")]))
        result = ap.decide(cfg())
        self.assertEqual(result["mode"], "skip")
        self.assertIn("waiting on dependencies", result["reason"])

    def test_skips_blocked_and_needs_human(self):
        self.use(FakeGH(issues=[issue(1, labels=["blocked"]), issue(2, labels=["needs-human"]), issue(3)]))
        self.assertEqual(ap.decide(cfg())["issue"], "3")

    def test_skips_human_and_integrator_even_if_night_safe(self):
        self.use(FakeGH(issues=[issue(1, labels=["human"]), issue(2, labels=["integrator"]), issue(3)]))
        self.assertEqual(ap.decide(cfg())["issue"], "3")

    def test_parse_deps_variants(self):
        self.assertEqual(ap.parse_deps("Blocked by: #3 and #4\nDepends on #5"), [3, 4, 5])
        self.assertEqual(ap.parse_deps("Parallel with #8"), [])
        self.assertEqual(ap.parse_deps(None), [])

    def test_parse_idea_factory_links_block(self):
        body = ("Story...\n<!-- idea-factory:deps -->\n### Links (generated)\n"
                "- Blocked by: #12, #13\n- Blocks: #20\n- Parallel with: #14\n- Source: `tickets/E01/W02/E01-S03.md`\n")
        self.assertEqual(ap.parse_deps(body), [12, 13])
        self.assertEqual(ap.parse_deps("- Blocked by: none\n- Blocks: #9"), [])
        self.assertEqual(ap.parse_deps("- Blocked by: #12, E02-S01"), [ap.UNRESOLVED, 12])

    def test_unresolved_dependency_blocks(self):
        self.use(FakeGH(issues=[issue(2, body="- Blocked by: E02-S01")]))
        self.assertEqual(ap.decide(cfg())["mode"], "skip")

    def test_idea_factory_wave_labels(self):
        a = issue(7); a["labels"] = [{"name": n} for n in ("lane:api", "night-safe", "wave:W02")]
        b = issue(9); b["labels"] = [{"name": n} for n in ("lane:api", "night-safe", "wave:W01")]
        self.use(FakeGH(issues=[a, b]))
        self.assertEqual(ap.decide(cfg())["issue"], "9")

    def test_assigned_issues_are_left_to_humans(self):
        self.use(FakeGH(issues=[issue(1, assignees=["paawan"]), issue(2)]))
        self.assertEqual(ap.decide(cfg())["issue"], "2")

    # --- attempt counting -------------------------------------------------------------
    def test_three_strikes_marks_blocked(self):
        tries = [(BOT, ap.ATTEMPT_MARKER)] * 3
        fake = self.use(FakeGH(issues=[issue(1), issue(2)], comments={1: tries}))
        self.assertEqual(ap.decide(cfg())["issue"], "2")
        self.assertIn(["issue", "edit", "1", "-R", "o/r", "--add-label", "blocked"], fake.calls)

    def test_forged_markers_from_outsiders_are_ignored(self):
        forged = [("random-user", ap.ATTEMPT_MARKER)] * 5
        fake = self.use(FakeGH(issues=[issue(1)], comments={1: forged}))
        self.assertEqual(ap.decide(cfg())["issue"], "1")
        self.assertFalse(fake.wrote(["issue", "edit"]))

    def test_infra_runs_have_their_own_higher_cap(self):
        self.use(FakeGH(issues=[issue(1)], comments={1: [(BOT, ap.INFRA_MARKER)] * 5}))
        self.assertEqual(ap.decide(cfg())["issue"], "1")
        fake = self.use(FakeGH(issues=[issue(1)], comments={1: [(BOT, ap.INFRA_MARKER)] * 6}))
        self.assertEqual(ap.decide(cfg())["mode"], "skip")
        self.assertTrue(fake.wrote(["issue", "edit"]))

    def test_login_is_looked_up_when_not_given(self):
        fake = self.use(FakeGH(issues=[issue(1)]))
        ap.decide(cfg(AGENT_LOGIN=""))
        self.assertIn(["api", "user", "--jq", ".login"], fake.calls)

    # --- model routing ----------------------------------------------------------------
    def test_lane_default_model(self):
        self.use(FakeGH(issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg(LANE_MODEL="haiku"))["model"], "haiku")

    def test_retry_escalates_model(self):
        self.use(FakeGH(issues=[issue(1)], comments={1: [(BOT, ap.ATTEMPT_MARKER)]}))
        self.assertEqual(ap.decide(cfg())["model"], "opus")

    def test_label_overrides_model(self):
        self.use(FakeGH(issues=[issue(1, labels=["model:opus"])]))
        self.assertEqual(ap.decide(cfg(LANE_MODEL="haiku"))["model"], "opus")

    def test_bad_model_values_are_ignored(self):
        self.use(FakeGH(issues=[issue(1, labels=["model:$(rm -rf /)"])]))
        self.assertEqual(ap.decide(cfg(LANE_MODEL="x; curl evil"))["model"], "sonnet")

    def test_fix_main_uses_strong_model(self):
        self.use(FakeGH(runs=RED_MAIN, issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg(ESCALATE_MODEL="claude-opus-5-5"))["model"], "claude-opus-5-5")

    # --- open PRs ---------------------------------------------------------------------
    def test_pending_pr_keeps_lane_busy(self):
        self.use(FakeGH(prs=[pr(RUN_PENDING)], issues=[issue(2)]))
        self.assertEqual(ap.decide(cfg())["mode"], "skip")

    def test_pr_with_no_checks_yet_is_pending(self):
        self.use(FakeGH(prs=[pr([])], issues=[issue(2)]))
        self.assertEqual(ap.decide(cfg())["mode"], "skip")

    def test_pr_without_checks_goes_stale(self):
        fake = self.use(FakeGH(prs=[pr([], created="2020-01-01T00:00:00Z")], issues=[issue(2)]))
        self.assertEqual(ap.decide(cfg())["issue"], "2")
        self.assertTrue(fake.wrote(["pr", "close"]))

    def test_failed_pr_is_closed_without_double_counting(self):
        fake = self.use(FakeGH(prs=[pr(RUN_FAIL)], issues=[issue(2)]))
        self.assertEqual(ap.decide(cfg())["issue"], "2")
        self.assertTrue(fake.wrote(["pr", "close"]))
        comment = [c for c in fake.calls if c[:3] == ["issue", "comment", "1"]]
        self.assertTrue(comment and ap.ATTEMPT_MARKER not in comment[0][-1])
        self.assertFalse([c for c in fake.calls if c[:3] == ["api", "-X", "PATCH"]])

    def test_review_error_refunds_the_attempt(self):
        comments = {1: [(BOT, ap.ATTEMPT_MARKER), (BOT, ap.ATTEMPT_MARKER)]}
        fake = self.use(FakeGH(prs=[pr(RUN_FAIL, labels=["agent", "review-error"])], issues=[issue(2)],
                               comments=comments))
        ap.decide(cfg())
        patch = [c for c in fake.calls if c[:3] == ["api", "-X", "PATCH"]]
        self.assertEqual(len(patch), 1)
        self.assertTrue(patch[0][3].endswith("/comments/1001"))  # the latest attempt marker
        self.assertIn(ap.INFRA_MARKER, patch[0][-1])

    def test_conflicted_pr_is_closed(self):
        fake = self.use(FakeGH(prs=[pr(RUN_OK, mergeable="CONFLICTING")], issues=[issue(2)]))
        self.assertEqual(ap.decide(cfg())["issue"], "2")
        self.assertTrue(fake.wrote(["pr", "close"]))

    def test_conflicted_pr_refunds_the_attempt(self):
        comments = {1: [(BOT, ap.ATTEMPT_MARKER)]}
        fake = self.use(FakeGH(prs=[pr(RUN_OK, mergeable="CONFLICTING")], issues=[issue(2)],
                               comments=comments))
        ap.decide(cfg())
        patch = [c for c in fake.calls if c[:3] == ["api", "-X", "PATCH"]]
        self.assertEqual(len(patch), 1)
        self.assertIn(ap.INFRA_MARKER, patch[0][-1])

    def test_green_pr_is_merged_and_run_stops(self):
        checks = RUN_OK + [{"__typename": "StatusContext", "state": "SUCCESS"}]
        fake = self.use(FakeGH(prs=[pr(checks)], issues=[issue(1), issue(2)]))
        result = ap.decide(cfg())
        self.assertTrue(fake.wrote(["pr", "merge"]))
        self.assertEqual(result["mode"], "skip")          # never re-picks #1 before GitHub closes it
        self.assertIn("merged PR #10", result["reason"])

    def test_unblocking_gives_fresh_attempts(self):
        history = [(BOT, ap.ATTEMPT_MARKER)] * 3 + [(BOT, "Night shift: 3 failed attempts; marking blocked for a human.")]
        self.use(FakeGH(issues=[issue(1)], comments={1: history}))
        result = ap.decide(cfg())
        self.assertEqual(result["issue"], "1")
        self.assertEqual(result["model"], "sonnet")       # fresh start: no escalation either

    def test_attempts_after_unblock_still_count(self):
        history = ([(BOT, ap.ATTEMPT_MARKER)] * 3 + [(BOT, ap.BLOCK_MARKER + " blocked")]
                   + [(BOT, ap.ATTEMPT_MARKER)] * 3)
        fake = self.use(FakeGH(issues=[issue(1)], comments={1: history}))
        self.assertEqual(ap.decide(cfg())["mode"], "skip")
        self.assertTrue(fake.wrote(["issue", "edit"]))

    # --- main health ------------------------------------------------------------------
    def test_red_main_fix_lane(self):
        self.use(FakeGH(runs=RED_MAIN, issues=[issue(1)]))
        result = ap.decide(cfg())
        self.assertEqual((result["mode"], result["fail_sha"]), ("fix-main", "abc1234"))

    def test_fix_main_gives_up_after_max_attempts(self):
        marks = [(BOT, ap.fix_marker("abc1234"))] * 3
        self.use(FakeGH(runs=RED_MAIN, issues=[issue(1)], comments={LOG: marks}))
        result = ap.decide(cfg())
        self.assertEqual(result["mode"], "skip")
        self.assertIn("gave up", result["reason"])

    def test_fix_main_attempts_on_older_commit_do_not_count(self):
        marks = [(BOT, ap.fix_marker("0ld5ha0"))] * 3
        self.use(FakeGH(runs=RED_MAIN, issues=[issue(1)], comments={LOG: marks}))
        self.assertEqual(ap.decide(cfg())["mode"], "fix-main")

    def test_red_main_other_lane_waits(self):
        runs = [{"status": "in_progress", "conclusion": "", "event": "push"}] + RED_MAIN
        self.use(FakeGH(runs=runs, issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg(FIX_LANE="web"))["mode"], "skip")

    def test_green_main_after_red(self):
        runs = [{"status": "completed", "conclusion": "success", "event": "push"}] + RED_MAIN
        self.use(FakeGH(runs=runs, issues=[issue(1)]))
        self.assertEqual(ap.decide(cfg())["mode"], "issue")

    def test_unreadable_ci_fails_closed(self):
        self.use(FakeGH(runs_error=True, issues=[issue(1)]))
        result = ap.decide(cfg())
        self.assertEqual(result["mode"], "skip")
        self.assertIn("Actions: Read", result["reason"])


if __name__ == "__main__":
    unittest.main()
