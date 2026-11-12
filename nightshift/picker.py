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
from .commands import can_command
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
    parse_cross_deps,
    parse_deps,
    path_is_protected,
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
        self.awaiting_approval: set[int] = set()
        self._cross: dict[tuple[str, int], dict] = {}
        self._files: dict[int, list[str]] = {}
        self._unenforced: list[str] | None = None
        self._owner_groups: dict[int, list[set[str] | None]] = {}

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

    def cross_state(self, ref: tuple[str, int]) -> dict:
        """An issue in another repository. The PAT must be able to read it (public, or a token with access);
        if it cannot, the dependency counts as unmet and `status` shows it as waiting."""
        if ref not in self._cross and ref[0].lower() not in {r.lower() for r in self.cfg.cross_repos}:
            # M10: the PAT reads (and copies text from) only repos the config names.
            print(f"::warning::cross-repo dependency {ref[0]}#{ref[1]} is not read: {ref[0]} is not in cross_repos")
            self._cross[ref] = {"state": "not in cross_repos", "title": "", "body": ""}
        if ref not in self._cross:
            try:
                data = self.gh.get_or_none(f"repos/{ref[0]}/issues/{ref[1]}") or {}
            except GHError:
                data = {}
            self._cross[ref] = {"state": data.get("state", "unknown"), "title": data.get("title", ""),
                                "body": data.get("body") or ""}
        return self._cross[ref]

    def cross_hand_off(self, number: int, refs: list[tuple[str, int]], comments: list[dict]) -> None:
        """Pull-style hand-off: this repo's PAT cannot write to the other repo, so the waiting side posts it."""
        got = {x.get("from") for x in ns.trusted(comments, self.gh.trusted, "handoff")}
        for repo, n in refs:
            ref = f"{repo}#{n}"
            if ref in got:
                continue
            dep = self.cross_state((repo, n))
            body = self.gh.comment(number, ns.cross_handoff_comment(ref, dep["title"], section(dep["body"], "Interface"),
                                                                    number))
            comments.append(body or {})

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
                if issue_no not in parse_deps(dep.get("body"), self.gh.repo):
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

        # The merge closes the issue a moment later; count it as done now rather than wait for the next refresh.
        extra = [{**merged, "state": "closed"}] if m and merged.get("number") else []
        roadmap.safe_update(self.gh, self.cfg, extra=extra)
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
        hint = (" If the ticket itself keeps the code from passing a repo rule or the security review, the next "
                "attempt should report `decision` and ask a person." if reason in {"verdict_failed", "security_failed"} else "")
        self.gh.comment(issue, f"Night shift: PR #{num} was closed ({why}). "
                               f"See the review comments on it before retrying.{hint}")

    def merge_body(self, pr: dict, sha: str, m: re.Match | None) -> dict:
        """The squash commit's title and message are written here. GitHub's default message is the list of the
        agent's own commit messages, where "closes #n" would close other issues and "[skip ci]" would switch off
        CI for the commit that lands on main."""
        title = " ".join(ns.no_skip_ci(ns.no_close(str(pr.get("title") or f"PR {pr['number']}"))).split())
        closes = f"Closes #{int(m.group(2))}\n\n" if m else ""
        return {"merge_method": "squash", "sha": sha, "commit_title": f"{title} (#{pr['number']})"[:250],
                "commit_message": f"{closes}Night-shift lane {self.lane.name}, PR #{pr['number']}."}

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
            runs = self.gh.check_runs(sha)
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
                unenforced = self.unenforced_checks()
                if unenforced:
                    print(f"::warning::not merging PR #{num}: {self.cfg.main_branch} does not enforce the required "
                          f"check(s) {', '.join(unenforced)}, so a PR could merge without them. Run `gh nightshift "
                          "setup`, or set merge.allow_unenforced: true in .night-shift/config.yml to accept that.")
                    busy = (f"PR #{num} in lane {self.lane.name} is green but not merged: {self.cfg.main_branch} "
                            "does not enforce the required checks")
                    continue
                why = self.approval_needed(pr, int(m.group(2)) if m else None)
                if why and not self.approved(pr, sha):
                    self.ask_approval(pr, why)
                    if m:
                        self.awaiting_approval.add(int(m.group(2)))  # never re-picked while its PR waits
                    continue  # waiting for a person does not hold up the lane
                try:
                    self.gh.put(self.gh.r(f"pulls/{num}/merge"), self.merge_body(pr, sha, m))
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

    def unenforced_checks(self) -> list[str]:
        """M7: required checks that neither main's branch protection nor a ruleset enforces. Read with Contents read
        (the branch summary and the active rules), once per run. Empty with merge.allow_unenforced."""
        if self.cfg.merge_allow_unenforced:
            return []
        if self._unenforced is None:
            names: set[str] = set()
            try:
                branch = self.gh.get_or_none(self.gh.r(f"branches/{self.cfg.main_branch}")) or {}
                rsc = (branch.get("protection") or {}).get("required_status_checks") or {}
                if rsc.get("enforcement_level", "everyone") != "off":
                    names |= set(rsc.get("contexts") or []) | {c.get("context") for c in rsc.get("checks") or []}
                for rule in self.gh.get_or_none(self.gh.r(f"rules/branches/{self.cfg.main_branch}")) or []:
                    if rule.get("type") == "required_status_checks":
                        params = rule.get("parameters") or {}
                        names |= {c.get("context") for c in params.get("required_status_checks") or []}
            except GHError as err:
                print(f"warning: could not read how {self.cfg.main_branch} enforces checks: {err}")
            self._unenforced = [c for c in self.cfg.required_checks if c not in names]
        return self._unenforced

    # --- risk tiers (strict mode) -------------------------------------------------------
    def pr_files(self, num: int) -> list[str] | None:
        """Changed paths (both names of a rename), or None when the list cannot be complete: GitHub lists at most
        3000 files and paginate stops at 2000, so path rules then fail closed."""
        if num not in self._files:
            files = self.gh.paginate(self.gh.r(f"pulls/{num}/files"))
            expected = (self.gh.get_or_none(self.gh.r(f"pulls/{num}")) or {}).get("changed_files")
            if len(files) >= 2000 or (isinstance(expected, int) and len(files) < expected):
                self._files[num] = None
                return None
            # A rename counts under both names, so moving a file out of a sensitive folder still needs approval.
            self._files[num] = [x for f in files for x in (f.get("filename", ""), f.get("previous_filename", "")) if x]
        return self._files[num]

    def approval_needed(self, pr: dict, issue_no: int | None) -> str:
        """Why this green PR needs one human approval before merging, or "" if it may merge on its own."""
        cfg = self.cfg
        if not (cfg.risk_approve_labels or cfg.risk_approve_paths or cfg.risk_default == "approve"
                or cfg.risk_codeowners):
            return ""
        if (cfg.risk_approve_paths or cfg.risk_codeowners) and self.pr_files(pr["number"]) is None:
            return "too many changed files to check the path rules"
        owned = ""
        if cfg.risk_codeowners:
            # Owner groups first: whatever else asks for approval, owned files still need their owners.
            from . import codeowners

            try:
                rules = codeowners.fetch(self.gh, self.cfg.main_branch)
            except GHError:
                self._owner_groups[pr["number"]] = [set()]  # unreadable CODEOWNERS: nobody's approval suffices
                return "CODEOWNERS could not be read; fix access, then re-run"
            groups = {}
            for f in self.pr_files(pr["number"]) or []:
                owners = codeowners.owners_for(rules, f)
                if owners:
                    groups[f] = codeowners.user_owners(owners)
            if groups:
                self._owner_groups[pr["number"]] = list(groups.values())
                first = sorted(groups)[0]
                owned = f"code owners review: {first}" + (f" and {len(groups) - 1} more file(s)" if len(groups) > 1
                                                          else "")
        labels = label_names(pr) | (label_names(self.gh.issue(issue_no) or {}) if issue_no else set())
        hit = sorted(labels & set(cfg.risk_approve_labels))
        if hit:
            return f"label {hit[0]}"
        if cfg.risk_approve_paths:
            for f in self.pr_files(pr["number"]) or []:
                if path_is_protected(f, cfg.risk_approve_paths):
                    return f"changes {f}"
        if owned:
            return owned
        if cfg.risk_default == "approve" and not labels & set(cfg.risk_auto_labels):
            return "risk.default is approve"
        return ""

    def approved(self, pr: dict, sha: str) -> bool:
        """One approval of the current head from a person with write access who did not open the PR."""
        author = (pr.get("user") or {}).get("login", "")
        latest: dict[str, dict] = {}
        for r in self.gh.paginate(self.gh.r(f"pulls/{pr['number']}/reviews")):
            login = (r.get("user") or {}).get("login", "")
            if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[login] = r
        approvers = {login.lower() for login, r in latest.items()
                     if r.get("state") == "APPROVED" and r.get("commit_id") == sha and login != author
                     and not login.endswith("[bot]") and can_command(self.gh, login)}
        groups = self._owner_groups.get(pr["number"])
        if not groups:
            return bool(approvers)
        # Every owned file needs one of its owners (any writer when a team or email owns it).
        return all(approvers if g is None else approvers & g for g in groups)

    def ask_approval(self, pr: dict, why: str) -> None:
        """Ask once per head commit: updating the branch with main makes an earlier approval stale."""
        num, sha = pr["number"], pr["head"]["sha"]
        if any(m.get("sha") == sha for m in ns.trusted(self.gh.comments(num), self.gh.trusted, "approval")):
            return
        self.gh.add_labels(num, ["needs-approval"])
        self.gh.comment(num, f"{ns.marker('approval', pr=num, sha=sha)}\nNight shift: checks are green, but this PR needs "
                             f"one approval from a person with write access before it merges ({ns.defang(why)}). "
                             "Approve the latest commit; the next lane run merges it.")

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

    def fix_main_attempts(self, sha: str) -> tuple[int, int] | None:
        """(attempts, runs that never started) for this failing commit; None when there is no night log to count
        them on (then fix-main does not run: an uncounted retry loop would never stop)."""
        log = self.gh.night_log(self.cfg.label("night_log"))
        if log is None:
            return None
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
            if num in self.awaiting_approval:
                continue
            if [d for d in parse_deps(issue.get("body"), self.gh.repo) if self.issue_state(d) != "closed"]:
                waiting += 1
                continue
            cross = parse_cross_deps(issue.get("body"), self.gh.repo)
            if cross and [x for x in cross if self.cross_state(x)["state"] != "closed"]:
                waiting += 1
                continue
            comments = self.gh.comments(num) if issue.get("comments", 1) else []
            if cross:
                self.cross_hand_off(num, cross, comments)
            if self.awaiting_handoff(parse_deps(issue.get("body"), self.gh.repo), comments):
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
            # The budget is counted on the night log. Without one the cap must hold, not vanish.
            return (f"lane {self.lane.name} has a nightly budget but there is no open night-log issue to count it "
                    "on; run `gh nightshift setup`")
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
            counted = self.fix_main_attempts(sha)
            if counted is None:
                return skip(f"{self.cfg.main_branch} is red, but there is no open night-log issue to count fix-main "
                            "attempts on; run `gh nightshift setup`")
            tried, infra = counted
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
