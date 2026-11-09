"""Exportable audit log: one row per agent PR with lane, issue, outcome, reason code, model, prompt version and
cost (API-equivalent USD from the night log). Read-only; trusted PRs and comments only."""
from __future__ import annotations

import csv
import io
import json
import re

from . import protocol as ns
from .config import Config
from .gh import GH
from .picker import COST_LINE_RE
from .rules import BRANCH_RE

FIELDS = ["pr", "issue", "lane", "outcome", "reason", "model", "prompt_version", "cost_usd", "created_at",
          "closed_at", "url"]
MODEL_RE = re.compile(r"\*\*Model:\*\*\s*`([^`]+)`")
PROMPT_RE = re.compile(r"\*\*Prompt:\*\*\s*`([A-Za-z0-9._-]{1,60})`")


def rows(gh: GH, cfg: Config, since: str = "") -> list[dict]:
    trusted = gh.trusted
    costs: dict[int, float] = {}
    log = gh.night_log(cfg.label("night_log"))
    if log:
        for c in gh.comments(log):
            if (c.get("user") or {}).get("login") not in trusted:
                continue
            first = (c.get("body") or "").split("\n", 1)[0]
            m, pr = COST_LINE_RE.search(first), re.search(r"/pull/(\d+)\b", first)
            if m and pr:
                costs[int(pr.group(1))] = costs.get(int(pr.group(1)), 0.0) + float(m.group("cost"))
    out = []
    for p in gh.paginate(gh.r("pulls"), {"state": "all", "sort": "created", "direction": "asc"}):
        m = BRANCH_RE.match((p.get("head") or {}).get("ref", ""))
        if not m or (p.get("user") or {}).get("login") not in trusted:
            continue
        if since and (p.get("created_at") or "") < since:
            continue
        body = p.get("body") or ""
        outcome = "merged" if p.get("merged_at") else p.get("state", "")
        reason = ""
        if outcome != "open":
            reason = ns.pr_reason(gh.comments(p["number"]), trusted) or ("verdict_passed" if p.get("merged_at")
                                                                         else "manual")
        header = next((ln for ln in body.splitlines() if ln.startswith("**Lane:**")), "")
        model = MODEL_RE.search(header)
        prompt = PROMPT_RE.search(header)  # the header line only: the agent's summary follows it
        out.append({"pr": p["number"], "issue": int(m.group(2)), "lane": m.group(1), "outcome": outcome,
                    "reason": reason, "model": model.group(1) if model else "",
                    "prompt_version": prompt.group(1) if prompt else "",
                    "cost_usd": f"{costs[p['number']]:.2f}" if p["number"] in costs else "",
                    "created_at": p.get("created_at") or "", "closed_at": p.get("merged_at") or p.get("closed_at") or "",
                    "url": p.get("html_url") or ""})
    return out


def render(data: list[dict], fmt: str = "csv") -> str:
    if fmt == "jsonl":
        return "".join(json.dumps(r, sort_keys=True) + "\n" for r in data)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for r in data:
        # A leading = + - @ would run as a formula in a spreadsheet; model names come from PR bodies.
        w.writerow({k: ("'" + str(v) if str(v)[:1] and str(v)[0] in "=+-@\t\r" else v) for k, v in r.items()})
    return buf.getvalue()
