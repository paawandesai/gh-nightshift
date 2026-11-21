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
import hashlib

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
        if not why:
            try:
                why += [w for w in [partner(gh, cfg, allowed[t.repo.lower()])[1]] if w]
            except GHError as err:
                why.append(f"cannot read {t.repo}: {err}")
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
        sent.append((f"handed off to {repo}: {data['title']}", t.key or ""))
    return sent, notes


# --- partners ------------------------------------------------------------------------------------------

def other_config(gh: GH, repo: str) -> dict | None:
    """The other repo's Night Shift config, reduced to what hand-offs need and type-checked (the other repo's
    writers control it, so a malformed file must never break this repo's picker). None: not a Night Shift repo
    (or unreadable)."""
    data = gh.get_or_none(f"repos/{repo}/contents/{CONFIG_PATH}")
    if not isinstance(data, dict) or not data.get("content"):
        return None
    try:
        parsed = yamlish.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, yamlish.YAMLError):
        return None
    if not isinstance(parsed, dict):
        return None
    labels = parsed.get("labels") if isinstance(parsed.get("labels"), dict) else {}
    repos = parsed.get("cross_repos") if isinstance(parsed.get("cross_repos"), list) else []
    return {"bot_login": str(parsed.get("bot_login") or "") if isinstance(parsed.get("bot_login"), str) else "",
            "cross_repos": [r for r in repos if isinstance(r, str)],
            "night_log": str(labels.get("night_log") or DEFAULT_LABELS["night_log"])}


def partner(gh: GH, cfg: Config, repo: str) -> tuple[dict | None, str]:
    """(its config, '') when `repo` trades hand-offs with this one, else (None or its config, why not)."""
    other = other_config(gh, repo)
    if other is None:
        return None, f"{repo} is not a Night Shift repo, or this token cannot read it"
    if not cfg.bot_login or not _same(other["bot_login"], cfg.bot_login):
        return other, f"{repo} runs as {other['bot_login'] or '(no bot_login)'}, not {cfg.bot_login or '(unset)'}"
    if not any(_same(r, gh.repo) for r in other["cross_repos"]):
        return other, f"{repo} does not list {gh.repo} in its cross_repos"
    return other, ""


# --- receiving side (picker) ---------------------------------------------------------------------------

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


def content_key(title: str) -> str:
    """Same hand-off, sent twice (two reduce runs): the same key, so it is filed once even after the first closes."""
    from .planner import norm_title

    return hashlib.sha256(norm_title(title).encode()).hexdigest()[:16]


def _issue_markers(gh: GH, issue: dict, type_: str) -> list[ns.Marker]:
    return ns.trusted([{"id": 0, "user": issue.get("user"), "body": issue.get("body") or ""}], gh.trusted, type_)


def received(gh: GH, issues: list[dict], skipped: list[dict] = ()) -> tuple[set[str], set[str]]:
    """(sources, content keys) already filed here (trusted `xhandoff-in` in ticket bodies) or turned down
    (trusted `xhandoff-skip` on this repo's night log)."""
    srcs: set[str] = set()
    keys: set[str] = set()
    for i in issues:
        for m in _issue_markers(gh, i, "xhandoff-in"):
            srcs.add(str(m.get("src") or ""))
            keys.add(str(m.get("key") or ""))
    for m in ns.trusted(list(skipped), gh.trusted, "xhandoff-skip"):
        srcs.add(str(m.get("src") or ""))
    return srcs, keys - {""}


def filed_since(gh: GH, issues: list[dict], since: dt.datetime) -> int:
    """Hand-off tickets filed here since `since` (any lane): the cap is per night, not per picker run."""
    n = 0
    for i in issues:
        stamp = i.get("created_at")
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else since
        if when >= since and _issue_markers(gh, i, "xhandoff-in"):
            n += 1
    return n


def incoming(gh: GH, cfg: Config, repo: str, now: dt.datetime) -> tuple[list[ns.Marker], str]:
    """(xhandoff markers addressed to this repo on `repo`'s night log, '' or why none were read)."""
    other, why = partner(gh, cfg, repo)
    if other is None:
        return [], ""  # a plain cross-repo dependency: nothing to say
    if why:
        return [], why
    # The same issue the sender posts on (gh.night_log: the first open one with the label); whoever opened it,
    # only the bot's own comments count.
    logs = gh.get(f"repos/{repo}/issues", {"labels": other["night_log"], "state": "open", "per_page": 1}) or []
    logs = [i for i in logs if isinstance(i, dict) and "pull_request" not in i]
    if not logs:
        return [], f"{repo} has no open {other['night_log']!r} issue"
    since = (now - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    comments = gh.paginate(f"repos/{repo}/issues/{logs[0]['number']}/comments", {"since": since})
    # Repo writers can edit a bot comment, and the bot never edits a hand-off: an edited one is not trusted.
    comments = [c for c in comments if isinstance(c, dict)
                and (not c.get("updated_at") or c.get("updated_at") == c.get("created_at"))]
    return [m for m in ns.trusted(comments, cfg.bot_login, "xhandoff") if _same(str(m.get("to") or ""), gh.repo)], ""


def receive(gh: GH, cfg: Config, lane: str, now: dt.datetime | None = None) -> list[str]:
    """File the hand-offs other Night Shift repos sent here that belong to `lane`. Returns log lines."""
    from . import planner

    if not cfg.cross_repos:
        return []
    now = now or dt.datetime.now(dt.timezone.utc)
    lines: list[str] = []
    issues: list[dict] | None = None
    log: int | None = None
    srcs: set[str] = set()
    keys: set[str] = set()
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
            files = [str(f) for f in raw.get("files") or [] if isinstance(f, str)][:20] \
                if isinstance(raw.get("files"), list) else []
            src = f"{repo}#c{m.comment_id}"
            if resolve_lane(cfg, str(raw.get("lane") or ""), files) != lane:
                continue
            if issues is None:
                issues = gh.issues(state="all")
                log = gh.night_log(cfg.label("night_log"))
                srcs, keys = received(gh, issues, gh.comments(log) if log else [])
            t = planner.Ticket.from_dict({**raw, "files": files, "key": "xhandoff", "lane": lane, "blocked_by": [],
                                          "repo": ""})
            key = content_key(t.title)
            if src in srcs or key in keys:
                continue
            if filed_since(gh, issues, now - dt.timedelta(hours=24)) >= cfg.planner_max_tickets:
                lines.append(f"more hand-offs wait: at most {cfg.planner_max_tickets} filed per 24 hours")
                return lines
            ok, notes = planner.validate_plan([t], cfg, issues)
            if not ok:
                why = "; ".join(notes)
                lines.append(f"::warning::hand-off {src} not filed: {why}")
                srcs.add(src)
                if log:  # recorded, so it is reported once, not on every run until it ages out
                    gh.comment(log, f"{ns.marker('xhandoff-skip', src=src)}\nNight shift: hand-off {src} was not "
                                    f"filed: {ns.defang(ns.one_line(why, 600))}")
                continue
            t, human = ok[0]
            if not planner.ANY_ID_RE.match(t.title):
                t.title = f"{planner.next_id(issues, cfg.id_prefix(lane))} {t.title}"
            prs = ",".join(p for p in str(m.get("prs") or "").split(",") if p.strip().isdigit())
            body = planner.render(t, {}) + "\n" + "\n".join([
                f"Handed off from {repo} by its reduce pass over PR(s) "
                f"{', '.join(repo + '#' + p for p in prs.split(',') if p) or '(not given)'}: "
                "its code changed in a way this repository has to follow.",
                ns.marker("xhandoff-in", src=src, prs=prs, key=key)])
            labels = [f"lane:{lane}", f"wave:{t.wave}", cfg.label("human") if human else cfg.ready_label]
            created = gh.create_issue(t.title, body, labels)
            issues.append({**(created if isinstance(created, dict) else {}), "title": t.title, "body": body,
                           "state": "open", "user": {"login": gh.login}, "labels": [{"name": x} for x in labels],
                           "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
            srcs.add(src)
            keys.add(key)
            lines.append(f"filed hand-off from {repo} as #{(created or {}).get('number', 0)} [{lane}] {t.title}"
                         + (" (human: touches protected/contract paths)" if human else ""))
    return lines
