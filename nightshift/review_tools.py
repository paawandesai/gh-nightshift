"""Inputs and output for the review gate (`review-inputs`, `review-report`)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from collections.abc import Callable

from .gh import GH
from .rules import BRANCH_RE

GitRunner = Callable[[list[str]], str]
MAX_OVERLAP_DIFF = 40_000


def git_in(repo: str) -> GitRunner:
    def run(args: list[str]) -> str:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout
    return run


def drift_report(git: GitRunner, base_ref: str) -> tuple[str, str, list[str]]:
    """(diff of the PR, drift markdown, overlapping files). Drift = what changed on main since the PR's
    merge base: `git diff <merge-base>..<base_ref> --stat` plus the main-side diffs of files the PR touches."""
    mb = git(["merge-base", base_ref, "HEAD"]).strip()
    pr_diff = git(["diff", f"{mb}..HEAD"])
    def names(rng: str) -> set[str]:
        out = git(["-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", "-z", rng])
        return {p for p in out.replace("\n", "\0").split("\0") if p}

    pr_files = names(f"{mb}..HEAD")
    main_files = names(f"{mb}..{base_ref}")
    stat = git(["diff", "--stat", f"{mb}..{base_ref}"]).strip()
    overlap = sorted(pr_files & main_files)
    lines = [f"# What changed on main since this PR's base ({mb[:12]})", "",
             f"## `git diff {mb[:12]}..{base_ref} --stat`", "```", stat or "(nothing: main has not moved)", "```", ""]
    if overlap:
        lines += ["## Files changed both by this PR and on main (main-side diffs)", ""]
        budget = MAX_OVERLAP_DIFF
        for f in overlap:
            d = git(["diff", f"{mb}..{base_ref}", "--", f])
            if len(d) > budget:
                d = d[:budget] + "\n... (truncated)"
            budget = max(budget - len(d), 0)
            lines += [f"### {f}", "```diff", d.rstrip(), "```", ""]
    else:
        lines.append("No file is changed both by this PR and on main. Still check interfaces the PR depends on "
                     "(imports, endpoints, schemas) against the stat above.")
    return pr_diff, "\n".join(lines) + "\n", overlap


def write_inputs(gh: GH, git: GitRunner, base_ref: str, head_ref: str, out_dir: str) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    diff, drift, overlap = drift_report(git, base_ref)
    (out / "diff.patch").write_text(diff, encoding="utf-8")
    (out / "drift.md").write_text(drift, encoding="utf-8")
    m = BRANCH_RE.match(head_ref)
    issue = m.group(2) if m else ""
    if issue:
        data = gh.issue(int(issue)) or {}
        text = f"# Issue #{issue}: {data.get('title', '')}\n\n{data.get('body') or ''}\n"
    else:
        text = "No linked issue: this PR fixes failing CI on main.\n"
    (out / "issue.md").write_text(text, encoding="utf-8")
    return {"issue": issue, "overlap": str(len(overlap))}


def render_report(result_path: str, overlap: int = 0) -> tuple[str, str]:
    """(verdict, comment markdown). verdict is pass, fail or error (the reviewer itself broke).
    `overlap` is the mechanical count of files both this PR and main changed since the merge base: when it is
    above 0, a pass needs the reviewer to state that the PR is still correct against current main (add-on B)."""
    try:
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
        out = data.get("structured_output") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        out = {}
    verdict = out.get("verdict") if out.get("verdict") in {"pass", "fail"} else "error"
    if verdict == "error":
        return verdict, "Review run failed to produce a verdict; see the job log. The attempt is not counted.\n"
    lines = [f"### Night-shift review: **{verdict}**", ""]
    for a in out.get("answers") or []:
        q = str(a.get("question", ""))
        yes = a.get("answer") == "yes"
        if q.lower().startswith("main_drift") or "main change" in q.lower():
            mark = "⚠️" if yes else "✅"
        else:
            mark = "✅" if yes else "❌"
        lines.append(f"- {mark} **{q}**: {a.get('evidence', '')}")
    still = str(out.get("still_correct_against_main") or "").strip()
    if out.get("main_changed_overlap") or overlap:
        count = f" ({overlap} overlapping file(s) since the merge base)" if overlap else ""
        lines += ["", f"**Main changed under this PR.**{count} Still correct against current main: "
                      f"{still or '(not stated)'}"]
        if verdict == "pass" and not still:
            verdict = "fail"
            lines += ["", "Verdict changed to **fail**: main changed files this PR touches and the reviewer did not "
                          "confirm the PR is still correct against current main."]
        lines[0] = f"### Night-shift review: **{verdict}**"
    if out.get("fixes"):
        lines += ["", f"**Fixes needed:** {out['fixes']}"]
    return verdict, "\n".join(lines) + "\n"
