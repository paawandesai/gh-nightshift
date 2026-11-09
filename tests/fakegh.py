"""An in-memory GitHub for tests: a REST transport `(method, path, body) -> JSON` over a tiny repo model.

Only the endpoints gh-nightshift calls are implemented; anything else raises so a test notices.
Every call is recorded in `calls` as (method, path, body).
"""
from __future__ import annotations

import copy
import itertools
import re
import urllib.parse

from nightshift.gh import GH, GHError

BOT = "night-bot"
REPO = "o/r"


def label_objs(names):
    return [{"name": n} for n in names]


class FakeGitHub:
    def __init__(self, login: str = BOT, repo: str = REPO):
        self.login = login
        self.repo = repo
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.pulls: dict[int, dict] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self.statuses: dict[str, list[dict]] = {}
        self.behind: dict[str, int] = {}
        self.update_branch_error: dict[int, tuple[int, str]] = {}
        self.merge_error: dict[int, tuple[int, str]] = {}
        self.workflow_runs: list[dict] = []
        self.runs_error: int | None = None
        self.labels: dict[str, dict] = {}
        self.variables: dict[str, str] = {}
        self.secrets: set[str] = set()
        self.protection: dict | None = None
        # The public gh-nightshift tool repo and its refs, as `doctor` sees them.
        self.tool_repo = {"full_name": "paawandesai/gh-nightshift", "private": False}
        self.tool_refs = {"main", "v0.1.0", "v1.0.0"}
        self.rulesets: list[dict] = []
        self.repo_settings: dict = {"delete_branch_on_merge": True, "allow_squash_merge": True,
                                    "default_branch": "main"}
        self.refs: set[str] = set()
        self.permissions: dict[str, str] = {}
        self.commits_by_path: dict[str, list[dict]] = {}
        self.jobs: dict[int, list[dict]] = {}
        self.job_logs: dict[int, str] = {}
        self.calls: list[tuple[str, str, object]] = []
        self._ids = itertools.count(1000)

    # --- building state ----------------------------------------------------------------
    def next_number(self) -> int:
        return max([0, *self.issues, *self.pulls]) + 1

    def add_issue(self, number=None, title="t", body="", labels=(), state="open", assignees=(), user="alice"):
        number = number or self.next_number()
        self.issues[number] = {"number": number, "title": title, "body": body, "labels": label_objs(labels),
                               "state": state, "assignees": [{"login": a} for a in assignees],
                               "user": {"login": user}, "html_url": f"https://github.com/{self.repo}/issues/{number}",
                               "comments": 0}
        self.comments.setdefault(number, [])
        return self.issues[number]

    def add_comment(self, number, body, user=None, association="NONE"):
        c = {"id": next(self._ids), "body": body, "user": {"login": user or self.login},
             "author_association": association}
        self.comments.setdefault(number, []).append(c)
        if number in self.issues:
            self.issues[number]["comments"] = len(self.comments[number])
        return c

    def add_pull(self, number=None, branch="agent/api/issue-1", sha="sha1", state="open", labels=("agent",),
                 user=None, merged_at=None, body="", created_at="2099-01-01T00:00:00Z", mergeable=True,
                 mergeable_state="clean", title="pr"):
        number = number or self.next_number()
        self.pulls[number] = {"number": number, "title": title, "state": state, "body": body,
                              "head": {"ref": branch, "sha": sha, "repo": {"full_name": self.repo}},
                              "base": {"ref": "main"}, "labels": label_objs(labels),
                              "user": {"login": user or self.login}, "merged_at": merged_at,
                              "created_at": created_at, "mergeable": mergeable, "mergeable_state": mergeable_state,
                              "html_url": f"https://github.com/{self.repo}/pull/{number}",
                              "merge_commit_sha": f"m{number}"}
        self.comments.setdefault(number, [])
        self.refs.add(branch)
        return self.pulls[number]

    def gh(self, dry_run=False, out=None) -> GH:
        g = GH(self.repo, transport=self, dry_run=dry_run, out=out)
        return g

    # --- inspection ----------------------------------------------------------------------
    def writes(self, method=None, pattern=""):
        return [(m, p, b) for m, p, b in self.calls if m != "GET" and (method is None or m == method)
                and re.search(pattern, p)]

    def bodies(self, number):
        return [c["body"] for c in self.comments.get(number, [])]

    def label_names(self, number):
        return {lbl["name"] for lbl in self.issues[number]["labels"]}

    # --- transport -------------------------------------------------------------------------
    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        parsed = urllib.parse.urlparse(path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        p = parsed.path
        prefix = f"repos/{self.repo}"
        if p == "user":
            return {"login": self.login}
        if p.startswith("repos/paawandesai/gh-nightshift") and method == "GET":
            tail = p[len("repos/paawandesai/gh-nightshift"):]
            if not tail and self.tool_repo:
                return self.tool_repo
            if tail.startswith("/commits/") and tail[len("/commits/"):] in self.tool_refs:
                return {"sha": "0" * 40}
            raise GHError(f"not found: {path}", 404)
        if not p.startswith(prefix):
            raise GHError(f"unexpected path {path}", 404)
        rest = p[len(prefix):].lstrip("/")
        for pattern, handler in self.routes():
            m = re.fullmatch(pattern, rest)
            if m and handler[0] == method:
                return handler[1](q, body, *m.groups())
        raise GHError(f"fake GitHub has no route for {method} {path}", 404)

    def routes(self):
        return [
            (r"", ("GET", lambda q, b: {**self.repo_settings, "full_name": self.repo})),
            (r"", ("PATCH", lambda q, b: self.repo_settings.update(b) or self.repo_settings)),
            (r"issues", ("GET", self._list_issues)),
            (r"issues", ("POST", self._create_issue)),
            (r"issues/(\d+)", ("GET", self._get_issue)),
            (r"issues/(\d+)", ("PATCH", self._patch_issue)),
            (r"issues/(\d+)/comments", ("GET", self._list_comments)),
            (r"issues/(\d+)/comments", ("POST", self._post_comment)),
            (r"issues/comments/(\d+)", ("PATCH", self._edit_comment)),
            (r"issues/(\d+)/labels", ("POST", self._add_labels)),
            (r"issues/(\d+)/labels/(.+)", ("DELETE", self._remove_label)),
            (r"pulls", ("GET", self._list_pulls)),
            (r"pulls", ("POST", self._create_pull)),
            (r"pulls/(\d+)", ("GET", lambda q, b, n: self._pull(int(n)))),
            (r"pulls/(\d+)", ("PATCH", self._patch_pull)),
            (r"pulls/(\d+)/merge", ("PUT", self._merge)),
            (r"pulls/(\d+)/update-branch", ("PUT", self._update_branch)),
            (r"commits/([^/]+)/check-runs", ("GET", lambda q, b, s: self._page(q, self.check_runs.get(s, []),
                                                                                "check_runs"))),
            (r"commits/([^/]+)/status", ("GET", lambda q, b, s: {"statuses": self.statuses.get(s, [])})),
            (r"compare/([^.]+)\.\.\.(.+)", ("GET", lambda q, b, base, head: {"behind_by": self.behind.get(head, 0)})),
            (r"git/refs/heads/(.+)", ("DELETE", self._delete_ref)),
            (r"actions/workflows/([^/]+)/runs", ("GET", self._runs)),
            (r"actions/runs/(\d+)/jobs", ("GET", lambda q, b, r: {"jobs": self.jobs.get(int(r), [])})),
            (r"actions/jobs/(\d+)/logs", ("GET", lambda q, b, j: self.job_logs.get(int(j), ""))),
            (r"labels", ("GET", lambda q, b: self._page(q, list(self.labels.values())))),
            (r"labels", ("POST", lambda q, b: self.labels.setdefault(b["name"], dict(b)))),
            (r"labels/(.+)", ("PATCH", lambda q, b, n: self.labels[urllib.parse.unquote(n)].update(b))),
            (r"actions/variables/([A-Z_]+)", ("GET", self._get_var)),
            (r"actions/variables", ("POST", lambda q, b: self.variables.__setitem__(b["name"], b["value"]))),
            (r"actions/variables/([A-Z_]+)", ("PATCH", lambda q, b, n: self.variables.__setitem__(n, b["value"]))),
            (r"actions/secrets", ("GET", lambda q, b: self._page(q, [{"name": s} for s in sorted(self.secrets)],
                                                                 "secrets"))),
            (r"branches/([^/]+)/protection", ("GET", self._get_protection)),
            (r"branches/([^/]+)/protection", ("PUT", lambda q, b, br: setattr(self, "protection", self._prot(b)))),
            (r"rulesets", ("GET", lambda q, b: self.rulesets)),
            (r"rulesets", ("POST", lambda q, b: self.rulesets.append(b))),
            (r"collaborators/([^/]+)/permission", ("GET", lambda q, b, u: {"permission": self.permissions.get(u, "read")})),
            (r"commits", ("GET", lambda q, b: self.commits_by_path.get(q.get("path", ""), [])[: int(q.get("per_page", 30))])),
        ]

    # --- handlers --------------------------------------------------------------------------
    @staticmethod
    def _page(q, items, key=None):
        per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
        chunk = items[(page - 1) * per: page * per]
        return {key: chunk, "total_count": len(items)} if key else chunk

    def _list_issues(self, q, b):
        state = q.get("state", "open")
        want = [x for x in q.get("labels", "").split(",") if x]
        items = [copy.deepcopy(i) for _, i in sorted(self.issues.items())
                 if (state == "all" or i["state"] == state)
                 and all(w in {lbl["name"] for lbl in i["labels"]} for w in want)]
        return self._page(q, items)

    def _create_issue(self, q, b):
        issue = self.add_issue(title=b["title"], body=b.get("body", ""), labels=b.get("labels", []),
                               user=self.login)
        return copy.deepcopy(issue)

    def _get_issue(self, q, b, n):
        n = int(n)
        if n in self.issues:
            return copy.deepcopy(self.issues[n])
        if n in self.pulls:
            return {**copy.deepcopy(self.pulls[n]), "pull_request": {}}
        raise GHError("Not Found (HTTP 404)", 404)

    def _patch_issue(self, q, b, n):
        self.issues[int(n)].update(b)
        return copy.deepcopy(self.issues[int(n)])

    def _list_comments(self, q, b, n):
        return self._page(q, copy.deepcopy(self.comments.get(int(n), [])))

    def _post_comment(self, q, b, n):
        return dict(self.add_comment(int(n), b["body"], user=self.login, association="MEMBER"))

    def _edit_comment(self, q, b, cid):
        for cs in self.comments.values():
            for c in cs:
                if c["id"] == int(cid):
                    c["body"] = b["body"]
                    return dict(c)
        raise GHError("Not Found (HTTP 404)", 404)

    def _add_labels(self, q, b, n):
        target = self.issues.get(int(n)) or self.pulls[int(n)]
        names = {lbl["name"] for lbl in target["labels"]}
        for name in b["labels"]:
            if name not in names:
                target["labels"].append({"name": name})
        return target["labels"]

    def _remove_label(self, q, b, n, name):
        name = urllib.parse.unquote(name)
        target = self.issues.get(int(n)) or self.pulls[int(n)]
        before = len(target["labels"])
        target["labels"] = [x for x in target["labels"] if x["name"] != name]
        if len(target["labels"]) == before:
            raise GHError("Label does not exist (HTTP 404)", 404)

    def _list_pulls(self, q, b):
        state = q.get("state", "open")
        head = q.get("head")
        items = []
        for _, pr in sorted(self.pulls.items(), reverse=q.get("direction") == "desc"):
            if state != "all" and pr["state"] != state:
                continue
            if head and f"{self.repo.split('/')[0]}:{pr['head']['ref']}" != head:
                continue
            items.append(copy.deepcopy(pr))
        return self._page(q, items)

    def _pull(self, n):
        if n not in self.pulls:
            raise GHError("Not Found (HTTP 404)", 404)
        return copy.deepcopy(self.pulls[n])

    def _create_pull(self, q, b):
        if any(p["head"]["ref"] == b["head"] and p["state"] == "open" for p in self.pulls.values()):
            raise GHError("A pull request already exists (HTTP 422)", 422)
        pr = self.add_pull(branch=b["head"], sha=f"sha-{b['head']}", labels=(), title=b["title"], body=b["body"])
        return copy.deepcopy(pr)

    def _patch_pull(self, q, b, n):
        self.pulls[int(n)].update(b)
        return copy.deepcopy(self.pulls[int(n)])

    def _merge(self, q, b, n):
        n = int(n)
        if n in self.merge_error:
            status, msg = self.merge_error[n]
            raise GHError(f"{msg} (HTTP {status})", status)
        pr = self.pulls[n]
        pr.update({"state": "closed", "merged_at": "2099-01-01T01:00:00Z"})
        return {"merged": True}

    def _update_branch(self, q, b, n):
        n = int(n)
        if n in self.update_branch_error:
            status, msg = self.update_branch_error[n]
            raise GHError(f"{msg} (HTTP {status})", status)
        pr = self.pulls[n]
        pr["head"]["sha"] = pr["head"]["sha"] + "-updated"
        return {"message": "Updating pull request branch.", "url": pr["html_url"]}

    def _delete_ref(self, q, b, ref):
        if ref not in self.refs:
            raise GHError("Reference does not exist (HTTP 422)", 422)
        self.refs.discard(ref)

    def _runs(self, q, b, wf):
        if self.runs_error:
            raise GHError(f"HTTP {self.runs_error}", self.runs_error)
        runs = self.workflow_runs
        if q.get("status"):
            runs = [r for r in runs if r.get("conclusion") == q["status"]]
        return {"workflow_runs": runs[: int(q.get("per_page", 30))]}

    def _get_var(self, q, b, name):
        if name not in self.variables:
            raise GHError("Not Found (HTTP 404)", 404)
        return {"name": name, "value": self.variables[name]}

    def _get_protection(self, q, b, br):
        if self.protection is None:
            raise GHError("Branch not protected (HTTP 404)", 404)
        return self.protection

    @staticmethod
    def _prot(body):
        out = copy.deepcopy(body)
        out["enforce_admins"] = {"enabled": bool(body.get("enforce_admins"))}
        return out
