"""Picker tests. The first block ports every test of the hackathon kit's test_agent_pick.py
(legacy/hackathon-kit/github/night-shift/tests/) to the REST picker; the rest cover what is new:
strict-mode update-branch (add-on A), close reason codes (add-on D), and hand-offs (step 3a)."""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakegh import BOT, FakeGitHub  # noqa: E402

from nightshift import protocol as ns  # noqa: E402
from nightshift.config import loads  # noqa: E402
from nightshift.picker import Picker, Runtime  # noqa: E402
from nightshift.rules import parse_deps, UNRESOLVED  # noqa: E402

CONFIG = """
version: 1
fix_lane: api
required_checks: [test, "review / verdict"]
lanes:
  - name: api
    claude_token_secret: CLAUDE_TOKEN_A
    model: sonnet
  - name: web
    claude_token_secret: CLAUDE_TOKEN_B
"""

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"},
         {"name": "review / verdict", "status": "completed", "conclusion": "success"}]
FAILED_TEST = [{"name": "test", "status": "completed", "conclusion": "failure"},
               {"name": "review / verdict", "status": "completed", "conclusion": "success"}]
FAILED_VERDICT = [{"name": "test", "status": "completed", "conclusion": "success"},
                  {"name": "review / verdict", "status": "completed", "conclusion": "failure"}]
PENDING = [{"name": "test", "status": "in_progress", "conclusion": None}]
RED_MAIN = [{"status": "completed", "conclusion": "failure", "event": "push", "head_sha": "abc1234"}]
GREEN_MAIN = [{"status": "completed", "conclusion": "success", "event": "push", "head_sha": "def5678"}]


def cfg(text=CONFIG):
    return loads(text)


def ready(fake, number, wave=1, body="", labels=(), lane="api", assignees=()):
    return fake.add_issue(number, title=f"t{number}", body=body,
                          labels=[f"lane:{lane}", "night-safe", f"wave:{wave}", *labels], assignees=assignees)


def decide(fake, lane="api", config=None, **rt):
    rt.setdefault("enabled", True)
    gh = fake.gh(out=io.StringIO())
    gh.login = BOT
    return Picker(gh, config or cfg(), Runtime(lane=lane, **rt)).decide()


class Ported(unittest.TestCase):
    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)

    # --- gating -------------------------------------------------------------------------
    def test_kill_switch(self):
        ready(self.f, 1)
        self.assertEqual(decide(self.f, enabled=False)["mode"], "skip")
        self.assertEqual(self.f.calls, [])

    def test_only_lane_filter(self):
        ready(self.f, 1)
        self.assertEqual(decide(self.f, only_lane="web")["mode"], "skip")
        self.assertEqual(self.f.calls, [])

    # --- choosing issues ----------------------------------------------------------------
    def test_picks_lowest_wave_then_number(self):
        ready(self.f, 5, wave=2), ready(self.f, 9, wave=1), ready(self.f, 3, wave=1)
        self.assertEqual(decide(self.f), {"mode": "issue", "issue": "3", "reason": "picked #3: t3 (model sonnet)",
                                          "fail_sha": "", "model": "sonnet"})

    def test_respects_dependencies(self):
        self.f.add_issue(1, state="closed"), self.f.add_issue(7)
        ready(self.f, 2, body="Blocked by #1, #7"), ready(self.f, 4, body="depends on #1")
        self.assertEqual(decide(self.f)["issue"], "4")

    def test_unknown_dependency_counts_as_unmet(self):
        ready(self.f, 2, body="Blocked by #99")
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("waiting on dependencies", result["reason"])

    def test_skips_blocked_and_needs_human(self):
        ready(self.f, 1, labels=["blocked"]), ready(self.f, 2, labels=["needs-human"]), ready(self.f, 3)
        self.assertEqual(decide(self.f)["issue"], "3")

    def test_skips_human_and_integrator_even_if_night_safe(self):
        ready(self.f, 1, labels=["human"]), ready(self.f, 2, labels=["integrator"]), ready(self.f, 3)
        self.assertEqual(decide(self.f)["issue"], "3")

    def test_skips_proposed(self):
        ready(self.f, 1, labels=["proposed"]), ready(self.f, 2)
        self.assertEqual(decide(self.f)["issue"], "2")

    def test_parse_deps_variants(self):
        self.assertEqual(parse_deps("Blocked by: #3 and #4\nDepends on #5"), [3, 4, 5])
        self.assertEqual(parse_deps("Parallel with #8"), [])
        self.assertEqual(parse_deps(None), [])

    def test_parse_idea_factory_links_block(self):
        body = ("Story...\n<!-- idea-factory:deps -->\n### Links (generated)\n"
                "- Blocked by: #12, #13\n- Blocks: #20\n- Parallel with: #14\n- Source: `tickets/E01/W02/E01-S03.md`\n")
        self.assertEqual(parse_deps(body), [12, 13])
        self.assertEqual(parse_deps("- Blocked by: none\n- Blocks: #9"), [])
        self.assertEqual(parse_deps("- Blocked by: #12, E02-S01"), [UNRESOLVED, 12])

    def test_unresolved_dependency_blocks(self):
        ready(self.f, 2, body="- Blocked by: E02-S01")
        self.assertEqual(decide(self.f)["mode"], "skip")

    def test_idea_factory_wave_labels(self):
        self.f.add_issue(7, labels=["lane:api", "night-safe", "wave:W02"])
        self.f.add_issue(9, labels=["lane:api", "night-safe", "wave:W01"])
        self.assertEqual(decide(self.f)["issue"], "9")

    def test_assigned_issues_are_left_to_humans(self):
        ready(self.f, 1, assignees=["paawan"]), ready(self.f, 2)
        self.assertEqual(decide(self.f)["issue"], "2")

    # --- attempt counting ---------------------------------------------------------------
    def test_three_strikes_marks_blocked(self):
        ready(self.f, 1), ready(self.f, 2)
        for _ in range(3):
            self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.assertEqual(decide(self.f)["issue"], "2")
        self.assertIn("blocked", self.f.label_names(1))
        self.assertTrue(any('"type":"blocked"' in b for b in self.f.bodies(1)))

    def test_legacy_attempt_markers_still_count(self):
        ready(self.f, 1), ready(self.f, 2)
        for _ in range(3):
            self.f.add_comment(1, "<!-- agent-attempt -->\nstarted")
        self.assertEqual(decide(self.f)["issue"], "2")

    def test_forged_markers_from_outsiders_are_ignored(self):
        ready(self.f, 1)
        for _ in range(5):
            self.f.add_comment(1, ns.marker("attempt", issue=1), user="mallory", association="NONE")
        self.f.add_comment(1, ns.marker("attempt", issue=1), user="owner", association="OWNER")
        self.assertEqual(decide(self.f)["issue"], "1")

    def test_infra_runs_have_their_own_higher_cap(self):
        ready(self.f, 1)
        for _ in range(5):
            self.f.add_comment(1, ns.marker("infra", issue=1))
        self.assertEqual(decide(self.f)["issue"], "1")
        self.f.add_comment(1, ns.marker("infra", issue=1))
        self.assertEqual(decide(self.f)["mode"], "skip")

    def test_login_is_looked_up_when_not_given(self):
        ready(self.f, 1)
        gh = self.f.gh(out=io.StringIO())
        Picker(gh, cfg(), Runtime(lane="api", enabled=True)).decide()
        self.assertIn(("GET", "user", None), self.f.calls)

    # --- model routing ------------------------------------------------------------------
    def test_lane_default_model(self):
        ready(self.f, 1)
        self.assertEqual(decide(self.f)["model"], "sonnet")

    def test_retry_escalates_model(self):
        ready(self.f, 1)
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.assertEqual(decide(self.f)["model"], "opus")

    def test_label_overrides_model(self):
        ready(self.f, 1, labels=["model:haiku"])
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.assertEqual(decide(self.f)["model"], "haiku")

    def test_bad_model_values_are_ignored(self):
        ready(self.f, 1, labels=["model:$(rm -rf)"])
        self.assertEqual(decide(self.f, lane_model="", escalate_model="")["model"], "sonnet")

    def test_fix_main_uses_strong_model(self):
        self.f.workflow_runs = list(RED_MAIN)
        self.assertEqual(decide(self.f)["model"], "opus")

    # --- open PRs -----------------------------------------------------------------------
    def test_pending_pr_keeps_lane_busy(self):
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1")
        self.f.check_runs["s1"] = list(PENDING)
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("still in review", result["reason"])

    def test_pr_with_no_checks_yet_is_pending(self):
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1")
        self.assertIn("still in review", decide(self.f)["reason"])

    def test_pr_without_checks_goes_stale(self):
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1", created_at="2000-01-01T00:00:00Z")
        self.assertEqual(decide(self.f)["mode"], "issue")
        self.assertEqual(self.f.pulls[10]["state"], "closed")
        self.assertIn("stale", ns.pr_reason(self.f.comments[10], BOT))

    def test_failed_pr_is_closed_without_double_counting(self):
        ready(self.f, 1)
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.f.add_pull(10, sha="s1")
        self.f.check_runs["s1"] = list(FAILED_TEST)
        decide(self.f)
        self.assertEqual(self.f.pulls[10]["state"], "closed")
        self.assertEqual(ns.count(self.f.comments[1], BOT, "attempt"), 1)
        self.assertNotIn("agent/api/issue-1", self.f.refs)

    def test_review_error_refunds_the_attempt(self):
        ready(self.f, 1)
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.f.add_pull(10, sha="s1", labels=("agent", "review-error"))
        self.f.check_runs["s1"] = list(FAILED_VERDICT)
        decide(self.f)
        self.assertEqual(ns.count(self.f.comments[1], BOT, "attempt"), 0)
        self.assertEqual(ns.count(self.f.comments[1], BOT, "infra"), 1)

    def test_conflicted_pr_is_closed(self):
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1", mergeable=False, mergeable_state="dirty")
        decide(self.f)
        self.assertEqual(self.f.pulls[10]["state"], "closed")

    def test_conflicted_pr_refunds_the_attempt(self):
        ready(self.f, 1)
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.f.add_pull(10, sha="s1", mergeable=False, mergeable_state="dirty")
        decide(self.f)
        self.assertEqual(ns.count(self.f.comments[1], BOT, "attempt"), 0)

    def test_green_pr_is_merged_and_run_stops(self):
        ready(self.f, 1), ready(self.f, 2)
        self.f.add_pull(10, sha="s1")
        self.f.check_runs["s1"] = list(GREEN)
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("merged PR #10", result["reason"])
        self.assertTrue(self.f.pulls[10]["merged_at"])

    def test_unblocking_gives_fresh_attempts(self):
        ready(self.f, 1)
        for _ in range(3):
            self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.f.add_comment(1, ns.marker("blocked", issue=1))
        self.assertEqual(decide(self.f)["issue"], "1")

    def test_attempts_after_unblock_still_count(self):
        ready(self.f, 1)
        self.f.add_comment(1, ns.marker("blocked", issue=1))
        for _ in range(3):
            self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.assertEqual(decide(self.f)["mode"], "skip")

    # --- main health --------------------------------------------------------------------
    def test_red_main_fix_lane(self):
        self.f.workflow_runs = list(RED_MAIN)
        result = decide(self.f)
        self.assertEqual((result["mode"], result["fail_sha"]), ("fix-main", "abc1234"))

    def test_fix_main_gives_up_after_max_attempts(self):
        self.f.workflow_runs = list(RED_MAIN)
        self.f.add_issue(500, labels=["night-log"])
        for _ in range(3):
            self.f.add_comment(500, ns.marker("fix-attempt", sha="abc1234"))
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("fix-main gave up", result["reason"])

    def test_fix_main_attempts_on_older_commit_do_not_count(self):
        self.f.workflow_runs = list(RED_MAIN)
        self.f.add_issue(500, labels=["night-log"])
        for _ in range(3):
            self.f.add_comment(500, ns.marker("fix-attempt", sha="0ld0000"))
        self.assertEqual(decide(self.f)["mode"], "fix-main")

    def test_red_main_other_lane_waits(self):
        self.f.workflow_runs = list(RED_MAIN)
        ready(self.f, 1, lane="web")
        result = decide(self.f, lane="web")
        self.assertEqual(result["mode"], "skip")
        self.assertIn("is red", result["reason"])

    def test_green_main_after_red(self):
        self.f.workflow_runs = list(GREEN_MAIN) + list(RED_MAIN)
        ready(self.f, 1)
        self.assertEqual(decide(self.f)["mode"], "issue")

    def test_unreadable_ci_fails_closed(self):
        self.f.runs_error = 403
        ready(self.f, 1)
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("cannot read CI", result["reason"])


class StrictMode(unittest.TestCase):
    """Add-on A: test the merged result. A green PR behind main is updated, not merged."""

    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1")
        self.f.check_runs["s1"] = list(GREEN)

    def test_behind_pr_is_updated_and_waits_for_checks(self):
        self.f.behind["s1"] = 2
        result = decide(self.f)
        self.assertEqual(result["mode"], "skip")
        self.assertIn("behind main; updated", result["reason"])
        self.assertEqual(self.f.writes("PUT", r"pulls/10/update-branch$")[0][2], {"expected_head_sha": "s1"})
        self.assertFalse(self.f.writes("PUT", r"pulls/10/merge$"))
        self.assertEqual(self.f.pulls[10]["state"], "open")

    def test_updated_pr_merges_once_checks_rerun_green(self):
        self.f.behind["s1"] = 2
        decide(self.f)
        new_sha = self.f.pulls[10]["head"]["sha"]
        self.f.check_runs[new_sha] = list(PENDING)
        self.assertIn("still in review", decide(self.f)["reason"])  # checks re-running
        self.f.check_runs[new_sha] = list(GREEN)
        self.assertIn("merged PR #10", decide(self.f)["reason"])
        self.assertEqual(ns.pr_reason(self.f.comments[10], BOT), "verdict_passed")

    def test_update_conflict_closes_as_conflicted_and_refunds(self):
        self.f.add_comment(1, ns.marker("attempt", issue=1))
        self.f.behind["s1"] = 1
        self.f.update_branch_error[10] = (422, "merge conflict between base and head")
        decide(self.f)
        self.assertEqual(self.f.pulls[10]["state"], "closed")
        self.assertEqual(ns.pr_reason(self.f.comments[10], BOT), "conflict")
        self.assertEqual(ns.count(self.f.comments[1], BOT, "attempt"), 0)

    def test_update_race_retries_later(self):
        self.f.behind["s1"] = 1
        self.f.update_branch_error[10] = (422, "expected head sha didn't match current head ref")
        self.assertIn("behind main", decide(self.f)["reason"])
        self.assertEqual(self.f.pulls[10]["state"], "open")

    def test_up_to_date_green_pr_merges_with_sha_guard(self):
        decide(self.f)
        self.assertEqual(self.f.writes("PUT", r"pulls/10/merge$")[0][2], {"merge_method": "squash", "sha": "s1"})

    def test_queue_mode_never_updates_or_merges(self):
        self.f.behind["s1"] = 3
        result = decide(self.f, config=cfg(CONFIG + "merge:\n  mode: queue\n"))
        self.assertIn("merge queue", result["reason"])
        self.assertFalse(self.f.writes("PUT"))

    def test_missing_required_check_is_pending(self):
        self.f.check_runs["s1"] = [GREEN[0]]  # review / verdict has not reported
        self.assertIn("still in review", decide(self.f)["reason"])


class ReasonCodes(unittest.TestCase):
    """Add-on D: every close records why."""

    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        ready(self.f, 1)
        self.f.add_pull(10, sha="s1")

    def reason(self):
        return ns.pr_reason(self.f.comments[10], BOT)

    def test_verdict_failed(self):
        self.f.check_runs["s1"] = list(FAILED_VERDICT)
        decide(self.f)
        self.assertEqual(self.reason(), "verdict_failed")

    def test_checks_failed(self):
        self.f.check_runs["s1"] = list(FAILED_TEST)
        decide(self.f)
        self.assertEqual(self.reason(), "checks_failed")

    def test_failed_commit_status_counts_as_checks_failed(self):
        self.f.statuses["s1"] = [{"context": "ci/legacy", "state": "failure"}]
        decide(self.f)
        self.assertEqual(self.reason(), "checks_failed")

    def test_superseded_when_issue_closed_by_someone_else(self):
        self.f.issues[1]["state"] = "closed"
        decide(self.f)
        self.assertEqual(self.reason(), "superseded")
        self.assertEqual(self.f.pulls[10]["state"], "closed")

    def test_markers_from_others_do_not_set_reason(self):
        self.f.add_comment(10, ns.marker("pr", reason="verdict_passed", pr=10), user="mallory")
        self.assertIsNone(self.reason())


class HandOffs(unittest.TestCase):
    """Step 3a: a merged PR hands off to every open issue that lists its issue in Blocked by."""

    def setUp(self):
        self.f = FakeGitHub()
        self.f.workflow_runs = list(GREEN_MAIN)
        self.f.add_issue(1, title="[A1] Users endpoint", labels=["lane:api", "night-safe", "wave:1"],
                         body="## Goal\nx\n\n## Interface\nGET /users -> [{id, name}]\n")
        ready(self.f, 2, lane="web", body="## Goal\ny\n\nBlocked by: #1")
        ready(self.f, 3, lane="web", body="Blocked by: #1, #2")
        self.f.add_issue(4, body="no deps")
        handoff = {"files": ["api/users.py"], "interfaces": ["GET /users -> [{id, name, email}]"],
                   "env_vars": ["USERS_PAGE_SIZE"], "decisions": ["paginate by 50"], "assumptions": [],
                   "interface_differences": "added email field"}
        self.f.add_pull(10, sha="s1", body="Closes #1\n\n" + ns.marker("handoff-data", data=handoff))
        self.f.check_runs["s1"] = list(GREEN)

    def test_merge_posts_handoff_to_dependents(self):
        decide(self.f)
        for n in (2, 3):
            text = "\n".join(self.f.bodies(n))
            self.assertIn('"type":"handoff"', text)
            self.assertIn("GET /users -> [{id, name, email}]", text)  # interface as built
            self.assertIn("GET /users -> [{id, name}]", text)         # interface the ticket specified
            self.assertIn("Differs from the ticket:** added email field", text)
            self.assertIn("USERS_PAGE_SIZE", text)
        self.assertEqual(self.f.bodies(4), [])
        self.assertTrue(ns.trusted(self.f.comments[10], BOT, "handoff-done"))

    def test_auto_merged_pr_hands_off_on_next_run_once(self):
        self.f.pulls[10].update({"state": "closed", "merged_at": "2099-01-01T00:30:00Z"})
        decide(self.f, lane="api")
        decide(self.f, lane="api")
        self.assertEqual(sum('"type":"handoff"' in b for b in self.f.bodies(2)), 1)
        self.assertEqual(ns.pr_reason(self.f.comments[10], BOT), "verdict_passed")

    def test_handoff_without_data_says_so(self):
        self.f.pulls[10]["body"] = "Closes #1"
        decide(self.f)
        self.assertIn("not reported", "\n".join(self.f.bodies(2)))

    def test_handoff_data_from_a_pr_by_someone_else_is_ignored(self):
        self.f.pulls[10]["user"] = {"login": "mallory"}
        self.f.pulls[10].update({"state": "closed", "merged_at": "2099-01-01T00:30:00Z"})
        decide(self.f)
        self.assertEqual(self.f.bodies(2), [])

    def test_other_lane_does_not_hand_off_this_lanes_pr(self):
        self.f.pulls[10].update({"state": "closed", "merged_at": "2099-01-01T00:30:00Z"})
        decide(self.f, lane="web")
        self.assertEqual(self.f.bodies(2), [])

    def test_dependent_is_picked_after_merge_and_its_task_has_the_handoff(self):
        from nightshift.task import issue_task

        decide(self.f)                        # api merges #10; GitHub closes #1
        self.f.issues[1]["state"] = "closed"
        result = decide(self.f, lane="web")
        self.assertEqual(result["issue"], "2")
        gh = self.f.gh()
        gh.login = BOT
        text, _ = issue_task(gh, cfg(), 2)
        self.assertIn("## Coordination (posted by the night shift; trusted)", text)
        self.assertIn("GET /users -> [{id, name, email}]", text)


if __name__ == "__main__":
    unittest.main()
