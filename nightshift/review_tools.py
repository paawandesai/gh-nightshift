"""Inputs and output for the review gate (`review-inputs`, `review-report`)."""
from __future__ import annotations

from . import protocol as ns

import json
import subprocess
from pathlib import Path
from collections.abc import Callable

from . import review_rules
from .config import BUILTIN_PROTECTED, Config, ConfigError, load as load_config
from .gh import GH, GHError
from .rules import BRANCH_RE, _matches, path_is_protected

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


def security_trigger(cfg: Config | None, hits: list[review_rules.Hit], changed: list[str]) -> list[str]:
    """Why the security reviewer must run (empty: it need not)."""
    if cfg is None:
        return []
    reasons = ["`security.always` is on"] if cfg.security_always else []
    reasons += [f"security rule `{h.rule.id}` matched {h.path}" + (f":{h.line}" if h.line else "")
                for h in hits if h.rule.security]
    paths = cfg.security_trigger_paths
    reasons += [f"{p} is under a security path" for p in changed if paths and path_is_protected(p, paths)]
    return reasons


def write_rules(rules_root: str | None, pr_dir: str, diff: str, out: Path) -> dict[str, str]:
    """Run the base branch's review rules over the PR and write what the reviewers need."""
    cfg = None
    rules: list[review_rules.Rule] = []
    if rules_root:
        try:
            cfg = load_config(rules_root)
        except ConfigError:
            cfg = None  # no or broken config: the rules still run, the security gate has no paths
        rules = review_rules.load(rules_root)
    hits = review_rules.scan(rules, pr_dir, diff)
    cap = review_rules.MAX_HITS
    sec = [h for h in hits if h.rule.security]
    (out / "rules.md").write_text(review_rules.render(hits, cap), encoding="utf-8")
    (out / "security_rules.md").write_text(review_rules.render(sec, cap, "Security rules"), encoding="utf-8")
    (out / "rule_hits.json").write_text(json.dumps(review_rules.items(hits, cap)), encoding="utf-8")
    (out / "security_hits.json").write_text(json.dumps(review_rules.items(sec, cap)), encoding="utf-8")
    reasons = security_trigger(cfg, hits, sorted(review_rules.changed_lines(diff)))
    (out / "security_trigger.md").write_text(
        "# Why the security review ran\n\n" + "".join(f"- {r}\n" for r in reasons[:50]), encoding="utf-8")
    return {"rule_hits": str(len(hits)), "security": "true" if reasons else "false"}


def _names(git: GitRunner, rng: str) -> set[str]:
    out = git(["-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", "-z", rng])
    return {p for p in out.replace("\n", "\0").split("\0") if p}


def reusable_verdict(gh: GH, git: GitRunner, base_ref: str, check: str,
                     protected: list[str]) -> tuple[str, str]:
    """(previous head SHA, why) when this head only merged main into a PR whose last review passed, else ("", why).

    In strict mode the picker brings a PR up to date with `update-branch`, which adds a merge commit and re-runs
    the review. The earlier verdict still holds when all of these are true:
    - HEAD is a merge whose second parent is on main (an update from main, nothing else);
    - the PR's own diff against its merge base is byte for byte what it was before the update;
    - main changed none of the files the PR changes, and none of the protected paths (workflows, CLAUDE.md,
      .night-shift/ with the review rules and prompts), so the reviewer would see the same inputs;
    - the check `check` (verdict or security) completed with success on the previous head.
    CI still runs on the merged result, so a semantic break that crosses files is caught by the tests."""
    try:
        parents = git(["rev-list", "--parents", "-n", "1", "HEAD"]).split()
        if len(parents) != 3:
            return "", "the new head is not a merge from main"
        _, prev, merged = parents
        git(["merge-base", "--is-ancestor", merged, base_ref])
        mb_prev = git(["merge-base", base_ref, prev]).strip()
        mb_now = git(["merge-base", base_ref, "HEAD"]).strip()
        main_files = _names(git, f"{mb_prev}..{mb_now}")
        overlap = sorted(main_files & _names(git, f"{mb_now}..HEAD"))
        if overlap:
            return "", f"main changed file(s) this PR changes: {', '.join(overlap[:5])}"
        if git(["diff", f"{mb_prev}..{prev}"]) != git(["diff", f"{mb_now}..HEAD"]):
            return "", "the PR's own changes differ from the reviewed head"
        guarded = sorted(f for f in main_files if path_is_protected(f, protected))
        if guarded:
            return "", f"main changed review inputs (workflows, rules, prompts or agent files): {', '.join(guarded[:5])}"
    except RuntimeError as err:
        return "", f"git could not establish it ({str(err)[:200]})"
    try:
        runs = [r for r in gh.check_runs(prev) if _matches(check, r.get("name", ""))]
    except GHError as err:
        return "", f"could not read checks on {prev[:12]} (HTTP {err.status}; the caller needs checks: read)"
    if not runs:
        return "", f"no `{check}` check on the previous head {prev[:12]}"
    latest = max(runs, key=lambda r: r.get("completed_at") or r.get("started_at") or "")
    if (latest.get("status") or "").lower() != "completed" or (latest.get("conclusion") or "").lower() != "success":
        return "", f"`{check}` on the previous head {prev[:12]} did not pass"
    return prev, (f"this push only merged main into the PR. The PR's own diff is unchanged since {prev[:12]}, "
                  f"where `{check}` passed; main changed {len(main_files)} file(s), none of them touched by this PR "
                  "and none of them review inputs. CI still runs on the merged result.")


def write_inputs(gh: GH, git: GitRunner, base_ref: str, head_ref: str, out_dir: str,
                 rules_root: str | None = None, pr_dir: str = ".", reuse_check: str = "") -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if reuse_check:
        protected = BUILTIN_PROTECTED
        if rules_root:
            try:
                protected = load_config(rules_root).protected_paths
            except ConfigError:
                pass
        prev, why = reusable_verdict(gh, git, base_ref, reuse_check, protected)
        (out / "reuse.md").write_text(f"### Night-shift {'security review' if 'security' in reuse_check else 'review'}"
                                      f": **pass** (reused)\n\nVerdict reused: {why}\n" if prev else
                                      f"Full review: {why}\n", encoding="utf-8")
        if prev:
            return {"reuse": prev}
    diff, drift, overlap = drift_report(git, base_ref)
    rule_values = write_rules(rules_root, pr_dir, diff, out)
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
    return {"issue": issue, "overlap": str(len(overlap)), "reuse": "", **rule_values}


def _load_json(path: str | None) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _structured(result_path: str) -> dict:
    out = _load_json(result_path).get("structured_output") or {}
    return ns.defang(out) if isinstance(out, dict) else {}  # the reviewer read agent-written code


def _rule_lines(listed: dict, answers: object, security_only: bool = False) -> list[str]:
    given = {str(a.get("key", "")): a for a in (answers if isinstance(answers, list) else []) if isinstance(a, dict)}
    lines = []
    for item in listed.get("items") or []:
        if security_only and not item.get("security"):
            continue
        a = given.get(str(item.get("key", ""))) or {}
        mark = "✅" if a.get("answer") == "yes" else "❌"
        lines.append(f"- {mark} `{item.get('key', '')}`: {a.get('evidence', '(not answered)')}")
    return lines


def render_report(result_path: str, overlap: int = 0, rules_path: str | None = None) -> tuple[str, str]:
    """(verdict, comment markdown). verdict is pass, fail or error (the reviewer itself broke).
    `overlap` is the mechanical count of files both this PR and main changed since the merge base: when it is
    above 0, a pass needs the reviewer to state that the PR is still correct against current main (add-on B)."""
    out = _structured(result_path)
    verdict = out.get("verdict") if out.get("verdict") in {"pass", "fail"} else "error"
    if verdict == "error":
        return verdict, "Review run failed to produce a verdict; see the job log. The attempt is not counted.\n"
    listed = _load_json(rules_path)
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
    if listed.get("items") or listed.get("total"):
        lines += ["", "**Repo review rules**"] + _rule_lines(listed, out.get("rule_answers"))
        problems = review_rules.check_answers(listed, out.get("rule_answers"))
        if problems and verdict == "pass":
            verdict = "fail"
            lines += ["", "Verdict changed to **fail**: " + "; ".join(problems)]
        lines[0] = f"### Night-shift review: **{verdict}**"
    if out.get("fixes"):
        lines += ["", f"**Fixes needed:** {out['fixes']}"]
    return verdict, "\n".join(lines) + "\n"


BLOCKING = {"high", "critical"}


def render_security_report(result_path: str, rules_path: str | None = None) -> tuple[str, str]:
    """(verdict, comment) for the security reviewer: fail on a `fail`, an unanswered or `no` security rule
    item, or any high or critical finding, whatever verdict the reviewer gave."""
    out = _structured(result_path)
    verdict = out.get("verdict") if out.get("verdict") in {"pass", "fail"} else "error"
    if verdict == "error":
        return verdict, "Security review failed to produce a verdict; see the job log. The attempt is not counted.\n"
    listed = _load_json(rules_path)
    findings = [f for f in out.get("findings") or [] if isinstance(f, dict)]
    lines = ["### Night-shift security review: **pass**", ""]
    if findings:
        lines.append("**Findings**")
        for f in findings:
            lines.append(f"- **{f.get('severity', '?')}** {f.get('title', '')} ({f.get('location', '')}): "
                         f"{f.get('evidence', '')} Attack path: {f.get('attack_path', '')}")
    else:
        lines.append("No findings.")
    if listed.get("items") or listed.get("total"):
        lines += ["", "**Security rules**"] + _rule_lines(listed, out.get("rule_answers"), security_only=True)
    problems = review_rules.check_answers(listed, out.get("rule_answers"), security_only=True)
    blocking = [f for f in findings if f.get("severity") in BLOCKING]
    if verdict == "pass" and (problems or blocking):
        verdict = "fail"
        why = problems + [f"{len(blocking)} high or critical finding(s)"] * bool(blocking)
        lines += ["", "Verdict changed to **fail**: " + "; ".join(why)]
    if out.get("fixes"):
        lines += ["", f"**Fixes needed:** {out['fixes']}"]
    lines[0] = f"### Night-shift security review: **{verdict}**"
    return verdict, "\n".join(lines) + "\n"
