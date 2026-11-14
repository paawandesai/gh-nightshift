"""Pure rules shared by the picker, the planner and the CLI. No I/O here, so it is all unit-testable."""
from __future__ import annotations

import datetime as dt
import fnmatch
import posixpath
import re

UNRESOLVED = -1  # a dependency we can't resolve to an issue number counts as unmet
BRANCH_RE = re.compile(r"^agent/([^/]+)/issue-(\d+)$")
MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

RUN_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
STATUS_FAILED = {"failure", "error"}


def label_names(item: dict) -> set[str]:
    return {(lbl["name"] if isinstance(lbl, dict) else lbl) for lbl in item.get("labels", [])}


def clean_model(value: str | None) -> str:
    """Accept only alias/name-like strings, e.g. sonnet, opus, claude-opus-5-5."""
    value = (value or "").strip()
    return value if MODEL_RE.fullmatch(value) else ""


def model_for(item: dict, failed_attempts: int, lane_model: str, escalate_model: str) -> str:
    """A `model:<alias>` label wins; otherwise the lane model first, the stronger one on retries."""
    for name in sorted(label_names(item)):
        if name.startswith("model:") and clean_model(name[6:]):
            return name[6:]
    return escalate_model if failed_attempts > 0 else lane_model


CROSS_RE = re.compile(r"(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?(?:#|/|$))[A-Za-z0-9._-]{1,100})"
                      r"(?:#|/issues/)(\d+)")


def _dep_tokens(body: str | None):
    for line in (body or "").splitlines():
        m = re.search(r"(?:blocked\s+by|depends\s+on)\s*:?\s*(.*)", line, re.I)
        if not m:
            continue
        rest = m.group(1).strip().rstrip(".")
        if not rest or rest.lower() in {"none", "n/a", "-", "nothing", "#"}:
            continue
        for token in re.split(r"\s*(?:,|\band\b|\s)\s*", rest):
            token = token.strip().rstrip(".")
            if token:
                yield token


def parse_deps(body: str | None, repo: str = "") -> list[int]:
    """Issue numbers in this repo this issue waits on. Handles "Blocked by #3 and #4", "Depends on #5" and the
    Idea Factory's "- Blocked by: #12, #13" / "- Blocked by: none" / "- Blocked by: E01-S03". Cross-repo
    references (`owner/repo#7`) are left to parse_cross_deps, except `repo` itself written in full; anything else
    unresolvable counts as unmet."""
    deps: set[int] = set()
    for token in _dep_tokens(body):
        num = re.fullmatch(r"#(\d+)", token)
        cross = CROSS_RE.fullmatch(token)
        if num:
            deps.add(int(num.group(1)))
        elif cross and repo and cross.group(1).lower() == repo.lower():
            deps.add(int(cross.group(2)))
        elif not cross or not repo:
            deps.add(UNRESOLVED)  # without a repo (planner, roadmap) a cross-repo ref is not checkable: unmet
    return sorted(deps)


def parse_cross_deps(body: str | None, this_repo: str = "") -> list[tuple[str, int]]:
    """`Blocked by: owner/repo#7` (or an issue URL) in another repository; `this_repo` itself is left to parse_deps."""
    out: set[tuple[str, int]] = set()
    for token in _dep_tokens(body):
        m = CROSS_RE.fullmatch(token)
        if m:
            out.add((m.group(1), int(m.group(2))))
    return sorted(x for x in out if x[0].lower() != this_repo.lower())



def wave_of(item: dict) -> int:
    for name in label_names(item):
        m = re.fullmatch(r"wave:W?(\d+)", name, re.I)
        if m:
            return int(m.group(1))
    return 999


def lanes_of(item: dict) -> list[str]:
    return sorted(n[5:] for n in label_names(item) if n.startswith("lane:"))


def _matches(required: str, name: str) -> bool:
    # A reusable workflow's job shows up as "<caller job> / <job>"; accept either spelling.
    return name == required or name.endswith(" / " + required) or required.endswith(" / " + name)


def checks_state(check_runs: list[dict], statuses: list[dict], required: list[str] | None = None) -> str:
    """'failed', 'pending' or 'passed' for a commit, from REST check runs and commit statuses.
    Every required check must have reported before a commit counts as passed."""
    if not check_runs and not statuses:
        return "pending"
    pending = False
    seen: list[str] = []
    for run in check_runs:
        seen.append(run.get("name", ""))
        if (run.get("status") or "").lower() != "completed":
            pending = True
        elif (run.get("conclusion") or "").lower() in RUN_FAILED:
            return "failed"
    for status in statuses:
        seen.append(status.get("context", ""))
        state = (status.get("state") or "").lower()
        if state in STATUS_FAILED:
            return "failed"
        if state != "success":
            pending = True
    for req in required or []:
        if not any(_matches(req, name) for name in seen):
            pending = True
    return "pending" if pending else "passed"


def age_minutes(created_at: str | None, now: dt.datetime | None = None) -> float:
    if not created_at:
        return 0.0
    created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - created).total_seconds() / 60


# Files Claude Code (and similar agents) read as instructions wherever they sit in the tree.
AGENT_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", ".claude/")
# Built-in protected names compared without regard to case (W-03): on a macOS or Windows clone `.GitHub/` or
# `claude.md` lands on the real file. Keep in step with config.BUILTIN_PROTECTED (a test checks this).
# User-configured protected_paths stay case-sensitive: they name the repo's own files, as git stores them.
CASELESS_NAMES = frozenset(n.casefold() for n in AGENT_FILES + (".github/", ".night-shift/", "AGENT_RULES.md"))
CASELESS_AGENT_FILES = frozenset(n.casefold() for n in AGENT_FILES)


def normalize_path(path: str) -> str | None:
    """A repo-relative POSIX path, or None when it escapes the repo (`..`) or is empty."""
    p = posixpath.normpath(str(path).strip().replace("\\", "/")).lstrip("/")
    if p in ("", ".") or p == ".." or p.startswith("../"):
        return None
    return p


def split_paths(entry: str) -> list[str]:
    """`"api/x.py, contracts/y.json"` -> two paths (planner Files entries are free text)."""
    # Every path-like run counts, so markup around a path (`x`, (x), [t](x), <x>, *x*, a|b) cannot hide it.
    return [t.strip("_") for t in re.findall(r"[\w.\-/\\]+", str(entry)) if t.strip("_")]


def path_is_protected(path: str, protected: list[str]) -> bool:
    """`dir/` protects everything under it, `file` protects that exact path; a path that cannot be
    normalised (e.g. escapes with `..`) counts as protected. Agent instruction files (CLAUDE.md,
    CLAUDE.local.md, AGENTS.md, .mcp.json, .claude/) are protected at any depth when they are in the protected list,
    and built-in names (CASELESS_NAMES) match in any case; other entries are case-sensitive.
    An entry with `*`, `?` or `[` is a glob over the whole path; `**/name` matches `name` at any depth
    (`**/package.json`, `**/requirements*.txt`)."""
    norm = normalize_path(path)
    if norm is None:
        return True
    folded = norm.casefold()
    for p in protected:
        p = p.strip().lstrip("/")
        if not p:
            continue
        if any(c in p for c in "*?["):
            if p.startswith("**/"):
                parts = norm.split("/")
                if any(fnmatch.fnmatchcase("/".join(parts[i:]), p[3:]) for i in range(len(parts))):
                    return True
            elif fnmatch.fnmatchcase(norm, p):
                return True
            continue
        caseless = p.casefold() in CASELESS_NAMES
        target = folded if caseless else norm
        if caseless:
            p = p.casefold()
        agent = caseless and p in CASELESS_AGENT_FILES
        if p.endswith("/"):
            if target.startswith(p) or target == p.rstrip("/"):
                return True
            if agent and f"/{p}" in f"/{target}":
                return True
        elif target == p or (agent and target.endswith("/" + p)):
            return True
    return False


REVIEW_CHECK = "verdict"
SECURITY_CHECK = "security"


def failed_check_names(check_runs: list[dict], statuses: list[dict]) -> list[str]:
    names = [r.get("name", "") for r in check_runs
             if (r.get("status") or "").lower() == "completed" and (r.get("conclusion") or "").lower() in RUN_FAILED]
    names += [s.get("context", "") for s in statuses if (s.get("state") or "").lower() in STATUS_FAILED]
    return names


def close_reason(failed: list[str]) -> str:
    """security_failed when the security gate is among the failures, verdict_failed when the review gate is,
    else checks_failed."""
    if any(n == f"review / {SECURITY_CHECK}" for n in failed):
        return "security_failed"
    return "verdict_failed" if any(_matches(REVIEW_CHECK, n) for n in failed) else "checks_failed"


def section(body: str | None, heading: str) -> str:
    """Text under a markdown `## heading` up to the next `## ` heading ('' if absent)."""
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", body or "", re.M | re.S | re.I)
    return m.group(1).strip() if m else ""
