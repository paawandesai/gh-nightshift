#!/usr/bin/env python3
"""Pick the next GitHub issue for one agent lane. No model calls: plain rules.

Order of checks:
  1. Kill switch     AGENTS_ENABLED must be "true" (and ONLY_LANE, if set, must match).
  2. Lane busy       An open agent PR in this lane is still running checks -> wait.
                     A failed or conflicted one is closed so the lane can move on.
                     A green one that is still open is merged. One with no checks
                     after STALE_MINUTES is closed too.
  3. Main health     If the latest CI run on main failed, only FIX_LANE works,
                     and it works on fixing main (at most MAX_ATTEMPTS times per failing
                     commit, counted by markers on the night-log issue). If CI status
                     cannot be read, every lane waits (fail closed).
  4. Next issue      Oldest unassigned open issue in this lane with READY_LABEL and none of SKIP_LABELS,
                     lowest wave first (wave:3 or wave:W03), whose "Blocked by #N" issues are all closed and that
                     has fewer than MAX_ATTEMPTS failed attempts and fewer than
                     2*MAX_ATTEMPTS runs that never started. Only markers posted by
                     the AGENT_PAT account count, so outsiders cannot forge them.

Writes mode=issue|fix-main|skip, issue=<n>, reason=<text>, fail_sha=<sha>, model=<alias> to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys

ATTEMPT_MARKER = "<!-- agent-attempt -->"
INFRA_MARKER = "<!-- agent-infra -->"   # a run that never really started; capped separately
BLOCK_MARKER = "<!-- agent-blocked -->"  # attempts before the latest block notice don't count
LEGACY_BLOCK_TEXT = "marking blocked for a human"
# human/integrator: person-owned work (docs/NIGHT_SHIFT_ISSUES.md). Skipped even if night-safe is added by mistake.
SKIP_LABELS = {"blocked", "needs-human", "human", "integrator"}
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
PENDING_STATES = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
DEP_RE = re.compile(r"(?:blocked\s+by|depends\s+on)\s*:?\s*((?:#\d+(?:\s*,\s*|\s+and\s+|\s+)?)+)", re.I)
BRANCH_RE = re.compile(r"^agent/[^/]+/issue-(\d+)$")


# --- gh wrappers (replaced in tests) -------------------------------------------------

def run_gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def gh_json(args: list[str]):
    out = run_gh(args).strip()
    return json.loads(out) if out else None


# --- config ---------------------------------------------------------------------------

class Config:
    def __init__(self, env: dict[str, str]):
        self.lane = env["LANE"]
        self.repo = env["REPO"]
        self.enabled = env.get("AGENTS_ENABLED", "").strip().lower() == "true"
        self.ready_label = env.get("READY_LABEL") or "night-safe"
        self.max_attempts = int(env.get("MAX_ATTEMPTS") or 3)
        self.ci_workflow = env.get("CI_WORKFLOW") or "ci.yml"
        self.main_branch = env.get("MAIN_BRANCH") or "main"
        self.fix_lane = env.get("FIX_LANE") or ""
        self.only_lane = env.get("ONLY_LANE") or ""
        self.stale_minutes = int(env.get("STALE_MINUTES") or 45)
        self._login = env.get("AGENT_LOGIN") or ""
        # Model routing: lane default for first tries, a stronger model for retries and fixing main,
        # and a `model:<alias>` label on an issue overrides both.
        self.lane_model = clean_model(env.get("LANE_MODEL")) or "sonnet"
        self.escalate_model = clean_model(env.get("ESCALATE_MODEL")) or "opus"

    @property
    def login(self) -> str:
        """The AGENT_PAT's own account. Only markers it posted count, so outsiders can't forge them."""
        if not self._login:
            self._login = run_gh(["api", "user", "--jq", ".login"]).strip()
        return self._login

    @property
    def max_infra(self) -> int:
        return self.max_attempts * 2

    @property
    def lane_label(self) -> str:
        return f"lane:{self.lane}"


# --- helpers --------------------------------------------------------------------------

def clean_model(value: str | None) -> str:
    """Accept only alias/name-like strings, e.g. sonnet, opus, claude-opus-5-5."""
    value = (value or "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) else ""


def model_for(cfg: "Config", item: dict, failed_attempts: int) -> str:
    for name in label_names(item):
        if name.startswith("model:") and clean_model(name[6:]):
            return name[6:]
    return cfg.escalate_model if failed_attempts > 0 else cfg.lane_model


def label_names(item: dict) -> set[str]:
    return {lbl["name"] for lbl in item.get("labels", [])}


UNRESOLVED = -1  # a dependency we can't resolve to an issue number counts as unmet


def parse_deps(body: str | None) -> list[int]:
    """Issue numbers this issue waits on. Handles "Blocked by #3 and #4", "Depends on #5" and the
    Idea Factory's "- Blocked by: #12, #13" / "- Blocked by: none" / "- Blocked by: E01-S03"."""
    deps: set[int] = set()
    for line in (body or "").splitlines():
        m = re.search(r"(?:blocked\s+by|depends\s+on)\s*:?\s*(.*)", line, re.I)
        if not m:
            continue
        rest = m.group(1).strip().rstrip(".")
        if not rest or rest.lower() in {"none", "n/a", "-", "nothing"}:
            continue
        for token in re.split(r"\s*(?:,|\band\b|\s)\s*", rest):
            token = token.strip()
            if not token:
                continue
            num = re.fullmatch(r"#(\d+)", token)
            deps.add(int(num.group(1)) if num else UNRESOLVED)
    return sorted(deps)


def wave_of(item: dict) -> int:
    for name in label_names(item):
        m = re.fullmatch(r"wave:W?(\d+)", name, re.I)
        if m:
            return int(m.group(1))
    return 999


def checks_state(rollup: list[dict] | None) -> str:
    """Return 'failed', 'pending' or 'passed' for a PR's statusCheckRollup."""
    if not rollup:
        return "pending"  # CI has not reported yet
    pending = False
    for check in rollup:
        if check.get("__typename") == "StatusContext":
            state = (check.get("state") or "").upper()
            if state in {"FAILURE", "ERROR"}:
                return "failed"
            if state != "SUCCESS":
                pending = True
        else:
            status = (check.get("status") or "").upper()
            conclusion = (check.get("conclusion") or "").upper()
            if status != "COMPLETED" or status in PENDING_STATES:
                pending = True
            elif conclusion in FAILED_CONCLUSIONS:
                return "failed"
    return "pending" if pending else "passed"


def age_minutes(created_at: str | None) -> float:
    if not created_at:
        return 0.0
    created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60


def issue_state(cfg: Config, number: int, cache: dict[int, str]) -> str:
    if number == UNRESOLVED:
        return "unknown"
    if number not in cache:
        try:
            data = gh_json(["api", f"repos/{cfg.repo}/issues/{number}"])
            cache[number] = (data or {}).get("state", "unknown")
        except RuntimeError:
            cache[number] = "unknown"  # unknown deps count as unmet
    return cache[number]


def marker_comment_ids(cfg: Config, number: int, marker: str) -> list[int]:
    """IDs of comments on an issue that contain `marker` and were posted by the PAT account."""
    out = run_gh([
        "api", "--paginate", f"repos/{cfg.repo}/issues/{number}/comments",
        "--jq", f'.[] | select(.user.login == "{cfg.login}" and (.body | contains("{marker}"))) | .id',
    ])
    return [int(x) for x in out.split() if x.strip().isdigit()]


def count_markers(cfg: Config, number: int, marker: str) -> int:
    """Markers since the latest block notice. Removing `blocked` therefore gives an issue fresh attempts."""
    ids = marker_comment_ids(cfg, number, marker)
    if not ids:
        return 0
    blocks = (marker_comment_ids(cfg, number, BLOCK_MARKER)
              + marker_comment_ids(cfg, number, LEGACY_BLOCK_TEXT))
    last_block = max(blocks, default=0)
    return sum(1 for i in ids if i > last_block)


def refund_last_attempt(cfg: Config, number: int, why: str) -> None:
    """Turn the latest attempt marker into an infra marker: the failure wasn't the agent's."""
    ids = marker_comment_ids(cfg, number, ATTEMPT_MARKER)
    if ids:
        run_gh(["api", "-X", "PATCH", f"repos/{cfg.repo}/issues/comments/{ids[-1]}",
                "-f", f"body={INFRA_MARKER}\nAttempt not counted: {why}"])


# --- checks ---------------------------------------------------------------------------

def handle_open_prs(cfg: Config) -> str | None:
    """Return a skip reason if the lane is busy (or just merged something), else None."""
    prs = gh_json([
        "pr", "list", "-R", cfg.repo, "--state", "open",
        "--label", "agent", "--label", cfg.lane_label,
        "--json", "number,headRefName,mergeable,statusCheckRollup,createdAt,labels",
    ]) or []
    busy = None
    merged = []
    for pr in prs:
        num = pr["number"]
        state = checks_state(pr.get("statusCheckRollup"))
        conflicted = (pr.get("mergeable") or "").upper() == "CONFLICTING"
        stale = not pr.get("statusCheckRollup") and age_minutes(pr.get("createdAt")) > cfg.stale_minutes
        if state == "failed" or conflicted or stale:
            why = ("merge conflict with main" if conflicted
                   else f"no checks reported after {cfg.stale_minutes} min" if stale
                   else "checks failed")
            run_gh(["pr", "close", str(num), "-R", cfg.repo, "--delete-branch",
                    "--comment", f"Night shift: closing, {why}. The issue goes back in the queue."])
            m = BRANCH_RE.match(pr.get("headRefName") or "")
            if m and "review-error" in label_names(pr):
                refund_last_attempt(cfg, int(m.group(1)), f"the review gate itself errored on PR #{num}")
            elif m and conflicted:
                # Parallel lanes editing the same lines is not the agent's failure: refund the
                # attempt so conflicts alone never mark an issue blocked (it retries on fresh main).
                refund_last_attempt(cfg, int(m.group(1)), f"PR #{num} hit a merge conflict with main")
            if m:
                # No attempt marker here: the worker already counted this attempt when it started.
                run_gh(["issue", "comment", m.group(1), "-R", cfg.repo, "--body",
                        f"Night shift: PR #{num} was closed ({why}). See the review comments on it before retrying."])
            continue
        if state == "passed":
            try:
                run_gh(["pr", "merge", str(num), "-R", cfg.repo, "--squash", "--delete-branch"])
                merged.append(num)
                continue
            except RuntimeError as err:
                print(f"warning: could not merge green PR #{num}: {err}", file=sys.stderr)
        busy = f"PR #{num} in lane {cfg.lane} is still in review"
    if busy is None and merged:
        # GitHub closes the linked issue a moment after the merge; picking now could re-pick it.
        # The merge pushes to main, which starts a fresh run that continues the lane.
        busy = f"merged PR #{merged[0]}; the next run continues lane {cfg.lane}"
    return busy


def main_status(cfg: Config) -> tuple[str, str]:
    """Return ('green'|'red'|'unknown', head_sha_of_failing_run)."""
    try:
        runs = gh_json([
            "run", "list", "-R", cfg.repo, "--workflow", cfg.ci_workflow,
            "--branch", cfg.main_branch, "--limit", "10",
            "--json", "status,conclusion,event,headSha",
        ]) or []
    except RuntimeError as err:
        # Fail closed: if we cannot see CI, we cannot tell whether main is safe to build on.
        print(f"warning: cannot read CI runs ({err})", file=sys.stderr)
        return "unknown", ""
    for run in runs:
        if run.get("event") == "pull_request" or run.get("status") != "completed":
            continue
        if run.get("conclusion") in {"failure", "timed_out", "startup_failure"}:
            return "red", run.get("headSha", "")
        return "green", ""
    return "green", ""


def night_log_issue(cfg: Config) -> int | None:
    issues = gh_json(["issue", "list", "-R", cfg.repo, "--label", "night-log", "--state", "open",
                      "--limit", "1", "--json", "number"]) or []
    return issues[0]["number"] if issues else None


def fix_main_attempts(cfg: Config, sha: str) -> tuple[int, int]:
    """(real attempts, never-started runs) on this failing commit, from night-log markers."""
    log = night_log_issue(cfg)
    if log is None:
        return 0, 0
    return (count_markers(cfg, log, fix_marker(sha)),
            count_markers(cfg, log, fix_marker(sha, infra=True)))


def fix_marker(sha: str, infra: bool = False) -> str:
    kind = "fix-main-infra" if infra else "fix-main-attempt"
    return f"<!-- {kind} sha={sha} -->"


def next_issue(cfg: Config) -> tuple[int | None, str, str]:
    issues = gh_json([
        "issue", "list", "-R", cfg.repo, "--state", "open",
        "--label", cfg.lane_label, "--label", cfg.ready_label,
        "--limit", "200", "--json", "number,title,body,labels,assignees",
    ]) or []
    cache: dict[int, str] = {}
    waiting = 0
    for issue in sorted(issues, key=lambda i: (wave_of(i), i["number"])):
        if label_names(issue) & SKIP_LABELS:
            continue
        if issue.get("assignees"):
            continue  # a human claimed it by assigning themselves
        unmet = [d for d in parse_deps(issue.get("body")) if issue_state(cfg, d, cache) != "closed"]
        if unmet:
            waiting += 1
            continue
        tried = count_markers(cfg, issue["number"], ATTEMPT_MARKER)
        infra = count_markers(cfg, issue["number"], INFRA_MARKER)
        if tried >= cfg.max_attempts or infra >= cfg.max_infra:
            why = (f"{tried} failed attempts" if tried >= cfg.max_attempts
                   else f"{infra} runs that never started (check the lane token and usage)")
            run_gh(["issue", "edit", str(issue["number"]), "-R", cfg.repo, "--add-label", "blocked"])
            run_gh(["issue", "comment", str(issue["number"]), "-R", cfg.repo, "--body",
                    f"{BLOCK_MARKER}\nNight shift: {why}; marking blocked for a human. "
                    "Remove the `blocked` label to give it fresh attempts."])
            continue
        model = model_for(cfg, issue, tried)
        return issue["number"], f"picked #{issue['number']}: {issue['title']} (model {model})", model
    if waiting:
        return None, f"{waiting} issue(s) in lane {cfg.lane} are waiting on dependencies", ""
    return None, f"no ready issues in lane {cfg.lane}", ""


def skip(reason: str) -> dict[str, str]:
    return {"mode": "skip", "issue": "", "reason": reason, "fail_sha": "", "model": ""}


def decide(cfg: Config) -> dict[str, str]:
    if not cfg.enabled:
        return skip("AGENTS_ENABLED is not true (kill switch)")
    if cfg.only_lane and cfg.only_lane != cfg.lane:
        return skip(f"manual run limited to lane {cfg.only_lane}")
    busy = handle_open_prs(cfg)
    if busy:
        return skip(busy)
    health, sha = main_status(cfg)
    if health == "unknown":
        return skip("cannot read CI status on main; check AGENT_PAT has Actions: Read")
    if health == "red":
        if cfg.fix_lane != cfg.lane:
            return skip(f"{cfg.main_branch} is red; waiting for lane {cfg.fix_lane or '(none set)'}")
        tried, infra = fix_main_attempts(cfg, sha)
        if tried >= cfg.max_attempts or infra >= cfg.max_infra:
            return skip(f"fix-main gave up on {sha[:7]} ({tried} attempts, {infra} runs that never started); needs a human")
        return {"mode": "fix-main", "issue": "", "reason": f"latest CI on {cfg.main_branch} failed",
                "fail_sha": sha, "model": cfg.escalate_model}
    number, reason, model = next_issue(cfg)
    if number is None:
        return skip(reason)
    return {"mode": "issue", "issue": str(number), "reason": reason, "fail_sha": "", "model": model}


def main() -> int:
    cfg = Config(dict(os.environ))
    result = decide(cfg)
    print(json.dumps(result))
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a", encoding="utf-8") as fh:
            for key, value in result.items():
                fh.write(f"{key}={value.replace(chr(10), ' ')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
