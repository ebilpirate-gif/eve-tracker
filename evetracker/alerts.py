"""Everything that needs your attention, shown at the top of the Dashboard."""
import time
from datetime import datetime, timezone

from . import contracts, corp, db, health, market


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _hours_until(iso: str) -> float:
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() - time.time()) / 3600


def skill_queues() -> list[dict]:
    """Per character: what's training and when the queue runs out."""
    out = []
    now_iso = _iso(time.time())
    for c in db.q("SELECT character_id, name, scopes, total_sp FROM characters ORDER BY name"):
        if "esi-skills.read_skillqueue.v1" not in (c["scopes"] or "").split():
            out.append({"character_id": c["character_id"], "name": c["name"], "unknown": True})
            continue
        queue = db.q("""SELECT * FROM skillqueue WHERE character_id = ? AND (finish_date IS NULL OR finish_date > ?)
                        ORDER BY queue_position""", (c["character_id"], now_iso))
        ends = db.val("SELECT MAX(finish_date) FROM skillqueue WHERE character_id = ?", (c["character_id"],))
        paused = bool(queue) and queue[0]["finish_date"] is None
        current = queue[0] if queue else None
        out.append({
            "character_id": c["character_id"], "name": c["name"], "unknown": False, "total_sp": c["total_sp"],
            "count": len(queue), "paused": paused,
            "current": {"skill_id": current["skill_id"], "level": current["finished_level"],
                        "finish": current["finish_date"]} if current else None,
            "ends": ends if queue and not paused else None,
            "hours_left": _hours_until(ends) if queue and ends and not paused else (0 if not queue else None),
        })
    return out


def collect() -> list[dict]:
    """Alerts as {level: bad|warn|good|info, text, href}."""
    a = []

    def add(level, text, href):
        a.append({"level": level, "text": text, "href": href})

    for c in db.q("SELECT name FROM characters WHERE needs_reauth = 1"):
        add("bad", f"{c['name']} must log in again", "/settings")

    undercut = db.val("SELECT COUNT(*) FROM orders WHERE undercut = 1", default=0)
    if undercut:
        add("bad", f"{undercut} order{'s' if undercut != 1 else ''} undercut or outbid", "/orders")

    fuel_days = db.get_setting("alert_fuel_days") or 7
    for st in corp.structures(fuel_days):
        if st["danger"]:
            add("bad", f"{st['name']}: {st['state_label']}", "/#structures")
        elif st["low_power"]:
            add("bad", f"{st['name']} is out of fuel (low power)", "/#structures")
        elif st["fuel_low"]:
            add("warn", f"{st['name']}: {st['fuel_days']:.1f} days of fuel left", "/#structures")

    queue_hours = db.get_setting("alert_skillqueue_hours") or 24
    for q in skill_queues():
        if q["unknown"] or q["paused"]:
            continue
        if q["count"] == 0:
            add("warn", f"{q['name']}: skill queue is empty", "/#skills")
        elif q["hours_left"] is not None and q["hours_left"] < queue_hours:
            add("warn", f"{q['name']}: skill queue ends in {q['hours_left']:.0f}h", "/#skills")

    expiring = contracts.expiring_soon(24)
    if expiring:
        add("warn", f"{expiring} contract{'s' if expiring != 1 else ''} expiring within 24h", "/contracts")
    now_iso = _iso(time.time())
    ready = db.val("SELECT COUNT(*) FROM industry_jobs WHERE status = 'ready' OR (status = 'active' AND end_date <= ?)",
                   (now_iso,), 0)
    if ready:
        add("good", f"{ready} industry job{'s' if ready != 1 else ''} ready to deliver", "/industry")

    for level, text in health.snapshot()["warnings"]:
        add(level, text, "/system")

    hub = db.get_setting("valuation_hub")
    updated = db.get_kv("hub_updated:" + hub)
    if updated and time.time() - updated > 3 * 3600:
        add("warn", f"{market.HUBS[hub]['name']} prices are {((time.time() - updated) / 3600):.0f}h old", "/system")
    return a
