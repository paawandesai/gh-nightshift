"""Comment commands on issues and agent PRs, from people with write access:

  /night-shift take [#n]    a person takes the ticket: assigned to them, labelled `human`, agents skip it
  /night-shift retry [#n]   back in the queue with fresh attempts (blocked/needs-human/human/frozen removed)

Run by the commands workflow with the PAT; the comment body arrives through an environment variable."""
from __future__ import annotations

import re

from . import protocol as ns
from .config import Config
from .gh import GH, GHError
from .rules import BRANCH_RE, label_names, lanes_of

COMMAND_RE = re.compile(r"\A\s*/night-shift\s+(take|retry)\b(?:\s+#(\d+))?", re.I)
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")


def parse(body: str) -> tuple[str, int | None] | None:
    m = COMMAND_RE.match(body or "")
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2)) if m.group(2) else None


def can_command(gh: GH, login: str) -> bool:
    if not LOGIN_RE.fullmatch(login or ""):
        return False
    data = gh.get_or_none(gh.r(f"collaborators/{login}/permission")) or {}
    return data.get("permission") in {"admin", "maintain", "write"}


def _target(gh: GH, number: int, named: int | None) -> int:
    """The issue a command is about: `#n` if given; on an agent PR, the PR's issue; else this issue."""
    if named:
        return named
    pr = gh.get_or_none(gh.r(f"pulls/{number}"))
    if pr:
        m = BRANCH_RE.match((pr.get("head") or {}).get("ref", ""))
        if m:
            return int(m.group(2))
    return number


def take(gh: GH, cfg: Config, issue: dict, author: str) -> str:
    num = issue["number"]
    gh.add_labels(num, [cfg.label("human")])
    gh.remove_label(num, cfg.ready_label)
    gh.post(gh.r(f"issues/{num}/assignees"), {"assignees": [author]})
    gh.comment(num, f"{ns.marker('take', issue=num, by=author)}\nNight shift: @{author} took #{num}; agents skip it "
                    "now. Comment `/night-shift retry` to hand it back to the night shift.")
    return f"#{num} taken by {author}"


def retry(gh: GH, cfg: Config, issue: dict, author: str) -> str:
    num = issue["number"]
    if issue.get("state") == "closed":
        gh.patch(gh.r(f"issues/{num}"), {"state": "open"})
    for key in ("blocked", "needs_human", "human"):
        if cfg.label(key) in label_names(issue):
            gh.remove_label(num, cfg.label(key))
    if "frozen" in label_names(issue):
        gh.remove_label(num, "frozen")
    gh.add_labels(num, [cfg.ready_label])
    # Undo a `take`: unassign the people the night shift assigned (trusted take markers only).
    taken = {m.get("by") for m in ns.trusted(gh.comments(num), gh.trusted, "take")}
    assigned = {a.get("login") for a in issue.get("assignees") or []}
    drop = sorted(x for x in taken & assigned if isinstance(x, str))
    if drop:
        gh.call("DELETE", gh.r(f"issues/{num}/assignees"), {"assignees": drop})
    # A trusted `retry` marker resets the attempt count (protocol.count), like a block notice does.
    gh.comment(num, f"{ns.marker('retry', issue=num, by=author)}\nNight shift: @{author} put #{num} back in the "
                    f"queue with fresh attempts ({cfg.max_attempts}). Hints in their comment above reach the agent.")
    return f"#{num} re-queued by {author}"


def handle(gh: GH, cfg: Config, author: str, body: str, number: int) -> str:
    cmd = parse(body)
    if cmd is None:
        return "skip: not a /night-shift take or retry command"
    action, named = cmd
    if author == gh.login or author.endswith("[bot]"):
        return "skip: commands from the night shift's own account are ignored"
    if not can_command(gh, author):
        return f"skip: /night-shift {action} needs a comment from a user with write access (got {author!r})"
    target = _target(gh, number, named)
    try:
        issue = gh.issue(target)
    except GHError:
        issue = None
    if not issue or "pull_request" in issue:
        gh.comment(number, f"Night shift: #{target} is not an issue; nothing done.")
        return f"skip: #{target} is not an issue"
    lanes = {lane.name for lane in cfg.lanes}
    if not set(lanes_of(issue)) & lanes:
        gh.comment(number, f"Night shift: #{target} has no lane label, so it is not a night-shift ticket.")
        return f"skip: #{target} has no lane label"
    if action == "take":
        if issue.get("state") == "closed":
            gh.comment(number, f"Night shift: #{target} is closed; nothing to take.")
            return f"skip: #{target} is closed"
        return take(gh, cfg, issue, author)
    return retry(gh, cfg, issue, author)
