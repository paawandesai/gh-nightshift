"""`gh nightshift audit`: did the merged agent PRs actually hold up?

Steps, each a plain function so the CLI (and tests) can call them one by one:

1. `sample`            draw a reproducible random sample of merged agent PRs (seeded).
2. `collect_evidence`  write ticket, diff and later commits per PR to an evidence folder.
3. `mechanical_check`  rerun the PR's tests at the merge commit, then with the implementation reverted
                       to the parent: tests that still pass without the change prove nothing.
4. `write_csv`         one row per PR, with empty label columns for an LLM and a human to fill.
5. `report`            rates with Wilson intervals, detection floor, LLM/human agreement (kappa).
6. `label_with_llm`    optional: pre-fill LLM labels. They are NOT ground truth.
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any
from collections.abc import Callable

from .gh import GH
from .rules import BRANCH_RE
from .stats_math import cohen_kappa, detection_floor, fmt_rate, wilson

Run = Callable[..., Any]

COLUMNS = ["pr", "issue", "lane", "title", "merged_at", "files", "later_commits", "pass_at_commit",
           "fail_on_parent", "tests_meaningful_mech", "llm_label", "llm_tests_meaningful", "human_label",
           "human_tests_meaningful", "notes"]
LABELS = ("ok", "defect_fixed_later", "extended_later", "wrong", "trivial")
BAD_LABELS = ("defect_fixed_later", "wrong")
TRI = ("yes", "no", "n/a")
NOT_GROUND_TRUTH = "LLM labels — not ground truth"

TEST_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.[^/]+$|\.test\.[^/]+$|_spec\.[^/]+$|(^|/)spec/")
DOC_RE = re.compile(r"\.md$|(^|/)docs/|(^|/)progress/")


# --- 1. sample ------------------------------------------------------------------------

def agent_prs(gh: GH, lanes: list[str] | None = None, bot: str = "", also: tuple[str, ...] = ()) -> list[dict]:
    """Merged PRs opened by the bot on an agent/<lane>/issue-<n> branch, sorted by number."""
    out = []
    for pr in gh.paginate(gh.r("pulls"), {"state": "closed"}):
        if not pr.get("merged_at"):
            continue
        m = BRANCH_RE.match(((pr.get("head") or {}).get("ref")) or "")
        if not m or ((pr.get("user") or {}).get("login")) not in {bot or gh.login, *also}:
            continue
        if lanes and m.group(1) not in lanes:
            continue
        out.append(pr)
    return sorted(out, key=lambda p: p["number"])


def sample(gh: GH, n: int, seed: int, lanes: list[str] | None = None, bot: str = "",
           also: tuple[str, ...] = ()) -> list[dict]:
    """Reproducible: the same seed over the same PR set always draws the same PRs (sorted by number)."""
    prs = agent_prs(gh, lanes, bot, also)
    if len(prs) <= n:
        return prs
    return sorted(random.Random(seed).sample(prs, n), key=lambda p: p["number"])


# --- 2. evidence ----------------------------------------------------------------------

def is_test_file(path: str) -> bool:
    return bool(TEST_RE.search(path))


def is_doc_file(path: str) -> bool:
    return bool(DOC_RE.search(path))


def classify(paths: list[str]) -> tuple[list[str], list[str]]:
    """(test files, implementation files); docs and progress notes are neither."""
    tests = [p for p in paths if is_test_file(p)]
    impl = [p for p in paths if not is_test_file(p) and not is_doc_file(p)]
    return tests, impl


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def collect_evidence(gh: GH, prs: list[dict], out_dir: str) -> list[dict]:
    repo = gh.get(gh.r()) or {}
    main_branch = repo.get("default_branch") or "main"
    rows = []
    for pr in prs:
        n = pr["number"]
        m = BRANCH_RE.match(((pr.get("head") or {}).get("ref")) or "")
        lane, issue_no = (m.group(1), int(m.group(2))) if m else ("", 0)
        merged_at = pr.get("merged_at") or ""
        merge_sha = pr.get("merge_commit_sha") or ""
        folder = os.path.join(out_dir, f"pr-{n}")
        os.makedirs(folder, exist_ok=True)

        issue = (gh.issue(issue_no) if issue_no else None) or {}
        with open(os.path.join(folder, "ticket.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# #{issue_no} {issue.get('title', '(issue not found)')}\n\n{issue.get('body') or ''}\n")

        files = gh.paginate(gh.r(f"pulls/{n}/files"))
        paths = [f["filename"] for f in files]
        with open(os.path.join(folder, "diff.patch"), "w", encoding="utf-8") as fh:
            for f in files:
                fh.write(f"--- {f['filename']} ({f.get('status', 'modified')})\n")
                fh.write((f.get("patch") or "(no textual patch)") + "\n\n")

        later: dict[str, dict] = {}
        lines = [f"# Commits on {main_branch} touching this PR's files after {merged_at}\n"]
        for path in paths:
            commits = gh.paginate(gh.r("commits"), {"path": path, "since": merged_at, "sha": main_branch})
            commits = [c for c in commits if c.get("sha") != merge_sha]
            lines.append(f"\n## {path}\n")
            if not commits:
                lines.append("(none)\n")
            for c in commits:
                info = c.get("commit") or {}
                date = ((info.get("author") or {}).get("date")) or ""
                lines.append(f"- {c.get('sha', '')[:12]} {date} {_first_line(info.get('message'))}\n")
                later[c.get("sha", "")] = c
        with open(os.path.join(folder, "later_commits.md"), "w", encoding="utf-8") as fh:
            fh.write("".join(lines))

        tests, impl = classify(paths)
        rows.append({"pr": n, "issue": issue_no, "lane": lane, "title": pr.get("title", ""),
                     "merged_at": merged_at, "merge_commit_sha": merge_sha, "files": len(paths),
                     "test_files": tests, "impl_files": impl, "later_commit_count": len(later),
                     "later_commits": len(later)})
    return rows


# --- 3. mechanical check --------------------------------------------------------------

def _skip(reason: str) -> dict:
    return {"pass_at_commit": "n/a", "fail_on_parent": "n/a", "tests_meaningful_mech": "n/a", "reason": reason}


def mechanical_check(repo_dir: str, merge_sha: str, impl_files: list[str], test_files: list[str],
                     test_cmd: str | None, run: Run = subprocess.run) -> dict:
    """Tests pass at the merge commit and fail once the implementation is reverted -> meaningful."""
    if not test_files:
        return _skip("no test files in the PR")
    if not impl_files:
        return _skip("no implementation files in the PR")
    if not test_cmd:
        return _skip("no test command configured")

    def git(*args: str):
        return run(["git", "-C", work, *args], capture_output=True, text=True)

    tmp = tempfile.mkdtemp(prefix="ns-audit-")
    work = os.path.join(tmp, "wt")
    added = False
    try:
        try:
            res = run(["git", "-C", repo_dir, "worktree", "add", "--detach", work, merge_sha],
                      capture_output=True, text=True)
        except FileNotFoundError:
            return _skip("git not found")
        if res.returncode != 0:
            return _skip(f"git worktree add failed: {(res.stderr or res.stdout or '').strip()[:200]}")
        added = True
        cmd = shlex.split(test_cmd.replace("{tests}", " ".join(shlex.quote(t) for t in test_files)))

        def tests_pass() -> bool:
            return run(cmd, cwd=work, capture_output=True, text=True).returncode == 0

        try:
            passed = tests_pass()
        except FileNotFoundError:
            return _skip(f"test toolchain not found: {cmd[0] if cmd else test_cmd}")
        if not passed:
            return {"pass_at_commit": "no", "fail_on_parent": "n/a", "tests_meaningful_mech": "no",
                    "reason": "tests fail at the merge commit"}

        existing, new = [], []
        for f in impl_files:
            (existing if git("cat-file", "-e", f"{merge_sha}^:{f}").returncode == 0 else new).append(f)
        if existing:
            res = git("checkout", f"{merge_sha}^", "--", *existing)
            if res.returncode != 0:
                return {**_skip(f"could not revert impl files: {(res.stderr or '').strip()[:200]}"),
                        "pass_at_commit": "yes"}
        for f in new:
            try:
                os.remove(os.path.join(work, f))
            except FileNotFoundError:
                pass
        failed = not tests_pass()
        return {"pass_at_commit": "yes", "fail_on_parent": "yes" if failed else "no",
                "tests_meaningful_mech": "yes" if failed else "no",
                "reason": "" if failed else "tests still pass with the implementation reverted"}
    finally:
        if added:
            try:
                run(["git", "-C", repo_dir, "worktree", "remove", "--force", work], capture_output=True, text=True)
            except FileNotFoundError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# --- 4. CSV ---------------------------------------------------------------------------

def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({c: ("" if row.get(c) is None else row.get(c)) for c in COLUMNS})


def read_csv(path: str) -> list[dict]:
    """Rows as strings; raises ValueError on an unknown label or tests_meaningful value."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {', '.join(missing)}")
        rows = [{c: (r.get(c) or "").strip() for c in COLUMNS} for r in reader]
    for i, r in enumerate(rows, start=2):
        for col in ("llm_label", "human_label"):
            if r[col] and r[col] not in LABELS:
                raise ValueError(f"{path}:{i}: {col}={r[col]!r}; use one of {' | '.join(LABELS)}")
        for col in ("pass_at_commit", "fail_on_parent", "tests_meaningful_mech", "llm_tests_meaningful",
                    "human_tests_meaningful"):
            if r[col] and r[col] not in TRI:
                raise ValueError(f"{path}:{i}: {col}={r[col]!r}; use one of {' | '.join(TRI)}")
    return rows


# --- 5. report ------------------------------------------------------------------------

def report(csv_path: str) -> str:
    rows = read_csv(csv_path)
    human = [r for r in rows if r["human_label"]]
    llm = [r for r in rows if r["llm_label"]]
    out = [f"Audit of {len(rows)} merged agent PRs ({csv_path})", ""]
    if human:
        labelled, col = human, "human_label"
        out.append(f"Labels: human ({len(human)} of {len(rows)} rows labelled)")
    elif llm:
        labelled, col = llm, "llm_label"
        out.append(f"Labels: {NOT_GROUND_TRUTH} ({len(llm)} of {len(rows)} rows); fill human_label")
    else:
        labelled, col = [], ""
        out.append("Labels: none filled yet (run label_with_llm or fill human_label)")
    n = len(labelled)
    if n:
        for label in LABELS:
            k = sum(1 for r in labelled if r[col] == label)
            out.append(f"  {label:<20} {fmt_rate(k, n)}")
        bad = sum(1 for r in labelled if r[col] in BAD_LABELS)
        out.append(f"  {'bad (defect|wrong)':<20} {fmt_rate(bad, n)}")
        out.append(f"  ok rate: {fmt_rate(sum(1 for r in labelled if r[col] == 'ok'), n)}")

    yes = no = mech = 0
    for r in rows:
        value = r["tests_meaningful_mech"] if r["tests_meaningful_mech"] in ("yes", "no") else ""
        if value:
            mech += 1
        else:
            value = r["human_tests_meaningful"] or r["llm_tests_meaningful"]
        yes += value == "yes"
        no += value == "no"
    out.append("")
    out.append(f"Tests meaningful: {fmt_rate(yes, yes + no)} "
               f"({mech} mechanical, {yes + no - mech} from labels; n/a rows excluded)")
    floor_n = n or len(rows)
    out.append(f"Detection floor at n={floor_n}: about {detection_floor(floor_n) * 100:.0f} points "
               f"(full width of a 95% interval at 50%: differences smaller than this cannot be told apart from noise)")

    both = [r for r in rows if r["llm_label"] and r["human_label"]]
    out.append("")
    if both:
        kappa = cohen_kappa([r["llm_label"] for r in both], [r["human_label"] for r in both])
        agree = sum(1 for r in both if r["llm_label"] == r["human_label"])
        k_txt = "undefined" if kappa is None else f"{kappa:.2f}"
        out.append(f"LLM vs human agreement: kappa {k_txt}, raw {agree}/{len(both)} rows")
    else:
        out.append("LLM vs human agreement: human labels not filled yet")
    return "\n".join(out) + "\n"


def catch_rate(rejected_bad: int, merged: int, merged_bad_rate_rows: tuple[int, int]) -> str:
    """Share of bad PRs the review gate stopped, estimating the bad ones that slipped through from the audit."""
    bad, audited = merged_bad_rate_rows
    if audited <= 0:
        return "Catch rate: n/a (no audited PRs to estimate merged bad PRs from)"
    est_bad = merged * bad / audited
    total = rejected_bad + est_bad
    if total <= 0:
        return "Catch rate: n/a (no bad PRs rejected or estimated)"
    rate = rejected_bad / total
    n = rejected_bad + round(est_bad)
    _, lo, hi = wilson(rejected_bad, n) if n else (0.0, 0.0, 1.0)
    return (f"Catch rate: {rate:.0%} (95% CI {lo:.0%}–{hi:.0%}, n={n}) = {rejected_bad} rejected bad / "
            f"({rejected_bad} + {est_bad:.1f} estimated merged bad)\n"
            f"Assumption: the audited sample ({bad} bad of {audited}) is representative of all {merged} merged "
            f"PRs, and every rejected PR counted as bad really was bad. The interval ignores the uncertainty "
            f"of the estimate itself, so the true range is wider.")


# --- 6. optional LLM labels -----------------------------------------------------------

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "tests_meaningful": {"type": "string", "enum": list(TRI)},
        "notes": {"type": "string"},
    },
    "required": ["label", "tests_meaningful"],
    "additionalProperties": False,
}


def llm_label_prompt(evidence_dir: str) -> str:
    return f"""You are auditing one pull request that an AI agent wrote and that was merged.
The evidence is in {evidence_dir}: ticket.md (the issue the PR answered), diff.patch (the change)
and later_commits.md (commits that touched the same files after the merge). Read all three.

Pick exactly one label:
- ok: the change does what the ticket asked and nothing later had to fix it.
- defect_fixed_later: a later commit fixed a bug this PR introduced or left.
- extended_later: later commits built on it (new features), with no fix to this PR's behaviour.
- wrong: the change does not do what the ticket asked, or does it in a broken way.
- trivial: too small to judge (renames, config, docs-only).

tests_meaningful: yes if the PR's tests would fail without its implementation change, no if they
would still pass (or test nothing relevant), n/a if the PR adds or changes no tests.
Judge only from the evidence. Put one or two sentences of reasoning in notes."""


def _parse_claude(stdout: str) -> dict:
    data = json.loads(stdout)
    if isinstance(data, dict) and isinstance(data.get("structured_output"), dict):
        return data["structured_output"]
    if isinstance(data, dict) and "result" in data:
        result = data["result"]
        return result if isinstance(result, dict) else json.loads(result)
    return data


def label_with_llm(csv_path: str, evidence_root: str, run: Run = subprocess.run, model: str = "opus") -> str:
    rows = read_csv(csv_path)
    out = ["LLM labels are not ground truth; fill human_label", ""]
    schema = json.dumps(LABEL_SCHEMA)
    for r in rows:
        if r["llm_label"]:
            continue
        evidence = os.path.join(evidence_root, f"pr-{r['pr']}")
        args = ["claude", "-p", llm_label_prompt(evidence), "--model", model, "--output-format", "json",
                "--json-schema", schema, "--allowedTools", "Read,Grep,Glob", "--add-dir", evidence]
        try:
            res = run(args, capture_output=True, text=True)
        except FileNotFoundError:
            out.append("claude CLI not found; stopping")
            break
        try:
            if res.returncode != 0:
                raise ValueError((res.stderr or "").strip()[:200] or f"exit {res.returncode}")
            got = _parse_claude(res.stdout)
            label, tm = got.get("label"), got.get("tests_meaningful")
            if label not in LABELS or tm not in TRI:
                raise ValueError(f"unexpected answer {got!r}"[:200])
        except (ValueError, TypeError, AttributeError) as err:
            out.append(f"PR #{r['pr']}: labelling failed ({err})")
            continue
        r["llm_label"], r["llm_tests_meaningful"] = label, tm
        if got.get("notes"):
            r["notes"] = (r["notes"] + " | " if r["notes"] else "") + "LLM: " + str(got["notes"]).strip()
        out.append(f"PR #{r['pr']}: {label} (tests meaningful: {tm})")
    write_csv(rows, csv_path)
    return "\n".join(out) + "\n"
