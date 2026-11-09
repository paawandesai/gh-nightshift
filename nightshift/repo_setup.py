"""Repository setup over REST: labels, settings, variables, branch protection, night-log issue, secrets,
and the switches (start, pause, freeze, resume). Every write goes through GH, so --dry-run prints the
exact `gh api` call instead of making it."""
from __future__ import annotations

import getpass
import subprocess
import sys
import urllib.parse
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
    try:
        gh.put(gh.r(f"branches/{cfg.main_branch}/protection"), protection_body(cfg))
        out.append(f"protected {cfg.main_branch}: required {', '.join(cfg.required_checks)}, "
                   f"strict={str(cfg.merge_mode == 'strict').lower()}, admins included")
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
    return ensure_labels(gh, cfg) + repo_settings(gh) + ensure_variables(gh, cfg) + prot + ensure_night_log(gh, cfg)


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
        raise GHError(f"gh secret set {name}: {result.stderr.strip()}")


def prompt_secret(gh: GH, name: str, hint: str, setter: SecretSetter = gh_secret_set,
                  ask: Callable[[str], str] = getpass.getpass) -> str:
    """Ask for a secret without echoing it and store it. Dry-run prints the command only."""
    if gh.dry_run:
        print(f"[dry-run] gh secret set {name} -R {gh.repo}   # value read from a hidden prompt ({hint})", file=gh.out)
        return f"would set {name}"
    value = ask(f"{name} ({hint}), input hidden, empty to skip: ").strip()
    if not value:
        return f"skipped {name}"
    setter(gh.repo, name, value)
    return f"secret {name} set"


SECRET_HINTS = {
    "AGENT_PAT": "fine-grained PAT for this repo only: Contents, Issues, Pull requests read/write; Actions read; NO Workflows",
}


def secret_hint(cfg: Config, name: str) -> str:
    if name in SECRET_HINTS:
        return SECRET_HINTS[name]
    if name == cfg.review_token_secret:
        return "`claude setup-token` from someone who does not own a lane (review gate)"
    lanes = [lane.name for lane in cfg.lanes if lane.token_secret == name]
    if lanes:
        return f"`claude setup-token` from the owner of lane(s) {', '.join(lanes)}"
    pats = [lane.name for lane in cfg.lanes if lane.pat_secret == name]
    if pats:
        return f"lane owner's own PAT for {', '.join(pats)}, same permissions as AGENT_PAT"
    return "Claude token for the planner"


def prompt_missing_secrets(gh: GH, cfg: Config, setter: SecretSetter = gh_secret_set,
                           ask: Callable[[str], str] = getpass.getpass) -> list[str]:
    have = existing_secrets(gh)
    return [prompt_secret(gh, n, secret_hint(cfg, n), setter, ask) for n in cfg.secret_names() if n not in have]


# --- switches -------------------------------------------------------------------------

def start(gh: GH) -> list[str]:
    return [set_variable(gh, "AGENTS_ENABLED", "true")]


def pause(gh: GH) -> list[str]:
    return [set_variable(gh, "AGENTS_ENABLED", "false")]


def freeze(gh: GH, cfg: Config) -> list[str]:
    """Code freeze: switch off and hand every remaining ready issue to people."""
    out = pause(gh)
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
