"""Morning digest: what merged, what is stuck and what it cost, posted to the night-log issue and optionally to a
Slack or Discord webhook. Read-only except for the one comment (and the webhook POST)."""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable

from . import protocol as ns
from .config import Config
from .gh import GH
from .picker import COST_LINE_RE
from .rules import BRANCH_RE, label_names, lanes_of


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def build(gh: GH, cfg: Config, hours: float = 24, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours)
    trusted = gh.trusted
    pulls = [p for p in gh.paginate(gh.r("pulls"), {"state": "all", "sort": "updated", "direction": "desc"})
             if BRANCH_RE.match((p.get("head") or {}).get("ref", "")) and (p.get("user") or {}).get("login") in trusted]
    merged = [p for p in pulls if (_ts(p.get("merged_at")) or since) > since]
    closed = [p for p in pulls if not p.get("merged_at") and (_ts(p.get("closed_at")) or since) > since]
    open_prs = [p for p in pulls if p.get("state") == "open"]
    reasons = Counter(ns.pr_reason(gh.comments(p["number"]), trusted) or "manual" for p in closed)

    stuck_labels = {cfg.label("blocked"), cfg.label("needs_human"), "waiting"}
    stuck = [i for i in gh.issues() if label_names(i) & stuck_labels]

    cost: dict[str, float] = defaultdict(float)
    log = gh.night_log(cfg.label("night_log"))
    if log:
        for c in gh.comments(log):
            if (c.get("user") or {}).get("login") not in trusted or (_ts(c.get("created_at")) or now) < since:
                continue
            m = COST_LINE_RE.search((c.get("body") or "").splitlines()[0] if c.get("body") else "")
            if m:
                cost[m.group("lane")] += float(m.group("cost"))

    title = lambda x: ns.defang(" ".join(str(x.get("title", "")).split()))[:90]  # noqa: E731
    lines = [f"**Night shift digest** for the last {hours:g} h (until {now:%Y-%m-%d %H:%M} UTC)", ""]
    nums = ", ".join("#" + str(p["number"]) for p in merged)
    lines.append(f"- Merged: **{len(merged)}**" + (f" ({nums})" if merged else ""))
    lines.append(f"- Closed without merging: **{len(closed)}**"
                 + (f" ({', '.join(f'{k} {v}' for k, v in sorted(reasons.items()))})" if closed else ""))
    lines.append(f"- Open agent PRs: **{len(open_prs)}**")
    lines.append(f"- Blocked, needs-human or waiting: **{len(stuck)}**")
    total = sum(cost.values())
    lines.append(f"- Cost (API-equivalent, from the night log): **${total:.2f}**"
                 + (f" ({', '.join(f'{k} ${v:.2f}' for k, v in sorted(cost.items()))})" if cost else "")
                 + (f", ${total / len(merged):.2f} per merged PR" if merged and total else ""))
    if merged:
        lines += ["", "**Merged**", *[f"- #{p['number']} {title(p)}" for p in merged[:20]]]
    if stuck:
        lines += ["", "**Needs a person**"]
        for i in stuck[:20]:
            why = ", ".join(sorted(label_names(i) & stuck_labels))
            lines.append(f"- #{i['number']} [{', '.join(lanes_of(i)) or '-'}] {title(i)} ({why})")
    return "\n".join(lines) + "\n"


def post(gh: GH, cfg: Config, text: str) -> str:
    log = gh.night_log(cfg.label("night_log"))
    if not log:
        return "no night-log issue (run `gh nightshift setup`); digest not posted"
    gh.comment(log, ns.marker("digest", date=dt.date.today().isoformat()) + "\n" + text)
    return f"digest posted on #{log}"


def webhook_payload(url: str, text: str) -> dict:
    """Slack incoming webhooks take {"text"}; Discord takes {"content"} (max 2000 characters)."""
    plain = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text) if "slack" in url else text
    if re.search(r"discord(app)?\.com/api/webhooks/", url):
        return {"content": text[:1990]}
    return {"text": plain}


def send_webhook(url: str, text: str, dry_run: bool = False,
                 opener: Callable[[urllib.request.Request], object] | None = None) -> str:
    if not url.startswith("https://"):
        return "webhook not sent: the URL must start with https://"
    if dry_run:
        return "[dry-run] would POST the digest to the webhook (URL not shown)"
    req = urllib.request.Request(url, data=json.dumps(webhook_payload(url, text)).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "gh-nightshift"},
                                 method="POST")
    try:
        (opener or (lambda r: urllib.request.urlopen(r, timeout=20)))(req)
    except Exception as err:  # never print the URL: it is a secret
        return f"webhook failed: {type(err).__name__}"
    return "digest sent to the webhook"
