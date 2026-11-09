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
PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write", "actions": "read", "metadata": "read"}
FORBIDDEN = {"workflows", "administration", "secrets", "environments", "organization_administration"}


class AppError(RuntimeError):
    pass


def manifest(name: str = "night-shift", url: str = "https://github.com/paawandesai/gh-nightshift") -> dict:
    """App manifest for https://github.com/settings/apps/new (or an org's), with the minimum permissions."""
    return {
        "name": name,
        "url": url,
        "public": False,
        "default_permissions": dict(PERMISSIONS),
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
         sign: Callable[[bytes, str], bytes] | None = None) -> dict:
    """Return {"token", "login", "permissions", "expires_at"} for an installation token limited to `repo`."""
    if not app_id or not key_pem.strip():
        raise AppError("the App id and private key are both required")
    request = request or http  # looked up at call time, so tests can patch it
    owner, _, name = repo.partition("/")
    token = jwt(app_id, key_pem, sign=sign)
    app = request("GET", "/app", token) or {}
    inst = request("GET", f"/repos/{owner}/{name}/installation", token) or {}
    if not inst.get("id"):
        raise AppError(f"the App is not installed on {repo}")
    data = request("POST", f"/app/installations/{inst['id']}/access_tokens", token,
                   {"repositories": [name], "permissions": dict(PERMISSIONS)}) or {}
    perms = data.get("permissions") or {}
    bad = sorted(set(perms) & FORBIDDEN)
    if bad:
        raise AppError(f"the minted token has forbidden permissions: {', '.join(bad)}")
    if not data.get("token"):
        raise AppError("GitHub returned no installation token")
    return {"token": data["token"], "login": f"{app.get('slug', '')}[bot]" if app.get("slug") else "",
            "permissions": perms, "expires_at": data.get("expires_at", "")}


def write_github_env(path: str, result: dict) -> None:
    """Mask the token in the log, then expose it to later steps of this job only."""
    print(f"::add-mask::{result['token']}")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"NIGHT_SHIFT_APP_TOKEN={result['token']}\n")
        if result.get("login"):
            f.write(f"NIGHT_SHIFT_LOGIN={result['login']}\n")
