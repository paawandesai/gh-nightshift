"""`.night-shift/config.yml`: the one file a project edits to adopt night shift.

Validated against `nightshift/schema/config.schema.json`. Repo *variables* still win at run time for
the things you flip from a phone (AGENTS_ENABLED, FIX_LANE, models), so the kill switch never needs a
commit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import schema, yamlish

CONFIG_PATH = ".night-shift/config.yml"
# Always protected, whatever the config says: agents may never change their own rules or CI.
BUILTIN_PROTECTED = [".github/", ".claude/", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".night-shift/"]
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
    paths: list[str] = field(default_factory=list)
    description: str = ""
    test_command: str = ""
    setup_commands: str = ""
    setup_action: str = ""
    postgres_image: str = ""

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
    bot_login: str = ""           # the PAT's login; read-only commands (stats, audit) trust markers from it

    # --- derived -----------------------------------------------------------------------
    @property
    def protected_paths(self) -> list[str]:
        return BUILTIN_PROTECTED + [p for p in self.extra_protected if p not in BUILTIN_PROTECTED]

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

    def secret_names(self) -> list[str]:
        names = {"AGENT_PAT", self.review_token_secret, self.planner_token_secret or self.lanes[0].token_secret}
        for lane in self.lanes:
            names.add(lane.token_secret)
            if lane.pat_secret:
                names.add(lane.pat_secret)
        return sorted(names)


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
                          escalate_model=raw.get("escalate_model", ""), paths=raw.get("paths", []),
                          description=raw.get("description", ""), test_command=raw.get("test_command", ""),
                          setup_commands=cmds, setup_action=(setup.get("action") or "").removeprefix("./"),
                          postgres_image=raw.get("postgres_image", "")))
    names = [lane.name for lane in lanes]
    if len(set(names)) != len(names):
        raise ConfigError("lane names must be unique")
    models = data.get("models", {})
    limits = data.get("limits", {})
    planner = data.get("planner", {})
    cfg = Config(
        lanes=lanes,
        main_branch=data.get("main_branch", "main"),
        ci_workflow=data.get("ci_workflow", "ci.yml"),
        fix_lane=data.get("fix_lane", ""),
        required_checks=data.get("required_checks", ["test", "review / verdict"]),
        timezone=data.get("timezone", "UTC"),
        extra_protected=data.get("protected_paths", []),
        merge_mode=(data.get("merge") or {}).get("mode", "strict"),
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
        planner_enabled=planner.get("enabled", True),
        planner_docs=planner.get("docs", ["docs/PRD.md"]),
        planner_max_tickets=planner.get("max_tickets_per_run", 8),
        planner_refill_below=planner.get("refill_below", 0),
        planner_id_prefixes=planner.get("id_prefixes", {}),
        planner_human_paths=planner.get("human_paths", ["contracts/"]),
        planner_token_secret=planner.get("claude_token_secret", lanes[0].token_secret),
        planner_schedule=planner.get("schedule", "17 * * * *"),
        review_token_secret=data.get("review_token_secret", "CLAUDE_TOKEN_REVIEW"),
        ci_workflow_name=data.get("ci_workflow_name", ""),
        tool_ref=data.get("tool_ref", "main"),
        bot_login=data.get("bot_login", ""),
    )
    if cfg.fix_lane and cfg.fix_lane not in names:
        raise ConfigError(f"fix_lane {cfg.fix_lane!r} is not a lane")
    unknown = sorted(set(cfg.planner_id_prefixes) - set(names))
    if unknown:
        raise ConfigError(f"planner.id_prefixes names unknown lane(s): {', '.join(unknown)}")
    # A Claude token secret that is also a PAT secret would hand the PAT to the agent job.
    pats = {"AGENT_PAT"} | {lane.agent_pat_secret for lane in lanes}  # AGENT_PAT is always passed as fallback
    claude = {lane.token_secret for lane in lanes} | {cfg.planner_token_secret, cfg.review_token_secret}
    clash = sorted(pats & claude)
    if clash:
        raise ConfigError(f"secret(s) {', '.join(clash)} used both as a Claude token and as a PAT: the agent job "
                          "would receive the PAT")
    return cfg


def loads(text: str) -> Config:
    try:
        return parse(yamlish.loads(text))
    except yamlish.YAMLError as err:
        raise ConfigError(f"{CONFIG_PATH}: {err}") from err


def load(root: str | Path = ".") -> Config:
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"{path} not found; run `gh nightshift init` first")
    return loads(path.read_text(encoding="utf-8"))
