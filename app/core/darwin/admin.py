"""Darwin's admin routes, the parts that are not HTTP: `/api/admin-metrics`' aggregation and
`/api/admin-org-brands`' domain rules (`netlify/functions/admin-metrics.ts`, `admin-org-brands.ts`).

`metrics_body` turns the storage port's admin rows (`AnalyticsPort.admin_overview` /
`admin_activity`) into the dashboard's nine keys exactly as Darwin computed them, quirks included:

* the `summary.*30d` names do not change with `days`;
* `recentDecks`, `transcripts` and `slideTranscripts` ignore `days`; decks, events and spend use it;
* `topClicks` / `perUserClicks` are computed from at most 2000 events;
* the day buckets are UTC days, oldest first, exactly `days` of them, ending today.

What differs: the costs are real (C10 is fixed in the ledger, app/core/darwin/usage.py), so
`totalCostUsd` and `dailyActivity[].costUsd` are no longer always 0.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

from app.core.brand.kit import js_plain
from app.core.storage.models import AdminActivity, AdminOverview

NO_EMAIL = "—"
DEFAULT_DAYS = 30

# ------------------------------------------------------------------------------- org brand domains
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
#: Consumer mailbox providers: mapping one would hand a company brand to everyone with an address there.
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "qq.com",
    "163.com", "hotmail.co.uk", "yahoo.co.uk", "outlook.co.uk",
})


class DomainError(ValueError):
    """A domain Darwin answers with 400; the message is Darwin's exact text."""


def normalize_domain(raw: Any) -> str:
    """`normalizeDomain`: trimmed, lower case, one leading '@' stripped; a bare domain that is not a
    public mailbox provider."""
    if not isinstance(raw, str):
        raise DomainError("domain is required")
    domain = raw.strip().lower()
    domain = domain[1:] if domain.startswith("@") else domain
    if not DOMAIN_RE.match(domain):
        raise DomainError("Enter a bare domain like acme.com")
    if domain in PUBLIC_EMAIL_DOMAINS:
        raise DomainError(f"{domain} is a public email provider and can't be mapped to a brand")
    return domain


# ---------------------------------------------------------------------------------------- metrics
def iso(value: datetime | None) -> str | None:
    """A timestamp as PostgREST wrote it (`2026-10-08T09:00:00.123456+00:00`)."""
    return value.isoformat() if value is not None else None


def js_string(value: Any) -> str:
    """`String(value)` for a JSON value that is not null."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        return str(js_plain(value)) if math.isfinite(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, list):
        return ",".join("" if v is None else js_string(v) for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _js_or_empty(value: Any) -> Any:
    """`value ?? ''`."""
    return "" if value is None else value


def metrics_body(overview: AdminOverview, activity: AdminActivity, *, days: int, now: datetime) -> dict[str, Any]:
    """The /api/admin-metrics body (see the module docstring)."""
    profiles = overview.profiles
    decks = overview.decks_since
    usage = overview.usage_since
    email_of = {p.id: (p.email if p.email is not None else NO_EMAIL) for p in profiles}

    today = now.date().isoformat()
    total_cost = 0.0
    for row in usage:
        total_cost += row.est_cost_usd or 0.0

    daily: dict[str, dict[str, Any]] = {}
    for i in range(days - 1, -1, -1):
        daily[(now - timedelta(days=i)).date().isoformat()] = {"decks": 0, "costUsd": 0.0}
    for deck in decks:
        bucket = daily.get(deck.created_at.date().isoformat())
        if bucket is not None:
            bucket["decks"] += 1
    for row in usage:
        bucket = daily.get(row.created_at.date().isoformat())
        if bucket is not None:
            bucket["costUsd"] += row.est_cost_usd or 0.0

    events = activity.events
    clicks: dict[str, dict[str, Any]] = {}
    per_user: dict[str, dict[str, Any]] = {}
    per_user_texts: dict[str, dict[str, int]] = {}
    for event in events:
        if event.event_name != "element_clicked":
            continue
        props = event.properties
        text = js_string(_js_or_empty(props.get("text")))
        path_value = props.get("path")
        path = js_string(_js_or_empty(path_value if path_value is not None else event.page_path))
        key = f"{path}|{text}"
        clicks.setdefault(key, {"text": text, "path": path, "count": 0})["count"] += 1
        uid = event.user_id
        summary = per_user.setdefault(uid, {"email": email_of.get(uid, NO_EMAIL), "total": 0, "topText": text,
                                            "topCount": 0})
        summary["total"] += 1
        texts = per_user_texts.setdefault(uid, {})
        texts[text] = texts.get(text, 0) + 1
    for uid, summary in per_user.items():
        ranked = sorted(per_user_texts.get(uid, {}).items(), key=lambda kv: -kv[1])
        if ranked:
            summary["topText"], summary["topCount"] = ranked[0]

    return {
        "summary": {
            "totalUsers": len(profiles),
            "totalDecks30d": len(decks),
            "decksToday": sum(1 for d in decks if d.created_at.date().isoformat() == today),
            "totalCostUsd": js_plain(total_cost),
            "usersWithDecks": sum(1 for p in profiles if p.lifetime_decks >= 1),
            "powerUsers": sum(1 for p in profiles if p.lifetime_decks >= 3),
            "chatDecks": sum(1 for d in decks if d.creation_method == "intake_chat"),
            "wizardDecks": sum(1 for d in decks if d.creation_method == "wizard"),
            "totalRefines30d": sum(d.refine_count for d in decks),
            "totalExports30d": sum(d.export_count for d in decks),
        },
        "dailyActivity": [{"date": day, "decks": v["decks"], "costUsd": js_plain(v["costUsd"])}
                          for day, v in daily.items()],
        "users": [{
            "id": p.id, "email": p.email if p.email is not None else NO_EMAIL, "createdAt": iso(p.created_at),
            "lifetimeDecks": p.lifetime_decks, "decksToday": p.decks_today, "isAdmin": p.is_admin,
            "totalSlidesGenerated": p.total_slides_generated, "totalRefinements": p.total_refinements,
            "totalExports": p.total_exports, "lastSeenAt": iso(p.last_seen_at),
        } for p in profiles],
        "recentDecks": [{
            "id": d.id, "title": d.title or "Untitled", "ownerEmail": email_of.get(d.owner_id, NO_EMAIL),
            "status": d.status, "createdAt": iso(d.created_at), "creationMethod": d.creation_method,
            "slideCount": d.slide_count, "refineCount": d.refine_count, "exportCount": d.export_count,
        } for d in overview.recent_decks],
        "events": [{
            "userId": e.user_id, "userEmail": email_of.get(e.user_id, NO_EMAIL), "eventName": e.event_name,
            "properties": e.properties, "pagePath": e.page_path, "createdAt": iso(e.created_at),
        } for e in events],
        "transcripts": [{
            "id": t.id, "userEmail": email_of.get(t.user_id, NO_EMAIL), "deckId": t.deck_id,
            "turnCount": t.turn_count, "totalChars": t.total_chars, "messages": t.messages, "brief": t.brief,
            "createdAt": iso(t.created_at), "completedAt": iso(t.completed_at),
        } for t in activity.intake],
        "topClicks": sorted(clicks.values(), key=lambda c: -int(c["count"]))[:50],
        "perUserClicks": sorted(per_user.values(), key=lambda u: -int(u["total"])),
        "slideTranscripts": [{
            "id": t.id, "userEmail": email_of.get(t.user_id, NO_EMAIL), "deckId": t.deck_id,
            "slideNumber": t.slide_number, "messages": t.messages, "refineCount": t.refine_count,
            "updatedAt": iso(t.updated_at),
        } for t in activity.slide_edits],
    }
