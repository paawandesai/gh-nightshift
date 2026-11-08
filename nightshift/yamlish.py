"""A small, strict YAML subset (stdlib only), enough for `.night-shift/config.yml`.

Supported: block mappings and sequences by indentation, `- key: value` sequence items, comments,
plain / 'single' / "double" quoted scalars, ints, floats, true/false, null/~, flow lists `[a, b]`,
empty `{}` / `[]`, and `|` literal block scalars. Anything else (anchors, tags, multi-docs, flow
mappings with content) is rejected with a line number rather than guessed at.
"""
from __future__ import annotations

import json
import re
from typing import Any


class YAMLError(ValueError):
    pass


_INT = re.compile(r"^[-+]?\d+$")
_FLOAT = re.compile(r"^[-+]?\d+\.\d+$")


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_flow(text: str, lineno: int) -> list[str]:
    items, cur, quote = [], [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur).strip())
            cur = []
        elif ch in "[]{}":
            raise YAMLError(f"line {lineno}: nested flow collections are not supported")
        else:
            cur.append(ch)
    if quote:
        raise YAMLError(f"line {lineno}: unterminated quote")
    last = "".join(cur).strip()
    if last:
        items.append(last)
    return items


def scalar(text: str, lineno: int = 0) -> Any:
    t = text.strip()
    if t == "" or t in ("~", "null"):
        return None
    if t[0] in "&*!|>" and t not in ("|",):
        raise YAMLError(f"line {lineno}: anchors, aliases, tags and folded scalars are not supported")
    if t.startswith('"'):
        if not t.endswith('"') or len(t) < 2:
            raise YAMLError(f"line {lineno}: unterminated double quote")
        try:
            return json.loads(t)
        except json.JSONDecodeError as err:
            raise YAMLError(f"line {lineno}: bad double-quoted string") from err
    if t.startswith("'"):
        if not t.endswith("'") or len(t) < 2:
            raise YAMLError(f"line {lineno}: unterminated single quote")
        return t[1:-1].replace("''", "'")
    if t == "[]":
        return []
    if t == "{}":
        return {}
    if t.startswith("["):
        if not t.endswith("]"):
            raise YAMLError(f"line {lineno}: unterminated flow list")
        return [scalar(x, lineno) for x in _split_flow(t[1:-1], lineno)]
    if t.startswith("{"):
        raise YAMLError(f"line {lineno}: flow mappings are not supported; use block style")
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t):
        return float(t)
    return t


_KEY = re.compile(r"^([A-Za-z0-9_.\-/]+|\"[^\"]*\"|'[^']*')\s*:(?:\s+(.*)|\s*)$")


class _Parser:
    def __init__(self, text: str):
        self.raw = text.splitlines()
        self.lines: list[tuple[int, int, str]] = []  # (lineno, indent, content)
        for n, line in enumerate(self.raw, 1):
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise YAMLError(f"line {n}: tabs are not allowed for indentation")
            stripped = _strip_comment(line)
            if stripped.strip() in ("---", "..."):
                if self.lines:
                    raise YAMLError(f"line {n}: multiple documents are not supported")
                continue
            if stripped.strip():
                self.lines.append((n, len(stripped) - len(stripped.lstrip()), stripped.strip()))
        self.i = 0

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self.block(self.lines[0][1])
        if self.i < len(self.lines):
            n, _, _ = self.lines[self.i]
            raise YAMLError(f"line {n}: unexpected indentation")
        return value

    def block(self, indent: int) -> Any:
        _, ind, content = self.lines[self.i]
        if content.startswith("- ") or content == "-":
            return self.seq(ind)
        return self.mapping(ind)

    def literal(self, parent_indent: int, lineno: int) -> str:
        # Use raw lines: comments and blank lines inside a literal block are content.
        out: list[str] = []
        start = lineno  # raw index of the first line after the "|" line
        block_indent = None
        j = start
        while j < len(self.raw):
            line = self.raw[j]
            if line.strip() == "":
                out.append("")
                j += 1
                continue
            ind = len(line) - len(line.lstrip())
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                break
            out.append(line[block_indent:])
            j += 1
        while out and out[-1] == "":
            out.pop()
        # skip parsed lines that belonged to the literal
        while self.i < len(self.lines) and self.lines[self.i][0] <= j:
            self.i += 1
        return "\n".join(out) + ("\n" if out else "")

    def value_after(self, rest: str | None, indent: int, lineno: int) -> Any:
        if rest is None or rest == "":
            if self.i < len(self.lines) and self.lines[self.i][1] > indent:
                return self.block(self.lines[self.i][1])
            if self.i < len(self.lines) and self.lines[self.i][1] == indent and self.lines[self.i][2].startswith("- "):
                return self.seq(indent)  # "key:\n- a" (sequence at the key's own indentation)
            return None
        if rest == "|" or rest == "|-":
            text = self.literal(indent, lineno)
            return text.rstrip("\n") if rest == "|-" else text
        return scalar(rest, lineno)

    def mapping(self, indent: int) -> dict:
        out: dict[str, Any] = {}
        while self.i < len(self.lines):
            n, ind, content = self.lines[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise YAMLError(f"line {n}: unexpected indentation")
            if content.startswith("- "):
                break
            m = _KEY.match(content)
            if not m:
                raise YAMLError(f"line {n}: expected 'key: value'")
            key = scalar(m.group(1), n) if m.group(1)[0] in "'\"" else m.group(1)
            if key in out:
                raise YAMLError(f"line {n}: duplicate key {key!r}")
            self.i += 1
            out[str(key)] = self.value_after(m.group(2), indent, n)
        return out

    def seq(self, indent: int) -> list:
        out: list[Any] = []
        while self.i < len(self.lines):
            n, ind, content = self.lines[self.i]
            if ind < indent or not (content.startswith("- ") or content == "-"):
                if ind > indent:
                    raise YAMLError(f"line {n}: unexpected indentation")
                break
            if ind > indent:
                raise YAMLError(f"line {n}: unexpected indentation")
            rest = content[1:].strip()
            if rest and _KEY.match(rest):
                # "- key: value" starts a mapping whose other keys are indented to the key's column
                self.lines[self.i] = (n, ind + (len(content) - len(content[1:].lstrip())), rest)
                out.append(self.mapping(self.lines[self.i][1]))
            else:
                self.i += 1
                out.append(self.value_after(rest, ind, n) if rest else
                           (self.block(self.lines[self.i][1]) if self.i < len(self.lines)
                            and self.lines[self.i][1] > ind else None))
        return out


def loads(text: str) -> Any:
    return _Parser(text).parse()


# --- dumping (used by `init` to write the config) --------------------------------------

def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or s != s.strip() or re.search(r"[:#\[\]{},&*!|>'\"%@`]", s) or \
            s.lower() in ("true", "false", "null", "~", "yes", "no") or _INT.match(s) or _FLOAT.match(s):
        return json.dumps(s)
    return s


def dumps(data: Any, indent: int = 0) -> str:
    pad = " " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{k}:")
                lines.append(dumps(v, indent + 2))
            elif isinstance(v, list) and v and any(isinstance(x, (dict, list)) for x in v):
                lines.append(f"{pad}{k}:")
                lines.append(dumps(v, indent + 2))
            elif isinstance(v, list):
                lines.append(f"{pad}{k}: [{', '.join(_dump_scalar(x) for x in v)}]")
            elif isinstance(v, str) and "\n" in v:
                lines.append(f"{pad}{k}: |")
                lines.extend(f"{pad}  {ln}" if ln else "" for ln in v.rstrip("\n").split("\n"))
            else:
                lines.append(f"{pad}{k}: {_dump_scalar(v) if not (isinstance(v, dict)) else '{}'}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                body = dumps(item, indent + 2).split("\n")
                lines.append(f"{pad}- {body[0].lstrip()}")
                lines.extend(body[1:])
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")
    return "\n".join(lines)
