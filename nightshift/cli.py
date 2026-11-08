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

from . import __version__, planner, prompts, repo_setup, scaffold
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


def build_config(root: Path, ask: Ask, lanes_arg: str | None, assume_yes: bool) -> dict:
    """Interactive (or --yes) config: lanes from the detected stack, one Claude token secret per owner."""
    stacks = scaffold.detect_stack(root)
    folders = [s for s in stacks if s["path"] != "./"]
    suggested = lanes_arg or ",".join(re.sub(r"[^a-z0-9_-]", "-", s["path"].rstrip("/").lower()) for s in folders) \
        or "app"
    say = (lambda *_: None) if assume_yes else print
    if stacks:
        say("Detected: " + "; ".join(f"{s['path']} ({s['stack']})" for s in stacks))
    q = (lambda p, d: d) if assume_yes else (lambda p, d: _ask(ask, p, d))
    names = [n.strip() for n in q("Lanes (comma-separated)", suggested).split(",") if n.strip()]
    lanes = []
    for name in names:
        guess = next((s for s in stacks if s["path"].rstrip("/") == name), stacks[0] if stacks else None)
        owner = q(f"Lane {name}: owner's name (for their Claude token secret)", name).upper()
        lane = {"name": name,
                "paths": [p.strip() for p in q(f"Lane {name}: folders it owns", f"{name}/").split(",") if p.strip()],
                "claude_token_secret": f"CLAUDE_TOKEN_{re.sub(r'[^A-Z0-9]', '_', owner)}",
                "model": "sonnet"}
        setup = q(f"Lane {name}: setup command", guess["setup"] if guess else "")
        test = q(f"Lane {name}: test command", guess["test"] if guess else "")
        if setup:
            lane["setup"] = {"commands": setup + "\n"}
        if test:
            lane["test_command"] = test
        lanes.append(lane)
    ci = q("CI workflow file (its runs on main gate the lanes)", "ci.yml")
    mode = q("Merge mode: strict (PRs must be up to date) or queue (GitHub merge queue)", "strict")
    return {"version": 1, "main_branch": "main", "ci_workflow": ci,
            "required_checks": ["test", "review / verdict"], "fix_lane": lanes[0]["name"] if lanes else "",
            "merge": {"mode": mode}, "tool_ref": "main", "lanes": lanes,
            "limits": {"max_attempts": 3, "infra_cap": 6, "max_needs_per_issue": 2, "max_notes_per_issue": 2},
            "planner": {"docs": ["docs/PRD.md"], "max_tickets_per_run": 8, "refill_below": 0}}


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
    else:
        text = scaffold.config_yaml(build_config(ctx.root, ask, a.lanes, a.yes))
        cfg = loads(text)  # validates what we are about to write
    ctx.say(*write_files(ctx, scaffold.files(cfg, text, ctx.root), overwrite=a.force))
    if a.files_only:
        return 0
    ctx.say(*repo_setup.setup(ctx.gh, cfg))
    if not a.skip_secrets:
        kwargs = {}
        if secret_ask:
            kwargs["ask"] = secret_ask
        if secret_setter:
            kwargs["setter"] = secret_setter
        ctx.say(*repo_setup.prompt_missing_secrets(ctx.gh, cfg, **kwargs))
    ctx.say("", "Next: commit and push the new files (your gh token needs the `workflow` scope), run "
                "`gh nightshift doctor`, label a few issues night-safe, then `gh nightshift start`.")
    return 0


def cmd_setup(ctx: Ctx) -> int:
    ctx.say(*repo_setup.setup(ctx.gh, ctx.cfg))
    return 0


def cmd_join(ctx: Ctx, secret_ask=None, secret_setter=None) -> int:
    cfg = ctx.cfg
    lane = cfg.lane(ctx.args.lane)
    kwargs = {}
    if secret_ask:
        kwargs["ask"] = secret_ask
    if secret_setter:
        kwargs["setter"] = secret_setter
    ctx.say(f"Lane {lane.name}: run `claude setup-token` and paste the token.")
    ctx.say(repo_setup.prompt_secret(ctx.gh, lane.token_secret, f"Claude token for lane {lane.name}", **kwargs))
    if ctx.args.pat and lane.pat_secret:
        ctx.say(repo_setup.prompt_secret(ctx.gh, lane.pat_secret, repo_setup.secret_hint(cfg, lane.pat_secret), **kwargs))
    return 0


# --- doctor / status / stats ----------------------------------------------------------

def cmd_doctor(ctx: Ctx, run=subprocess.run) -> int:
    from .doctor import FAIL, run_doctor

    token = os.environ.get(ctx.args.pat_env) if ctx.args.pat_env else None
    checks = run_doctor(ctx.gh, ctx.root, token, run)
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
    from .status import stats

    ctx.say(stats(ctx.gh, ctx.cfg, ctx.args.audit))
    return 0


def cmd_switch(ctx: Ctx) -> int:
    name = ctx.args.command
    if name == "start":
        lines = repo_setup.start(ctx.gh)
    elif name == "pause":
        lines = repo_setup.pause(ctx.gh)
    elif name == "freeze":
        lines = repo_setup.freeze(ctx.gh, ctx.cfg)
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
             "--max-turns", "60", "--add-dir", add_dir, "--allowedTools", "Read,Grep,Glob"],
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
        for m in ns.trusted(gh.comments(log), gh.login, "plan"):
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
    raw = Path(path).read_text(encoding="utf-8") if Path(path).is_file() else "{}"
    result = planner.publish_plan(gh, cfg, raw, lane)
    lines = result.lines()
    ctx.say(*lines)
    summary = "**Night shift planner**\n\n" + "\n".join(f"- {line}" for line in lines)
    if reply_to:
        gh.comment(int(reply_to), summary)
    log = gh.night_log(cfg.label("night_log"))
    if log:
        marker = ns.marker("plan", lane=lane or "*", complete=result.coverage_complete,
                           docs_sha=docs_sha(gh, cfg.planner_docs), created=len(result.created) + len(result.human))
        gh.comment(log, marker + "\n" + summary)
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
    from .doctor import FAIL, pat_checks

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    checks = pat_checks(token, run)
    for c in checks:
        ctx.say(c.line())
    if any(c.level == FAIL for c in checks):
        ctx.say("::error::the PAT must authenticate and must not have the Workflows permission")
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
    write_outputs(a.github_output, values)
    ctx.say(json.dumps(values))
    return 0


def cmd_publish(ctx: Ctx) -> int:
    from .publish import Job, publish

    a = ctx.args
    token = os.environ.get("GH_TOKEN", "")
    job = Job(lane=a.lane, mode=a.mode, issue=a.issue, branch=a.branch, title=a.title, model=a.model,
              attempt_comment=a.attempt_comment, fail_sha=a.fail_sha, result_path=a.result, bundle_path=a.bundle,
              push_url=f"https://x-access-token:{token}@github.com/{ctx.repo}.git")
    ctx.say(publish(ctx.gh, ctx.cfg, job))
    return 0


def cmd_prompt(ctx: Ctx) -> int:
    values = dict(kv.split("=", 1) for kv in ctx.args.set or [])
    sys.stdout.write(prompts.render(ctx.args.name, values, ctx.root))
    return 0


def cmd_schema(ctx: Ctx) -> int:
    sys.stdout.write(prompts.schema_json(ctx.args.name))
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

    verdict, comment = render_report(ctx.args.result)
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
        ctx.say(audit.label_with_llm(a.label_llm, a.out_dir or "audit"))
        return 0
    out_dir = Path(a.out_dir or f"audit-seed{a.seed}")
    prs = audit.sample(ctx.gh, a.sample, a.seed, [a.lane] if a.lane else None)
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
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", parents=[common], help="set up night shift in this repo (interactive)")
    s.add_argument("--lanes", help="comma-separated lane names (skips that question)")
    s.add_argument("--yes", action="store_true", help="accept every suggested answer")
    s.add_argument("--force", action="store_true", help="overwrite existing files and config")
    s.add_argument("--regenerate", action="store_true", help="only rewrite the caller workflows from the config")
    s.add_argument("--files-only", action="store_true", help="write files; skip labels, settings and secrets")
    s.add_argument("--skip-secrets", action="store_true", help="do not prompt for secrets")
    sub.add_parser("setup", parents=[common], help="labels, settings, variables, branch protection, night log")
    s = sub.add_parser("join", parents=[common], help="a teammate adds their Claude token for a lane")
    s.add_argument("--lane", required=True)
    s.add_argument("--pat", action="store_true", help="also set the lane's own PAT (agent_pat_secret)")
    s = sub.add_parser("doctor", parents=[common], help="check the setup; prints a fix for each problem")
    s.add_argument("--pat-env", help="name of an env var holding the PAT, to check its permissions")
    sub.add_parser("status", parents=[common], help="lane decisions, PRs, blocked issues, queue, log")
    s = sub.add_parser("stats", parents=[common], help="merge rate, reasons, escalation, cost, catch rate")
    s.add_argument("--audit", help="audit CSV (from `audit`) to estimate the review gate's catch rate")
    for name, text in (("start", "switch the night shift on"), ("pause", "switch it off (kill switch)"),
                       ("freeze", "switch off and hand remaining ready work to people"),
                       ("resume", "undo freeze and switch on")):
        sub.add_parser(name, parents=[common], help=text)
    sub.add_parser("lint", parents=[common], help="check ready issues against the ticket guide")

    s = sub.add_parser("plan", parents=[common], help="draft tickets: --from PATH | --gap | --lane X")
    s.add_argument("--from", dest="from_path", help="plan from this doc (prd mode)")
    s.add_argument("--gap", action="store_true", help="compare the docs with the code (gap mode)")
    s.add_argument("--lane", default="", help="only plan for this lane")
    s.add_argument("--plan-file", help="publish an existing plan.json instead of running claude")
    s.add_argument("--out-dir", help="where to keep the context and draft")
    psub = s.add_subparsers(dest="plan_action")
    c = psub.add_parser("context", parents=[common], help="(workflow) write the planning context")
    c.add_argument("--mode", required=True)
    c.add_argument("--docs", default="")
    c.add_argument("--lane", default="")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--github-output")
    c = psub.add_parser("publish", parents=[common], help="(workflow) validate a plan.json and create issues")
    c.add_argument("file")
    c.add_argument("--lane", default="")
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
    for flag in ("--issue", "--attempt-comment", "--fail-sha", "--result", "--bundle"):
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
    s.add_argument("--github-output")
    return p


COMMANDS = {
    "init": cmd_init, "setup": cmd_setup, "join": cmd_join, "doctor": cmd_doctor, "status": cmd_status,
    "stats": cmd_stats, "start": cmd_switch, "pause": cmd_switch, "freeze": cmd_switch, "resume": cmd_switch,
    "lint": cmd_lint, "plan": cmd_plan, "audit": cmd_audit, "lane-config": cmd_lane_config,
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
