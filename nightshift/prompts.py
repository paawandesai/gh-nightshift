"""Prompt templates and output schemas. A project overrides a prompt by committing
`.night-shift/prompts/<name>.md` (worker, review, planner, security, reduce, redteam) on the default branch."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .config import Config
from .planner import PLAN_SCHEMA
from .redteam import REDTEAM_SCHEMA

_STRS = {"type": "array", "items": {"type": "string"}}
WORKER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "stuck", "waiting", "decision"]},
        "summary": {"type": "string"},
        "assumptions": _STRS,
        "tests_passed": {"type": "boolean"},
        "needs": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "lane": {"type": "string"}, "title": {"type": "string"}, "goal": {"type": "string"},
            "acceptance": _STRS, "interface": {"type": "string"}},
            "required": ["lane", "title", "goal", "acceptance"]}},
        "notes": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "issue": {"type": "integer"}, "text": {"type": "string"}}, "required": ["issue", "text"]}},
        "follow_ups": {"type": "array", "items": {"type": "object", "properties": {
            "lane": {"type": "string"}, "title": {"type": "string"}, "goal": {"type": "string"},
            "acceptance": _STRS}, "required": ["title", "goal"]}},
        "handoff": {"type": "object", "properties": {
            "files": _STRS, "interfaces": _STRS, "env_vars": _STRS, "decisions": _STRS, "assumptions": _STRS,
            "interface_differences": {"type": "string"}}},
    },
    "required": ["status", "summary", "assumptions", "tests_passed"],
}

_RULE_ANSWERS = {"type": "array", "items": {"type": "object", "properties": {
    "key": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no"]}, "evidence": {"type": "string"}},
    "required": ["key", "answer", "evidence"]}}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "answers": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no"]},
            "evidence": {"type": "string"}}, "required": ["question", "answer", "evidence"]}},
        "fixes": {"type": "string"},
        "main_changed_overlap": {"type": "boolean"},
        "still_correct_against_main": {"type": "string"},
        "rule_answers": _RULE_ANSWERS,
    },
    "required": ["verdict", "answers", "main_changed_overlap"],
}

SECURITY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "rule_answers": _RULE_ANSWERS,
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "location": {"type": "string"}, "evidence": {"type": "string"}, "attack_path": {"type": "string"}},
            "required": ["title", "severity", "location", "evidence", "attack_path"]}},
        "fixes": {"type": "string"},
    },
    "required": ["verdict", "rule_answers", "findings"],
}

SCHEMAS = {"worker": WORKER_SCHEMA, "review": REVIEW_SCHEMA, "planner": PLAN_SCHEMA, "security": SECURITY_SCHEMA,
           "reduce": PLAN_SCHEMA, "redteam": REDTEAM_SCHEMA}
PROMPTS = ("worker", "review", "planner", "security", "reduce", "redteam")
# Appended when a project's own review or security prompt predates the rules file, so its hits are still seen.
RULES_FALLBACK = ("\n\nRepo review rules: `{path}` lists code this PR adds or changes that the repository's own rules "
                  "selected. Answer every item in `rule_answers` with its key exactly as listed: `yes` only if the "
                  "item's question holds for that code, quoting the lines that show it, otherwise `no` with the fix.\n")


def template(name: str, root: str | Path = ".") -> str:
    override = Path(root) / ".night-shift" / "prompts" / f"{name}.md"
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return resources.files("nightshift").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")


def version(name: str, root: str | Path = ".") -> str:
    """Which prompt an agent ran with: `builtin` or `repo` (a committed override) plus a short content hash."""
    import hashlib

    source = "repo" if (Path(root) / ".night-shift" / "prompts" / f"{name}.md").is_file() else "builtin"
    return f"{name}-{source}-{hashlib.sha256(template(name, root).encode()).hexdigest()[:12]}"


def render(name: str, values: dict[str, str], root: str | Path = ".") -> str:
    text = template(name, root)
    if "RULES_FILE" in values and "__RULES_FILE__" not in text:
        text += RULES_FALLBACK.format(path=values["RULES_FILE"])
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    return text


def worker_values(cfg: Config, lane: str, task_file: str) -> dict[str, str]:
    ln = cfg.lane(lane)
    return {"LANE": lane, "TASK_FILE": task_file,
            "LANE_PATHS": ", ".join(f"`{p}`" for p in ln.paths) or "(not listed; infer from the ticket)",
            "TEST_COMMAND": ln.test_command or "see CLAUDE.md",
            "PROTECTED": ", ".join(f"`{p}`" for p in cfg.protected_paths) or "(none)",
            "LANES": ", ".join(f"`{x.name}`" + (f" ({', '.join(x.paths)})" if x.paths else "") for x in cfg.lanes)}


def schema_json(name: str) -> str:
    return json.dumps(SCHEMAS[name], separators=(",", ":"))
