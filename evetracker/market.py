"""Market data: ESI reference prices, trade-hub order books, volume history, and the trade finder."""
import asyncio
import logging
import time
from datetime import date, timedelta

from . import db
from .esi import ESI, ESIError

log = logging.getLogger("market")

HUBS = {
    "jita": {"name": "Jita", "station_id": 60003760, "region_id": 10000002, "system_id": 30000142},
    "amarr": {"name": "Amarr", "station_id": 60008494, "region_id": 10000043, "system_id": 30002187},
    "dodixie": {"name": "Dodixie", "station_id": 60011866, "region_id": 10000032, "system_id": 30002659},
    "rens": {"name": "Rens", "station_id": 60004588, "region_id": 10000030, "system_id": 30002510},
    "hek": {"name": "Hek", "station_id": 60005686, "region_id": 10000042, "system_id": 30002053},
}

ACCOUNTING = 16622
BROKER_RELATIONS = 3446


# ---------- fees ----------

def fees() -> dict:
    """Sales tax and broker fee (as fractions). Defaults derive from the best skill levels
    across all characters; both can be overridden in Settings."""
    acc = db.val("SELECT MAX(level) FROM skills WHERE skill_id = ?", (ACCOUNTING,), 0)
    br = db.val("SELECT MAX(level) FROM skills WHERE skill_id = ?", (BROKER_RELATIONS,), 0)
    tax = db.get_setting("sales_tax_pct")
    broker = db.get_setting("broker_fee_pct")
    if tax is None:
        tax = 7.5 * (1 - 0.11 * acc)
    if broker is None:
        broker = 3.0 - 0.3 * br
    return {"sales_tax": tax / 100, "broker_fee": broker / 100, "accounting": acc, "broker_relations": br}


# ---------- reference prices ----------

async def update_prices(esi: ESI) -> str:
    data = await esi.get("/markets/prices/")
    rows = [(p["type_id"], p.get("adjusted_price"), p.get("average_price")) for p in data]
    with db.tx() as c:
        c.execute("DELETE FROM prices")
        c.executemany("INSERT INTO prices (type_id, adjusted, average) VALUES (?, ?, ?)", rows)
    return f"{len(rows)} prices"


async def update_cost_indices(esi: ESI) -> str:
    data = await esi.get("/industry/systems/")
    rows = [(s["solar_system_id"], ci["activity"], ci["cost_index"]) for s in data for ci in s["cost_indices"]]
    with db.tx() as c:
        c.execute("DELETE FROM cost_indices")
        c.executemany("INSERT INTO cost_indices VALUES (?, ?, ?)", rows)
    return f"{len(data)} systems"


# ---------- hub order books ----------

class _HubAggregator:
    def __init__(self, station_id: int):
        self.station_id = station_id
        self.data: dict[int, list] = {}  # type_id -> [best_buy, best_sell, buy_vol, sell_vol, buy_n, sell_n]

    def __call__(self, orders):
        sid = self.station_id
        d = self.data
        for o in orders:
            if o["location_id"] != sid:
                continue
            rec = d.get(o["type_id"])
            if rec is None:
                rec = d[o["type_id"]] = [0.0, None, 0, 0, 0, 0]
            if o["is_buy_order"]:
                if o["price"] > rec[0]:
                    rec[0] = o["price"]
                rec[2] += o["volume_remain"]
                rec[4] += 1
            else:
                if rec[1] is None or o["price"] < rec[1]:
                    rec[1] = o["price"]
                rec[3] += o["volume_remain"]
                rec[5] += 1


async def update_hub(esi: ESI, hub_key: str) -> str:
    hub = HUBS[hub_key]
    agg = _HubAggregator(hub["station_id"])
    await esi.get_pages(f"/markets/{hub['region_id']}/orders/", {"order_type": "all"}, concurrency=8, on_page=agg)
    rows = [(hub["station_id"], tid, r[0] or None, r[1], r[2], r[3], r[4], r[5]) for tid, r in agg.data.items()]
    with db.tx() as c:
        c.execute("DELETE FROM hub_prices WHERE station_id = ?", (hub["station_id"],))
        c.executemany("INSERT INTO hub_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.set_kv(f"hub_updated:{hub_key}", time.time())
    return f"{hub['name']}: {len(rows)} types"


# ---------- volume history ----------

def summarize_history(entries, days: int = 30, today: date | None = None):
    """Average daily volume / price over the last `days` calendar days (missing days count as 0)."""
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    recent = [e for e in entries if e["date"] >= cutoff]
    vol = sum(e["volume"] for e in recent)
    avg_price = (sum(e["average"] * e["volume"] for e in recent) / vol) if vol else None
    return vol / days, avg_price


async def update_history(esi: ESI, region_id: int, type_ids, max_age_hours: float = 20) -> int:
    fresh_before = time.time() - max_age_hours * 3600
    known = {r[0] for r in db.q(
        "SELECT type_id FROM market_history WHERE region_id = ? AND fetched_at > ?", (region_id, fresh_before))}
    todo = [t for t in dict.fromkeys(type_ids) if t not in known]
    sem = asyncio.Semaphore(4)

    async def one(tid):
        async with sem:
            try:
                entries = await esi.get(f"/markets/{region_id}/history/", {"type_id": tid}, cache=False)
            except ESIError as e:
                if e.status not in (400, 404, 422):
                    raise
                entries = []
            vol, price = summarize_history(entries)
            db.ex("INSERT OR REPLACE INTO market_history VALUES (?, ?, ?, ?, ?)",
                  (region_id, tid, vol, price, time.time()))

    await asyncio.gather(*(one(t) for t in todo))
    return len(todo)


def history_candidates(limit_per_hub: int = 250) -> dict[int, list[int]]:
    """Types worth fetching volume history for: profitable-looking spreads with many orders
    (order count is a cheap liquidity proxy), plus anything we hold orders for."""
    f = fees()
    out: dict[int, list[int]] = {}
    for key in db.get_setting("hubs"):
        hub = HUBS.get(key)
        if not hub:
            continue
        rows = db.q(
            """SELECT type_id FROM hub_prices WHERE station_id = ? AND best_buy > 0 AND best_sell > 0
               AND best_sell * (1 - ? - ?) > best_buy * (1 + ?) ORDER BY buy_orders + sell_orders DESC LIMIT ?""",
            (hub["station_id"], f["broker_fee"], f["sales_tax"], f["broker_fee"], limit_per_hub))
        top = db.q("""SELECT type_id FROM hub_prices WHERE station_id = ? ORDER BY buy_orders + sell_orders DESC
                      LIMIT ?""", (hub["station_id"], limit_per_hub))
        mine = db.q("SELECT DISTINCT type_id FROM orders WHERE region_id = ?", (hub["region_id"],))
        out.setdefault(hub["region_id"], []).extend(r[0] for r in rows + top + mine)
    # Products of market-seeded blueprints (BPOs) and owned blueprints, for the industry page.
    ind = HUBS.get(db.get_setting("industry_hub"))
    if ind:
        prods = db.q("""SELECT DISTINCT p.product_type_id FROM bp_products p JOIN types b ON b.type_id = p.type_id
                        WHERE p.activity_id IN (1, 11) AND b.published = 1
                        AND (p.type_id IN (SELECT type_id FROM prices WHERE average > 0)
                             OR p.type_id IN (SELECT type_id FROM blueprints))""")
        out.setdefault(ind["region_id"], []).extend(r[0] for r in prods)
    return out


# ---------- trade finder ----------

def station_trades(hub_key: str, min_margin_pct: float = 5, min_volume: float = 10,
                   max_price: float | None = None, limit: int = 200):
    hub = HUBS[hub_key]
    f = fees()
    rows = db.q(
        """SELECT h.type_id, t.name, h.best_buy, h.best_sell, h.buy_orders, h.sell_orders,
                  m.avg_volume, m.avg_price
           FROM hub_prices h JOIN types t ON t.type_id = h.type_id
           LEFT JOIN market_history m ON m.region_id = ? AND m.type_id = h.type_id
           WHERE h.station_id = ? AND h.best_buy > 0 AND h.best_sell > 0""",
        (hub["region_id"], hub["station_id"]))
    out = []
    for r in rows:
        cost = r["best_buy"] * (1 + f["broker_fee"])
        revenue = r["best_sell"] * (1 - f["broker_fee"] - f["sales_tax"])
        profit = revenue - cost
        margin = profit / cost * 100
        vol = r["avg_volume"]
        if margin < min_margin_pct or (max_price and r["best_buy"] > max_price):
            continue
        if vol is None or vol < min_volume:
            continue
        out.append({
            "type_id": r["type_id"], "name": r["name"], "buy": r["best_buy"], "sell": r["best_sell"],
            "profit": profit, "margin": margin, "volume": vol,
            "orders": r["buy_orders"] + r["sell_orders"],
            # Rough capacity: assume you can capture 10% of daily volume.
            "daily_potential": profit * vol * 0.10,
        })
    out.sort(key=lambda x: x["daily_potential"], reverse=True)
    return out[:limit]


def hauling_trades(src_key: str, dst_key: str, mode: str = "instant", min_profit: float = 0,
                   max_m3: float | None = None, max_investment: float | None = None,
                   min_volume: float = 1, limit: int = 200):
    """Buy from sell orders at src, then either sell instantly into buy orders at dst ('instant')
    or list sell orders at dst ('list')."""
    src, dst = HUBS[src_key], HUBS[dst_key]
    f = fees()
    rows = db.q(
        """SELECT s.type_id, t.name, COALESCE(t.packaged_volume, t.volume) AS m3, s.best_sell AS src_price,
                  s.sell_volume AS src_avail, d.best_buy AS dst_buy, d.best_sell AS dst_sell,
                  d.buy_volume AS dst_buy_volume, m.avg_volume
           FROM hub_prices s JOIN hub_prices d ON d.type_id = s.type_id AND d.station_id = ?
           JOIN types t ON t.type_id = s.type_id
           LEFT JOIN market_history m ON m.region_id = ? AND m.type_id = s.type_id
           WHERE s.station_id = ? AND s.best_sell > 0""",
        (dst["station_id"], dst["region_id"], src["station_id"]))
    out = []
    for r in rows:
        if mode == "instant":
            if not r["dst_buy"]:
                continue
            revenue = r["dst_buy"] * (1 - f["sales_tax"])
            qty_cap = r["dst_buy_volume"] or 0
        else:
            if not r["dst_sell"]:
                continue
            revenue = r["dst_sell"] * (1 - f["sales_tax"] - f["broker_fee"])
            qty_cap = (r["avg_volume"] or 0) * 0.10 * 7  # a week's worth at 10% share
        profit = revenue - r["src_price"]
        if profit <= min_profit:
            continue
        if mode == "list" and (r["avg_volume"] or 0) < min_volume:
            continue
        m3 = r["m3"] or 0.01
        qty = min(r["src_avail"] or 0, qty_cap)
        if max_m3:
            qty = min(qty, int(max_m3 // m3))
        if max_investment:
            qty = min(qty, int(max_investment // r["src_price"]))
        if qty <= 0:
            continue
        out.append({
            "type_id": r["type_id"], "name": r["name"], "buy": r["src_price"],
            "sell": r["dst_buy"] if mode == "instant" else r["dst_sell"],
            "profit": profit, "margin": profit / r["src_price"] * 100, "per_m3": profit / m3,
            "qty": int(qty), "trip_profit": profit * qty, "investment": r["src_price"] * qty,
            "cargo": m3 * qty, "volume": r["avg_volume"],
        })
    out.sort(key=lambda x: x["trip_profit"], reverse=True)
    return out[:limit]


# ---------- price lookup used for valuation ----------

def price_map(hub_key: str | None = None, side: str | None = None) -> dict[int, float]:
    hub = HUBS[hub_key or db.get_setting("valuation_hub")]
    side = side or db.get_setting("valuation_side")
    col = "best_sell" if side == "sell" else "best_buy"
    prices = {r[0]: r[1] for r in db.q("SELECT type_id, average FROM prices WHERE average > 0")}
    for tid, p in db.q(f"SELECT type_id, {col} FROM hub_prices WHERE station_id = ? AND {col} > 0",
                       (hub["station_id"],)):
        prices[tid] = p
    return prices


# ---------- undercut checks ----------

async def check_orders(esi: ESI) -> str:
    """Compare each of our open orders against competitors at the same station."""
    my_orders = db.q("SELECT * FROM orders")
    if not my_orders:
        return "no orders"
    mine = {o["order_id"] for o in my_orders}
    pairs = {(o["region_id"], o["type_id"]) for o in my_orders}
    books: dict[tuple, list] = {}
    sem = asyncio.Semaphore(6)

    async def fetch(region_id, type_id):
        async with sem:
            books[(region_id, type_id)] = await esi.get(
                f"/markets/{region_id}/orders/", {"type_id": type_id, "order_type": "all"})

    await asyncio.gather(*(fetch(r, t) for r, t in pairs))
    undercut = 0
    now = time.time()
    for o in my_orders:
        rivals = [x for x in books.get((o["region_id"], o["type_id"]), [])
                  if x["location_id"] == o["location_id"] and bool(x["is_buy_order"]) == bool(o["is_buy"])
                  and x["order_id"] not in mine]
        if o["is_buy"]:
            best = max((x["price"] for x in rivals), default=None)
            bad = best is not None and best > o["price"]
        else:
            best = min((x["price"] for x in rivals), default=None)
            bad = best is not None and best < o["price"]
        undercut += bad
        db.ex("UPDATE orders SET competitor_price = ?, undercut = ?, checked_at = ? WHERE order_id = ?",
              (best, int(bad), now, o["order_id"]))
    return f"{len(my_orders)} orders, {undercut} undercut"
