"""`gh nightshift doctor`: read-only checks with a fix for every problem."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import review_rules, scaffold
from .rules import path_is_protected
from .config import CONFIG_PATH, Config, ConfigError, load
from .gh import GH, GHError
from .repo_setup import ACTIONS_APP_ID, existing_secrets, get_variable

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


def pat_checks(token: str, run: Callable = subprocess.run,
               known: tuple[str | None, str] | None = None) -> list[Check]:
    login, scopes = known or token_scopes(token, run)
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


ACCESS_PROBES = (("", "Metadata", "the PAT's Repository access list"),
                 ("commits?per_page=1", "Contents", "Contents: Read and write"),
                 ("issues?per_page=1", "Issues", "Issues: Read and write"),
                 ("pulls?per_page=1", "Pull requests", "Pull requests: Read and write"),
                 ("actions/runs?per_page=1", "Actions", "Actions: Read"),
                 ("commits/{main}/status", "Commit statuses", "Commit statuses: Read"))


ADVISORIES_PROBE = ("security-advisories?per_page=1", "Repository security advisories",
                    "Repository security advisories: Read and write (the red-team scan files drafts)")


def access_checks(gh: GH, advisories: bool = False) -> list[Check]:
    """Probe the endpoints a lane reads with this token. A fine-grained PAT that misses a permission, or does not
    list the repo, gets 404 (or 403) here instead of failing later inside `pick` with a bare Not Found."""
    out: list[Check] = []
    main = "main"
    if advisories:
        try:
            advisories = not repo_private(gh)  # private repos get `security` issues, not advisories
        except GHError:
            advisories = False  # the first probe below reports an unreadable repo
    for suffix, what, fix in ACCESS_PROBES + ((ADVISORIES_PROBE,) if advisories else ()):
        try:
            data = gh.get(gh.r(suffix.format(main=main)))
            if suffix == "" and isinstance(data, dict):
                main = data.get("default_branch") or main
        except GHError as err:
            if suffix == "":
                return [Check(FAIL, f"the PAT cannot see {gh.repo} (HTTP {err.status})",
                              f"edit the PAT: add {gh.repo} under {fix}")]
            out.append(Check(FAIL, f"the PAT cannot read {what} on {gh.repo} (HTTP {err.status})",
                             f"edit the PAT: set {fix}"))
    if advisories and not out and not repo_private(gh) and not ((gh.get(gh.r("")) or {}).get("permissions") or {}).get("admin"):
        out.append(Check(WARN, f"the PAT's account is not an admin of {gh.repo}: filing a security advisory needs an "
                               "admin or security manager", "make the account a security manager, or use App mode"))
    return out or [Check(OK, "PAT can read the repo, contents, issues, pull requests, Actions and commit statuses "
                             + ("and security advisories " if advisories else "") + f"of {gh.repo}")]


def repo_private(gh: GH) -> bool:
    return bool((gh.get(gh.r("")) or {}).get("private", False))


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
        for other in sorted((root / ".github/workflows").glob("night-shift*.yml")):
            body = "\n".join(ln for ln in other.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
            if "secrets: inherit" in body or re.search(r"secrets\[", body):
                out.append(Check(FAIL, f".github/workflows/{other.name} uses dynamic secrets (secrets[...] or inherit): "
                                       "every repo secret would reach its runners", "run `gh nightshift init --regenerate`"))
        for lane in cfg.lanes:
            job = f"lane-{lane.name}:"
            if job not in text:
                out.append(Check(FAIL, f"lane {lane.name} has no job in {LANE_WORKFLOW}",
                                 "run `gh nightshift init --regenerate`"))
            elif f"secrets.{lane.token_secret} " not in text:
                out.append(Check(FAIL, f"lane {lane.name}'s job does not pass secrets.{lane.token_secret}",
                                 "run `gh nightshift init --regenerate`"))
            elif lane.fallback_token_secret and f"secrets.{lane.fallback_token_secret} " not in text:
                out.append(Check(FAIL, f"lane {lane.name}'s job does not pass its fallback secrets."
                                       f"{lane.fallback_token_secret}", "run `gh nightshift init --regenerate`"))
        if cfg.extends and not (root / ".night-shift/base.yml").is_file():
            out.append(Check(WARN, f"config extends {cfg.extends} but .night-shift/base.yml is missing",
                             "run `gh nightshift config pull` and commit the file"))
        co = next((loc for loc in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
                   if (root / loc).is_file()), "")
        if co and not cfg.risk_codeowners:
            out.append(Check(WARN, f"{co} exists but agent PRs merge without a code owner's review",
                             "set `risk: {codeowners: true}` in .night-shift/config.yml"))
        if cfg.auth_mode == "app":
            if f"app_private_key: ${{{{ secrets.{cfg.app_key_secret} }}}}" not in text or "agent_pat:" in code:
                out.append(Check(FAIL, f"auth.mode is app but {LANE_WORKFLOW} does not pass only the App key",
                                 "run `gh nightshift init --regenerate`"))
            else:
                out.append(Check(OK, f"lanes authenticate as GitHub App {cfg.app_id}; the key reaches only the "
                                     "pick and publish jobs"))
        if f"@{cfg.tool_ref}" not in text:
            out.append(Check(WARN, f"caller workflow is not pinned to tool_ref {cfg.tool_ref}",
                             "run `gh nightshift init --regenerate`"))
        elif not re.fullmatch(r"[0-9a-f]{40}", cfg.tool_ref):
            moving = "a tag, which can be moved" if re.fullmatch(r"v\d+(\.\d+)*", cfg.tool_ref) else "a moving branch"
            out.append(Check(WARN, f"workflows follow gh-nightshift@{cfg.tool_ref}, {moving}",
                             "pin tool_ref to the release's 40-character commit SHA in .night-shift/config.yml "
                             "(`gh nightshift init` writes it), then `gh nightshift init --regenerate`"))
        elif sum(c.level == FAIL for c in out) == fails_before:
            out.append(Check(OK, f"caller workflow has one job per lane with explicit secrets, pinned @{cfg.tool_ref}"))
    for name in ("night-shift-review.yml",):
        if not (root / ".github/workflows" / name).is_file():
            out.append(Check(FAIL, f".github/workflows/{name} is missing", "run `gh nightshift init --regenerate`"))
    out += rules_checks(root, cfg)
    out += workflow_checks(root, cfg)
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
    own = [lane.name for lane in cfg.lanes if lane.pat_secret]
    if own and not cfg.trusted_logins:
        out.append(Check(WARN, f"lane(s) {', '.join(own)} use their own PAT but trusted_logins is empty: each lane "
                               "ignores hand-offs, needs and notes posted by the others",
                         f"list every PAT's login under trusted_logins in {CONFIG_PATH}"))
    return cfg, out


SCRIPT_RE = re.compile(r"(?:^|[\s;&|(])(?:(?:bash|sh|zsh|python3?|node|ruby|perl|pwsh)\s+(?:-\S+\s+)*)?"
                       r"(\.{0,2}/?[\w.-]+(?:/[\w.-]+)*\.(?:sh|py|js|mjs|ts|rb|pl|ps1)|\./[\w./-]+)")


def workflow_checks(root: Path, cfg: Config) -> list[Check]:
    """The repo's own workflows that hold secrets: Actions caches they restore (H5), local scripts they run that an
    agent PR could change (M6), and pull_request_target in CI (M7). Night shift's own workflows are skipped."""
    out: list[Check] = []
    folder = root / ".github/workflows"
    if not folder.is_dir():
        return out
    scripts: dict[str, set[str]] = {}
    for wf in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]):
        if wf.name.startswith("night-shift"):
            continue
        try:
            text = wf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        if wf.name == cfg.ci_workflow and re.search(r"^\s*pull_request_target\s*:", code, re.M):
            out.append(Check(WARN, f"{cfg.ci_workflow} runs on pull_request_target: agent-written code would run with "
                                   "this repo's write token and secrets", "use `pull_request` for CI"))
        if not re.search(r"\bsecrets\.(?!GITHUB_TOKEN\b)\w+", code):
            continue
        if re.search(r"uses:\s*actions/cache[@/]|^\s*cache:\s*\S", code, re.M):
            out.append(Check(WARN, f".github/workflows/{wf.name} holds secrets and restores an Actions cache: an agent "
                                   "job can write cache entries in the default branch's scope (H5)",
                             "drop the cache from workflows that hold secrets, or key it so agent jobs cannot write it"))
        for line in code.splitlines():
            for m in SCRIPT_RE.finditer(line):
                path = m.group(1).removeprefix("./")
                if (root / path).is_file() and not path_is_protected(path, cfg.protected_paths):
                    scripts.setdefault(path, set()).add(wf.name)
    for path, names in sorted(scripts.items()):
        out.append(Check(WARN, f"{path} runs in {', '.join(sorted(names))}, which holds secrets, and agents may change it",
                         f"add {path} to protected_paths (or risk.approve_paths) in {CONFIG_PATH}"))
    return out


def rules_checks(root: Path, cfg: Config) -> list[Check]:
    """The review rules file parses, and the security gate is wired into the review caller."""
    out = []
    try:
        rules = review_rules.load(root)
    except review_rules.RulesError as err:
        return [Check(FAIL, f"{err}: every review would error until it is fixed", f"fix {review_rules.RULES_PATH}")]
    if rules:
        sec = sum(r.security for r in rules)
        out.append(Check(OK, f"{review_rules.RULES_PATH}: {len(rules)} rule(s), {sec} security rule(s)"))
    review = root / ".github/workflows/night-shift-review.yml"
    text = review.read_text(encoding="utf-8") if review.is_file() else ""
    writes = {m.group(1) for m in re.finditer(r"([a-z-]+):\s*write\b", text)} - {"pull-requests", "issues"}
    if text and (not re.search(r"^permissions:", text, re.M) or re.search(r"write-all|read-all", text) or writes):
        out.append(Check(FAIL, "night-shift-review.yml must set its own permissions block (contents: read, "
                               "pull-requests: write, issues: write, checks: read, actions: read): the reusable "
                               "review workflow takes its token permissions from it", "run `gh nightshift init --regenerate`"))
    if cfg.reuse_on_update and text and "checks: read" not in text:
        out.append(Check(WARN, "night-shift-review.yml does not grant checks: read, so every update-branch reruns the "
                               "full review", "run `gh nightshift init --regenerate`"))
    if cfg.security_enabled:
        if review.is_file() and "security: true" not in review.read_text(encoding="utf-8"):
            out.append(Check(FAIL, "security.enabled is true but night-shift-review.yml does not run the security gate",
                             "run `gh nightshift init --regenerate`"))
        if not any(r.security for r in rules) and not cfg.security_trigger_paths and not cfg.security_always:
            out.append(Check(WARN, "security.enabled is true but nothing triggers it: no security rule, security path "
                                   "or security.always", f"add `security: true` rules to {review_rules.RULES_PATH}"))
    if cfg.redteam_enabled:
        caller = root / ".github/workflows/night-shift-redteam.yml"
        if not caller.is_file():
            out.append(Check(FAIL, "redteam.enabled is true but night-shift-redteam.yml is missing",
                             "run `gh nightshift init --regenerate`"))
        if not cfg.bot_login and not cfg.trusted_logins:
            out.append(Check(FAIL, "redteam.enabled needs bot_login (or trusted_logins): the scan's context job runs on "
                                   "GITHUB_TOKEN, which cannot read GET /user, so it cannot tell which markers to trust",
                             f"set bot_login in {CONFIG_PATH}"))
        who = "the App" if cfg.auth_mode == "app" else "AGENT_PAT (whose account must be a repo admin or security manager)"
        out.append(Check(WARN, f"on a public repo the red-team scan files draft advisories: {who} needs Repository "
                               "security advisories: Read and write. On a private repo it files `security` issues instead",
                         "App: approve the new permission on the App's installation; PAT: edit it in Developer settings"))
    return out


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
        bound = {c.get("context") for c in rsc.get("checks") or [] if c.get("app_id") == ACTIONS_APP_ID}
        loose = [c for c in cfg.required_checks if c in contexts and c not in bound]
        if loose:
            out.append(Check(WARN, f"required checks {', '.join(loose)} can be passed by any app or commit status",
                             "run `gh nightshift setup` (binds them to GitHub Actions)"))
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
    out += actions_settings_checks(gh)
    if not gh.night_log(cfg.label("night_log")):
        out.append(Check(FAIL, "no open night-log issue", "run `gh nightshift setup`"))
    out += tool_checks(gh, cfg)
    return out


def actions_settings_checks(gh: GH) -> list[Check]:
    """M7: the `test` check runs agent-written code in CI. With a read/write default GITHUB_TOKEN, or with Actions
    allowed to approve PRs, that code can post statuses, label or approve its own PR."""
    try:
        perms = gh.get_or_none(gh.r("actions/permissions/workflow"))
    except GHError as err:
        return [Check(WARN, f"cannot read the Actions token settings ({err.status or err})", "run doctor as a repo admin")]
    if not perms:
        return []
    out = []
    if perms.get("default_workflow_permissions") == "write":
        out.append(Check(WARN, "the default GITHUB_TOKEN is read/write: agent-written code in CI can post statuses, "
                               "labels and comments", f"Settings > Actions > Workflow permissions: read, or: gh api -X PUT "
                               f"repos/{gh.repo}/actions/permissions/workflow -f default_workflow_permissions=read"))
    if perms.get("can_approve_pull_request_reviews"):
        out.append(Check(WARN, "GitHub Actions may approve pull requests: agent-written code in CI could approve its "
                               "own PR", "Settings > Actions > untick 'Allow GitHub Actions to create and approve "
                               "pull requests'"))
    if not out:
        out.append(Check(OK, "the default GITHUB_TOKEN is read-only and cannot approve PRs"))
    return out


def tool_checks(gh: GH, cfg: Config) -> list[Check]:
    """The caller's `uses:` and the unauthenticated tool checkouts need TOOL_REPO public and tool_ref to exist."""
    from .scaffold import TOOL_REPO

    try:
        tool = gh.get_or_none(f"repos/{TOOL_REPO}")
        ref = gh.get_or_none(f"repos/{TOOL_REPO}/commits/{cfg.tool_ref}") if tool else None
    except GHError as err:
        return [Check(WARN, f"could not check {TOOL_REPO}@{cfg.tool_ref}: {err}", "re-run `gh nightshift doctor` later")]
    if tool is None:
        return [Check(FAIL, f"cannot read {TOOL_REPO}", "the workflows need it reachable; check the name")]
    if tool.get("private"):
        return [Check(FAIL, f"{TOOL_REPO} is private: other repos cannot use its workflows or check it out",
                      "make it public, or fork it publicly and point the caller workflows at the fork")]
    if ref is None:
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


def run_doctor(gh: GH, root: Path, pat_token: str | list[str] | None = None,
               run: Callable = subprocess.run) -> list[Check]:
    cfg, checks = local_checks(root)
    checks += own_token_checks(run)
    tokens = [pat_token] if isinstance(pat_token, str) else list(pat_token or [])
    for token in tokens:
        if not token:
            checks.append(Check(FAIL, "a --pat-env variable is empty or unset", "export the PAT in that variable"))
            continue
        login, scopes = token_scopes(token, run)
        checks += pat_checks(token, run, (login, scopes))
        if cfg is not None and login and cfg.trusted_logins and login not in {cfg.bot_login, *cfg.trusted_logins}:
            checks.append(Check(WARN, f"PAT login {login} is not in trusted_logins: other lanes ignore its markers",
                                f"add {login} to trusted_logins in {CONFIG_PATH}"))
    if not tokens:
        checks.append(Check(WARN, "PAT permissions not checked", "re-run with --pat-env VAR (VAR holds the PAT); "
                                                                "the lane workflow also checks it on every run"))
    if cfg is not None:
        try:
            checks += remote_checks(gh, cfg)
        except GHError as err:
            checks.append(Check(FAIL, f"cannot read {gh.repo}: {err}", "check `gh auth status` and the repo name"))
    return checks
