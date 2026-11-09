"""Pick the next issue for one lane. No model calls, plain rules, REST only.

Order of checks:
  1. Kill switch   AGENTS_ENABLED must be "true" (and ONLY_LANE, if set, must match).
  2. Hand-offs     Merged PRs of this lane that have not handed off yet post a hand-off comment on every
                   open issue that lists the merged issue in "Blocked by" (step 3a), and record
                   `verdict_passed` (add-on D). Idempotent; runs even when the lane is busy.
  3. Lane busy     An open agent PR in this lane still running checks -> wait. Closed with a reason code:
                   failed review (verdict_failed), failed checks (checks_failed), conflict, stale (no
                   checks after stale_minutes), superseded (its issue was closed). In strict merge mode a
                   PR behind main is updated with PUT .../update-branch and re-checked before it can merge
                   (add-on A); a conflict while updating closes it as `conflict`. Green and current: merged.
  4. Main health   If the latest CI run on main failed, only the fix lane works, on fixing main (max_attempts
                   per failing commit, counted on the night-log issue). Unreadable CI: everyone waits.
  5. Next issue    Lowest wave, then lowest number: open, this lane, ready label, none of the skip labels,
                   unassigned, every "Blocked by" issue closed, under the attempt caps. Only markers posted
                   by the PAT's own login count.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass

from . import protocol as ns
from .config import Config
from .gh import GH, GHError
from .rules import (
    BRANCH_RE,
    UNRESOLVED,
    age_minutes,
    checks_state,
    clean_model,
    close_reason,
    failed_check_names,
    label_names,
    lanes_of,
    model_for,
    parse_deps,
    section,
    wave_of,
)

HANDOFF_WINDOW_HOURS = 72
HANDOFF_GRACE_MINUTES = 30  # wait this long for a just-merged dependency's hand-off before starting without it


@dataclass
class Runtime:
    """Run-time switches (repo variables / workflow inputs) layered over the config file."""
    lane: str
    enabled: bool = False
    only_lane: str = ""
    fix_lane: str = ""
    lane_model: str = ""
    escalate_model: str = ""

    @classmethod
    def from_env(cls, lane: str, env: dict | None = None) -> Runtime:
        env = dict(os.environ) if env is None else env
        return cls(lane=lane,
                   enabled=(env.get("AGENTS_ENABLED") or "").strip().lower() == "true",
                   only_lane=env.get("ONLY_LANE") or "",
                   fix_lane=env.get("FIX_LANE") or "",
                   lane_model=clean_model(env.get("LANE_MODEL")),
                   escalate_model=clean_model(env.get("ESCALATE_MODEL")))


class Picker:
    def __init__(self, gh: GH, cfg: Config, rt: Runtime, now: dt.datetime | None = None):
        self.gh, self.cfg, self.rt = gh, cfg, rt
        self.lane = cfg.lane(rt.lane)
        self.fix_lane = rt.fix_lane or cfg.effective_fix_lane
        self.lane_model = rt.lane_model or self.lane.model or cfg.default_model
        self.escalate_model = rt.escalate_model or self.lane.escalate_model or cfg.escalate_model
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self._states: dict[int, str] = {}
        self._closed_at: dict[int, str] = {}

    # --- small helpers -----------------------------------------------------------------
    def issue_state(self, number: int) -> str:
        if number == UNRESOLVED:
            return "unknown"
        if number not in self._states:
            try:
                data = self.gh.issue(number)
            except GHError:
                data = None
            self._states[number] = (data or {}).get("state", "unknown")  # unknown deps count as unmet
            self._closed_at[number] = (data or {}).get("closed_at") or ""
        return self._states[number]

    def awaiting_handoff(self, deps: list[int], comments: list[dict]) -> list[int]:
        """Dependencies that closed moments ago and have not handed off to this issue yet. The merge that closes a
        dependency also wakes the other lanes, so without this the dependent could start before the hand-off lands."""
        got = {x.get("from") for x in ns.trusted(comments, self.gh.trusted, "handoff")}
        return [d for d in deps if d not in got and self._closed_at.get(d)
                and age_minutes(self._closed_at[d], self.now) < HANDOFF_GRACE_MINUTES]

    def refund_last_attempt(self, number: int, why: str) -> None:
        """Turn the latest attempt marker into an infra marker: the failure wasn't the agent's."""
        cid = ns.latest_id(self.gh.comments(number), self.gh.trusted, "attempt")
        if cid:
            self.gh.edit_comment(cid, f"{ns.marker('infra', issue=number)}\nAttempt not counted: {why}")

    def delete_branch(self, branch: str) -> None:
        try:
            self.gh.delete(self.gh.r(f"git/refs/heads/{branch}"))
        except GHError as err:
            if err.status not in (404, 422):  # already gone (auto-delete on merge)
                raise

    def is_lane_pr(self, pr: dict) -> bool:
        head = pr.get("head") or {}
        return (head.get("ref", "").startswith(f"agent/{self.lane.name}/")
                and (head.get("repo") or {}).get("full_name") == self.gh.repo
                and (pr.get("user") or {}).get("login") in self.gh.trusted)

    # --- 2. hand-offs (step 3a) --------------------------------------------------------
    def reconcile_merged(self) -> list[str]:
        """Hand off every recently merged PR of this lane that has not handed off yet."""
        done: list[str] = []
        closed = self.gh.get(self.gh.r("pulls"), {"state": "closed", "sort": "updated", "direction": "desc",
                                                  "per_page": 100}) or []
        for pr in closed:
            if not self.is_lane_pr(pr):
                continue
            if not pr.get("merged_at"):
                # Closed unmerged by a person (the night shift always records why it closes): record `manual`.
                if pr.get("closed_at") and age_minutes(pr["closed_at"], self.now) <= HANDOFF_WINDOW_HOURS * 60:
                    comments = self.gh.comments(pr["number"])
                    if not ns.pr_reason(comments, self.gh.trusted):
                        m = BRANCH_RE.match(pr["head"]["ref"])
                        self.gh.comment(pr["number"], ns.marker("pr", reason="manual", pr=pr["number"],
                                                                issue=int(m.group(2)) if m else None)
                                        + "\nNight shift: closed by a person without merging (recorded as manual).")
                        done.append(f"PR #{pr['number']} recorded as closed manually")
                continue
            if age_minutes(pr["merged_at"], self.now) > HANDOFF_WINDOW_HOURS * 60:
                continue
            comments = self.gh.comments(pr["number"])
            if ns.trusted(comments, self.gh.trusted, "handoff-done"):
                continue
            done.append(self.hand_off(pr, record_pass=not ns.pr_reason(comments, self.gh.trusted)))
        return done

    def hand_off(self, pr: dict, record_pass: bool = True) -> str:
        num = pr["number"]
        m = BRANCH_RE.match(pr["head"]["ref"])
        posted: list[int] = []
        if m:
            issue_no = int(m.group(2))
            merged = self.gh.issue(issue_no) or {}
            data = ns.trusted([{"id": 0, "user": pr.get("user"), "body": pr.get("body") or ""}],
                              self.gh.trusted, "handoff-data")
            # The real marker is the last one: publish writes it after the (defanged) agent text.
            handoff = data[-1].data.get("data") if data else None
            for dep in self.gh.issues():
                if issue_no not in parse_deps(dep.get("body")):
                    continue
                already = [x for x in ns.trusted(self.gh.comments(dep["number"]), self.gh.trusted, "handoff")
                           if x.get("from") == issue_no]
                if already:
                    continue
                self.gh.comment(dep["number"], ns.handoff_comment(
                    issue_no, merged.get("title", ""), self.lane.name, num, dep["number"], handoff,
                    section(merged.get("body"), "Interface")))
                posted.append(dep["number"])
        body = ns.marker("handoff-done", pr=num, to=",".join(f"#{n}" for n in posted) or "none")
        if record_pass:
            issue = int(m.group(2)) if m else None
            body = ns.marker("pr", reason="verdict_passed", pr=num, issue=issue) + "\n" + body
        self.gh.comment(num, body + "\nNight shift: merged; hand-off sent to "
                        + (", ".join(f"#{n}" for n in posted) or "no waiting issues") + ".")
        from . import roadmap

        roadmap.safe_update(self.gh, self.cfg)
        return f"PR #{num} handed off to {posted or 'nobody'}"

    # --- 3. lane busy ------------------------------------------------------------------
    def agent_prs(self) -> list[dict]:
        return [p for p in self.gh.paginate(self.gh.r("pulls"), {"state": "open"}) if self.is_lane_pr(p)]

    def close_pr(self, pr: dict, reason: str, why: str) -> None:
        """Close an agent PR without merging, recording a reason code (add-on D)."""
        num, branch = pr["number"], pr["head"]["ref"]
        m = BRANCH_RE.match(branch)
        issue = int(m.group(2)) if m else None
        self.gh.comment(num, f"{ns.marker('pr', reason=reason, pr=num, issue=issue)}\n"
                             f"Night shift: closing (`{reason}`), {why}. The issue goes back in the queue.")
        self.gh.patch(self.gh.r(f"pulls/{num}"), {"state": "closed"})
        self.delete_branch(branch)
        if issue is None or reason == "superseded":
            return
        if "review-error" in label_names(pr):
            self.refund_last_attempt(issue, f"the review gate itself errored on PR #{num}")
        elif reason == "conflict":
            # Parallel lanes editing the same lines is not the agent's failure.
            self.refund_last_attempt(issue, f"PR #{num} hit a merge conflict with main")
        self.gh.comment(issue, f"Night shift: PR #{num} was closed ({why}). "
                               "See the review comments on it before retrying.")

    def behind_main(self, sha: str) -> bool:
        data = self.gh.get(self.gh.r(f"compare/{self.cfg.main_branch}...{sha}")) or {}
        return int(data.get("behind_by") or 0) > 0

    def update_branch(self, pr: dict) -> str:
        """Bring a PR up to date with main. Returns 'updating', 'conflict' or 'retry'."""
        try:
            self.gh.put(self.gh.r(f"pulls/{pr['number']}/update-branch"), {"expected_head_sha": pr["head"]["sha"]})
            return "updating"
        except GHError as err:
            text = str(err).lower()
            if err.status == 422 and ("conflict" in text or "merge" in text and "could not" in text):
                return "conflict"
            if err.status == 422:
                return "retry"  # head moved under us; the next run looks again
            raise

    def handle_open_prs(self) -> str | None:
        busy = None
        merged: list[int] = []
        for pr in self.agent_prs():
            num, sha = pr["number"], pr["head"]["sha"]
            m = BRANCH_RE.match(pr["head"]["ref"])
            if m and self.issue_state(int(m.group(2))) == "closed":
                self.close_pr(pr, "superseded", f"issue #{m.group(2)} was closed while this PR was open")
                continue
            detail = self.gh.get(self.gh.r(f"pulls/{num}")) or pr
            conflicted = detail.get("mergeable") is False and detail.get("mergeable_state") == "dirty"
            runs = self.gh.paginate(self.gh.r(f"commits/{sha}/check-runs"), key="check_runs")
            statuses = (self.gh.get(self.gh.r(f"commits/{sha}/status")) or {}).get("statuses", [])
            state = checks_state(runs, statuses, self.cfg.required_checks)
            stale = not runs and not statuses and age_minutes(pr.get("created_at"), self.now) > self.cfg.stale_minutes
            if conflicted:
                self.close_pr(pr, "conflict", "merge conflict with main")
                continue
            if state == "failed":
                self.close_pr(pr, close_reason(failed_check_names(runs, statuses)), "checks failed")
                continue
            if stale:
                self.close_pr(pr, "stale", f"no checks reported after {self.cfg.stale_minutes} min")
                continue
            if self.cfg.merge_mode == "queue":
                busy = f"PR #{num} in lane {self.lane.name} is in review or in the merge queue"
                continue
            # Strict mode: test the merged result. A PR behind main is updated and re-checked first.
            if self.behind_main(sha):
                result = self.update_branch(pr)
                if result == "conflict":
                    self.close_pr(pr, "conflict", "updating it with main hit a merge conflict")
                    continue
                busy = f"PR #{num} in lane {self.lane.name} was behind main; updated, waiting for checks to re-run"
                continue
            if state == "passed":
                try:
                    self.gh.put(self.gh.r(f"pulls/{num}/merge"), {"merge_method": "squash", "sha": sha})
                except GHError as err:
                    print(f"warning: could not merge green PR #{num}: {err}")
                else:
                    self.delete_branch(pr["head"]["ref"])
                    self.hand_off(pr)
                    merged.append(num)
                    continue
            busy = f"PR #{num} in lane {self.lane.name} is still in review"
        if busy is None and merged:
            # GitHub closes the linked issue a moment after the merge; picking now could re-pick it.
            busy = f"merged PR #{merged[0]}; the next run continues lane {self.lane.name}"
        return busy

    # --- 4. main health ----------------------------------------------------------------
    def main_status(self) -> tuple[str, str]:
        try:
            data = self.gh.get(self.gh.r(f"actions/workflows/{self.cfg.ci_workflow}/runs"),
                               {"branch": self.cfg.main_branch, "per_page": 10}) or {}
        except GHError as err:
            print(f"warning: cannot read CI runs ({err})")
            return "unknown", ""
        for run in data.get("workflow_runs", []):
            if run.get("event") in ("pull_request", "merge_group") or run.get("status") != "completed":
                continue
            if run.get("conclusion") in {"failure", "timed_out", "startup_failure"}:
                return "red", run.get("head_sha", "")
            return "green", ""
        return "green", ""

    def fix_main_attempts(self, sha: str) -> tuple[int, int]:
        log = self.gh.night_log(self.cfg.label("night_log"))
        if log is None:
            return 0, 0
        comments = self.gh.comments(log)
        return (ns.count(comments, self.gh.trusted, "fix-attempt", sha=sha),
                ns.count(comments, self.gh.trusted, "fix-infra", sha=sha))

    # --- 5. next issue -----------------------------------------------------------------
    def candidates(self) -> list[dict]:
        issues = self.gh.issues([self.lane.label, self.cfg.ready_label])
        return sorted(issues, key=lambda i: (wave_of(i), i["number"]))

    def next_issue(self) -> tuple[int | None, str, str]:
        waiting = 0
        for issue in self.candidates():
            if label_names(issue) & self.cfg.skip_labels or issue.get("assignees"):
                continue  # person-owned, proposed, or claimed by a human assigning themselves
            if lanes_of(issue) != [self.lane.name]:
                continue  # exactly one lane label
            num = issue["number"]
            if [d for d in parse_deps(issue.get("body")) if self.issue_state(d) != "closed"]:
                waiting += 1
                continue
            comments = self.gh.comments(num) if issue.get("comments", 1) else []
            if self.awaiting_handoff(parse_deps(issue.get("body")), comments):
                waiting += 1
                continue
            tried = ns.count(comments, self.gh.trusted, "attempt")
            infra = ns.count(comments, self.gh.trusted, "infra")
            if tried >= self.cfg.max_attempts or infra >= self.cfg.infra_cap:
                why = (f"{tried} failed attempts" if tried >= self.cfg.max_attempts
                       else f"{infra} runs that never started (check the lane token and usage)")
                self.gh.add_labels(num, [self.cfg.label("blocked")])
                self.gh.comment(num, f"{ns.marker('blocked', issue=num)}\nNight shift: {why}; marking blocked "
                                     "for a human. Remove the `blocked` label to give it fresh attempts.")
                continue
            model = model_for(issue, tried, self.lane_model, self.escalate_model)
            return num, f"picked #{num}: {issue.get('title', '')} (model {model})", model
        if waiting:
            return None, f"{waiting} issue(s) in lane {self.lane.name} are waiting on dependencies", ""
        return None, f"no ready issues in lane {self.lane.name}", ""

    # --- budget (optional) -------------------------------------------------------------
    def budget_reached(self) -> str:
        """Skip reason when the lane's nightly budget is spent; '' when there is no cap or room is left."""
        cap = self.lane.budget_usd_per_night
        if not cap:
            return ""
        log = self.gh.night_log(self.cfg.label("night_log"))
        if not log:
            return ""
        now = self.now or dt.datetime.now(dt.timezone.utc)
        since = night_start(now, self.cfg.timezone)
        comments = self.gh.comments(log)
        spent = lane_spend(comments, self.gh.trusted, self.lane.name, since)
        if spent < cap:
            return ""
        noted = [m for m in ns.trusted(comments, self.gh.trusted, "budget")
                 if m.get("lane") == self.lane.name and m.get("night") == since.date().isoformat()]
        if not noted:
            self.gh.comment(log, ns.marker("budget", lane=self.lane.name, night=since.date().isoformat())
                            + f"\nNight shift: lane `{self.lane.name}` reached its budget (${spent:.2f} of ${cap:.2f}); "
                              "paused until noon.")
        return f"lane {self.lane.name} spent ${spent:.2f} of its ${cap:.2f} nightly budget; paused until noon"

    # --- decision ----------------------------------------------------------------------
    def decide(self) -> dict[str, str]:
        if not self.rt.enabled:
            return skip("AGENTS_ENABLED is not true (kill switch)")
        if self.rt.only_lane and self.rt.only_lane != self.lane.name:
            return skip(f"manual run limited to lane {self.rt.only_lane}")
        for line in self.reconcile_merged():
            print(line)
        busy = self.handle_open_prs()
        if busy:
            return skip(busy)
        health, sha = self.main_status()
        if health == "unknown":
            return skip("cannot read CI status on main; check the PAT has Actions: Read and ci_workflow is right")
        if health == "red":
            if self.fix_lane != self.lane.name:
                return skip(f"{self.cfg.main_branch} is red; waiting for lane {self.fix_lane or '(none set)'}")
            tried, infra = self.fix_main_attempts(sha)
            if tried >= self.cfg.max_attempts or infra >= self.cfg.infra_cap:
                return skip(f"fix-main gave up on {sha[:7]} ({tried} attempts, {infra} runs that never "
                            "started); needs a human")
            return {"mode": "fix-main", "issue": "", "reason": f"latest CI on {self.cfg.main_branch} failed",
                    "fail_sha": sha, "model": self.escalate_model}
        over = self.budget_reached()
        if over:
            return skip(over)
        number, reason, model = self.next_issue()
        if number is None:
            return skip(reason)
        return {"mode": "issue", "issue": str(number), "reason": reason, "fail_sha": "", "model": model}


COST_LINE_RE = re.compile(r"`(?P<lane>[a-z0-9][a-z0-9_-]*)`.*≈\$(?P<cost>[0-9]+(?:\.[0-9]+)?)\)?\s*$")


def night_start(now: dt.datetime, tz_name: str) -> dt.datetime:
    """A night runs noon to noon in the configured time zone; returns the start of the current one (UTC)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:  # unknown zone or no tz database: fall back to UTC
        tz = dt.timezone.utc
    local = now.astimezone(tz)
    start = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local < start:
        start -= dt.timedelta(days=1)
    return start.astimezone(dt.timezone.utc)


def lane_spend(comments: list[dict], trusted: frozenset[str], lane: str, since: dt.datetime) -> float:
    """API-equivalent USD this lane logged on the night log since `since` (trusted comments only)."""
    total = 0.0
    for c in comments:
        if (c.get("user") or {}).get("login") not in trusted or not c.get("created_at"):
            continue
        if dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")) < since:
            continue
        m = COST_LINE_RE.search((c.get("body") or "").splitlines()[0] if c.get("body") else "")
        if m and m.group("lane") == lane:
            total += float(m.group("cost"))
    return total


def skip(reason: str) -> dict[str, str]:
    return {"mode": "skip", "issue": "", "reason": reason, "fail_sha": "", "model": ""}
