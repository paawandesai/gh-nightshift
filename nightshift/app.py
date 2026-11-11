"""GitHub App auth, as an alternative to a PAT.

The App's private key is a repository secret that only the PAT-equivalent jobs (pick, publish, planner create,
digest, commands) receive. Each of those jobs mints a short-lived installation token here, down-scoped to this
repository and to exactly the permissions night shift needs, and never the Workflows permission. The agent job
never receives the key or the token.

The JWT is signed RS256 with the `openssl` binary (present on GitHub's runners), so the tool stays stdlib-only.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
# Exactly what night shift uses. `workflows` is never requested, so the token cannot change .github/workflows/.
PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write", "actions": "read",
               "checks": "read", "statuses": "read", "metadata": "read"}
FORBIDDEN = {"workflows", "administration", "secrets", "environments", "organization_administration"}
# Asked for only by the job that files red-team advisories (`app-token --advisories`); every other token lacks it.
ADVISORIES = {"repository_advisories": "write"}


class AppError(RuntimeError):
    pass


def manifest(name: str = "night-shift", url: str = "https://github.com/paawandesai/gh-nightshift") -> dict:
    """App manifest for https://github.com/settings/apps/new (or an org's), with the minimum permissions."""
    return {
        "name": name,
        "url": url,
        "public": False,
        "default_permissions": {**PERMISSIONS, **ADVISORIES},
        "default_events": [],
        "hook_attributes": {"active": False},
        "description": "Runs gh-nightshift lanes: opens and merges agent PRs, comments on issues. No Workflows access.",
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def openssl_sign(message: bytes, key_pem: str, run: Callable[..., Any] = subprocess.run) -> bytes:
    fd, path = tempfile.mkstemp(prefix="ns-app-", suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key_pem)
        result = run(["openssl", "dgst", "-sha256", "-sign", path], input=message, capture_output=True)
        if result.returncode != 0:
            raise AppError("openssl could not sign with the App private key (is it a PEM RSA key?)")
        return result.stdout
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def jwt(app_id: str, key_pem: str, now: float | None = None, sign: Callable[[bytes, str], bytes] | None = None) -> str:
    now = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64((sign or openssl_sign)(signing, key_pem))}"


def http(method: str, path: str, token: str, body: Any = None) -> Any:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "gh-nightshift", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raise AppError(f"GitHub returned HTTP {err.code} for {method} {path}") from None


def mint(repo: str, app_id: str, key_pem: str, request: Callable[..., Any] | None = None,
         sign: Callable[[bytes, str], bytes] | None = None, advisories: bool = False) -> dict:
    """Return {"token", "login", "permissions", "expires_at"} for an installation token limited to `repo`."""
    if not app_id or not key_pem.strip():
        raise AppError("the App id and private key are both required")
    request = request or http  # looked up at call time, so tests can patch it
    owner, _, name = repo.partition("/")
    wanted = {**PERMISSIONS, **(ADVISORIES if advisories else {})}
    token = jwt(app_id, key_pem, sign=sign)
    app = request("GET", "/app", token) or {}
    inst = request("GET", f"/repos/{owner}/{name}/installation", token) or {}
    if not inst.get("id"):
        raise AppError(f"the App is not installed on {repo}")
    data = request("POST", f"/app/installations/{inst['id']}/access_tokens", token,
                   {"repositories": [name], "permissions": wanted}) or {}
    perms = data.get("permissions") or {}
    # Allowlist: nothing beyond what was requested, at no higher level.
    rank = {"read": 1, "write": 2, "admin": 3}
    bad = sorted(k for k, v in perms.items()
                 if k in FORBIDDEN or k not in wanted or rank.get(v, 9) > rank[wanted[k]])
    if bad:
        raise AppError(f"the minted token has permissions beyond what night shift asked for: {', '.join(bad)}")
    if not data.get("token"):
        raise AppError("GitHub returned no installation token")
    return {"token": data["token"], "login": f"{app.get('slug', '')}[bot]" if app.get("slug") else "",
            "permissions": perms, "expires_at": data.get("expires_at", "")}


READ_ONLY = {"metadata": "read", "issues": "read"}


def installations(app_id: str, key_pem: str, request: Callable[..., Any] | None = None,
                  sign: Callable[[bytes, str], bytes] | None = None) -> dict:
    """Where the App is installed and when it last acted: {"slug", "installations": [{account, type,
    selection, created, suspended, repos: [{name, private, last_comment}]}]}. Each installation gets a read-only
    token (metadata and issues) to list its repositories and find the App's newest issue or PR comment in each."""
    if not app_id or not key_pem.strip():
        raise AppError("the App id and private key are both required")
    request = request or http
    token = jwt(app_id, key_pem, sign=sign)
    app = request("GET", "/app", token) or {}
    login = f"{app.get('slug', '')}[bot]"
    out = []
    found: list[dict] = []
    for page in range(1, 11):
        batch = request("GET", f"/app/installations?per_page=100&page={page}", token) or []
        found += batch
        if len(batch) < 100:
            break
    for inst in found:
        account = inst.get("account") or {}
        row = {"account": account.get("login", "?"), "type": account.get("type", "?"),
               "selection": inst.get("repository_selection", "?"), "created": inst.get("created_at", ""),
               "suspended": inst.get("suspended_at") or "", "repos": []}
        if not row["suspended"] and inst.get("id"):
            try:
                data = request("POST", f"/app/installations/{inst['id']}/access_tokens", token,
                               {"permissions": dict(READ_ONLY)}) or {}
            except AppError as err:
                row["error"] = str(err)
                out.append(row)
                continue
            perms = data.get("permissions") or {}
            if any(v != "read" for v in perms.values()):
                raise AppError("the read-only listing token came back with write permissions")
            itoken = data.get("token") or ""
            repos: list[dict] = []
            for page in range(1, 11):
                listing = request("GET", f"/installation/repositories?per_page=100&page={page}", itoken) or {}
                repos += listing.get("repositories") or []
                if len(listing.get("repositories") or []) < 100:
                    break
            for repo in repos:
                name = repo.get("full_name", "?")
                try:
                    comments = request("GET", f"/repos/{name}/issues/comments?sort=created&direction=desc&per_page=100",
                                       itoken) or []
                except AppError:
                    comments = []
                mine = [c.get("created_at", "") for c in comments if (c.get("user") or {}).get("login") == login]
                row["repos"].append({"name": name, "private": bool(repo.get("private")),
                                     "last_comment": max(mine) if mine else ""})
        out.append(row)
    return {"slug": app.get("slug", ""), "installations": out}


def write_github_env(path: str, result: dict) -> None:
    """Mask the token in the log, then expose it to later steps of this job only."""
    print(f"::add-mask::{result['token']}")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"NIGHT_SHIFT_APP_TOKEN={result['token']}\n")
        if result.get("login"):
            f.write(f"NIGHT_SHIFT_LOGIN={result['login']}\n")
