"""The only door to GitHub: REST calls through `gh api` (no GraphQL anywhere).

Every call goes through a *transport* `(method, path, body) -> parsed JSON`. The real transport shells
out to `gh api`; tests pass a fake. In dry-run mode reads still happen (they change nothing) but every
write is recorded and printed as the exact `gh api` command that would have run, then answered with a
harmless placeholder so the calling code can keep going.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import urllib.parse
from typing import Any, TextIO
from collections.abc import Callable

Transport = Callable[[str, str, Any], Any]

WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
PER_PAGE = 100
MAX_PAGES = 20


class GHError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def with_query(path: str, params: dict[str, Any] | None) -> str:
    if not params:
        return path
    sep = "&" if "?" in path else "?"
    return path + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def command_for(method: str, path: str, body: Any = None) -> str:
    """The exact shell command for one call, copy-pasteable."""
    parts = ["gh", "api"]
    if method != "GET":
        parts += ["-X", method]
    parts.append(path)
    if body is None:
        return shlex.join(parts)
    parts += ["--input", "-"]
    return shlex.join(parts) + " <<'JSON'\n" + json.dumps(body, indent=2, sort_keys=True) + "\nJSON"


def subprocess_transport(method: str, path: str, body: Any = None) -> Any:
    args = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    stdin = None
    if body is not None:
        args += ["--input", "-"]
        stdin = json.dumps(body)
    result = subprocess.run(args, input=stdin, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        m = re.search(r"HTTP (\d{3})", err)
        raise GHError(f"gh api -X {method} {path}: {err}", int(m.group(1)) if m else None)
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out  # plain-text endpoints such as job logs


class GH:
    def __init__(self, repo: str, transport: Transport | None = None, dry_run: bool = False,
                 out: TextIO | None = None):
        self.repo = repo
        self.transport = transport or subprocess_transport
        self.dry_run = dry_run
        self.out = out or sys.stdout
        self.planned: list[tuple[str, str, Any]] = []
        self._login: str | None = None
        self.trusted_extra: set[str] = set()  # config trusted_logins: other PATs whose markers count

    # --- raw ---------------------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None) -> Any:
        method = method.upper()
        if self.dry_run and method in WRITE_METHODS:
            self.planned.append((method, path, body))
            print(f"[dry-run] {command_for(method, path, body)}", file=self.out)
            return {"number": 0, "id": 0, "html_url": "(dry-run)", "dry_run": True}
        return self.transport(method, path, body)

    def r(self, suffix: str = "") -> str:
        """Path under this repo: r('issues') -> repos/o/r/issues."""
        return f"repos/{self.repo}" + (f"/{suffix}" if suffix else "")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.call("GET", with_query(path, params))

    def get_or_none(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.get(path, params)
        except GHError as err:
            if err.status == 404:
                return None
            raise

    def paginate(self, path: str, params: dict[str, Any] | None = None, key: str | None = None) -> list:
        """All items of a list endpoint. `key` names the list inside object responses (e.g. check_runs)."""
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            batch = (data or {}).get(key, []) if key else (data or [])
            items.extend(batch)
            if len(batch) < PER_PAGE:
                break
        return items

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body if body is not None else {})

    def patch(self, path: str, body: Any) -> Any:
        return self.call("PATCH", path, body)

    def put(self, path: str, body: Any = None) -> Any:
        return self.call("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path)

    # --- helpers used everywhere -------------------------------------------------------
    @property
    def login(self) -> str:
        """Account behind the token. Only markers it posted count, so outsiders can't forge them."""
        if self._login is None:
            try:
                self._login = (self.get("user") or {}).get("login", "")
            except GHError as err:
                if err.status != 403:
                    raise
                # GITHUB_TOKEN and App installation tokens cannot read /user. Trust only the config's
                # bot_login and trusted_logins then (callers that need a login check `trusted`).
                self._login = ""
        return self._login

    @login.setter
    def login(self, value: str) -> None:
        self._login = value

    @property
    def trusted(self) -> frozenset[str]:
        """Logins whose ns markers and PRs count: this PAT's own login plus config `trusted_logins`."""
        return frozenset({self.login, *self.trusted_extra} - {""})

    def issues(self, labels: list[str] | None = None, state: str = "open") -> list[dict]:
        """Issues only (the REST issues endpoint also returns pull requests)."""
        params: dict[str, Any] = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)
        return [i for i in self.paginate(self.r("issues"), params) if "pull_request" not in i]

    def issue(self, number: int) -> dict | None:
        return self.get_or_none(self.r(f"issues/{number}"))

    # Events whose runs test a PR's own head commit. Runs started by workflow_run, issue_comment, schedule or
    # workflow_dispatch (the night shift itself) are not the PR's CI.
    CI_EVENTS = frozenset({"pull_request", "pull_request_target", "push", "merge_group"})

    def check_runs(self, sha: str) -> list[dict]:
        """The check runs on a commit. Fine-grained PATs cannot be given the Checks permission (only GitHub Apps
        can), so on 403 this rebuilds the same list from Actions (Actions: Read): the jobs of every workflow run
        on that commit, which carry the same names, status and conclusion as their check runs. Checks posted by
        other apps are not visible that way; commit statuses still are."""
        try:
            return self.paginate(self.r(f"commits/{sha}/check-runs"), key="check_runs")
        except GHError as err:
            if err.status != 403:
                raise
        jobs: list[dict] = []
        for run in self.paginate(self.r("actions/runs"), {"head_sha": sha}, key="workflow_runs"):
            if run.get("event") in self.CI_EVENTS:
                jobs += self.paginate(self.r(f"actions/runs/{run['id']}/jobs"), key="jobs")
        return jobs

    def comments(self, number: int) -> list[dict]:
        return self.paginate(self.r(f"issues/{number}/comments"))

    def comment(self, number: int, body: str) -> dict:
        return self.post(self.r(f"issues/{number}/comments"), {"body": body})

    def edit_comment(self, comment_id: int, body: str) -> dict:
        return self.patch(self.r(f"issues/comments/{comment_id}"), {"body": body})

    def add_labels(self, number: int, labels: list[str]) -> Any:
        return self.post(self.r(f"issues/{number}/labels"), {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.delete(self.r(f"issues/{number}/labels/{urllib.parse.quote(label, safe='')}"))
        except GHError as err:
            if err.status != 404:
                raise

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self.post(self.r("issues"), {"title": title, "body": body, "labels": labels})

    def night_log(self, label: str = "night-log") -> int | None:
        found = self.get(self.r("issues"), {"labels": label, "state": "open", "per_page": 1}) or []
        found = [i for i in found if "pull_request" not in i]
        return found[0]["number"] if found else None
