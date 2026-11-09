"""CODEOWNERS: which agent PRs need a code owner's review (risk.codeowners), and which owners a lane maps to.

GitHub's syntax: one pattern and its owners per line, the last matching line wins, `#` starts a comment, and a
line with a pattern but no owners removes ownership. Patterns follow .gitignore rules (subset below)."""
from __future__ import annotations

import base64
import re

from .gh import GH, GHError

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def parse(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("\\#") else line.strip()
        if not line:
            continue
        parts = line.split()
        rules.append((parts[0], [o for o in parts[1:] if o.startswith("@") or "@" in o]))
    return rules


def _regex(pattern: str) -> re.Pattern:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    directory = p.endswith("/")
    p = p.rstrip("/")
    out = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif p.startswith("/**", i) and i + 3 == len(p):
            out += r"/.*"
            i += 3
        elif p.startswith("**", i):
            out += r".*"
            i += 2
        elif p[i] == "*":
            out += r"[^/]*"
            i += 1
        elif p[i] == "?":
            out += r"[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    prefix = "" if anchored else r"(?:.*/)?"
    # A directory pattern matches everything below it; a plain name matches a file or a directory of that name.
    suffix = r"/.*" if directory else r"(?:/.*)?"
    return re.compile(rf"\A{prefix}{out}{suffix}\Z")


def owners_for(rules: list[tuple[str, list[str]]], path: str) -> list[str]:
    found: list[str] = []
    for pattern, owners in rules:
        if pattern == "*" or _regex(pattern).match(path.lstrip("/")):
            found = owners
    return found


def fetch(gh: GH, ref: str = "") -> list[tuple[str, list[str]]]:
    """CODEOWNERS from the default branch (never from the PR: an agent could rewrite it)."""
    for loc in LOCATIONS:
        try:
            data = gh.get_or_none(gh.r(f"contents/{loc}"), {"ref": ref} if ref else None)
        except GHError:
            data = None
        if data and data.get("content"):
            return parse(base64.b64decode(data["content"]).decode("utf-8", "replace"))
    return []


def user_owners(owners: list[str]) -> set[str] | None:
    """Logins that may approve, or None when a team or email owner is listed (then any writer counts, because a
    repo-scoped token cannot read team membership)."""
    users = set()
    for o in owners:
        if not o.startswith("@") or "/" in o:
            return None
        users.add(o[1:].lower())
    return users
