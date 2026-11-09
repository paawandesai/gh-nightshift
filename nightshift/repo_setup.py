"""Repository setup over REST: labels, settings, variables, branch protection, night-log issue, secrets,
and the switches (start, pause, freeze, resume). Every write goes through GH, so --dry-run prints the
exact `gh api` call instead of making it."""
from __future__ import annotations

import getpass
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from collections.abc import Callable

from . import scaffold
from .config import Config
from .gh import GH, GHError
from .rules import label_names

FROZEN = "frozen"


# --- labels ---------------------------------------------------------------------------

def ensure_labels(gh: GH, cfg: Config) -> list[str]:
    existing = {lbl["name"]: lbl for lbl in gh.paginate(gh.r("labels"))}
    out = []
    for name, color, desc in scaffold.labels(cfg):
        cur = existing.get(name)
        if cur is None:
            gh.post(gh.r("labels"), {"name": name, "color": color, "description": desc})
            out.append(f"label created: {name}")
        elif cur.get("color") != color or (cur.get("description") or "") != desc:
            gh.patch(gh.r(f"labels/{urllib.parse.quote(name, safe='')}"), {"color": color, "description": desc})
            out.append(f"label updated: {name}")
    return out


# --- settings, variables, protection --------------------------------------------------

def repo_settings(gh: GH) -> list[str]:
    gh.patch(gh.r(), {"allow_auto_merge": True, "delete_branch_on_merge": True, "allow_squash_merge": True})
    return ["repo settings: auto-merge on, delete branch on merge, squash merge on"]


def get_variable(gh: GH, name: str) -> str | None:
    data = gh.get_or_none(gh.r(f"actions/variables/{name}"))
    return None if data is None else str(data.get("value", ""))


def set_variable(gh: GH, name: str, value: str) -> str:
    if get_variable(gh, name) is None:
        gh.post(gh.r("actions/variables"), {"name": name, "value": value})
    else:
        gh.patch(gh.r(f"actions/variables/{name}"), {"name": name, "value": value})
    return f"variable {name}={value}"


def ensure_variables(gh: GH, cfg: Config) -> list[str]:
    out = []
    if get_variable(gh, "AGENTS_ENABLED") is None:  # never flip a running night shift off on re-run
        out.append(set_variable(gh, "AGENTS_ENABLED", "false") + " (turn on with `gh nightshift start`)")
    out.append(set_variable(gh, "FIX_LANE", cfg.effective_fix_lane))
    return out


def protection_body(cfg: Config) -> dict:
    """Strict mode: branches must be up to date before merging, so CI tests the merged result (add-on A).
    Queue mode: the merge queue tests the merged result, so up-to-date is not required."""
    return {
        "required_status_checks": {"strict": cfg.merge_mode == "strict", "contexts": list(cfg.required_checks)},
        "enforce_admins": True,
        "required_pull_request_reviews": None,
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }


def _names(items: list | None, key: str) -> list[str]:
    return [str(x[key]) for x in items or [] if isinstance(x, dict) and x.get(key)]


def _actors(group: dict | None) -> dict:
    """GET lists users, teams and apps as objects; PUT wants logins and slugs."""
    group = group or {}
    return {"users": _names(group.get("users"), "login"), "teams": _names(group.get("teams"), "slug"),
            "apps": _names(group.get("apps"), "slug")}


# Switches GitHub reports as {"enabled": bool}. Kept as they are; `setup` only ever adds to a protection.
_KEPT_SWITCHES = ("required_linear_history", "required_conversation_resolution", "block_creations", "lock_branch",
                  "allow_fork_syncing")


def merged_protection(cfg: Config, current: dict | None) -> tuple[dict, list[str]]:
    """(the body to PUT, what it keeps or changes). The PUT replaces the whole protection, so everything a repo
    already had is carried over: required reviews, push restrictions, its other required checks and switches.
    `setup` adds the night-shift checks, strict mode and enforce_admins; it never removes a rule."""
    body = protection_body(cfg)
    if not current:
        return body, []
    notes: list[str] = []
    rsc = current.get("required_status_checks") or {}
    old_checks = [c for c in rsc.get("checks") or [] if isinstance(c, dict) and c.get("context")]
    old_names = list(dict.fromkeys([*(rsc.get("contexts") or []), *[c["context"] for c in old_checks]]))
    added = [c for c in cfg.required_checks if c not in old_names]
    extra = [c for c in old_names if c not in cfg.required_checks]
    status = {"strict": bool(rsc.get("strict")) or cfg.merge_mode == "strict"}
    if old_checks:
        # Keep each check's app binding (null from GET means any app, which PUT spells -1).
        by_name = {c["context"]: c for c in old_checks}
        status["checks"] = [{"context": n, "app_id": by_name[n]["app_id"] if by_name.get(n, {}).get("app_id") else -1}
                            for n in [*old_names, *added]]
    else:
        status["contexts"] = [*old_names, *added]
    body["required_status_checks"] = status
    if extra:
        notes.append(f"kept the required checks already there: {', '.join(extra)}")
    if added:
        notes.append(f"added required checks: {', '.join(added)}")
    if status["strict"] and not rsc.get("strict"):
        notes.append("turned on `strict` (PRs must be up to date with the branch)")

    reviews = current.get("required_pull_request_reviews")
    if reviews:
        kept = {k: reviews[k] for k in ("dismiss_stale_reviews", "require_code_owner_reviews",
                                         "required_approving_review_count", "require_last_push_approval")
                if k in reviews}
        if reviews.get("dismissal_restrictions"):
            kept["dismissal_restrictions"] = _actors(reviews["dismissal_restrictions"])
        if reviews.get("bypass_pull_request_allowances"):
            kept["bypass_pull_request_allowances"] = _actors(reviews["bypass_pull_request_allowances"])
        body["required_pull_request_reviews"] = kept
        count = kept.get("required_approving_review_count", 1)
        notes.append(f"kept required pull request reviews ({count} approval(s)"
                     + (", code owners" if kept.get("require_code_owner_reviews") else "") + "): agent PRs will wait "
                     "for a person's approval before the night shift can merge them")
    if current.get("restrictions"):
        body["restrictions"] = _actors(current["restrictions"])
        notes.append("kept the push restrictions (make sure the night shift's account or App may push, or its "
                     "merges are refused)")
    for key in _KEPT_SWITCHES:
        if (current.get(key) or {}).get("enabled"):
            body[key] = True
            notes.append(f"kept {key}")
    if not (current.get("enforce_admins") or {}).get("enabled"):
        notes.append("turned on enforce_admins (admins no longer bypass the rules)")
    for key in ("allow_force_pushes", "allow_deletions"):
        if (current.get(key) or {}).get("enabled"):
            notes.append(f"turned off {key}")
    if (current.get("required_signatures") or {}).get("enabled"):
        notes.append("signed commits stay required (a separate setting, untouched): the night shift's squash merges "
                     "are signed by GitHub")
    return body, notes


MERGE_QUEUE_RULESET = {
    "name": "night-shift merge queue",
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [{"type": "merge_queue", "parameters": {
        "merge_method": "SQUASH", "grouping_strategy": "ALLGREEN", "max_entries_to_build": 5,
        "min_entries_to_merge": 1, "max_entries_to_merge": 5, "min_entries_to_merge_wait_minutes": 1,
        "check_response_timeout_minutes": 60}}],
}


def ensure_protection(gh: GH, cfg: Config) -> list[str]:
    out = []
    path = gh.r(f"branches/{cfg.main_branch}/protection")
    try:
        current = gh.get_or_none(path)  # 404: not protected yet
    except GHError as err:
        # Writing now would replace rules that could not be read (required reviews, restrictions, other checks).
        return [f"WARNING: could not read the current protection of {cfg.main_branch} ({err}); nothing was changed. "
                "Run `gh nightshift setup` as a repo admin. Private repos on free plans cannot use branch protection."]
    body, notes = merged_protection(cfg, current if isinstance(current, dict) else None)
    try:
        gh.put(path, body)
        rsc = body["required_status_checks"]
        names = rsc.get("contexts") or [c["context"] for c in rsc.get("checks", [])]
        out.append(f"protected {cfg.main_branch}: required {', '.join(names)}, "
                   f"strict={str(rsc['strict']).lower()}, admins included")
        out += [f"  {cfg.main_branch} protection: {n}" for n in notes]
    except GHError as err:
        out.append(f"WARNING: branch protection failed ({err}). Private repos on free plans cannot use it.")
    if cfg.merge_mode == "queue":
        rulesets = gh.get_or_none(gh.r("rulesets")) or []
        if not any(r.get("name") == MERGE_QUEUE_RULESET["name"] for r in rulesets):
            try:
                gh.post(gh.r("rulesets"), MERGE_QUEUE_RULESET)
                out.append("merge queue ruleset created (CI must also run on `merge_group`)")
            except GHError as err:
                out.append(f"WARNING: merge queue unavailable ({err}); use merge.mode: strict")
    return out


def ensure_night_log(gh: GH, cfg: Config) -> list[str]:
    label = cfg.label("night_log")
    if gh.night_log(label):
        return []
    created = gh.create_issue("Night-shift log", "Each agent run adds one line here: time, lane, issue, outcome, "
                                                 "cost. Pin this issue.", [label])
    return [f"night-log issue #{created.get('number', 0)} created (pin it)"]


def setup(gh: GH, cfg: Config, protection: bool = True) -> list[str]:
    prot = ensure_protection(gh, cfg) if protection else ["branch protection: skipped for now; run "
                                                          "`gh nightshift setup` after pushing the new files"]
    from . import roadmap

    return (ensure_labels(gh, cfg) + repo_settings(gh) + ensure_variables(gh, cfg) + prot + ensure_night_log(gh, cfg)
            + [roadmap.safe_update(gh, cfg, create=True)])


# --- secrets --------------------------------------------------------------------------

def existing_secrets(gh: GH) -> set[str]:
    try:
        return {s["name"] for s in gh.paginate(gh.r("actions/secrets"), key="secrets")}
    except GHError:
        return set()


SecretSetter = Callable[[str, str, str], None]


def gh_secret_set(repo: str, name: str, value: str) -> None:
    """`gh secret set` encrypts with the repo's public key over REST; the value goes in on stdin."""
    result = subprocess.run(["gh", "secret", "set", name, "-R", repo], input=value, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip()
        if re.search(r"HTTP 40[34]|not found|forbidden|must have admin", err, re.I):
            err += (f"\n  Setting repository secrets needs admin access to {repo} (or a custom role that can manage "
                    "secrets). Ask a repo admin to grant it, then run this again.")
        raise GHError(f"gh secret set {name}: {err}")


def prompt_secret(gh: GH, name: str, hint: str, setter: SecretSetter | None = None,
                  ask: Callable[[str], str] = getpass.getpass,
                  validate: Callable[[str], str | None] | None = None) -> str:
    """Ask for a secret without echoing it and store it. Dry-run prints the command only. `validate` returns
    an error message to refuse the value (it is never printed)."""
    if gh.dry_run:
        print(f"[dry-run] gh secret set {name} -R {gh.repo}   # value read from a hidden prompt ({hint})", file=gh.out)
        return f"would set {name}"
    value = ask(f"{name} ({hint}), input hidden, empty to skip: ").strip()
    if not value:
        return f"skipped {name}"
    if validate:
        problem = validate(value)
        if problem:
            return f"not set {name}: {problem}"
    (setter or gh_secret_set)(gh.repo, name, value)
    return f"secret {name} set"


SECRET_HINTS = {
    "AGENT_PAT": "fine-grained PAT for this repo only: Contents, Issues, Pull requests read/write; "
                 "Actions, Commit statuses read; NO Workflows",
}


def secret_hint(cfg: Config, name: str) -> str:
    if cfg.auth_mode == "app" and name == cfg.app_key_secret:
        return "the GitHub App's private key (.pem)"
    if name in SECRET_HINTS:
        return SECRET_HINTS[name]
    if name == cfg.review_token_secret:
        return "`claude setup-token` from someone who does not own a lane (review gate)"
    lanes = [lane.name for lane in cfg.lanes if lane.token_secret == name]
    if lanes:
        return f"`claude setup-token` from the owner of lane(s) {', '.join(lanes)}"
    fallbacks = [lane.name for lane in cfg.lanes if lane.fallback_token_secret == name]
    if fallbacks:
        return (f"`claude setup-token` from a second Claude account, used by lane(s) {', '.join(fallbacks)} only when "
                "the first token cannot start a run")
    pats = [lane.name for lane in cfg.lanes if lane.pat_secret == name]
    if pats:
        return f"lane owner's own PAT for {', '.join(pats)}, same permissions as AGENT_PAT"
    return "Claude token for the planner"


def prompt_missing_secrets(gh: GH, cfg: Config, setter: SecretSetter | None = None,
                           ask: Callable[[str], str] = getpass.getpass, app_key_file: str = "") -> list[str]:
    have = existing_secrets(gh)
    out = []
    for n in cfg.secret_names():
        if n in have:
            continue
        if cfg.auth_mode == "app" and n == cfg.app_key_secret:
            out.append(set_app_key(gh, n, app_key_file, setter))  # a multi-line PEM cannot go through a prompt
        else:
            out.append(prompt_secret(gh, n, secret_hint(cfg, n), setter, ask))
    return out


def set_app_key(gh: GH, name: str, path: str, setter: SecretSetter | None = None) -> str:
    if not path:
        return f"not set {name}: run `gh secret set {name} -R {gh.repo} < key.pem`"
    if gh.dry_run:
        print(f"[dry-run] gh secret set {name} -R {gh.repo}   # value read from {path}", file=gh.out)
        return f"would set {name}"
    text = Path(path).read_text(encoding="utf-8")
    if "PRIVATE KEY-----" not in text:
        return f"not set {name}: {path} is not a PEM private key"
    (setter or gh_secret_set)(gh.repo, name, text)
    return f"secret {name} set from {path}"


# --- switches -------------------------------------------------------------------------

def start(gh: GH) -> list[str]:
    return [set_variable(gh, "AGENTS_ENABLED", "true")]


def pause(gh: GH, cancel: bool = True) -> list[str]:
    """Switch off, and by default cancel lane runs already queued or in progress (their publish job would
    otherwise still open a PR)."""
    out = [set_variable(gh, "AGENTS_ENABLED", "false")]
    if cancel:
        out += cancel_runs(gh)
    return out


LANE_CALLER = "night-shift.yml"


def cancel_runs(gh: GH, workflow: str = LANE_CALLER) -> list[str]:
    out = []
    for status in ("in_progress", "queued", "waiting"):
        try:
            runs = gh.get(gh.r(f"actions/workflows/{workflow}/runs"), {"status": status, "per_page": 100}) or {}
        except GHError as err:
            if err.status == 404:
                return ["no lane workflow runs to cancel (night-shift.yml not found)"]
            raise
        for run in runs.get("workflow_runs", []):
            gh.post(gh.r(f"actions/runs/{run['id']}/cancel"))
            out.append(f"cancelled run {run['id']} ({status})")
    return out or ["no lane runs in flight"]


def freeze(gh: GH, cfg: Config, cancel: bool = True) -> list[str]:
    """Code freeze: switch off and hand every remaining ready issue to people."""
    out = pause(gh, cancel)
    for issue in gh.issues([cfg.ready_label]):
        if cfg.label("human") in label_names(issue):
            continue
        gh.add_labels(issue["number"], [cfg.label("human"), FROZEN])
        gh.remove_label(issue["number"], cfg.ready_label)
        out.append(f"#{issue['number']} frozen (labelled {cfg.label('human')})")
    return out


def resume(gh: GH, cfg: Config) -> list[str]:
    """Undo freeze: give frozen issues back to the agents and switch on."""
    out = []
    for issue in gh.issues([FROZEN]):
        gh.add_labels(issue["number"], [cfg.ready_label])
        gh.remove_label(issue["number"], cfg.label("human"))
        gh.remove_label(issue["number"], FROZEN)
        out.append(f"#{issue['number']} back on the night shift")
    return out + start(gh)


def eprint(*args) -> None:
    print(*args, file=sys.stderr)
