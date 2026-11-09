"""`gh nightshift doctor`: read-only checks with a fix for every problem."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import scaffold
from .config import CONFIG_PATH, Config, ConfigError, load
from .gh import GH, GHError
from .repo_setup import existing_secrets, get_variable

OK, WARN, FAIL = "ok", "warn", "FAIL"
LANE_WORKFLOW = ".github/workflows/night-shift.yml"


@dataclass
class Check:
    level: str
    what: str
    fix: str = ""

    def line(self) -> str:
        mark = {"ok": "✓", "warn": "!", "FAIL": "✗"}[self.level]
        return f"{mark} {self.what}" + (f"\n    fix: {self.fix}" if self.fix else "")


def token_scopes(token: str | None = None, run: Callable = subprocess.run) -> tuple[str | None, str]:
    """(login, X-OAuth-Scopes header) for a token (default: gh's own). Fine-grained tokens send no
    scopes header, so their scopes come back as ''."""
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    result = run(["gh", "api", "-i", "user"], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        return None, ""
    scopes, login = "", None
    for line in result.stdout.splitlines():
        if line.lower().startswith("x-oauth-scopes:"):
            scopes = line.split(":", 1)[1].strip()
        m = re.search(r'"login"\s*:\s*"([^"]+)"', line)
        if m and login is None:
            login = m.group(1)
    return login, scopes


def pat_checks(token: str, run: Callable = subprocess.run) -> list[Check]:
    login, scopes = token_scopes(token, run)
    if login is None:
        return [Check(FAIL, "the PAT does not authenticate", "create a new fine-grained PAT and set AGENT_PAT")]
    out = [Check(OK, f"PAT authenticates as {login}")]
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    if "workflow" in scope_list:
        out.append(Check(FAIL, "PAT has the `workflow` scope: agents could change CI and the review gate",
                         "use a fine-grained PAT without the Workflows permission"))
    elif scope_list:
        out.append(Check(WARN, f"classic PAT with scopes {scopes}: it reaches every repo you can",
                         "prefer a fine-grained PAT limited to this repo"))
    else:
        out.append(Check(WARN, "fine-grained PAT: GitHub does not expose its permissions, so Workflows cannot be "
                               "checked from here", "confirm in Settings > Developer settings that Workflows is "
                                                    "'No access' and Actions is 'Read'"))
    return out


def local_checks(root: Path) -> tuple[Config | None, list[Check]]:
    out: list[Check] = []
    try:
        cfg = load(root)
        out.append(Check(OK, f"{CONFIG_PATH} is valid ({len(cfg.lanes)} lanes: {', '.join(cfg.lane_names)})"))
    except ConfigError as err:
        return None, [Check(FAIL, str(err), "run `gh nightshift init`, or fix the file")]
    wf = root / LANE_WORKFLOW
    if not wf.is_file():
        out.append(Check(FAIL, f"{LANE_WORKFLOW} is missing", "run `gh nightshift init --regenerate`"))
    else:
        text = wf.read_text(encoding="utf-8")
        fails_before = sum(c.level == FAIL for c in out)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        if "secrets: inherit" in code or re.search(r"secrets\[", code):
            out.append(Check(FAIL, f"{LANE_WORKFLOW} uses dynamic secrets (secrets[...] or inherit): every repo secret "
                                   "would reach the lane runners", "run `gh nightshift init --regenerate`"))
        for lane in cfg.lanes:
            job = f"lane-{lane.name}:"
            if job not in text:
                out.append(Check(FAIL, f"lane {lane.name} has no job in {LANE_WORKFLOW}",
                                 "run `gh nightshift init --regenerate`"))
            elif f"secrets.{lane.token_secret} " not in text:
                out.append(Check(FAIL, f"lane {lane.name}'s job does not pass secrets.{lane.token_secret}",
                                 "run `gh nightshift init --regenerate`"))
        if f"@{cfg.tool_ref}" not in text:
            out.append(Check(WARN, f"caller workflow is not pinned to tool_ref {cfg.tool_ref}",
                             "run `gh nightshift init --regenerate`"))
        elif not re.fullmatch(r"v\d+(\.\d+)*|[0-9a-f]{40}", cfg.tool_ref):
            out.append(Check(WARN, f"workflows follow gh-nightshift@{cfg.tool_ref}, a moving branch",
                             "pin tool_ref to a release tag or commit SHA in .night-shift/config.yml"))
        elif sum(c.level == FAIL for c in out) == fails_before:
            out.append(Check(OK, f"caller workflow has one job per lane with explicit secrets, pinned @{cfg.tool_ref}"))
    for name in ("night-shift-review.yml",):
        if not (root / ".github/workflows" / name).is_file():
            out.append(Check(FAIL, f".github/workflows/{name} is missing", "run `gh nightshift init --regenerate`"))
    ci = root / ".github/workflows" / cfg.ci_workflow
    if not ci.is_file():
        out.append(Check(FAIL, f"CI workflow .github/workflows/{cfg.ci_workflow} not found: every lane would wait",
                         "set ci_workflow in .night-shift/config.yml to your CI file"))
    elif cfg.merge_mode == "queue" and "merge_group" not in ci.read_text(encoding="utf-8"):
        out.append(Check(FAIL, f"merge.mode is queue but {cfg.ci_workflow} does not run on merge_group",
                         "add `merge_group:` to its `on:` triggers"))
    for lane in cfg.lanes:
        if lane.setup_action and not any((root / lane.setup_action / f).is_file() for f in ("action.yml", "action.yaml")):
            out.append(Check(FAIL, f"lane {lane.name}: setup action {lane.setup_action} has no action.yml",
                             "create the composite action or remove setup.action"))
        if not lane.test_command:
            out.append(Check(WARN, f"lane {lane.name} has no test_command", "set lanes[].test_command"))
    return cfg, out


def remote_checks(gh: GH, cfg: Config) -> list[Check]:
    out: list[Check] = []
    have = existing_secrets(gh)
    if not have:
        out.append(Check(WARN, "cannot list Actions secrets (needs repo admin)", "run doctor as a repo admin"))
    for name in cfg.secret_names():
        if have and name not in have:
            out.append(Check(FAIL, f"secret {name} is missing",
                             f"`gh nightshift join --lane <lane>` or `gh secret set {name} -R {gh.repo}`"))
    if have and all(n in have for n in cfg.secret_names()):
        out.append(Check(OK, f"all {len(cfg.secret_names())} secrets exist"))
    labels = {lbl["name"] for lbl in gh.paginate(gh.r("labels"))}
    missing = [n for n, _, _ in scaffold.labels(cfg) if n not in labels]
    out.append(Check(FAIL, f"labels missing: {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}",
                     "run `gh nightshift init` (or `gh nightshift setup`)") if missing else Check(OK, "labels exist"))
    try:
        prot = gh.get(gh.r(f"branches/{cfg.main_branch}/protection"))
    except GHError as err:
        prot = None
        out.append(Check(FAIL, f"{cfg.main_branch} is not protected ({err.status or err})", "run `gh nightshift setup`"))
    if prot:
        rsc = prot.get("required_status_checks") or {}
        contexts = set(rsc.get("contexts") or []) | {c.get("context") for c in rsc.get("checks") or []}
        lacking = [c for c in cfg.required_checks if c not in contexts]
        if lacking:
            out.append(Check(FAIL, f"required checks missing: {', '.join(lacking)}", "run `gh nightshift setup`"))
        if cfg.merge_mode == "strict" and not rsc.get("strict"):
            out.append(Check(FAIL, "branch protection has strict=false: PRs can merge without being tested against "
                                   "current main (one hackathon PR merged with a test failing at its own merge commit)",
                             f"run `gh nightshift setup`, or: gh api -X PATCH repos/{gh.repo}/branches/{cfg.main_branch}"
                             "/protection/required_status_checks -F strict=true"))
        elif cfg.merge_mode == "strict":
            out.append(Check(OK, "branch protection is strict (PRs are tested against current main)"))
        if not (prot.get("enforce_admins") or {}).get("enabled"):
            out.append(Check(WARN, "admins can bypass branch protection (the PAT owner is often an admin)",
                             "run `gh nightshift setup` (enforce_admins=true)"))
    if cfg.merge_mode == "queue":
        rulesets = gh.get_or_none(gh.r("rulesets")) or []
        if not any(r.get("name") == "night-shift merge queue" for r in rulesets):
            out.append(Check(FAIL, "merge.mode is queue but no merge queue ruleset exists", "run `gh nightshift setup`"))
    repo = gh.get(gh.r()) or {}
    if not repo.get("delete_branch_on_merge") or not repo.get("allow_squash_merge"):
        out.append(Check(WARN, "squash merge or delete-branch-on-merge is off", "run `gh nightshift setup`"))
    enabled = get_variable(gh, "AGENTS_ENABLED")
    if enabled is None:
        out.append(Check(FAIL, "variable AGENTS_ENABLED is missing", "run `gh nightshift setup`"))
    else:
        out.append(Check(OK if enabled == "true" else WARN, f"AGENTS_ENABLED={enabled}",
                         "" if enabled == "true" else "`gh nightshift start` when main is green"))
    fix_lane = get_variable(gh, "FIX_LANE")
    if cfg.effective_fix_lane and fix_lane != cfg.effective_fix_lane:
        out.append(Check(WARN, f"variable FIX_LANE is {fix_lane!r}, config says {cfg.effective_fix_lane!r}",
                         "run `gh nightshift setup`"))
    if not gh.night_log(cfg.label("night_log")):
        out.append(Check(FAIL, "no open night-log issue", "run `gh nightshift setup`"))
    out += tool_checks(gh, cfg)
    return out


def tool_checks(gh: GH, cfg: Config) -> list[Check]:
    """The caller's `uses:` and the unauthenticated tool checkouts need TOOL_REPO public and tool_ref to exist."""
    from .scaffold import TOOL_REPO

    tool = gh.get_or_none(f"repos/{TOOL_REPO}")
    if tool is None:
        return [Check(FAIL, f"cannot read {TOOL_REPO}", "the workflows need it reachable; check the name")]
    if tool.get("private"):
        return [Check(FAIL, f"{TOOL_REPO} is private: other repos cannot use its workflows or check it out",
                      "make it public, or fork it publicly and point the caller workflows at the fork")]
    if gh.get_or_none(f"repos/{TOOL_REPO}/commits/{cfg.tool_ref}") is None:
        return [Check(FAIL, f"{TOOL_REPO}@{cfg.tool_ref} does not exist",
                      "set tool_ref in .night-shift/config.yml to a pushed tag or SHA, then `gh nightshift init --regenerate`")]
    return [Check(OK, f"{TOOL_REPO}@{cfg.tool_ref} is reachable")]


def own_token_checks(run: Callable = subprocess.run) -> list[Check]:
    login, scopes = token_scopes(None, run)
    if login is None:
        return [Check(WARN, "gh is not authenticated here", "gh auth login")]
    if scopes and "workflow" not in scopes:
        return [Check(WARN, f"your gh token ({login}) lacks the `workflow` scope, so pushing the caller workflows "
                            "will be rejected", "gh auth refresh -s workflow")]
    return [Check(OK, f"gh authenticated as {login}")]


def run_doctor(gh: GH, root: Path, pat_token: str | None = None, run: Callable = subprocess.run) -> list[Check]:
    cfg, checks = local_checks(root)
    checks += own_token_checks(run)
    if pat_token:
        checks += pat_checks(pat_token, run)
    else:
        checks.append(Check(WARN, "PAT permissions not checked", "re-run with --pat-env VAR (VAR holds the PAT); "
                                                                "the lane workflow also checks it on every run"))
    if cfg is not None:
        try:
            checks += remote_checks(gh, cfg)
        except GHError as err:
            checks.append(Check(FAIL, f"cannot read {gh.repo}: {err}", "check `gh auth status` and the repo name"))
    return checks
