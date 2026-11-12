"""`.night-shift/config.yml`: the one file a project edits to adopt night shift.

Validated against `nightshift/schema/config.schema.json`. Repo *variables* still win at run time for
the things you flip from a phone (AGENTS_ENABLED, FIX_LANE, models), so the kill switch never needs a
commit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import schema, yamlish

CONFIG_PATH = ".night-shift/config.yml"
# Always protected, whatever the config says: agents may never change their own rules or CI.
SECURITY_CHECK = "review / security"
BUILTIN_PROTECTED = [".github/", ".claude/", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENT_RULES.md", ".mcp.json",
                     ".night-shift/"]
DEFAULT_LABELS = {
    "ready": "night-safe", "blocked": "blocked", "needs_human": "needs-human", "human": "human",
    "proposed": "proposed", "agent": "agent", "in_progress": "agent:in-progress", "night_log": "night-log",
}


class ConfigError(ValueError):
    pass


@dataclass
class Lane:
    name: str
    token_secret: str
    model: str = ""
    escalate_model: str = ""
    pat_secret: str = ""          # optional per-lane PAT so PRs are credited to the lane owner
    fallback_token_secret: str = ""  # optional second Claude token, used when the first one cannot start a run
    paths: list[str] = field(default_factory=list)
    description: str = ""
    test_command: str = ""
    setup_commands: str = ""
    setup_action: str = ""
    postgres_image: str = ""
    budget_usd_per_night: float = 0.0  # 0 = no cap
    sudo: bool = True  # False: the agent step runs without sudo (setup commands still have it)

    @property
    def label(self) -> str:
        return f"lane:{self.name}"

    @property
    def claude_token_secret(self) -> str:
        return self.token_secret

    @property
    def agent_pat_secret(self) -> str:
        return self.pat_secret or "AGENT_PAT"


@dataclass
class Config:
    lanes: list[Lane]
    main_branch: str = "main"
    ci_workflow: str = "ci.yml"
    fix_lane: str = ""
    required_checks: list[str] = field(default_factory=lambda: ["test", "review / verdict"])
    timezone: str = "UTC"
    extra_protected: list[str] = field(default_factory=list)
    merge_mode: str = "strict"
    merge_allow_unenforced: bool = False  # merge even when main does not enforce the required checks (M7)
    labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    default_model: str = "sonnet"
    escalate_model: str = "opus"
    review_model: str = "opus"
    planner_model: str = "opus"
    max_attempts: int = 3
    infra_cap: int = 6
    stale_minutes: int = 45
    max_turns: int = 80
    max_needs_per_issue: int = 2
    max_notes_per_issue: int = 2
    auto_ready_needs: bool = True  # false: an agent's `needs` ticket is `proposed` until a person promotes it
    planner_enabled: bool = True
    planner_docs: list[str] = field(default_factory=lambda: ["docs/PRD.md"])
    planner_max_tickets: int = 8
    planner_refill_below: int = 0
    planner_id_prefixes: dict[str, str] = field(default_factory=dict)
    planner_human_paths: list[str] = field(default_factory=lambda: ["contracts/"])
    planner_token_secret: str = ""
    planner_schedule: str = "17 * * * *"
    review_token_secret: str = "CLAUDE_TOKEN_REVIEW"
    ci_workflow_name: str = ""
    tool_ref: str = "main"
    # Runner labels: `github` runs the jobs holding a GitHub token or the App key (pick, publish, planner
    # context and create, review, commands, digest); `agent` runs the Claude-only jobs (lane agent, planner
    # draft). Never give both the same self-hosted machine: the agent must not share a box with the token.
    runner_github: list[str] = field(default_factory=lambda: ["ubuntu-latest"])
    runner_agent: list[str] = field(default_factory=lambda: ["ubuntu-latest"])
    bot_login: str = ""           # the PAT's login; read-only commands (stats, audit) trust markers from it
    trusted_logins: list[str] = field(default_factory=list)  # other PAT logins (per-contributor PATs) to trust
    cross_repos: list[str] = field(default_factory=list)  # repos whose issues `Blocked by: owner/repo#N` may read
    auth_mode: str = "pat"  # pat | app (GitHub App: an installation token minted in the PAT jobs)
    app_id: str = ""
    app_key_secret: str = "NIGHT_SHIFT_APP_KEY"
    risk_default: str = "auto"  # auto: green PRs merge; approve: they also need one human approval
    risk_approve_labels: list[str] = field(default_factory=list)
    risk_approve_paths: list[str] = field(default_factory=list)
    risk_auto_labels: list[str] = field(default_factory=list)
    risk_codeowners: bool = False  # PRs touching files with CODEOWNERS owners need one of those owners' approval
    reuse_on_update: bool = True  # review.reuse_on_update: keep a passed verdict when update-branch only merged main
    security_enabled: bool = False  # the `review / security` gate (see review_rules.py)
    security_token_secret: str = ""  # default: review_token_secret
    security_model: str = ""  # default: review_model
    security_paths: list[str] = field(default_factory=list)
    security_always: bool = False
    reduce_enabled: bool = False  # the nightly cross-lane pass (see reduce.py); runs in the planner workflow
    reduce_schedule: str = "30 6 * * *"
    redteam_enabled: bool = False  # scheduled red-team scan of main, filed as private advisories (see redteam.py)
    redteam_schedule: str = "0 5 * * *"
    redteam_min_severity: str = "high"
    redteam_token_secret: str = ""  # default: the security reviewer's token
    redteam_model: str = ""  # default: security.model, then models.review
    reduce_window_hours: int = 48  # above the daily period, so a late or failed run is caught up
    extends: str = ""  # owner/repo whose .night-shift/config.yml is the org-wide base (`gh nightshift config pull`)
    digest_enabled: bool = True
    digest_schedule: str = "0 7 * * *"
    digest_webhook_secret: str = ""  # optional secret holding a Slack or Discord webhook URL
    digest_weekly_schedule: str = "0 8 * * 1"  # weekly report (7 days); "" turns it off

    # --- derived -----------------------------------------------------------------------
    @property
    def protected_paths(self) -> list[str]:
        extra = ["CODEOWNERS", "docs/CODEOWNERS"] if self.risk_codeowners else []  # .github/ is already built in
        if self.planner_refill_below > 0:
            extra += self.planner_docs  # M8: the planner files tickets from these on a timer, unattended
        return BUILTIN_PROTECTED + [p for p in self.extra_protected + extra if p not in BUILTIN_PROTECTED]

    @property
    def security_token(self) -> str:
        return self.security_token_secret or self.review_token_secret

    @property
    def redteam_token(self) -> str:
        return self.redteam_token_secret or self.security_token

    @property
    def security_trigger_paths(self) -> list[str]:
        """Paths that always get a security review: security.paths plus risk.approve_paths."""
        return list(dict.fromkeys(self.security_paths + self.risk_approve_paths))

    @property
    def ready_label(self) -> str:
        return self.labels["ready"]

    def label(self, key: str) -> str:
        return self.labels[key]

    @property
    def skip_labels(self) -> set[str]:
        return {self.labels["blocked"], self.labels["needs_human"], self.labels["human"], "integrator",
                self.labels["proposed"]}

    def lane(self, name: str) -> Lane:
        for lane in self.lanes:
            if lane.name == name:
                return lane
        raise ConfigError(f"unknown lane {name!r}; lanes are {', '.join(self.lane_names)}")

    @property
    def lane_names(self) -> list[str]:
        return [lane.name for lane in self.lanes]

    @property
    def effective_fix_lane(self) -> str:
        return self.fix_lane or (self.lanes[0].name if self.lanes else "")

    def id_prefix(self, lane: str) -> str:
        return self.planner_id_prefixes.get(lane) or lane[:1].upper()

    @property
    def github_secret(self) -> str:
        """The secret that authenticates the PAT jobs: AGENT_PAT, or the App's private key."""
        return self.app_key_secret if self.auth_mode == "app" else "AGENT_PAT"

    def secret_names(self) -> list[str]:
        names = {self.github_secret, self.review_token_secret, self.planner_token_secret or self.lanes[0].token_secret}
        if self.security_enabled:
            names.add(self.security_token)
        if self.redteam_enabled:
            names.add(self.redteam_token)
        for lane in self.lanes:
            names.add(lane.token_secret)
            if lane.pat_secret:
                names.add(lane.pat_secret)
            if lane.fallback_token_secret:
                names.add(lane.fallback_token_secret)
        return sorted(names)


def _labels(value: object, key: str) -> list[str]:
    """Runner labels: one label or a list of them, each a plain name (they go into workflow YAML)."""
    labels = [value] if isinstance(value, str) else value
    if (not isinstance(labels, list) or not labels
            or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", x) for x in labels)):
        raise ConfigError(f"{key} must be a runner label or a list of them (letters, digits, . _ -)")
    return list(labels)


# Labels every self-hosted runner gets by default; they say nothing about which machine a job lands on.
GENERIC_RUNNER_LABELS = {"self-hosted", "linux", "windows", "macos", "x64", "arm", "arm64"}


def _check_runner_split(github: list[str], agent: list[str]) -> None:
    """Keep the agent jobs off the machines that hold the GitHub token or App key.

    A job runs on any runner that has all of its labels, so the two label sets must not be equal, and neither
    may be a subset of the other (that runner would take both kinds of job). Each set also needs a label of its
    own: generic labels alone (self-hosted, linux, x64, ...) match every self-hosted runner. Labels match
    without regard to case. This cannot prove two sets name different
    machines: register each class on its own machine, ideally in its own runner group.
    """
    sets = {key: {x.casefold() for x in labels} for key, labels in (("runners.github", github), ("runners.agent", agent))}
    if all(s == {"ubuntu-latest"} for s in sets.values()):
        return
    for key, s in sets.items():
        if not s - GENERIC_RUNNER_LABELS:  # with or without self-hosted, these match every runner
            raise ConfigError(f"{key} needs a label of its own besides {', '.join(sorted(s))}: those match any "
                              "self-hosted runner, including the other class's")
    g, a = sets.values()
    if g <= a or a <= g:
        raise ConfigError("runners.agent and runners.github must each have a label the other lacks: a runner "
                          "carrying both sets would put the agent on a machine with the GitHub token")


_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_NAMES = ({}, {}, {},
               {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())},
               {d: i for i, d in enumerate("SUN MON TUE WED THU FRI SAT".split())})


def cron_values(field: str, index: int) -> set[int] | None:
    """The values one cron field matches (`*`, values, ranges, steps, JAN/MON names), or None if invalid."""
    lo, hi = _CRON_RANGES[index]
    out: set[int] = set()
    for item in field.split(","):
        m = re.fullmatch(r"(\*|[0-9A-Za-z]+(?:-[0-9A-Za-z]+)?)(?:/([0-9]{1,2}))?", item)
        if not m or (m.group(2) is not None and int(m.group(2)) == 0):
            return None
        if m.group(1) == "*":
            a, b = lo, hi
        else:
            ends = []
            for v in m.group(1).split("-"):
                n = _CRON_NAMES[index].get(v.upper()) if not v.isdigit() else int(v)
                if n is None or not lo <= n <= hi:
                    return None
                ends.append(n)
            a, b = ends[0], ends[-1]
            if a > b:
                return None
        out |= set(range(a, b + 1, int(m.group(2) or 1)))
    return out


def cron_ok(expr: str) -> bool:
    fields = expr.split()
    return len(fields) == 5 and all(cron_values(f, i) is not None for i, f in enumerate(fields))


def parse(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    errors = schema.validate(data, schema.load("config"))
    if errors:
        raise ConfigError("invalid .night-shift/config.yml:\n  " + "\n  ".join(errors))
    lanes = []
    for raw in data["lanes"]:
        setup = raw.get("setup") or {}
        cmds = setup.get("commands") or ""
        if isinstance(cmds, list):
            cmds = "\n".join(cmds)
        lanes.append(Lane(name=raw["name"], token_secret=raw["claude_token_secret"],
                          pat_secret=raw.get("agent_pat_secret", ""), model=raw.get("model", ""),
                          fallback_token_secret=raw.get("fallback_token_secret", ""),
                          escalate_model=raw.get("escalate_model", ""), paths=raw.get("paths", []),
                          description=raw.get("description", ""), test_command=raw.get("test_command", ""),
                          setup_commands=cmds, setup_action=(setup.get("action") or "").removeprefix("./"),
                          postgres_image=raw.get("postgres_image", ""), sudo=raw.get("sudo", True) is not False,
                          budget_usd_per_night=float(raw.get("budget_usd_per_night", 0) or 0)))
    names = [lane.name for lane in lanes]
    if len(set(names)) != len(names):
        raise ConfigError("lane names must be unique")
    for lane in lanes:
        if lane.fallback_token_secret and lane.fallback_token_secret == lane.token_secret:
            raise ConfigError(f"lane {lane.name}: fallback_token_secret must differ from claude_token_secret")
    models = data.get("models", {})
    limits = data.get("limits", {})
    planner = data.get("planner", {})
    security = data.get("security") or {}
    checks = list(data.get("required_checks", ["test", "review / verdict"]))
    if security.get("enabled") is True and SECURITY_CHECK not in checks:
        checks.append(SECURITY_CHECK)  # once switched on, the security gate is always required
    cfg = Config(
        lanes=lanes,
        main_branch=data.get("main_branch", "main"),
        ci_workflow=data.get("ci_workflow", "ci.yml"),
        fix_lane=data.get("fix_lane", ""),
        required_checks=checks,
        timezone=data.get("timezone", "UTC"),
        extra_protected=data.get("protected_paths", []),
        merge_mode=(data.get("merge") or {}).get("mode", "strict"),
        merge_allow_unenforced=(data.get("merge") or {}).get("allow_unenforced") is True,
        labels={**DEFAULT_LABELS, **(data.get("labels") or {})},
        default_model=models.get("default", "sonnet"),
        escalate_model=models.get("escalate", "opus"),
        review_model=models.get("review", "opus"),
        planner_model=models.get("planner", "opus"),
        max_attempts=limits.get("max_attempts", 3),
        infra_cap=limits.get("infra_cap", limits.get("max_attempts", 3) * 2),
        stale_minutes=limits.get("stale_minutes", 45),
        max_turns=limits.get("max_turns", 80),
        max_needs_per_issue=limits.get("max_needs_per_issue", 2),
        max_notes_per_issue=limits.get("max_notes_per_issue", 2),
        auto_ready_needs=bool(limits.get("auto_ready_needs", True)),
        planner_enabled=planner.get("enabled", True),
        planner_docs=planner.get("docs", ["docs/PRD.md"]),
        planner_max_tickets=planner.get("max_tickets_per_run", 8),
        planner_refill_below=planner.get("refill_below", 0),
        planner_id_prefixes=planner.get("id_prefixes", {}),
        planner_human_paths=planner.get("human_paths", ["contracts/"]),
        planner_token_secret=planner.get("claude_token_secret", lanes[0].token_secret),
        planner_schedule=" ".join(str(planner.get("schedule", "17 * * * *")).split()),
        review_token_secret=data.get("review_token_secret", "CLAUDE_TOKEN_REVIEW"),
        ci_workflow_name=data.get("ci_workflow_name", ""),
        tool_ref=data.get("tool_ref", "main"),
        runner_github=_labels((data.get("runners") or {}).get("github", "ubuntu-latest"), "runners.github"),
        runner_agent=_labels((data.get("runners") or {}).get("agent", "ubuntu-latest"), "runners.agent"),
        bot_login=data.get("bot_login", ""),
        trusted_logins=data.get("trusted_logins", []),
        cross_repos=data.get("cross_repos", []),
        auth_mode=(data.get("auth") or {}).get("mode", "pat"),
        app_id=str((data.get("auth") or {}).get("app_id", "") or ""),
        app_key_secret=(data.get("auth") or {}).get("private_key_secret", "NIGHT_SHIFT_APP_KEY"),
        risk_default=(data.get("risk") or {}).get("default", "auto"),
        risk_approve_labels=list((data.get("risk") or {}).get("approve_labels", [])),
        risk_approve_paths=list((data.get("risk") or {}).get("approve_paths", [])),
        risk_auto_labels=list((data.get("risk") or {}).get("auto_labels", [])),
        risk_codeowners=bool((data.get("risk") or {}).get("codeowners", False)),
        reuse_on_update=(data.get("review") or {}).get("reuse_on_update", True) is not False,
        security_enabled=security.get("enabled") is True,
        security_token_secret=security.get("claude_token_secret", ""),
        security_model=security.get("model", ""),
        security_paths=list(security.get("paths", [])),
        security_always=security.get("always") is True,
        reduce_enabled=(data.get("reduce") or {}).get("enabled") is True,
        redteam_enabled=(data.get("redteam") or {}).get("enabled") is True,
        redteam_schedule=" ".join(str((data.get("redteam") or {}).get("schedule", "0 5 * * *")).split()),
        redteam_min_severity=str((data.get("redteam") or {}).get("min_severity", "high")),
        redteam_token_secret=str((data.get("redteam") or {}).get("claude_token_secret", "")),
        redteam_model=str((data.get("redteam") or {}).get("model", "")),
        reduce_schedule=" ".join(str((data.get("reduce") or {}).get("schedule", "30 6 * * *")).split()),
        reduce_window_hours=int((data.get("reduce") or {}).get("window_hours", 48)),
        extends=data.get("extends", ""),
        digest_enabled=(data.get("digest") or {}).get("enabled", True),
        digest_schedule=str((data.get("digest") or {}).get("schedule", "0 7 * * *")),
        digest_webhook_secret=(data.get("digest") or {}).get("webhook_secret", ""),
        digest_weekly_schedule=str((data.get("digest") or {}).get("weekly_schedule", "0 8 * * 1")),
    )
    if not re.fullmatch(r"[0-9*/,\- ]{9,60}", cfg.digest_schedule) or len(cfg.digest_schedule.split()) != 5:
        raise ConfigError(f"digest.schedule {cfg.digest_schedule!r} is not a 5-field cron expression")
    if cfg.extends and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", cfg.extends)
                        or cfg.extends.split("/")[1] in (".", "..")):
        raise ConfigError(f"extends must be owner/repo, not {cfg.extends!r}")
    _check_runner_split(cfg.runner_github, cfg.runner_agent)
    if cfg.auth_mode not in ("pat", "app"):
        raise ConfigError(f"auth.mode must be pat or app, not {cfg.auth_mode!r}")
    if cfg.auth_mode == "app":
        if not re.fullmatch(r"[0-9]{1,12}", cfg.app_id):
            raise ConfigError("auth.app_id must be the GitHub App's numeric id")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cfg.app_key_secret):
            raise ConfigError("auth.private_key_secret must be a secret name (A-Z, 0-9, _)")
        if any(lane.pat_secret for lane in cfg.lanes):
            raise ConfigError("auth.mode app replaces every PAT: remove agent_pat_secret from the lanes")
        if cfg.app_key_secret in claude_secrets(cfg) | {cfg.planner_token_secret, cfg.review_token_secret,
                                                        cfg.security_token, cfg.redteam_token}:
            raise ConfigError(f"secret {cfg.app_key_secret} is used both as a Claude token and as the App key")
    if cfg.risk_default not in ("auto", "approve"):
        raise ConfigError(f"risk.default must be auto or approve, not {cfg.risk_default!r}")
    weekly = cfg.digest_weekly_schedule
    if weekly and (not re.fullmatch(r"[0-9*/,\- ]{9,60}", weekly) or len(weekly.split()) != 5 or weekly == cfg.digest_schedule):
        raise ConfigError(f"digest.weekly_schedule {weekly!r} must be a 5-field cron expression unlike digest.schedule")
    if cfg.digest_webhook_secret and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cfg.digest_webhook_secret):
        raise ConfigError("digest.webhook_secret must be a secret name (A-Z, 0-9, _)")
    if cfg.digest_webhook_secret in pats_or_claude(cfg):
        raise ConfigError("digest.webhook_secret must not be a PAT or Claude token secret")
    if cfg.reduce_enabled:
        if not cron_ok(cfg.reduce_schedule):
            raise ConfigError(f"reduce.schedule {cfg.reduce_schedule!r} is not a 5-field cron expression")
        if cfg.planner_refill_below > 0:
            if not cron_ok(cfg.planner_schedule):
                raise ConfigError(f"planner.schedule {cfg.planner_schedule!r} is not a 5-field cron expression")
            if cron_values(cfg.reduce_schedule.split()[0], 0) & cron_values(cfg.planner_schedule.split()[0], 0):
                raise ConfigError("reduce.schedule must use minutes planner.schedule never uses (the planner tells "
                                  "the two timers apart by cron, so they must never fire on the same tick)")
        if not cfg.planner_enabled:
            raise ConfigError("reduce runs in the planner workflow: it needs planner.enabled")
    if cfg.redteam_enabled and not cron_ok(cfg.redteam_schedule):
        raise ConfigError(f"redteam.schedule {cfg.redteam_schedule!r} is not a 5-field cron expression")
    if cfg.redteam_min_severity not in ("low", "medium", "high", "critical"):
        raise ConfigError("redteam.min_severity must be low, medium, high or critical")
    if cfg.redteam_model and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", cfg.redteam_model):
        raise ConfigError("redteam.model must be a model name or alias (letters, digits, . _ -)")
    if cfg.redteam_token_secret and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cfg.redteam_token_secret):
        raise ConfigError("redteam.claude_token_secret must be a secret name (A-Z, 0-9, _)")
    if not 1 <= cfg.reduce_window_hours <= 168:
        raise ConfigError("reduce.window_hours must be between 1 and 168")
    if cfg.fix_lane and cfg.fix_lane not in names:
        raise ConfigError(f"fix_lane {cfg.fix_lane!r} is not a lane")
    unknown = sorted(set(cfg.planner_id_prefixes) - set(names))
    if unknown:
        raise ConfigError(f"planner.id_prefixes names unknown lane(s): {', '.join(unknown)}")
    # A lane's fallback must not be another lane's, the planner's or a reviewer's token, and the reviewers judge
    # the lanes' work, so they must not run on a lane owner's token (security review M9).
    lane_tokens = {lane.token_secret: lane.name for lane in lanes}
    for lane in lanes:
        fb = lane.fallback_token_secret
        if fb and (lane_tokens.get(fb, lane.name) != lane.name or fb in {cfg.review_token_secret, cfg.security_token,
                                                                        cfg.planner_token_secret}):
            raise ConfigError(f"lane {lane.name}: fallback_token_secret {fb} is another lane's, the planner's or a "
                              "reviewer's token")
    for who, secret in (("review_token_secret", cfg.review_token_secret),
                        ("security.claude_token_secret", cfg.security_token)):
        if secret in lane_tokens:
            raise ConfigError(f"{who} {secret} is also a lane's Claude token: the reviewer must not share a token "
                              "with the lanes it reviews")
    # A Claude token secret that is also a PAT secret would hand the PAT to the agent job.
    pats = {"AGENT_PAT"} | {lane.agent_pat_secret for lane in lanes}  # AGENT_PAT is always passed as fallback
    claude = claude_secrets(cfg) | {cfg.planner_token_secret, cfg.review_token_secret, cfg.security_token,
                                    cfg.redteam_token}
    clash = sorted(pats & claude)
    if clash:
        raise ConfigError(f"secret(s) {', '.join(clash)} used both as a Claude token and as a PAT: the agent job "
                          "would receive the PAT")
    return cfg


def claude_secrets(cfg: Config) -> set[str]:
    """Every lane's Claude token secrets, fallbacks included."""
    return {lane.token_secret for lane in cfg.lanes} | {lane.fallback_token_secret for lane in cfg.lanes
                                                        if lane.fallback_token_secret}


def pats_or_claude(cfg: Config) -> set[str]:
    return ({"AGENT_PAT", cfg.planner_token_secret, cfg.review_token_secret, cfg.security_token, cfg.redteam_token}
            | claude_secrets(cfg) | {lane.agent_pat_secret for lane in cfg.lanes})


BASE_PATH = ".night-shift/base.yml"  # written by `gh nightshift config pull` from the `extends:` repo
# What an org-wide base may set. Lanes, identities, auth and secret names always stay in the repo's own config.
# Labels, merge mode and tool_ref are left out: each could switch a safety check off (a renamed `human` label
# makes taken tickets pickable, queue mode skips risk tiers, tool_ref picks the code that runs with the PAT).
INHERITABLE = {"models", "limits", "protected_paths", "required_checks", "timezone", "risk", "digest", "planner",
               "security"}
NOT_FROM_BASE = {"planner": {"claude_token_secret"}, "digest": {"webhook_secret"}, "security": {"claude_token_secret"}}


def merge_base(base: dict, local: dict) -> dict:
    """Local wins key by key (one level deep for mappings); protected_paths are the union of both."""
    out = dict(local)
    for key, value in (base or {}).items():
        if key not in INHERITABLE:
            continue
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in NOT_FROM_BASE.get(key, set())}
        if key == "protected_paths":
            out[key] = list(value or []) + [p for p in (local.get(key) or []) if p not in (value or [])]
        elif key == "required_checks":
            # Added to the repo's own (or default) checks, never replacing them: the review gate stays required.
            own = local.get(key) or ["test", "review / verdict"]
            out[key] = list(dict.fromkeys(list(own) + list(value or [])))
        elif key == "planner" and isinstance(value, dict):
            mine = dict(local.get("planner") or {})
            merged = {**value, **mine}
            merged["human_paths"] = list(dict.fromkeys(list(mine.get("human_paths") or ["contracts/"])
                                                       + list(value.get("human_paths") or [])))
            out[key] = merged
        elif key == "risk" and isinstance(value, dict):
            # Only stricter: approval rules add up, and `approve`/codeowners from either side win.
            mine = dict(local.get("risk") or {})
            merged = {k: list(dict.fromkeys(list(value.get(k) or []) + list(mine.get(k) or [])))
                      for k in ("approve_labels", "approve_paths") if value.get(k) or mine.get(k)}
            if mine.get("auto_labels"):
                merged["auto_labels"] = mine["auto_labels"]
            if "approve" in (value.get("default"), mine.get("default")):
                merged["default"] = "approve"
            if value.get("codeowners") or mine.get("codeowners"):
                merged["codeowners"] = True
            out[key] = merged
        elif key == "security" and isinstance(value, dict):
            # Only stricter: the gate and `always` stay on if either side turns them on; paths add up.
            mine = dict(local.get("security") or {})
            merged = {**value, **mine}
            for flag in ("enabled", "always"):
                if value.get(flag) is True or mine.get(flag) is True:
                    merged[flag] = True
            paths = list(dict.fromkeys(list(value.get("paths") or []) + list(mine.get("paths") or [])))
            if paths:
                merged["paths"] = paths
            out[key] = merged
        elif isinstance(value, dict) and isinstance(local.get(key), dict):
            out[key] = {**value, **local[key]}
        elif key not in local:
            out[key] = value
    return out


def loads(text: str, base_text: str | None = None) -> Config:
    try:
        data = yamlish.loads(text)
        if base_text is not None and isinstance(data, dict):
            data = merge_base(yamlish.loads(base_text) or {}, data)
        return parse(data)
    except yamlish.YAMLError as err:
        raise ConfigError(f"{CONFIG_PATH}: {err}") from err


def load(root: str | Path = ".") -> Config:
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"{path} not found; run `gh nightshift init` first")
    base = Path(root) / BASE_PATH
    return loads(path.read_text(encoding="utf-8"), base.read_text(encoding="utf-8") if base.is_file() else None)
