"""`gh nightshift status` and `gh nightshift stats` (read-only)."""
from __future__ import annotations

import io
import re
from collections import Counter

from . import protocol as ns
from .config import Config
from .gh import GH
from .picker import Picker, Runtime
from .planner import ready_counts
from .rules import BRANCH_RE, checks_state, label_names, lanes_of
from .stats_math import fmt_rate

COST_RE = re.compile(r"≈\$([0-9]+(?:\.[0-9]+)?)")
MODEL_RE = re.compile(r"\*\*Model:\*\*\s*`([^`]+)`")


def status(gh: GH, cfg: Config, enabled: bool = True, log_lines: int = 10) -> str:
    out = []
    # Each lane's decision right now, from a picker run against a dry-run client: reads are real,
    # writes are only recorded, so status never changes anything.
    shadow = GH(gh.repo, transport=gh.transport, dry_run=True, out=io.StringIO())
    shadow.login = gh.login
    out.append("Lanes (what each picker would do now):")
    for lane in cfg.lanes:
        try:
            result = Picker(shadow, cfg, Runtime(lane=lane.name, enabled=True)).decide()
            what = result["reason"] if result["mode"] != "fix-main" else "fix main: " + result["reason"]
        except Exception as err:  # status must not crash on one lane
            what = f"error: {err}"
        out.append(f"  {lane.name:12} {what}")
    if not enabled:
        out.append("  (AGENTS_ENABLED is not true: no lane runs until `gh nightshift start`)")
    counts = ready_counts(gh, cfg)
    out.append("")
    out.append("Queue (ready issues per lane): " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    prs = [p for p in gh.paginate(gh.r("pulls"), {"state": "open"}) if BRANCH_RE.match((p.get("head") or {}).get("ref", ""))
           or (p.get("head") or {}).get("ref", "").startswith("agent/")]
    out.append("")
    out.append(f"Open agent PRs: {len(prs)}")
    for p in prs:
        sha = p["head"]["sha"]
        runs = gh.paginate(gh.r(f"commits/{sha}/check-runs"), key="check_runs")
        statuses = (gh.get(gh.r(f"commits/{sha}/status")) or {}).get("statuses", [])
        out.append(f"  #{p['number']} {p.get('title', '')} [{checks_state(runs, statuses, cfg.required_checks)}]")
    stuck = [i for i in gh.issues() if label_names(i) & {cfg.label("blocked"), cfg.label("needs_human"), "waiting"}]
    out.append("")
    out.append(f"Blocked / needs-human / waiting: {len(stuck)}")
    for i in stuck:
        out.append(f"  #{i['number']} [{', '.join(lanes_of(i)) or '-'}] {i.get('title', '')} "
                   f"({', '.join(sorted(label_names(i) & {cfg.label('blocked'), cfg.label('needs_human'), 'waiting'}))})")
    log = gh.night_log(cfg.label("night_log"))
    out.append("")
    if log:
        comments = gh.comments(log)
        lines = [c.get("body", "").strip() for c in comments if not ns.parse(c)][-log_lines:]
        out.append(f"Night log (#{log}, last {len(lines)}):")
        out.extend(f"  {line}" for line in lines)
    else:
        out.append("Night log: none (run `gh nightshift setup`)")
    return "\n".join(out)


def stats(gh: GH, cfg: Config, audit_csv: str | None = None) -> str:
    pulls = [p for p in gh.paginate(gh.r("pulls"), {"state": "all"})
             if BRANCH_RE.match((p.get("head") or {}).get("ref", "")) or
             (p.get("head") or {}).get("ref", "").startswith("agent/")]
    merged = [p for p in pulls if p.get("merged_at")]
    closed = [p for p in pulls if p.get("state") == "closed" and not p.get("merged_at")]
    open_ = [p for p in pulls if p.get("state") == "open"]
    decided = len(merged) + len(closed)
    out = [f"Agent PRs: {len(pulls)} ({len(merged)} merged, {len(closed)} closed unmerged, {len(open_)} open)",
           f"Merge rate: {fmt_rate(len(merged), decided)}"]
    reasons: Counter = Counter()
    for p in closed:
        reasons[ns.pr_reason(gh.comments(p["number"]), gh.login) or "manual"] += 1
    if closed:
        out.append("Unmerged by reason (from ns markers; no marker = manual):")
        for reason, n in reasons.most_common():
            out.append(f"  {reason:15} {n:4}  {fmt_rate(n, len(closed))}")
    models = [m.group(1) for p in merged if (m := MODEL_RE.search(p.get("body") or ""))]
    escalated = sum(1 for m in models if m == cfg.escalate_model)
    out.append(f"Escalation share (merged PRs built by {cfg.escalate_model}): {fmt_rate(escalated, len(models))}")
    log = gh.night_log(cfg.label("night_log"))
    costs = []
    if log:
        # Only writers' comments count, so an outsider cannot inflate the cost figure.
        writers = {"OWNER", "MEMBER", "COLLABORATOR"}
        costs = [float(m.group(1)) for c in gh.comments(log)
                 if (c.get("author_association") in writers or (c.get("user") or {}).get("login") == gh.login)
                 and (m := COST_RE.search(c.get("body") or ""))]
    if merged and costs:
        out.append(f"Est. cost per merged PR: ${sum(costs) / len(merged):.2f} API-equivalent "
                   f"(${sum(costs):.2f} over {len(costs)} logged runs)")
    else:
        out.append("Est. cost per merged PR: n/a (no costs in the night log yet)")
    blocked_now = len(gh.issues([cfg.label("blocked")], state="all"))
    out.append(f"Blocked issues (label, open or closed): {blocked_now}")
    if audit_csv:
        from .audit import BAD_LABELS, catch_rate, read_csv

        rows = read_csv(audit_csv)
        use = "human_label" if any(r["human_label"] for r in rows) else "llm_label"
        labelled = [r for r in rows if r[use]]
        bad = sum(1 for r in labelled if r[use] in BAD_LABELS)
        out.append("")
        out.append(f"Review gate catch rate (from {audit_csv}, {use}{'' if use == 'human_label' else ', not ground truth'}):")
        out.append("  " + catch_rate(reasons.get("verdict_failed", 0), len(merged), (bad, len(labelled))))
    return "\n".join(out)
