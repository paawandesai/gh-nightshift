"""What differs between the coding agents a lane can run (Claude Code, OpenAI Codex).

Everything after the agent job (publish, the night log, stats) reads one result shape, Claude Code's
`--output-format json`: `structured_output`, `num_turns`, `total_cost_usd`, `result`. A Codex run leaves
`codex.jsonl` (the `codex exec --json` event stream) and `last-message.json` (its final message, written to
match the output schema) instead; `codex_result` turns those into the same shape. Like every file from the
agent job, they are untrusted: this only reshapes them, and publish still validates and defangs the content.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

CODEX_EVENTS = "codex.jsonl"
CODEX_LAST_MESSAGE = "last-message.json"
MAX_EVENT_BYTES = 50_000_000  # a runaway log is cut, not parsed whole


def strict_schema(schema: dict) -> dict:
    """The output schema in the form OpenAI's structured outputs accept (Codex `--output-schema`): every object
    closes with additionalProperties false and lists all its properties as required; a property that was
    optional becomes nullable instead. `drop_nulls` undoes that on the way back, so publish sees Claude's shape.
    maxItems is dropped (strict mode may refuse it); publish caps those lists itself."""
    def walk(node: dict) -> dict:
        node = {k: v for k, v in node.items() if k != "maxItems"}
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            required = set(node.get("required") or [])
            props = {}
            for key, sub in node["properties"].items():
                sub = walk(sub)
                if key not in required:
                    sub = _nullable(sub)
                props[key] = sub
            node["properties"] = props
            node["required"] = list(props)
            node["additionalProperties"] = False
        elif node.get("type") == "array" and isinstance(node.get("items"), dict):
            node["items"] = walk(node["items"])
        return node
    return walk(copy.deepcopy(schema))


def _nullable(node: dict) -> dict:
    kind = node.get("type")
    if isinstance(kind, str):
        node = {**node, "type": [kind, "null"]}
        if "enum" in node:
            node["enum"] = [*node["enum"], None]
    return node


def drop_nulls(value):
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value if v is not None]
    return value


def _events(path: Path) -> list[dict]:
    try:
        raw = path.read_bytes()[:MAX_EVENT_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _error_text(event: dict) -> str:
    err = event.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return str(event.get("message") or err or "")


def codex_result(out_dir: str | Path) -> dict | None:
    """Claude-shaped result for a Codex run in `out_dir`, or None when the run left no event stream.

    num_turns counts the completed work items (commands, file changes, messages, tool calls), which is what the
    "never started" rule needs: a run that failed before doing anything has none. Codex reports token usage, not
    cost, so total_cost_usd is absent and a Codex lane has no budget cap.
    """
    out = Path(out_dir)
    events_path = out / CODEX_EVENTS
    if not events_path.is_file():
        return None
    events = _events(events_path)
    items = [e for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
             and e["item"].get("type") != "reasoning"]
    errors = [_error_text(e) for e in events if e.get("type") in ("turn.failed", "error")]
    usage: dict = {}
    for e in events:
        if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            for key, value in e["usage"].items():
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
    result: dict = {"provider": "codex", "num_turns": len(items), "usage": usage}
    last = out / CODEX_LAST_MESSAGE
    try:
        structured = json.loads(last.read_text(encoding="utf-8")) if last.is_file() else None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        structured = None
    if isinstance(structured, dict):
        result["structured_output"] = drop_nulls(structured)
    if errors:
        result["result"] = next((e for e in reversed(errors) if e), "codex reported an error")
    elif not isinstance(structured, dict):
        messages = [e["item"].get("text") for e in items if e["item"].get("type") == "agent_message"]
        result["result"] = str(messages[-1] if messages else "no result")
    return result


def load_result(result_path: str | Path | None) -> Path | dict | None:
    """The run's result: result.json when the agent job wrote one (Claude, or the token guard's verdict),
    else a Codex run's reshaped events from the same directory. None when there is neither."""
    if not result_path:
        return None
    path = Path(result_path)
    if path.is_file():
        return path
    return codex_result(path.parent)
