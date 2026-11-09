"""`gh nightshift <command>`. Every command that changes GitHub supports --dry-run, which prints the
exact `gh api` / `gh secret set` calls instead of making them."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from collections.abc import Callable

from . import __version__, digest, planner, prompts, repo_setup, roadmap, scaffold
from . import protocol as ns
from .config import CONFIG_PATH, Config, ConfigError, load, loads
from .gh import GH, GHError

Ask = Callable[[str], str]


# --- plumbing -------------------------------------------------------------------------

def detect_repo(root: Path) -> str:
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    if os.environ.get("GH_REPO"):
        return os.environ["GH_REPO"]
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True,
                             text=True).stdout.strip()
    except OSError:
        url = ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise SystemExit("cannot tell which GitHub repo this is; pass --repo OWNER/NAME")
    return m.group(1)


def write_outputs(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            value = str(value)
            if "\n" in value:
                delim = f"NS_{uuid.uuid4().hex}"
                fh.write(f"{key}<<{delim}\n{value}\n{delim}\n")
            else:
                fh.write(f"{key}={value}\n")


class Ctx:
    def __init__(self, args, transport=None, out=None):
        self.args = args
        self.root = Path(args.root)
        self.out = out or sys.stdout
        self._transport = transport
        self._gh: GH | None = None
        self._cfg: Config | None = None

    @property
    def repo(self) -> str:
        return self.args.repo or detect_repo(self.root)

    @property
    def gh(self) -> GH:
        if self._gh is None:
            self._gh = GH(self.repo, transport=self._transport, dry_run=getattr(self.args, "dry_run", False),
                          out=self.out)
            try:
                self._gh.trusted_extra = {self.cfg.bot_login, *self.cfg.trusted_logins} - {""}
            except ConfigError:
                pass  # init, before a config exists
            # App installation tokens cannot call GET /user; the mint step exports the App's bot login.
            app_login = os.environ.get("NIGHT_SHIFT_LOGIN", "")
            app_token = os.environ.get("NIGHT_SHIFT_APP_TOKEN", "")
            if app_login and app_token and os.environ.get("GH_TOKEN") == app_token:
                self._gh.login = app_login
        return self._gh

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load(self.root)
        return self._cfg

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.out)


# --- init / join / setup --------------------------------------------------------------

def _default_ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _ask(ask: Ask, prompt: str, default: str) -> str:
    answer = ask(f"{prompt} [{default}]: ").strip()
    return answer or default


def _pairs(text: str | None) -> dict[str, str]:
    """`api=python,web=node` -> {"api": "python", "web": "node"}; a bare value applies to every lane ("*")."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        out[key.strip() if sep else "*"] = (value if sep else key).strip()
    return out


def build_config(root: Path, ask: Ask, lanes_arg: str | None, assume_yes: bool, out=None,
                 tool_ref: str = "main", opts: argparse.Namespace | None = None) -> dict:
    """Interactive (or fully non-interactive with --yes and flags) config: lanes from the detected stack or a
    preset, one Claude token secret per owner."""
    o = opts or argparse.Namespace()
    presets, paths, owners = _pairs(getattr(o, "preset", None)), _pairs(getattr(o, "paths", None)), \
        _pairs(getattr(o, "owners", None))
    for name in presets.values():
        if name not in scaffold.PRESETS:
            raise ConfigError(f"unknown preset {name!r}; choose from {', '.join(scaffold.PRESETS)}")
    stacks = scaffold.detect_stack(root)
    folders = [s for s in stacks if s["path"] != "./"]
    suggested = lanes_arg or ",".join(re.sub(r"[^a-z0-9_-]", "-", s["path"].rstrip("/").lower()) for s in folders) \
        or "app"
    say = (lambda *_: None) if assume_yes else (lambda text: print(text, file=out or sys.stdout))
    if stacks:
        say("Detected: " + "; ".join(f"{s['path']} ({s['stack']})" for s in stacks))
    q = (lambda p, d: d) if assume_yes else (lambda p, d: _ask(ask, p, d))
    from . import codeowners

    co_text = next(((root / loc).read_text(encoding="utf-8") for loc in codeowners.LOCATIONS if (root / loc).is_file()),
                   "")
    co_rules = codeowners.parse(co_text)
    names = [n.strip() for n in q("Lanes (comma-separated)", suggested).split(",") if n.strip()]
    lane_pats = bool(getattr(o, "lane_pats", False))
    if lane_pats and getattr(o, "app_id", None):
        raise ConfigError("--lane-pats and --app-id do not mix: in App mode the App replaces every PAT")
    owner_logins: list[str] = []
    lanes = []
    for name in names:
        lane_paths = paths.get(name) or paths.get("*") or f"{name}/"
        preset = presets.get(name) or presets.get("*")
        if preset:
            folder = lane_paths.split(";")[0].rstrip("/")
            guess = scaffold.preset_commands(preset, folder if folder not in ("", ".") else "")
        else:
            guess = next((s for s in stacks if s["path"].rstrip("/") == name), stacks[0] if stacks else None)
        # CODEOWNERS-aware: the lane's folder owner (a user, not a team) is the suggested lane owner.
        co_users = codeowners.user_owners(codeowners.owners_for(co_rules, lane_paths.split(";")[0].rstrip("/") + "/x"))
        suggested_owner = sorted(co_users)[0] if co_users else name
        who = "owner's GitHub login (their PAT posts this lane's PRs)" if lane_pats else \
            "owner's name (for their Claude token secret)"
        login = owners.get(name) or q(f"Lane {name}: {who}", suggested_owner)
        owner = login.upper()
        lane = {"name": name,
                "paths": [p.strip() for p in q(f"Lane {name}: folders it owns", lane_paths).replace(";", ",").split(",")
                          if p.strip()],
                "claude_token_secret": f"CLAUDE_TOKEN_{re.sub(r'[^A-Z0-9]', '_', owner)}",
                "model": "sonnet"}
        setup = q(f"Lane {name}: setup command", guess["setup"] if guess else "")
        test = q(f"Lane {name}: test command", guess["test"] if guess else "")
        if setup:
            lane["setup"] = {"commands": setup + "\n"}
        if test:
            lane["test_command"] = test
        if lane_pats:
            # The lane's PRs, comments and markers are posted as its owner, with the owner's own PAT.
            lane["agent_pat_secret"] = f"AGENT_PAT_{re.sub(r'[^A-Z0-9]', '_', owner)}"
            if login not in owner_logins:
                owner_logins.append(login)
        budget = getattr(o, "budget", None)
        if budget:
            lane["budget_usd_per_night"] = float(budget)
        lanes.append(lane)
    ci = getattr(o, "ci_workflow", None) or q("CI workflow file (its runs on main gate the lanes)", "ci.yml")
    mode = getattr(o, "merge_mode", None) or q("Merge mode: strict (PRs must be up to date) or queue (GitHub merge queue)",
                                               "strict")
    auth = {"mode": "app", "app_id": str(o.app_id)} if getattr(o, "app_id", None) else None
    data = {"version": 1, "main_branch": "main", "ci_workflow": ci,
            "required_checks": ["test", "review / verdict"], "fix_lane": lanes[0]["name"] if lanes else "",
            "merge": {"mode": mode}, "tool_ref": tool_ref, "lanes": lanes,
            "limits": {"max_attempts": 3, "infra_cap": 6, "max_needs_per_issue": 2, "max_notes_per_issue": 2},
            "planner": {"docs": ["docs/PRD.md"], "max_tickets_per_run": 8, "refill_below": 0}}
    if auth:
        data["auth"] = auth
    if owner_logins:
        # Every lane posts as its owner, so each owner's markers must be trusted by the other lanes. The planner,
        # digest and commands still post with the shared AGENT_PAT, so its login must be trusted too.
        data["trusted_logins"] = owner_logins
        bot = getattr(o, "bot_login", None) or q("AGENT_PAT owner's GitHub login (the planner posts as it)", "")
        if bot:
            data["bot_login"] = bot
    if co_rules:
        data["risk"] = {"codeowners": True}  # agent PRs touching owned files wait for an owner's approval
    return data


def write_files(ctx: Ctx, files: dict[str, str], overwrite: bool) -> list[str]:
    out = []
    for rel, text in files.items():
        path = ctx.root / rel
        exists = path.exists()
        if exists and not overwrite and rel != CONFIG_PATH:
            out.append(f"kept {rel} (exists; use --force to overwrite)")
            continue
        if ctx.args.dry_run:
            out.append(f"[dry-run] would {'overwrite' if exists else 'write'} {rel} ({len(text.splitlines())} lines)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out.append(f"wrote {rel}")
    return out


def cmd_init(ctx: Ctx, ask: Ask = _default_ask, secret_ask=None, secret_setter=None) -> int:
    a = ctx.args
    cfg_path = ctx.root / CONFIG_PATH
    if a.regenerate:
        cfg = ctx.cfg
        text = cfg_path.read_text(encoding="utf-8")
        files = {k: v for k, v in scaffold.files(cfg, text, ctx.root).items() if k.startswith(".github/workflows/")}
        ctx.say(*write_files(ctx, files, overwrite=True))
        return 0
    if cfg_path.exists() and not a.force:
        cfg = ctx.cfg
        text = cfg_path.read_text(encoding="utf-8")
        ctx.say(f"{CONFIG_PATH} exists; using it (pass --force to start over)")
        if a.tool_ref and a.tool_ref != cfg.tool_ref:
            ctx.say(f"note: --tool-ref ignored; the config pins {cfg.tool_ref}. Edit tool_ref in {CONFIG_PATH}, "
                    "then run `gh nightshift init --regenerate`.")
    else:
        data = build_config(ctx.root, ask, a.lanes, a.yes, ctx.out, a.tool_ref or "main", a)
        if getattr(a, "template", None):
            data = scaffold.apply_template(data, a.template)
        text = scaffold.config_yaml(data)
        cfg = loads(text)  # validates what we are about to write
    ctx.say(*write_files(ctx, scaffold.files(cfg, text, ctx.root), overwrite=a.force))
    if getattr(a, "template", None):
        ctx.say(*write_files(ctx, scaffold.template_files(a.template), overwrite=False))
    if a.files_only:
        return 0
    # Branch protection waits for `setup`: protecting main now would refuse the push of these very files.
    ctx.say(*repo_setup.setup(ctx.gh, cfg, protection=False))
    if not a.skip_secrets:
        kwargs = {}
        if secret_ask:
            kwargs["ask"] = secret_ask
        elif a.secrets_from_env:  # non-interactive: AGENT_PAT=... CLAUDE_TOKEN_API=... gh nightshift init --yes
            kwargs["ask"] = lambda prompt: os.environ.get(prompt.split(" ", 1)[0], "")
        if secret_setter:
            kwargs["setter"] = secret_setter
        if cfg.auth_mode == "app":
            kwargs["app_key_file"] = getattr(a, "app_key_file", None) or ""
        ctx.say(*repo_setup.prompt_missing_secrets(ctx.gh, cfg, **kwargs))
    ctx.say("", "Next: commit and push the new files to main (your gh token needs the `workflow` scope), then run "
                "`gh nightshift setup` (branch protection), `gh nightshift doctor`, label a few issues night-safe, "
                "and `gh nightshift start`.")
    return 0


def cmd_setup(ctx: Ctx) -> int:
    ctx.say(*repo_setup.setup(ctx.gh, ctx.cfg))
    return 0


def cmd_join(ctx: Ctx, secret_ask=None, secret_setter=None, run=subprocess.run) -> int:
    cfg = ctx.cfg
    lane = cfg.lane(ctx.args.lane)
    kwargs = {}
    if secret_ask:
        kwargs["ask"] = secret_ask
    if secret_setter:
        kwargs["setter"] = secret_setter
    ctx.say(f"Lane {lane.name}: run `claude setup-token` and paste the token.")
    ctx.say(repo_setup.prompt_secret(ctx.gh, lane.token_secret, f"Claude token for lane {lane.name}", **kwargs))
    if not lane.pat_secret or ctx.args.no_pat:
        if ctx.args.pat and not lane.pat_secret:
            ctx.say(f"lane {lane.name} has no agent_pat_secret in {CONFIG_PATH}; it uses the shared AGENT_PAT. Add "
                    f"`agent_pat_secret: AGENT_PAT_{lane.name.upper().replace('-', '_')}` to the lane to use your own.")
        return 0
    # Per-contributor PAT: the lane's PRs and markers are posted as this person. Check it before storing it.
    seen: dict[str, str] = {}

    def check(token: str) -> str | None:
        from .doctor import token_scopes

        login, scopes = token_scopes(token, run)
        if login is None:
            return "the token does not authenticate"
        if "workflow" in [s.strip() for s in scopes.split(",")]:
            return "the token has the `workflow` scope; agents could change CI. Use a fine-grained PAT without Workflows"
        seen["login"] = login
        return None

    ctx.say(f"Lane {lane.name}: create a fine-grained PAT for {ctx.repo} only (Contents, Issues, Pull requests "
            "read/write; Actions, Commit statuses read; no Workflows) and paste it.")
    ctx.say(repo_setup.prompt_secret(ctx.gh, lane.pat_secret, repo_setup.secret_hint(cfg, lane.pat_secret),
                                     validate=None if ctx.gh.dry_run else check, **kwargs))
    login = seen.get("login")
    if login and login not in {cfg.bot_login, *cfg.trusted_logins}:
        ctx.say(f"Add {login} to trusted_logins in {CONFIG_PATH} (and commit it) so the other lanes trust this "
                f"lane's markers:", f"  trusted_logins: [{', '.join([*cfg.trusted_logins, login])}]")
    return 0


# --- doctor / status / stats ----------------------------------------------------------

def cmd_doctor(ctx: Ctx, run=subprocess.run) -> int:
    from .doctor import FAIL, run_doctor

    tokens = [os.environ.get(name, "") for name in ctx.args.pat_env]
    checks = run_doctor(ctx.gh, ctx.root, tokens or None, run)
    for c in checks:
        ctx.say(c.line())
    bad = sum(1 for c in checks if c.level == FAIL)
    ctx.say("", f"{bad} problem(s)" if bad else "No problems found.")
    return 1 if bad else 0


def cmd_status(ctx: Ctx) -> int:
    from .repo_setup import get_variable
    from .status import status

    enabled = get_variable(ctx.gh, "AGENTS_ENABLED") == "true"
    ctx.say(status(ctx.gh, ctx.cfg, enabled))
    return 0


def cmd_stats(ctx: Ctx) -> int:
    from .status import org_stats, stats

    if ctx.args.org:
        def make_gh(repo: str) -> GH:
            return GH(repo, transport=ctx._transport, dry_run=False, out=ctx.out)

        ctx.say(org_stats(GH(ctx.args.repo or "x/x", transport=ctx._transport, out=ctx.out), ctx.args.org, make_gh)
                .rstrip())
        return 0
    ctx.say(stats(ctx.gh, ctx.cfg, ctx.args.audit))
    return 0


def cmd_switch(ctx: Ctx) -> int:
    name = ctx.args.command
    if name == "start":
        lines = repo_setup.start(ctx.gh)
    elif name == "pause":
        lines = repo_setup.pause(ctx.gh, cancel=not ctx.args.no_cancel)
    elif name == "freeze":
        lines = repo_setup.freeze(ctx.gh, ctx.cfg, cancel=not ctx.args.no_cancel)
    else:
        lines = repo_setup.resume(ctx.gh, ctx.cfg)
    ctx.say(*lines)
    return 0


def cmd_lint(ctx: Ctx) -> int:
    cfg = ctx.cfg
    issues = ctx.gh.issues([cfg.ready_label])
    open_issues = ctx.gh.issues()
    from .rules import parse_deps

    depended = {d for i in open_issues for d in parse_deps(i.get("body"))}
    bad = 0
    for issue in sorted(issues, key=lambda i: i["number"]):
        problems = planner.lint_issue(issue, cfg, issue["number"] in depended)
        if problems:
            bad += 1
            ctx.say(f"#{issue['number']} {issue.get('title', '')}", *[f"  - {p}" for p in problems])
    ctx.say(f"{len(issues)} ready issue(s), {bad} with problems")
    return 1 if bad else 0


# --- planner --------------------------------------------------------------------------

def run_claude(prompt: str, schema: str, model: str, add_dir: str, run=subprocess.run) -> dict:
    if not shutil.which("claude"):
        raise SystemExit("the `claude` CLI is not installed here; run the night-shift-planner workflow instead, "
                         "or pass --plan-file")
    r = run(["claude", "-p", prompt, "--output-format", "json", "--json-schema", schema, "--model", model,
             "--max-turns", "60", "--add-dir", add_dir, "--allowedTools", "Read,Grep,Glob",
             "--disallowedTools", "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch"],
            capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as err:
        raise SystemExit(f"claude did not return JSON: {r.stderr[-500:]}") from err


def plan_context(ctx: Ctx, mode: str, docs: list[str] | None, lane: str, out_dir: Path,
                 request: str = "") -> dict[str, str]:
    cfg = ctx.cfg
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "context.md").write_text(planner.context(ctx.gh, cfg, mode, docs, lane, request), encoding="utf-8")
    (out_dir / "prompt.md").write_text(prompts.render("planner", {
        "CONTEXT_FILE": str(out_dir / "context.md"), "MAX": str(cfg.planner_max_tickets)}, ctx.root), encoding="utf-8")
    (out_dir / "schema.json").write_text(prompts.schema_json("planner"), encoding="utf-8")
    return {"context": str(out_dir / "context.md")}


def docs_sha(gh: GH, docs: list[str]) -> str:
    shas = []
    for d in docs:
        commits = gh.get_or_none(gh.r("commits"), {"path": d, "per_page": 1}) or []
        shas.append(commits[0]["sha"][:12] if commits else "-")
    return ",".join(shas)


def refill_lane(ctx: Ctx) -> tuple[str, str]:
    """(lane, why) for an auto-refill run, or ('', reason to skip)."""
    from .planner import lanes_to_refill, ready_counts

    cfg, gh = ctx.cfg, ctx.gh
    if os.environ.get("AGENTS_ENABLED", "true").lower() != "true":
        return "", "AGENTS_ENABLED is not true"
    lanes = lanes_to_refill(gh, cfg)
    if not lanes:
        return "", f"every lane has at least {cfg.planner_refill_below} ready issues"
    counts = ready_counts(gh, cfg)
    log = gh.night_log(cfg.label("night_log"))
    sha = docs_sha(gh, cfg.planner_docs)
    done = set()
    if log:
        for m in ns.trusted(gh.comments(log), gh.trusted, "plan"):
            if m.get("complete") and m.get("docs_sha") == sha:
                done.add(m.get("lane") or "*")
    lanes = [ln for ln in lanes if ln not in done and "*" not in done]
    if not lanes:
        return "", "coverage is complete for the current docs (stopping rule); edit the docs to plan more"
    lane = min(lanes, key=lambda ln: counts.get(ln, 0))
    return lane, f"lane {lane} has {counts.get(lane, 0)} ready issues (< {cfg.planner_refill_below})"


def cmd_plan(ctx: Ctx, run=subprocess.run) -> int:
    a = ctx.args
    cfg = ctx.cfg
    if a.plan_action == "context":
        mode, lane, request, issue = a.mode, a.lane, "", ""
        docs = [d.strip() for d in (a.docs or "").split(",") if d.strip()] or None
        if mode == "refill":
            lane, why = refill_lane(ctx)
            if not lane:
                ctx.say(f"skip: {why}")
                write_outputs(a.github_output, {"run": "false"})
                return 0
            mode = "gap"
            ctx.say(why)
        elif mode == "review":
            author, body = os.environ.get("COMMENT_AUTHOR", ""), os.environ.get("COMMENT_BODY", "")
            issue = os.environ.get("ISSUE_NUMBER", "")
            if not body.strip().startswith("/night-shift plan") or not planner.can_request_plan(ctx.gh, author):
                ctx.say(f"skip: `/night-shift plan` needs a comment from a user with write access (got {author!r})")
                write_outputs(a.github_output, {"run": "false"})
                return 0
            data = ctx.gh.issue(int(issue)) or {}
            request = (f"Issue #{issue}: {data.get('title', '')}\n\n{data.get('body') or ''}\n\n"
                       f"Request from {author}: {body.strip()[len('/night-shift plan'):].strip()}")
        elif mode not in planner.MODES:
            raise SystemExit(f"unknown mode {mode!r}")
        plan_context(ctx, mode, docs, lane, Path(a.out_dir), request)
        write_outputs(a.github_output, {"run": "true", "mode": mode, "lane": lane, "issue": issue})
        ctx.say(f"planning context written ({mode}{', lane ' + lane if lane else ''})")
        return 0
    if a.plan_action == "publish":
        return publish_plan_file(ctx, a.file, a.lane, a.reply_to)
    if a.show:
        ctx.say(roadmap.render(cfg, roadmap.tickets(ctx.gh, cfg)).split("\n", 1)[1].rstrip())
        return 0
    # Local run: gh nightshift plan --from PATH | --gap | --lane X
    mode = "gap" if a.gap or (a.lane and not a.from_path) else "prd"
    docs = [a.from_path] if a.from_path else None
    if a.plan_file:
        plan_path = a.plan_file
    else:
        work = Path(a.out_dir or ".night-shift-plan")
        plan_context(ctx, mode, docs, a.lane or "", work)
        data = run_claude((work / "prompt.md").read_text(), (work / "schema.json").read_text(), cfg.planner_model,
                          str(work), run)
        plan_path = str(work / "plan.json")
        Path(plan_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        ctx.say(f"draft saved to {plan_path}")
    return publish_plan_file(ctx, plan_path, a.lane or "", "")


def publish_plan_file(ctx: Ctx, path: str, lane: str, reply_to: str) -> int:
    cfg, gh = ctx.cfg, ctx.gh
    if not Path(path).is_file():
        raise ConfigError(f"plan file not found: {path}")
    raw = Path(path).read_text(encoding="utf-8")
    result = planner.publish_plan(gh, cfg, raw, lane)
    lines = [ns.defang(line) for line in result.lines()]  # titles, notes, keys come from the model
    ctx.say(*lines)
    summary = "**Night shift planner**\n\n" + "\n".join(f"- {line}" for line in lines)
    if reply_to:
        gh.comment(int(reply_to), summary)
    log = gh.night_log(cfg.label("night_log"))
    if log:
        marker = ns.marker("plan", lane=lane or "*", complete=result.coverage_complete,
                           docs_sha=docs_sha(gh, cfg.planner_docs), created=len(result.created) + len(result.human))
        gh.comment(log, marker + "\n" + summary)
    ctx.say(roadmap.safe_update(gh, cfg, extra=result.issues))
    return 0


def cmd_command(ctx: Ctx) -> int:
    """(workflow) Handle a `/night-shift take|retry` comment; author, body and number come from the env."""
    from . import commands

    a = ctx.args
    author = a.author or os.environ.get("COMMENT_AUTHOR", "")
    body = a.body or os.environ.get("COMMENT_BODY", "")
    number = int(a.issue or os.environ.get("ISSUE_NUMBER", "0") or 0)
    ctx.say(commands.handle(ctx.gh, ctx.cfg, author, body, number))
    return 0


def cmd_take_retry(ctx: Ctx) -> int:
    """Local `take N` / `retry N`: the same as commenting the command, as your own gh login."""
    from . import commands

    a = ctx.args
    ctx.say(commands.handle(ctx.gh, ctx.cfg, ctx.gh.login, f"/night-shift {a.command} #{a.number}", a.number))
    return 0


def cmd_app_token(ctx: Ctx) -> int:
    """(workflow) Mint a down-scoped installation token for this repo and expose it to later steps of this job."""
    from . import app

    try:
        result = app.mint(ctx.repo, os.environ.get("APP_ID", ""), os.environ.get("APP_PRIVATE_KEY", ""))
    except app.AppError as err:
        ctx.say(f"::error::GitHub App token: {err}")
        return 1
    app.write_github_env(ctx.args.github_env, result)
    ctx.say(f"App token minted for {ctx.repo} as {result['login'] or 'the App'} "
            f"(permissions: {', '.join(f'{k}={v}' for k, v in sorted(result['permissions'].items()))})")
    return 0


def cmd_app(ctx: Ctx, request=None) -> int:
    """`app manifest`: the App to create. `app check`: mint a token locally and show what it can do (never the token)."""
    from . import app

    a = ctx.args
    if a.app_action == "manifest":
        ctx.say(json.dumps(app.manifest(a.name), indent=2))
        ctx.say("", "Create it at https://github.com/settings/apps/new (or your org's Settings > Developer settings > "
                    "GitHub Apps) with exactly these permissions and no webhook. Install it on this repository only, "
                    "generate a private key, then run `gh nightshift init --app-id <id> --app-key-file <key.pem>` "
                    "(or set auth: in the config and `gh secret set NIGHT_SHIFT_APP_KEY < key.pem`).")
        return 0
    key = Path(a.key_file).read_text(encoding="utf-8") if a.key_file else os.environ.get("APP_PRIVATE_KEY", "")
    app_id = a.app_id or ctx.cfg.app_id
    if ctx.gh.dry_run:
        ctx.say(f"[dry-run] would mint an installation token for {ctx.repo} with App {app_id or '?'} "
                "(GET /app, GET /repos/{owner}/{repo}/installation, POST /app/installations/{id}/access_tokens)")
        return 0
    try:
        result = app.mint(ctx.repo, app_id, key, **({"request": request} if request else {}))
    except app.AppError as err:
        ctx.say(f"FAIL  {err}")
        return 1
    ctx.say(f"OK    the App mints a token for {ctx.repo} as {result['login']}",
            f"OK    permissions: {', '.join(f'{k}={v}' for k, v in sorted(result['permissions'].items()))} (no workflows)")
    if result["login"] and result["login"] not in {ctx.cfg.bot_login, *ctx.cfg.trusted_logins}:
        ctx.say(f"WARN  set `bot_login: {result['login']}` in {CONFIG_PATH} so stats and audit trust its markers")
    return 0


def cmd_config(ctx: Ctx) -> int:
    """`config pull`: copy the org-wide base config from the `extends:` repo into .night-shift/base.yml."""
    import base64

    from .config import BASE_PATH

    cfg = ctx.cfg
    if not cfg.extends:
        raise ConfigError(f"set `extends: owner/repo` in {CONFIG_PATH} first")
    data = ctx.gh.get_or_none(f"repos/{cfg.extends}/contents/{CONFIG_PATH}") or {}
    if not data.get("content"):
        raise ConfigError(f"{cfg.extends} has no {CONFIG_PATH} (or this token cannot read it)")
    text = base64.b64decode(data["content"]).decode("utf-8")
    header = (f"# Copied from {cfg.extends}:{CONFIG_PATH} by `gh nightshift config pull`; do not edit here.\n"
              f"# Only shared settings are used (models, limits, protected_paths, required_checks, timezone, risk,\n"
              f"# digest, planner), and only to add checks; lanes, labels, merge mode, tool_ref, logins, auth and\n"
              f"# secret names stay in config.yml.\n")
    loads((ctx.root / CONFIG_PATH).read_text(encoding="utf-8"), text)  # the merged result must be valid
    target = ctx.root / BASE_PATH
    if ctx.gh.dry_run:
        ctx.say(f"[dry-run] would write {BASE_PATH} from {cfg.extends}")
        return 0
    target.write_text(header + text, encoding="utf-8")
    ctx.say(f"wrote {BASE_PATH} from {cfg.extends}; commit it so the workflows use it")
    return 0


def cmd_audit_log(ctx: Ctx) -> int:
    from . import auditlog

    a = ctx.args
    text = auditlog.render(auditlog.rows(ctx.gh, ctx.cfg, a.since), a.format)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        ctx.say(f"wrote {a.out} ({max(0, text.count(chr(10)) - (a.format == 'csv'))} PRs)")
    else:
        ctx.out.write(text)
    return 0


def cmd_roadmap(ctx: Ctx) -> int:
    ctx.say(roadmap.update(ctx.gh, ctx.cfg, create=True))
    return 0


def cmd_digest(ctx: Ctx) -> int:
    a = ctx.args
    text = digest.build(ctx.gh, ctx.cfg, hours=168 if a.weekly else a.hours)
    if not a.post:
        ctx.say(text.rstrip())
        return 0
    ctx.say(digest.post(ctx.gh, ctx.cfg, text))
    if a.webhook_env:
        url = os.environ.get(a.webhook_env, "").strip()
        ctx.say(digest.send_webhook(url, text, dry_run=ctx.gh.dry_run) if url
                else f"no webhook: ${a.webhook_env} is empty")
    return 0


# --- workflow-internal commands -------------------------------------------------------

def cmd_lane_config(ctx: Ctx) -> int:
    lane = ctx.cfg.lane(ctx.args.lane)
    values = {"postgres_image": lane.postgres_image, "setup_action": lane.setup_action,
              "max_turns": str(ctx.cfg.max_turns), "test_command": lane.test_command}
    write_outputs(ctx.args.github_output, values)
    ctx.say(json.dumps(values))
    return 0


def cmd_check_pat(ctx: Ctx, run=subprocess.run) -> int:
    from .doctor import FAIL, OK, access_checks, pat_checks

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    checks = pat_checks(token, run)
    if checks[0].level == OK:
        checks += access_checks(ctx.gh)
    for c in checks:
        ctx.say(c.line())
    if any(c.level == FAIL for c in checks):
        ctx.say("::error::the PAT must authenticate, reach this repo with the permissions above, and must not have "
                "the Workflows permission")
        return 1
    return 0


def cmd_pick(ctx: Ctx) -> int:
    from .picker import Picker, Runtime

    result = Picker(ctx.gh, ctx.cfg, Runtime.from_env(ctx.args.lane)).decide()
    ctx.say(json.dumps(result))
    write_outputs(ctx.args.github_output, {k: v.replace("\n", " ") for k, v in result.items()})
    return 0


def cmd_task(ctx: Ctx) -> int:
    from .task import prepare

    a = ctx.args
    out = Path(a.out_dir)
    values = prepare(ctx.gh, ctx.cfg, a.lane, a.mode, a.issue, a.fail_sha, a.run_url, str(out / "task.md"))
    values_prompt = prompts.worker_values(ctx.cfg, a.lane, "__TASK_FILE__")
    (out / "prompt.md").write_text(prompts.render("worker", values_prompt, ctx.root), encoding="utf-8")
    (out / "schema.json").write_text(prompts.schema_json("worker"), encoding="utf-8")
    (out / "setup.sh").write_text(ctx.cfg.lane(a.lane).setup_commands, encoding="utf-8")
    values["prompt_version"] = prompts.version("worker", ctx.root)
    write_outputs(a.github_output, values)
    ctx.say(json.dumps(values))
    return 0


def cmd_publish(ctx: Ctx) -> int:
    from .publish import Job, publish

    a = ctx.args
    job = Job(lane=a.lane, mode=a.mode, issue=a.issue, branch=a.branch, title=a.title, model=a.model,
              attempt_comment=a.attempt_comment, fail_sha=a.fail_sha, result_path=a.result, bundle_path=a.bundle,
              prompt_version=a.prompt_version if re.fullmatch(r"[A-Za-z0-9._-]{1,60}", a.prompt_version) else "",
              push_url=f"https://github.com/{ctx.repo}.git")  # credentials via GH_TOKEN, never in argv
    ctx.say(publish(ctx.gh, ctx.cfg, job))
    return 0


def cmd_prompt(ctx: Ctx) -> int:
    values = dict(kv.split("=", 1) for kv in ctx.args.set or [])
    ctx.out.write(prompts.render(ctx.args.name, values, ctx.root))
    return 0


def cmd_schema(ctx: Ctx) -> int:
    ctx.out.write(prompts.schema_json(ctx.args.name))
    return 0


def cmd_review_inputs(ctx: Ctx) -> int:
    from .review_tools import git_in, write_inputs

    a = ctx.args
    values = write_inputs(ctx.gh, git_in(a.pr_dir), a.base_ref, a.head_ref, a.out_dir)
    write_outputs(a.github_output, values)
    ctx.say(json.dumps(values))
    return 0


def cmd_review_report(ctx: Ctx) -> int:
    from .review_tools import render_report

    verdict, comment = render_report(ctx.args.result, ctx.args.overlap)
    Path(ctx.args.out).write_text(comment, encoding="utf-8")
    write_outputs(ctx.args.github_output, {"verdict": verdict})
    ctx.say(f"verdict={verdict}")
    return 0


# --- audit ----------------------------------------------------------------------------

def cmd_audit(ctx: Ctx) -> int:
    from . import audit

    a = ctx.args
    if a.report:
        ctx.say(audit.report(a.report))
        return 0
    if a.label_llm:
        # Evidence folders (pr-N/) sit next to the CSV unless --out-dir says otherwise.
        ctx.say(audit.label_with_llm(a.label_llm, a.out_dir or os.path.dirname(os.path.abspath(a.label_llm))))
        return 0
    out_dir = Path(a.out_dir or f"audit-seed{a.seed}")
    prs = audit.sample(ctx.gh, a.sample, a.seed, [a.lane] if a.lane else None, ctx.cfg.bot_login,
                       tuple(ctx.cfg.trusted_logins))
    rows = audit.collect_evidence(ctx.gh, prs, str(out_dir))
    for row in rows:
        if a.test_cmd:
            row.update(audit.mechanical_check(str(ctx.root), row["merge_commit_sha"], row["impl_files"],
                                              row["test_files"], a.test_cmd))
        else:
            row.update({"pass_at_commit": "n/a", "fail_on_parent": "n/a", "tests_meaningful_mech": "n/a"})
    csv_path = out_dir / "audit.csv"
    audit.write_csv(rows, str(csv_path))
    ctx.say(f"sampled {len(rows)} merged agent PR(s) with seed {a.seed}; evidence in {out_dir}/, labels in {csv_path}",
            "Fill human_label (ok | defect_fixed_later | extended_later | wrong | trivial) and human_tests_meaningful,",
            f"optionally run `gh nightshift audit --label-llm {csv_path}` (LLM labels are not ground truth),",
            f"then `gh nightshift audit --report {csv_path}`.")
    ctx.say("", audit.report(str(csv_path)))
    return 0


# --- parser ---------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gh nightshift", description="Night shift: Claude agents build your GitHub "
                                "issues overnight in parallel lanes.")
    p.add_argument("--version", action="version", version=f"gh-nightshift {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="OWNER/NAME (default: origin remote, or $GITHUB_REPOSITORY)")
    common.add_argument("--root", default=".", help="repository checkout (default: .)")
    common.add_argument("--dry-run", action="store_true", help="print the GitHub calls instead of making them")
    # Same options for `plan context|publish`, but SUPPRESS keeps a value already parsed by `plan` (argparse would
    # otherwise reset it to the default, turning `plan --dry-run publish` into a real run).
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--repo", default=argparse.SUPPRESS)
    sub_common.add_argument("--root", default=argparse.SUPPRESS)
    sub_common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", parents=[common], help="set up night shift in this repo (interactive)")
    s.add_argument("--lanes", help="comma-separated lane names (skips that question)")
    s.add_argument("--yes", action="store_true", help="accept every suggested answer")
    s.add_argument("--force", action="store_true", help="overwrite existing files and config")
    s.add_argument("--regenerate", action="store_true", help="only rewrite the caller workflows from the config")
    s.add_argument("--files-only", action="store_true", help="write files; skip labels, settings and secrets")
    s.add_argument("--skip-secrets", action="store_true", help="do not prompt for secrets")
    s.add_argument("--tool-ref", help="gh-nightshift ref the workflows use (tag or SHA; default main)")
    s.add_argument("--preset", help="stack preset for setup/test commands: python, node or go; per lane: api=python,web=node")
    s.add_argument("--paths", help="folders per lane, e.g. api=api/,web=web/;shared/ (default <lane>/)")
    s.add_argument("--owners", help="lane owners' GitHub logins, which name their secrets, e.g. api=alice,web=bob")
    s.add_argument("--lane-pats", action="store_true",
                   help="each lane uses its owner's own PAT (AGENT_PAT_<OWNER>), so its PRs are opened as that owner; "
                        "the owners become trusted_logins")
    s.add_argument("--bot-login", help="GitHub login that owns AGENT_PAT (trusted by every lane; asked with --lane-pats)")
    s.add_argument("--ci-workflow", help="CI workflow file (default ci.yml); with --preset, written if missing")
    s.add_argument("--merge-mode", choices=["strict", "queue"], help="default strict")
    s.add_argument("--budget", type=float, help="optional nightly budget per lane in USD (API-equivalent); off by default")
    s.add_argument("--template", choices=sorted(scaffold.TEMPLATES),
                   help="start from a ready-made setup, e.g. hackathon (fast refill, short retries, digest every 4 h)")
    s.add_argument("--app-id", help="use a GitHub App instead of AGENT_PAT (see `gh nightshift app manifest`)")
    s.add_argument("--app-key-file", help="the App's private key (.pem); stored as the NIGHT_SHIFT_APP_KEY secret")
    s.add_argument("--secrets-from-env", action="store_true",
                   help="read each missing secret from an environment variable of the same name (for CI); no prompts")
    sub.add_parser("setup", parents=[common], help="labels, settings, variables, branch protection, night log")
    s = sub.add_parser("join", parents=[common], help="a teammate adds their Claude token for a lane")
    s.add_argument("--lane", required=True)
    s.add_argument("--pat", action="store_true", help=argparse.SUPPRESS)  # kept for compatibility; now the default
    s.add_argument("--no-pat", action="store_true", help="do not ask for the lane's own PAT (agent_pat_secret)")
    s = sub.add_parser("doctor", parents=[common], help="check the setup; prints a fix for each problem")
    s.add_argument("--pat-env", action="append", default=[],
                   help="env var holding a PAT to check (repeat for each per-contributor PAT)")
    sub.add_parser("status", parents=[common], help="lane decisions, PRs, blocked issues, queue, log")
    s = sub.add_parser("stats", parents=[common], help="merge rate, reasons, escalation, cost, catch rate")
    s.add_argument("--audit", help="audit CSV (from `audit`) to estimate the review gate's catch rate")
    s.add_argument("--org", help="summarise every repo in this org (or user) that has a night-shift config")
    for name, text in (("start", "switch the night shift on"),
                       ("pause", "switch it off and cancel lane runs in flight (kill switch)"),
                       ("freeze", "pause, and hand remaining ready work to people"),
                       ("resume", "undo freeze and switch on")):
        s = sub.add_parser(name, parents=[common], help=text)
        if name in ("pause", "freeze"):
            s.add_argument("--no-cancel", action="store_true", help="let runs already in flight finish")
    sub.add_parser("lint", parents=[common], help="check ready issues against the ticket guide")
    sub.add_parser("roadmap", parents=[common], help="create or refresh the roadmap issue")
    s = sub.add_parser("audit-log", parents=[common], help="export every agent PR: lane, outcome, model, prompt, cost")
    s.add_argument("--since", default="", help="only PRs created on or after this date (YYYY-MM-DD)")
    s.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    s.add_argument("--out", default="", help="write to this file instead of stdout")
    s = sub.add_parser("config", parents=[common], help="org-wide config: `config pull` copies the extends: base")
    s.add_argument("config_action", choices=["pull"])
    s = sub.add_parser("take", parents=[common], help="take a ticket yourself: assigned, labelled human, agents skip it")
    s.add_argument("number", type=int)
    s = sub.add_parser("retry", parents=[common], help="put a ticket back in the queue with fresh attempts")
    s.add_argument("number", type=int)
    s = sub.add_parser("digest", parents=[common], help="summarise the last night: merged, stuck, cost")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--weekly", action="store_true", help="the weekly report: the last 7 days")
    s.add_argument("--post", action="store_true", help="post it on the night-log issue (and the webhook)")
    s.add_argument("--webhook-env", default="", metavar="VAR",
                   help="env var holding a Slack or Discord webhook URL (the URL is never printed)")

    s = sub.add_parser("plan", parents=[common], help="draft tickets: --from PATH | --gap | --lane X")
    s.add_argument("--from", dest="from_path", help="plan from this doc (prd mode)")
    s.add_argument("--gap", action="store_true", help="compare the docs with the code (gap mode)")
    s.add_argument("--lane", default="", help="only plan for this lane")
    s.add_argument("--plan-file", help="publish an existing plan.json instead of running claude")
    s.add_argument("--out-dir", help="where to keep the context and draft")
    s.add_argument("--show", action="store_true", help="print the roadmap (waves, status, Blocked-by graph) and exit")
    psub = s.add_subparsers(dest="plan_action")
    c = psub.add_parser("context", parents=[sub_common], help="(workflow) write the planning context")
    c.add_argument("--mode", required=True)
    c.add_argument("--docs", default="")
    c.add_argument("--lane", default=argparse.SUPPRESS)
    c.add_argument("--out-dir", required=True)
    c.add_argument("--github-output")
    c = psub.add_parser("publish", parents=[sub_common], help="(workflow) validate a plan.json and create issues")
    c.add_argument("file")
    c.add_argument("--lane", default=argparse.SUPPRESS)
    c.add_argument("--reply-to", default="")

    s = sub.add_parser("audit", parents=[common], help="sample merged agent PRs and measure quality")
    s.add_argument("--sample", type=int, default=25)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--lane", default="")
    s.add_argument("--test-cmd", help="test command; {tests} is replaced by the PR's test files")
    s.add_argument("--out-dir")
    s.add_argument("--report", metavar="CSV", help="print rates for a filled-in audit CSV")
    s.add_argument("--label-llm", metavar="CSV", help="optional: let Claude fill llm_label (not ground truth)")

    # Used by the reusable workflows.
    s = sub.add_parser("app", parents=[common], help="GitHub App auth: print the manifest, or check minting")
    asub = s.add_subparsers(dest="app_action", required=True)
    m = asub.add_parser("manifest", parents=[sub_common])
    m.add_argument("--name", default="night-shift")
    m = asub.add_parser("check", parents=[sub_common])
    m.add_argument("--app-id", default="")
    m.add_argument("--key-file", default="", help="private key .pem (default: $APP_PRIVATE_KEY)")
    s = sub.add_parser("app-token", parents=[common])
    s.add_argument("--github-env", required=True)
    s = sub.add_parser("command", parents=[common])
    for flag in ("--author", "--body", "--issue"):
        s.add_argument(flag, default="")
    s = sub.add_parser("lane-config", parents=[common])
    s.add_argument("--lane", required=True)
    s.add_argument("--github-output")
    sub.add_parser("check-pat", parents=[common])
    s = sub.add_parser("pick", parents=[common])
    s.add_argument("--lane", required=True)
    s.add_argument("--github-output")
    s = sub.add_parser("task", parents=[common])
    for flag in ("--lane", "--mode", "--out-dir"):
        s.add_argument(flag, required=True)
    for flag in ("--issue", "--fail-sha", "--run-url", "--github-output"):
        s.add_argument(flag, default="")
    s = sub.add_parser("publish", parents=[common])
    for flag in ("--lane", "--mode", "--branch", "--title", "--model"):
        s.add_argument(flag, required=True)
    for flag in ("--issue", "--attempt-comment", "--fail-sha", "--result", "--bundle", "--prompt-version"):
        s.add_argument(flag, default="")
    s = sub.add_parser("prompt", parents=[common])
    s.add_argument("name", choices=["worker", "review", "planner"])
    s.add_argument("--set", action="append", metavar="KEY=VALUE")
    s = sub.add_parser("schema", parents=[common])
    s.add_argument("name", choices=["worker", "review", "planner"])
    s = sub.add_parser("review-inputs", parents=[common])
    for flag in ("--pr-dir", "--base-ref", "--head-ref", "--out-dir"):
        s.add_argument(flag, required=True)
    s.add_argument("--github-output")
    s = sub.add_parser("review-report", parents=[common])
    s.add_argument("--result", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--overlap", type=int, default=0, help="files changed by both the PR and main (review-inputs)")
    s.add_argument("--github-output")
    return p


COMMANDS = {
    "init": cmd_init, "setup": cmd_setup, "join": cmd_join, "doctor": cmd_doctor, "status": cmd_status,
    "stats": cmd_stats, "start": cmd_switch, "pause": cmd_switch, "freeze": cmd_switch, "resume": cmd_switch,
    "lint": cmd_lint, "plan": cmd_plan, "roadmap": cmd_roadmap, "digest": cmd_digest,
    "config": cmd_config, "audit-log": cmd_audit_log, "app": cmd_app, "app-token": cmd_app_token,
    "take": cmd_take_retry, "retry": cmd_take_retry, "command": cmd_command,
    "audit": cmd_audit, "lane-config": cmd_lane_config,
    "check-pat": cmd_check_pat, "pick": cmd_pick, "task": cmd_task, "publish": cmd_publish, "prompt": cmd_prompt,
    "schema": cmd_schema, "review-inputs": cmd_review_inputs, "review-report": cmd_review_report,
}


def main(argv: list[str] | None = None, transport=None, out=None) -> int:
    args = parser().parse_args(argv)
    ctx = Ctx(args, transport=transport, out=out)
    try:
        return COMMANDS[args.command](ctx)
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except GHError as err:
        print(f"error: {err}", file=sys.stderr)
        return 3
