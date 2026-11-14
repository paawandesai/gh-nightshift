"""Where a merge gate came from (W-01 follow-up).

Night Shift's gates (`review / verdict`, `review / security`) are jobs of the reusable review workflow, called from
the generated caller `.github/workflows/night-shift-review.yml`. Branch protection binds them to the GitHub Actions
app, but any workflow whose token has `checks: write` (for example a repo's CI running agent-written code) creates
check runs as that same app, so a check run's name and app prove nothing. This module accepts a gate only when it is
a real Actions job of a workflow run of the review caller on the same commit:

    check run id -> GET actions/jobs/{id}  (a check run made through the Checks API has no job: 404)
    job.run_id   -> GET actions/runs/{id}  (path is the review caller, event pull_request/merge_group, same sha)

and only once that workflow run has completed successfully, so a check run patched to `success` while the review
was still running (or after it failed) does not count either. With a fine-grained PAT, `GH.check_runs` already
lists Actions jobs with their workflow run attached, so nothing more is fetched.

A gate that fails these tests is dropped from the list, as if it had not reported: the commit stays pending until
the real review reports. Gate check runs only, never statuses: `rules.checks_state` already ignores those.
"""
from __future__ import annotations

from .gh import GH, GHError
from .rules import is_gate

REVIEW_WORKFLOW = ".github/workflows/night-shift-review.yml"
REVIEW_EVENTS = frozenset({"pull_request", "merge_group"})
ACTIONS_SLUG = "github-actions"


def _path(run: dict) -> str:
    # A workflow run's path can carry the ref it ran from (".github/workflows/x.yml@refs/heads/main").
    return (run.get("path") or "").split("@", 1)[0]


class GateVerifier:
    """Checks gate check runs against the Actions job and workflow run behind them. One per picker run: workflow
    runs are cached, so two gates from the same review run cost three calls, not four."""

    def __init__(self, gh: GH, workflow: str = REVIEW_WORKFLOW):
        self.gh = gh
        self.workflow = workflow
        self._runs: dict[int, dict | None] = {}

    def _run(self, run_id: int) -> dict | None:
        if run_id not in self._runs:
            self._runs[run_id] = self.gh.get_or_none(self.gh.r(f"actions/runs/{run_id}"))
        return self._runs[run_id]

    def why_not(self, check: dict, sha: str) -> str:
        """'' when `check` is a gate produced by the review workflow on `sha`, else why it is not trusted."""
        name = check.get("name", "")
        if check.get("head_sha") and check["head_sha"] != sha:
            return f"it was reported on {str(check['head_sha'])[:12]}, not on {sha[:12]}"
        if "workflow_run" in check:  # GH.check_runs' Actions jobs fallback: already a job, with its run
            job, run = check, check.get("workflow_run") or {}
        else:
            app = (check.get("app") or {}).get("slug")
            if app and app != ACTIONS_SLUG:
                return f"it was created by the app `{app}`, not by GitHub Actions"
            try:
                job = self.gh.get_or_none(self.gh.r(f"actions/jobs/{check.get('id')}"))
            except GHError as err:
                return f"its Actions job could not be read (HTTP {err.status}; the token needs Actions: read)"
            if not job:
                return "it is not a GitHub Actions job (a check run created through the Checks API)"
            url = job.get("check_run_url") or ""
            if url and not url.endswith(f"/check-runs/{check.get('id')}"):
                return "its Actions job belongs to another check run"
            try:
                run = self._run(int(job.get("run_id") or 0)) or {}
            except GHError as err:
                return f"its workflow run could not be read (HTTP {err.status}; the token needs Actions: read)"
        if job.get("name") != name:
            return f"its Actions job is named `{job.get('name')}`"
        if job.get("head_sha") and job["head_sha"] != sha:
            return f"its Actions job ran on {str(job['head_sha'])[:12]}, not on {sha[:12]}"
        if _path(run) != self.workflow:
            return f"it comes from {_path(run) or 'an unknown workflow'}, not from {self.workflow}"
        if run.get("event") not in REVIEW_EVENTS:
            return f"its workflow run was started by `{run.get('event')}`, not by a pull request"
        if run.get("head_sha") != sha:
            return f"its workflow run tested {str(run.get('head_sha'))[:12]}, not {sha[:12]}"
        if (check.get("conclusion") or "").lower() == "success":
            if (run.get("status") or "").lower() != "completed":
                return "the review workflow run behind it has not finished yet"
            if (run.get("conclusion") or "").lower() != "success":
                return f"the review workflow run behind it concluded `{run.get('conclusion')}`"
        return ""

    def filter(self, checks: list[dict], sha: str, gates: list[str]) -> tuple[list[dict], list[str]]:
        """`checks` without the gate check runs (named in `gates`) that the review workflow did not produce, and a
        line for each one dropped."""
        kept, dropped = [], []
        for check in checks:
            name = check.get("name", "")
            if is_gate(name) and name in gates:
                why = self.why_not(check, sha)
                if why:
                    dropped.append(f"`{name}` (check run {check.get('id')}) on {sha[:12]}: {why}")
                    continue
            kept.append(check)
        return kept, dropped
