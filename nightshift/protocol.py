"""The night-shift comment protocol: how lanes coordinate through GitHub comments.

Every machine-readable fact is a hidden marker inside an ordinary comment:

    <!-- ns:{"type":"handoff","from":12,"to":15,"pr":40} -->

and it is **trusted only when the comment was posted by the PAT's own login** (the account the lanes
publish as). Anyone else can write the same text; it is ignored, so outsiders can neither forge
attempt counts nor block, unblock or steer work.

Types
  attempt / infra / blocked / waiting   attempt bookkeeping on an issue (picker + publish)
  need      {"from": waiting issue, "to": new or existing issue, "lane": its lane}
  handoff   {"from": merged issue, "to": dependent issue, "pr": merged PR}  (step 3a)
  note      {"from": sender issue, "to": receiver issue}                     (step 3c)
  pr        {"reason": verdict_passed|verdict_failed|checks_failed|conflict|stale|superseded|manual,
             "pr": n, "issue": n}                                             (audit add-on D)
  handoff-data  in the body of a PR the PAT opened: the agent's `handoff` output, read at merge time
  handoff-done  on a merged PR once its hand-offs went out (idempotence)
  note-sent     on the sender issue, one per note, so the cap of notes per issue can be counted
  plan          on the night-log issue after a planner run: {"mode","lane","complete","docs_sha"}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MARKER_RE = re.compile(r"<!--\s*ns:(\{.*?\})\s*-->", re.S)
TYPES = {"attempt", "infra", "blocked", "waiting", "need", "handoff", "note", "pr", "handoff-done",
         "handoff-data", "note-sent", "fix-attempt", "fix-infra", "plan"}
PR_REASONS = ("verdict_passed", "verdict_failed", "checks_failed", "conflict", "stale", "superseded", "manual")

# Markers written by the original hackathon kit; still counted so a repo can migrate mid-flight.
LEGACY = {"attempt": "<!-- agent-attempt -->", "infra": "<!-- agent-infra -->", "blocked": "<!-- agent-blocked -->"}
LEGACY_BLOCK_TEXT = "marking blocked for a human"


@dataclass(frozen=True)
class Marker:
    type: str
    data: dict
    comment_id: int
    body: str

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def marker(type_: str, **fields: Any) -> str:
    if type_ not in TYPES:
        raise ValueError(f"unknown ns marker type {type_!r}")
    if type_ == "pr" and fields.get("reason") not in PR_REASONS:
        raise ValueError(f"unknown PR reason {fields.get('reason')!r}")
    payload = {"type": type_}
    payload.update({k: v for k, v in fields.items() if v not in (None, "")})
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # "--" may not appear inside an HTML comment; "-" is the same character to a JSON parser.
    return "<!-- ns:" + text.replace("--", "-\\u002d") + " -->"


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("--", "—").replace(">", "›")[:200]
    return value


def author(comment: dict) -> str:
    return (comment.get("user") or {}).get("login", "")


def is_trusted(comment: dict, bot_login: str) -> bool:
    """Trusted means posted by the PAT's own login. Nothing else, not even repo admins."""
    return bool(bot_login) and author(comment) == bot_login


def parse(comment: dict) -> list[Marker]:
    out = []
    body = comment.get("body") or ""
    for m in MARKER_RE.finditer(body):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") in TYPES:
            out.append(Marker(data["type"], data, int(comment.get("id") or 0), body))
    return out


def trusted(comments: list[dict], bot_login: str, type_: str | None = None) -> list[Marker]:
    """Trusted markers, oldest first, optionally of one type."""
    found: list[Marker] = []
    for c in sorted(comments, key=lambda c: c.get("id", 0)):
        if not is_trusted(c, bot_login):
            continue
        found += [m for m in parse(c) if type_ is None or m.type == type_]
    return found


def trusted_comments(comments: list[dict], bot_login: str) -> list[dict]:
    """Comments posted by the PAT login that carry at least one ns marker (shown to the next agent)."""
    return [c for c in sorted(comments, key=lambda c: c.get("id", 0))
            if is_trusted(c, bot_login) and parse(c)]


def _legacy_ids(comments: list[dict], bot_login: str, text: str) -> list[int]:
    return [c["id"] for c in comments if is_trusted(c, bot_login) and text in (c.get("body") or "")]


def count(comments: list[dict], bot_login: str, type_: str, **match: Any) -> int:
    """Markers of `type_` (matching `match` fields) since the latest block notice, so removing the
    `blocked` label (which posts nothing) and the picker's block notice reset the count."""
    ids = [m.comment_id for m in trusted(comments, bot_login, type_)
           if all(m.get(k) == v for k, v in match.items())]
    if type_ in LEGACY and not match:
        ids += _legacy_ids(comments, bot_login, LEGACY[type_])
    if not ids:
        return 0
    blocks = [m.comment_id for m in trusted(comments, bot_login, "blocked")]
    blocks += _legacy_ids(comments, bot_login, LEGACY["blocked"]) + _legacy_ids(comments, bot_login, LEGACY_BLOCK_TEXT)
    last_block = max(blocks, default=0)
    return sum(1 for i in set(ids) if i > last_block)


def latest_id(comments: list[dict], bot_login: str, type_: str) -> int | None:
    ids = [m.comment_id for m in trusted(comments, bot_login, type_)]
    if type_ in LEGACY:
        ids += _legacy_ids(comments, bot_login, LEGACY[type_])
    return max(ids) if ids else None


def pr_reason(comments: list[dict], bot_login: str) -> str | None:
    reasons = [m.get("reason") for m in trusted(comments, bot_login, "pr") if m.get("reason") in PR_REASONS]
    return reasons[-1] if reasons else None


# --- graph ----------------------------------------------------------------------------

def reachable(edges: dict[int, set[int]], start: int, target: int) -> bool:
    """True if `target` can be reached from `start` following dependency edges."""
    seen: set[int] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def would_cycle(edges: dict[int, set[int]], waiter: int, provider: int) -> bool:
    """Adding `waiter blocked by provider` closes a cycle if provider already (transitively) waits on waiter."""
    return waiter == provider or reachable(edges, provider, waiter)


def cycles(edges: dict) -> list[list]:
    """Simple cycles in a small graph (node lists, first node repeated at the end)."""
    found: list[list] = []
    keys: set[frozenset] = set()
    order = {n: i for i, n in enumerate(sorted(edges, key=str))}

    def walk(node, path):
        for nxt in sorted(edges.get(node, ()), key=str):
            if nxt == path[0]:
                key = frozenset(path)
                if key not in keys:
                    keys.add(key)
                    found.append(path + [nxt])
            elif nxt not in path and order.get(nxt, -1) > order[path[0]]:
                walk(nxt, path + [nxt])

    for start in sorted(edges, key=str):
        walk(start, [start])
    return found


# --- Blocked-by lines -----------------------------------------------------------------

BLOCKED_LINE = re.compile(r"^(?P<lead>\s*(?:-\s*)?blocked\s+by\s*:?\s*)(?P<rest>.*)$", re.I | re.M)


def add_blocked_by(body: str | None, number: int) -> str:
    """Append #number to the body's "Blocked by:" line (or add one). Idempotent."""
    body = body or ""
    m = BLOCKED_LINE.search(body)
    if m:
        rest = m.group("rest").strip()
        if re.search(rf"#{number}\b", rest):
            return body
        if not rest or rest.lower() in {"none", "n/a", "-", "nothing", "#"}:
            new = f"#{number}"
        else:
            new = f"{rest.rstrip('.')}, #{number}"
        return body[: m.start()] + m.group("lead") + new + body[m.end():]
    return body.rstrip("\n") + f"\n\nBlocked by: #{number}\n"


# --- comment bodies -------------------------------------------------------------------

def handoff_comment(merged_issue: int, merged_title: str, lane: str, pr: int, to: int,
                    handoff: dict | None, ticket_interface: str) -> str:
    """Posted on every open issue blocked by `merged_issue` once its PR merges (step 3a)."""
    h = handoff or {}
    lines = [marker("handoff", **{"from": merged_issue, "to": to, "pr": pr, "lane": lane}),
             f"**Night shift hand-off:** #{merged_issue} ({_clean(merged_title)}) merged in PR #{pr}"
             f"{f' (lane `{lane}`)' if lane else ''}. This issue no longer waits on it."]

    def block(title: str, items: Any) -> None:
        if isinstance(items, str):
            items = [items] if items.strip() else []
        items = [str(x) for x in items or [] if str(x).strip()]
        if items:
            lines.extend(["", f"**{title}:**", *[f"- {x}" for x in items]])

    if h:
        block("Interface as built (functions, endpoints)", h.get("interfaces"))
        block("Files", h.get("files"))
        block("Env vars", h.get("env_vars"))
        block("Decisions", h.get("decisions"))
        block("Assumptions", h.get("assumptions"))
    else:
        lines += ["", "**Interface as built:** not reported (the PR carried no hand-off); read the code on main "
                      "before relying on the ticket."]
    if ticket_interface:
        lines += ["", "**Interface the ticket specified:**", ticket_interface]
    diff = str(h.get("interface_differences") or "").strip()
    if diff and diff.lower().rstrip(".") not in {"none", "n/a", "no differences"}:
        lines += ["", f"⚠️ **Differs from the ticket:** {diff}"]
    elif h:
        lines += ["", "Differences from the ticket: none reported."]
    return "\n".join(lines)


def need_comment(waiter: int, provider: int, lane: str, title: str, ready: bool) -> str:
    state = "queued for that lane" if ready else "filed as `proposed` (a person must review it first)"
    return "\n".join([
        marker("need", **{"from": waiter, "to": provider, "lane": lane}),
        f"**Night shift: waiting on another lane.** This issue needs #{provider} ({_clean(title)}, lane `{lane}`), "
        f"{state}. `Blocked by: #{provider}` was added above; the picker resumes this issue once #{provider} closes. "
        "This attempt was not counted.",
    ])


def note_comment(sender: int, to: int, sender_lane: str, text: str) -> str:
    return "\n".join([marker("note", **{"from": sender, "to": to, "lane": sender_lane}),
                      f"**Night shift note from #{sender} (lane `{sender_lane}`):**", "", _quote(text)])


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in str(text)[:1500].splitlines()) or "> (empty)"
