"""Publish job (clean runner, holds the PAT): validate the agent's bundle, push, open the PR, report.

The agent job had no GitHub credentials. Everything it produced arrives as two files: result.json
(Claude's structured output) and agent.bundle (its commits). Nothing in them is trusted: the bundle is
fetched into a fresh clone and checked against protected paths before anything is pushed, and every
GitHub write here is capped (needs, notes, follow-ups) so a confused agent cannot flood the repo.

Outcomes the agent can report:
  done     committed and tested -> PR (with its `handoff` stored in the PR body for step 3a)
  stuck    a real attempt failed -> comment, counts as an attempt
  waiting  blocked on another lane -> one ticket per `needs` entry in that lane (ready if it passes the
           ticket validator, else `proposed`), "Blocked by: #new" appended here, attempt NOT counted
"""
from __future__ import annotations

import datetime as dt
import json
import re
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from zoneinfo import ZoneInfo

from . import planner
from . import protocol as ns
from .config import Config
from .gh import GH, GHError
from .rules import label_names, lanes_of, parse_deps, path_is_protected, wave_of

GitRunner = Callable[[list[str]], str]
MAX_FOLLOW_UPS = 3


def _git_env() -> dict[str, str]:
    """Credentials for pushes come from $GH_TOKEN through a credential helper set in the environment, so the
    PAT never appears in git's argv (visible in `ps`) or in error messages."""
    env = dict(os.environ)
    if env.get("GH_TOKEN"):
        n = int(env.get("GIT_CONFIG_COUNT") or 0)  # keep entries the runner already set
        env.update({"GIT_CONFIG_COUNT": str(n + 2),
                    f"GIT_CONFIG_KEY_{n}": "credential.https://github.com.helper", f"GIT_CONFIG_VALUE_{n}": "",
                    f"GIT_CONFIG_KEY_{n + 1}": "credential.https://github.com.helper",
                    f"GIT_CONFIG_VALUE_{n + 1}": '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f',
                    "GIT_TERMINAL_PROMPT": "0"})
    return env


def _redact(text: str) -> str:
    return re.sub(r"(https://)[^@/\s]+@", r"\1***@", text)


def real_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, env=_git_env())
    if result.returncode != 0:
        raise RuntimeError(_redact(f"git {' '.join(args)}: {result.stderr.strip()}"))
    return result.stdout


@dataclass
class Result:
    status: str
    summary: str
    assumptions: list[str] = field(default_factory=list)
    needs: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    follow_ups: list[dict] = field(default_factory=list)
    handoff: dict = field(default_factory=dict)
    cost: str = ""
    num_turns: int = 0


def _dicts(value) -> list[dict]:
    return [x for x in value or [] if isinstance(x, dict)]


def _cost(value) -> str:
    """The run's cost as a plain number, or "". result.json comes from the agent's runner, so this field is as
    untrusted as the rest: copied as text it could carry a line break and an ns marker into the night log."""
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{cost:.4f}".rstrip("0").rstrip(".") if 0 < cost < 100000 else ""


def _turns(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def classify(result_path: str | None) -> Result:
    """A run that never really started (no result file, or <2 turns and no structured output: expired
    token, usage limit, outage) is infra-error and gets refunded, not counted."""
    if not result_path or not Path(result_path).is_file():
        return Result("infra-error", "The agent job never produced a result (setup failed); see the run log.")
    try:
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Result("stuck", "The agent run was cut off (crash or timeout); see the run log.")
    if not isinstance(data, dict):
        return Result("stuck", "The agent run produced an unreadable result.")
    if data.get("night_shift_guard") == "token-leak":
        # Written by the agent job's token guard, which withheld the commits, the result and the log.
        where = re.sub(r"[^a-z' ]", "", str(data.get("where") or "its output"))[:80]
        return Result("token-leak", f"The run was withheld: the lane's Claude token appeared in {where}. Nothing "
                                    "from it was uploaded or pushed. Rotate the token (`claude setup-token`, then "
                                    "`gh nightshift join --lane <lane>`, and revoke the old one), check what the "
                                    "ticket asked for, then remove the needs-human label.")
    cost, turns = _cost(data.get("total_cost_usd")), _turns(data.get("num_turns"))
    out = data.get("structured_output")
    if isinstance(out, dict) and out.get("status") in {"done", "stuck", "waiting"}:
        out = ns.defang(out)  # everything below is re-posted under the bot's trusted login
        assumptions = out.get("assumptions") if isinstance(out.get("assumptions"), list) else []
        # Convert to text, then defang again: a dict or list rendered as text could carry raw markers in keys.
        return Result(out["status"], ns.defang(str(out.get("summary") or "")),
                      [ns.defang(str(a)) for a in assumptions],
                      _dicts(out.get("needs")), _dicts(out.get("notes")), _dicts(out.get("follow_ups")),
                      out.get("handoff") if isinstance(out.get("handoff"), dict) else {}, cost, turns)
    if turns < 2:
        return Result("infra-error", ns.defang(str(data.get("result") or "no result")[:500]), cost=cost,
                      num_turns=turns)
    return Result("stuck", "Run ended without a structured result (likely hit the turn limit).", cost=cost,
                  num_turns=turns)


def pr_body(mode: str, issue: str, lane: str, model: str, res: Result, prompt_version: str = "") -> str:
    parts = []
    if mode == "issue":
        parts += [f"Closes #{issue}", ""]
    prompt = f" · **Prompt:** `{prompt_version}`" if prompt_version else ""
    # Only the line above may close an issue: "closes #n" in the agent's own text is broken up (ns.no_close).
    parts += [f"**Lane:** `{lane}` (night shift) · **Model:** `{model}`{prompt}", "", ns.no_close(res.summary)]
    if res.assumptions:
        parts += ["", "**Assumptions the agent made (review in the morning):**",
                  *[f"- {ns.one_line(ns.no_close(a))}" for a in res.assumptions]]
    if res.handoff:
        h = ns.no_close(res.handoff)
        parts += ["", "## Hand-off", ns.marker("handoff-data", data=h)]
        for key, title in (("interfaces", "Interface as built"), ("files", "Files"), ("env_vars", "Env vars"),
                           ("decisions", "Decisions")):
            items = h.get(key) or []
            items = [items] if isinstance(items, str) else items
            if items:
                parts += [f"**{title}:**", *[f"- {ns.one_line(x)}" for x in items]]
        if h.get("interface_differences"):
            parts.append(f"**Differences from the ticket:** {ns.one_line(h['interface_differences'], 1000)}")
    return "\n".join(parts) + "\n"


@dataclass
class Job:
    lane: str
    mode: str
    issue: str
    branch: str
    title: str
    model: str
    attempt_comment: str = ""
    fail_sha: str = ""
    result_path: str = ""
    bundle_path: str = ""
    push_url: str = ""          # https://github.com/o/r.git; the PAT comes from $GH_TOKEN (see _git_env)
    prompt_version: str = ""    # prompts.version("worker"), for the audit log


def publish(gh: GH, cfg: Config, job: Job, git: GitRunner = real_git) -> str:
    res = classify(job.result_path)
    outcome = res.status
    if res.status == "done":
        outcome = push_and_open_pr(gh, cfg, job, res, git)
    issue = gh.issue(int(job.issue)) if job.mode == "issue" else None

    if issue is not None and res.status == "waiting":
        outcome = park_waiting(gh, cfg, job, issue, res)
    if issue is not None and res.notes:
        for line in send_notes(gh, cfg, job, issue, res.notes):
            print(line)
    if issue is not None and res.follow_ups:
        for line in file_follow_ups(gh, cfg, job, issue, res.follow_ups):
            print(line)

    if res.status == "infra-error" and job.attempt_comment:
        kind = "infra" if job.mode == "issue" else "fix-infra"
        fields = {"issue": int(job.issue)} if job.mode == "issue" else {"sha": job.fail_sha}
        gh.edit_comment(int(job.attempt_comment),
                        f"{ns.marker(kind, **fields)}\nRun did not start; attempt not counted. {res.summary}")
        outcome = "infra-error (attempt not counted)"

    if res.status == "token-leak":
        outcome = "withheld: Claude token found in the run's output (needs a person)"
        if issue is not None:
            gh.add_labels(int(job.issue), [cfg.label("needs_human")])  # never retried automatically

    if issue is not None:
        if not outcome.startswith("PR") and res.status != "waiting":
            gh.comment(int(job.issue), f"Night shift lane `{job.lane}`: **{ns.defang(outcome)}**\n\n{res.summary}")
        gh.remove_label(int(job.issue), cfg.label("in_progress"))

    log = gh.night_log(cfg.label("night_log"))
    if log:
        what = f"#{job.issue}" if job.mode == "issue" else "fix main"
        try:
            now = dt.datetime.now(ZoneInfo(cfg.timezone))
        except Exception:  # unknown timezone name: fall back to UTC rather than fail the publish
            now = dt.datetime.now(dt.timezone.utc)
        cost = f" (≈${res.cost})" if res.cost else ""
        # `≈$` marks the cost; agent text (outcome) must not be able to fake one (budget, stats, digest).
        text = ns.defang(outcome).replace("≈", "~")
        gh.comment(log, f"{now:%H:%M} `{job.lane}` {what} [{job.model}] → {text}{cost}")
    return ns.defang(outcome)


def push_and_open_pr(gh: GH, cfg: Config, job: Job, res: Result, git: GitRunner) -> str:
    if not job.bundle_path or not Path(job.bundle_path).is_file():
        return "done but no commits"
    ref = f"refs/heads/{job.branch}"
    try:
        git(["fetch", "-q", job.bundle_path, f"{ref}:{ref}"])
    except RuntimeError:
        return "rejected: unreadable bundle"
    # --no-renames: a rename lists both paths, so moving CLAUDE.md away is caught; -z and quotePath=false:
    # unusual file names are listed verbatim instead of quoted (a quoted name would dodge the match).
    changed = [p for p in git(["-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", "-z",
                               f"origin/{cfg.main_branch}...{ref}"]).replace("\n", "\0").split("\0") if p]
    blocked = [p for p in changed if path_is_protected(p, cfg.protected_paths)]
    if blocked:
        return f"rejected: touches protected paths ({', '.join(blocked[:5])})"
    if not changed:
        return "done but no changes"
    if gh.dry_run:
        print(f"[dry-run] git push --force <PAT remote> {ref}:{ref}", file=gh.out)
    else:
        git(["push", "-q", "--force", job.push_url, f"{ref}:{ref}"])
    owner = gh.repo.split("/")[0]
    for old in gh.get(gh.r("pulls"), {"state": "open", "head": f"{owner}:{job.branch}"}) or []:
        # A leftover PR on this branch would swallow the new push silently; replace it visibly.
        issue = int(job.issue) if job.mode == "issue" else None
        gh.comment(old["number"], f"{ns.marker('pr', reason='superseded', pr=old['number'], issue=issue)}\n"
                                  "Night shift: superseded by a newer attempt on the same branch.")
        gh.patch(gh.r(f"pulls/{old['number']}"), {"state": "closed"})
    try:
        pr = gh.post(gh.r("pulls"), {"title": ns.no_close(f"[{job.lane}] {job.title}"), "head": job.branch,
                                     "base": cfg.main_branch,
                                     "body": pr_body(job.mode, job.issue, job.lane, job.model, res,
                                                                  job.prompt_version)})
    except GHError as err:
        return f"push ok but PR creation failed: {err}"
    labels = [cfg.label("agent"), f"lane:{job.lane}"] + (["fix-main"] if job.mode == "fix-main" else [])
    gh.add_labels(pr.get("number", 0), labels)
    return f"PR {pr.get('html_url', '')}"


# --- waiting on another lane (step 3b) ------------------------------------------------

def dependency_edges(gh: GH) -> dict[int, set[int]]:
    """'Blocked by' edges among open issues (enough to refuse cycles)."""
    return {i["number"]: set(d for d in parse_deps(i.get("body")) if d > 0) for i in gh.issues()}


def park_waiting(gh: GH, cfg: Config, job: Job, issue: dict, res: Result) -> str:
    me = issue["number"]
    comments = gh.comments(me)
    already = len(ns.trusted(comments, gh.trusted, "need"))
    room = max(cfg.max_needs_per_issue - already, 0)
    lines: list[str] = []
    providers: list[int] = []
    edges = dependency_edges(gh)
    body = issue.get("body") or ""
    for need in res.needs[:room]:
        lane = str(need.get("lane") or "").strip()
        if lane not in cfg.lane_names or lane == job.lane:
            lines.append(f"need refused: {lane!r} is not another configured lane")
            continue
        ticket = planner.Ticket.from_need(need, lane, wave_of(issue) if wave_of(issue) != 999 else 0)
        ticket.files = ticket.files or list(cfg.lane(lane).paths)  # a need names the lane, not the files
        existing = planner.find_duplicate(ticket.plain_title, gh.issues([f"lane:{lane}"]))
        if existing:
            provider, ready = existing["number"], cfg.ready_label in label_names(existing)
        else:
            ticket.title = planner.next_id(gh.issues(state="all"), cfg.id_prefix(lane)) + " " + ticket.plain_title
            problems, human = planner.check_ticket(ticket, cfg, depended_on=True, existing={})
            # An agent wrote this ticket. It goes straight to the other lane when it passes the validator, unless
            # the repo set limits.auto_ready_needs: false; then a person promotes it.
            ready = cfg.auto_ready_needs and not problems and not human
            labels = [f"lane:{lane}", f"wave:{ticket.wave}",
                      cfg.ready_label if ready else (cfg.label("human") if human else cfg.label("proposed"))]
            body_text = planner.render(ticket, {}) + f"\nRequested by #{me} (lane `{job.lane}`), which waits on it.\n"
            if problems:
                body_text += "\n**Why this is not night-safe yet:**\n" + "\n".join(f"- {p}" for p in problems) + "\n"
            elif not ready and not human:
                body_text += ("\nWritten by an agent, so it waits for a person: swap `" + cfg.label("proposed") + "` for `"
                              + cfg.ready_label + "` to queue it (`limits.auto_ready_needs` is off).\n")
            provider = gh.create_issue(ticket.title, body_text, labels).get("number", 0)
        if provider and ns.would_cycle(edges, me, provider):
            gh.add_labels(me, [cfg.label("needs_human")])
            lines.append(f"need #{provider} refused: dependency cycle")
            gh.comment(me, f"Night shift: lane `{job.lane}` needs #{provider}, but #{provider} already waits on this "
                           "issue (a dependency cycle). A person needs to split or reorder these tickets.")
            continue
        edges.setdefault(me, set()).add(provider)
        body = ns.add_blocked_by(body, provider)
        providers.append(provider)
        gh.comment(me, ns.need_comment(me, provider, lane, ticket.plain_title, ready))
        lines.append(f"needs #{provider} ({lane}, {'ready' if ready else 'proposed'})")
    if len(res.needs) > room:
        lines.append(f"{len(res.needs) - room} need(s) over the cap of {cfg.max_needs_per_issue} per issue ignored")
    if providers:
        gh.patch(gh.r(f"issues/{me}"), {"body": body})
        gh.add_labels(me, ["waiting"])
        if job.attempt_comment:
            gh.edit_comment(int(job.attempt_comment),
                            f"{ns.marker('waiting', issue=me)}\nAttempt not counted: waiting on "
                            + ", ".join(f"#{p}" for p in providers) + ".")
        return "waiting on " + ", ".join(f"#{p}" for p in providers) + " (attempt not counted)"
    # Nothing could be filed: this was a failed attempt after all.
    gh.comment(me, "Night shift lane: the agent reported it is waiting on another lane, but no request could be "
                   "filed (" + ns.defang("; ".join(lines) or "no needs given") + "). Counted as a failed attempt.")
    return "stuck (waiting with no valid needs)"


def send_notes(gh: GH, cfg: Config, job: Job, issue: dict, notes: list[dict]) -> list[str]:
    """Informational messages to other issues (step 3c), capped per sending issue."""
    me = issue["number"]
    sent = len(ns.trusted(gh.comments(me), gh.trusted, "note-sent"))
    out = []
    for note in notes[: max(cfg.max_notes_per_issue - sent, 0)]:
        try:
            to = int(note.get("issue") or 0)
        except (TypeError, ValueError):
            to = 0
        text = str(note.get("text") or "").strip()
        target = gh.issue(to) if to and to != me else None
        if not target or target.get("state") != "open" or "pull_request" in target or not text:
            out.append(f"note to #{to} skipped: needs an open issue and text")
            continue
        gh.comment(to, ns.note_comment(me, to, job.lane, text))
        gh.comment(me, f"{ns.marker('note-sent', **{'from': me, 'to': to})}\nNight shift: sent a note to #{to}.")
        out.append(f"note sent to #{to}")
    return out


def file_follow_ups(gh: GH, cfg: Config, job: Job, issue: dict, follow_ups: list[dict]) -> list[str]:
    """Builders may suggest follow-up work (step 4). Filed as `proposed`; a person promotes them."""
    out = []
    lanes = lanes_of(issue)
    for fu in follow_ups[:MAX_FOLLOW_UPS]:
        lane = str(fu.get("lane") or (lanes[0] if lanes else "")).strip()
        if lane not in cfg.lane_names:
            out.append(f"follow-up skipped: unknown lane {lane!r}")
            continue
        ticket = planner.Ticket.from_need(fu, lane, wave_of(issue) if wave_of(issue) != 999 else 0)
        if planner.find_duplicate(ticket.plain_title, gh.issues(state="all")):
            out.append(f"follow-up skipped (duplicate): {ticket.plain_title}")
            continue
        ticket.title = planner.next_id(gh.issues(state="all"), cfg.id_prefix(lane)) + " " + ticket.plain_title
        created = gh.create_issue(ticket.title, planner.render(ticket, {}) + f"\nFollow-up suggested by #{issue['number']}.\n",
                                  [f"lane:{lane}", f"wave:{ticket.wave}", cfg.label("proposed")])
        out.append(f"follow-up #{created.get('number', 0)} filed as proposed")
    return out
