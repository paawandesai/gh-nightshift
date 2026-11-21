"""Cross-repo hand-offs: a reduce run in one repository drafts a ticket for another, which files it as ready.

Each repository's token writes only to its own repository, so a hand-off travels in two steps:

  send     (the sending repo's planner `create` job, reduce mode) A drafted ticket with `repo: owner/name` is
           checked (the target must be in this repo's `cross_repos`) and posted as a trusted `xhandoff`
           marker on this repo's night-log issue. Nothing is written to the other repository.
  receive  (the receiving repo's picker, step 2b) For every repo in its own `cross_repos` that is a Night Shift
           repo run by the same bot login, the picker reads the `xhandoff` markers addressed to it on that
           repo's night log and files each one as a ready ticket in the lane that owns it, once (an
           `xhandoff-in` marker in the new ticket's body records the source).

Trust: both sides must list each other in `cross_repos`; a marker counts only when the shared `bot_login` posted
it and nobody edited the comment since (repo writers can edit bot comments); the ticket is validated against the
receiving repo's own config (lanes, protected and human-only paths) like any planner ticket. The ticket text was
drafted by the sending repo's reducer from agent-written hand-offs, so it gets the same scrutiny as a planner
ticket: the receiving repo's review gates still judge the PR that implements it.
"""
from __future__ import annotations

import base64
import datetime as dt

from . import protocol as ns
from . import yamlish
from .config import CONFIG_PATH, DEFAULT_LABELS, Config
from .gh import GH, GHError

LOOKBACK_DAYS = 14  # hand-offs older than this on the sender's night log are not read any more
MAX_REPOS = 20
LIMITS = {"title": 120, "goal": 2000, "interface": 3000, "item": 500, "items": 20}


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def ticket_data(t) -> dict:
    """The part of a drafted ticket that travels, defanged (it is re-posted under the bot's login), then capped."""
    d = ns.defang

    def items(xs: list[str]) -> list[str]:
        return [ns.one_line(d(x), LIMITS["item"]) for x in xs[:LIMITS["items"]]]

    return {"title": ns.one_line(d(t.title), LIMITS["title"]), "goal": d(t.goal)[:LIMITS["goal"]],
            "do": items(t.do), "acceptance": items(t.acceptance), "files": items(t.files),
            "interface": d(t.interface)[:LIMITS["interface"]], "lane": ns.one_line(d(t.lane), 40),
            "wave": t.wave if 0 <= t.wave <= 99 else 0}


def send(gh: GH, cfg: Config, tickets: list, cap: int, prs: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Post each valid cross-repo ticket as an `xhandoff` marker on this repo's night log.
    Returns ([(summary line, ticket key)], rejection notes)."""
    allowed = {r.lower(): r for r in cfg.cross_repos if not _same(r, gh.repo)}
    sent: list[tuple[str, str]] = []
    notes: list[str] = []
    log = gh.night_log(cfg.label("night_log"))
    for t in tickets:
        name = t.key or t.title
        why = []
        if t.repo.lower() not in allowed:
            why.append(f"{t.repo!r} is not in cross_repos (or is this repository)")
        if not t.title or not t.goal or not t.do or not t.acceptance:
            why.append("needs a title, goal, do and acceptance")
        if t.blocked_by:
            why.append("a hand-off cannot depend on tickets (the other repository cannot see them)")
        if not log:
            why.append("there is no open night-log issue to post the hand-off on; run `gh nightshift setup`")
        if not why and len(sent) >= cap:
            why.append(f"over the cap of {cap} tickets this run")
        if why:
            notes.append(f"rejected hand-off {name!r}: {'; '.join(why)}")
            continue
        repo = allowed[t.repo.lower()]
        data = ticket_data(t)
        body = "\n".join([
            ns.marker("xhandoff", to=repo, prs=prs, ticket=data),
            f"**Night shift hand-off to {repo}:** {data['title']}", "",
            f"Drafted by the reduce pass over PR(s) {', '.join('#' + p for p in prs.split(',') if p) or '(none)'}. "
            f"{repo}'s night shift files it as a ready ticket if it lists this repository in its `cross_repos` and "
            "runs with the same bot login. Do not edit this comment: an edited hand-off is ignored."])
        gh.comment(log, body)
        sent.append((f"handed off to {repo}: {data['title']}", t.key))
    return sent, notes


# --- receiving side (picker) ---------------------------------------------------------------------------

def other_config(gh: GH, repo: str) -> dict | None:
    data = gh.get_or_none(f"repos/{repo}/contents/{CONFIG_PATH}")
    if not isinstance(data, dict) or not data.get("content"):
        return None
    try:
        parsed = yamlish.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, yamlish.YAMLError):
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve_lane(cfg: Config, hint: str, files: list[str]) -> str:
    """The lane that should build it here: the sender's lane name if it exists here, else the one lane owning
    every listed file, else the fix lane."""
    if hint in cfg.lane_names:
        return hint
    owners = {ln.name for ln in cfg.lanes if ln.paths and files and all(
        any(f == p.rstrip("/") or f.startswith(p.rstrip("/") + "/") for p in ln.paths) for f in files)}
    if len(owners) == 1:
        return owners.pop()
    return cfg.effective_fix_lane


def received(gh: GH, issues: list[dict]) -> set[str]:
    """Sources (`owner/repo#c<comment id>`) already filed here, from trusted `xhandoff-in` markers."""
    found = set()
    for i in issues:
        for m in ns.trusted([{"id": 0, "user": i.get("user"), "body": i.get("body") or ""}], gh.trusted, "xhandoff-in"):
            found.add(str(m.get("src") or ""))
    return found


def incoming(gh: GH, cfg: Config, repo: str, now: dt.datetime) -> tuple[list[ns.Marker], str]:
    """(xhandoff markers addressed to this repo on `repo`'s night log, '' or why none were read)."""
    other = other_config(gh, repo)
    if other is None:
        return [], ""  # not a Night Shift repo (or unreadable): a plain cross-repo dependency, nothing to say
    if not cfg.bot_login or not _same(str(other.get("bot_login") or ""), cfg.bot_login):
        return [], f"{repo} runs as {other.get('bot_login') or '(no bot_login)'}, not {cfg.bot_login or '(unset)'}"
    if not any(_same(str(r), gh.repo) for r in other.get("cross_repos") or []):
        return [], f"{repo} does not list {gh.repo} in its cross_repos"
    label = str(((other.get("labels") or {}).get("night_log")) or DEFAULT_LABELS["night_log"])
    logs = gh.get(f"repos/{repo}/issues", {"labels": label, "state": "open", "per_page": 5}) or []
    logs = [i for i in logs if "pull_request" not in i and ns.author(i) == cfg.bot_login]
    if not logs:
        return [], ""
    since = (now - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = gh.paginate(f"repos/{repo}/issues/{logs[0]['number']}/comments", {"since": since})
    # Repo writers can edit a bot comment, and the bot never edits a hand-off: an edited one is not trusted.
    comments = [c for c in comments if not c.get("updated_at") or c.get("updated_at") == c.get("created_at")]
    return [m for m in ns.trusted(comments, cfg.bot_login, "xhandoff") if _same(str(m.get("to") or ""), gh.repo)], ""


def receive(gh: GH, cfg: Config, lane: str, now: dt.datetime | None = None) -> list[str]:
    """File the hand-offs other Night Shift repos sent here that belong to `lane`. Returns log lines."""
    from . import planner

    if not cfg.cross_repos:
        return []
    now = now or dt.datetime.now(dt.timezone.utc)
    lines: list[str] = []
    issues: list[dict] | None = None
    done: set[str] = set()
    filed = 0
    for repo in [r for r in cfg.cross_repos if not _same(r, gh.repo)][:MAX_REPOS]:
        try:
            markers, why = incoming(gh, cfg, repo, now)
        except GHError as err:
            lines.append(f"::warning::cross-repo hand-offs from {repo} not read: {err}")
            continue
        if why:
            if lane == cfg.effective_fix_lane:  # say it once per run, not once per lane
                lines.append(f"::notice::hand-offs from {repo} are not accepted: {why}")
            continue
        for m in markers:
            raw = m.get("ticket") if isinstance(m.get("ticket"), dict) else {}
            src = f"{repo}#c{m.comment_id}"
            target = resolve_lane(cfg, str(raw.get("lane") or ""), [str(f) for f in raw.get("files") or []][:20])
            if target != lane:
                continue
            if issues is None:
                issues = gh.issues(state="all")
                done = received(gh, issues)
            if src in done:
                continue
            if filed >= cfg.planner_max_tickets:
                lines.append(f"more hand-offs wait: at most {cfg.planner_max_tickets} filed per run")
                return lines
            t = planner.Ticket.from_dict({**raw, "key": "xhandoff", "lane": lane, "blocked_by": [], "repo": ""})
            ok, notes = planner.validate_plan([t], cfg, issues)
            if not ok:
                lines.append(f"::warning::hand-off {src} not filed: {'; '.join(notes)}")
                done.add(src)  # warn once per run; it stays unfiled until the sender sends a fixed one
                continue
            t, human = ok[0]
            if not planner.ANY_ID_RE.match(t.title):
                t.title = f"{planner.next_id(issues, cfg.id_prefix(lane))} {t.title}"
            prs = str(m.get("prs") or "")
            body = planner.render(t, {}) + "\n" + "\n".join([
                f"Handed off from {repo} by its reduce pass over PR(s) "
                f"{', '.join(repo + '#' + p for p in prs.split(',') if p.strip().isdigit()) or '(not given)'}: "
                "its code changed in a way this repository has to follow.",
                ns.marker("xhandoff-in", src=src, prs=prs)])
            labels = [f"lane:{lane}", f"wave:{t.wave}", cfg.label("human") if human else cfg.ready_label]
            created = gh.create_issue(t.title, body, labels)
            issues.append({**(created if isinstance(created, dict) else {}), "title": t.title, "body": body,
                           "state": "open", "user": {"login": gh.login}, "labels": [{"name": x} for x in labels]})
            done.add(src)
            filed += 1
            lines.append(f"filed hand-off from {repo} as #{(created or {}).get('number', 0)} [{lane}] {t.title}"
                         + (" (human: touches protected/contract paths)" if human else ""))
    return lines

