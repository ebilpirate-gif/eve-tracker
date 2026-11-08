"""Realized trading profit using FIFO matching of wallet transactions (combined across characters)."""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from . import db, market


def fifo(transactions, sales_tax: float = 0.0, since: str = ""):
    """transactions: dicts sorted by date with type_id, quantity, unit_price, is_buy, date.
    Each sale is matched against the oldest unconsumed purchases. Sales before `since` still
    consume purchase lots but are not reported. Sales tax is an estimate; actual taxes and
    broker fees come from the wallet journal."""
    lots: dict[int, deque] = defaultdict(deque)
    stats: dict[int, dict] = {}
    for t in transactions:
        tid = t["type_id"]
        if t["is_buy"]:
            lots[tid].append([t["quantity"], t["unit_price"]])
            continue
        qty = t["quantity"]
        cost = 0.0
        q = lots[tid]
        while qty and q:
            take = min(qty, q[0][0])
            cost += take * q[0][1]
            q[0][0] -= take
            qty -= take
            if q[0][0] == 0:
                q.popleft()
        if t["date"] < since:
            continue
        matched = t["quantity"] - qty
        s = stats.setdefault(tid, {"type_id": tid, "sold": 0, "revenue": 0.0, "cost": 0.0,
                                   "unmatched": 0, "tax": 0.0, "last_sale": None})
        # Units sold without a recorded purchase (loot, industry, older stock) have unknown cost,
        # so they are excluded from profit and counted as "unmatched".
        s["sold"] += matched
        s["unmatched"] += qty
        s["revenue"] += matched * t["unit_price"]
        s["tax"] += matched * t["unit_price"] * sales_tax
        s["cost"] += cost
        s["last_sale"] = t["date"]
    for s in stats.values():
        s["profit"] = s["revenue"] - s["cost"] - s["tax"]
    return stats


def report(days: int = 30, character_id: int | None = None):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    where, params = "", []
    if character_id:
        where, params = "WHERE character_id = ?", [character_id]
    rows = db.q(f"SELECT type_id, quantity, unit_price, is_buy, date FROM transactions {where} "
                "ORDER BY date, transaction_id", params)
    f = market.fees()
    stats = fifo([dict(r) for r in rows], f["sales_tax"], since)
    jwhere = "WHERE date >= ?" + (" AND character_id = ?" if character_id else "")
    jparams = [since] + ([character_id] if character_id else [])
    fee_rows = {r[0]: r[1] for r in db.q(
        f"SELECT ref_type, SUM(amount) FROM journal {jwhere} AND ref_type IN ('brokers_fee', 'transaction_tax') "
        "GROUP BY ref_type", jparams)}
    items = sorted((s for s in stats.values() if s["sold"] or s["unmatched"]), key=lambda s: s["profit"], reverse=True)
    totals = {
        "revenue": sum(s["revenue"] for s in items),
        "cost": sum(s["cost"] for s in items),
        "est_tax": sum(s["tax"] for s in items),
        "profit": sum(s["profit"] for s in items),
        "broker_fees": -(fee_rows.get("brokers_fee") or 0),
        "tax_paid": -(fee_rows.get("transaction_tax") or 0),
    }
    totals["net"] = totals["profit"] - totals["broker_fees"]
    return items, totals
