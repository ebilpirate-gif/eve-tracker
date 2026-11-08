"""Per-character data sync from authenticated ESI endpoints."""
import logging
import time

from . import db, market, sso
from .esi import ESI, ESIError

log = logging.getLogger("sync")

# step -> (minimum seconds between runs, required scope)
STEPS = {
    "wallet": (300, "esi-wallet.read_character_wallet.v1"),
    "orders": (1200, "esi-markets.read_character_orders.v1"),
    "transactions": (3600, "esi-wallet.read_character_wallet.v1"),
    "journal": (3600, "esi-wallet.read_character_wallet.v1"),
    "assets": (3600, "esi-assets.read_assets.v1"),
    "industry": (600, "esi-industry.read_character_jobs.v1"),
    "blueprints": (3600, "esi-characters.read_blueprints.v1"),
    "skills": (3600, "esi-skills.read_skills.v1"),
}

async def sync_character(esi: ESI, char_id: int, force: bool = False) -> dict:
    results = {}
    token = await sso.get_access_token(char_id)
    state = {r["step"]: r["last_run"] for r in db.q("SELECT step, last_run FROM sync_state WHERE character_id = ?", (char_id,))}
    for step, (interval, scope) in STEPS.items():
        if not force and time.time() - (state.get(step) or 0) < interval:
            continue
        if not sso.has_scope(char_id, scope):
            results[step] = "missing scope"
            continue
        started = time.time()
        err = None
        try:
            token = await sso.get_access_token(char_id)
            results[step] = await globals()["sync_" + step](esi, char_id, token)
        except ESIError as e:
            err = str(e)
            log.warning("sync %s/%s failed: %s", char_id, step, e)
        db.ex("INSERT OR REPLACE INTO sync_state VALUES (?, ?, ?, ?)", (char_id, step, started, err))
        if err:
            results[step] = "error: " + err
    errors = [f"{k}: {v}" for k, v in results.items() if str(v).startswith("error")]
    db.ex("UPDATE characters SET last_sync = ?, sync_error = ? WHERE character_id = ?",
          (time.time(), "; ".join(errors) or None, char_id))
    return results


async def sync_wallet(esi, cid, token):
    bal = await esi.get(f"/characters/{cid}/wallet/", token=token)
    db.ex("INSERT OR REPLACE INTO wallet VALUES (?, ?, ?)", (cid, bal, time.time()))
    return f"{bal:,.2f} ISK"


async def sync_orders(esi, cid, token):
    data = await esi.get(f"/characters/{cid}/orders/", token=token)
    with db.tx() as c:
        old = {r["order_id"]: r for r in c.execute("SELECT * FROM orders WHERE character_id = ?", (cid,))}
        c.execute("DELETE FROM orders WHERE character_id = ?", (cid,))
        for o in data:
            prev = old.get(o["order_id"])
            keep = prev and prev["price"] == o["price"]  # keep undercut state unless we repriced
            c.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (o["order_id"], cid, o["type_id"], o["region_id"], o["location_id"], int(o.get("is_buy_order", False)),
                 o["price"], o["volume_total"], o["volume_remain"], o["issued"], o["duration"], o.get("escrow"),
                 o.get("range"), prev["competitor_price"] if keep else None, prev["undercut"] if keep else None,
                 prev["checked_at"] if keep else None))
    return f"{len(data)} orders"


async def sync_transactions(esi, cid, token):
    data = await esi.get(f"/characters/{cid}/wallet/transactions/", token=token)
    db.exmany(
        "INSERT OR IGNORE INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(t["transaction_id"], cid, t["date"], t["type_id"], t["quantity"], t["unit_price"], int(t["is_buy"]),
          t["location_id"], t["client_id"], t["journal_ref_id"], int(t["is_personal"])) for t in data])
    return f"{len(data)} transactions"


async def sync_journal(esi, cid, token):
    data = await esi.get_pages(f"/characters/{cid}/wallet/journal/", token=token)
    db.exmany(
        "INSERT OR IGNORE INTO journal VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(j["id"], cid, j["date"], j["ref_type"], j.get("amount", 0), j.get("balance"), j.get("description"))
         for j in data])
    return f"{len(data)} journal entries"


def resolve_roots(assets: list[dict]) -> dict[int, int]:
    """Map item_id -> the station/structure/system the item ultimately sits in
    (walking up through ships and containers)."""
    by_id = {a["item_id"]: a for a in assets}
    roots: dict[int, int] = {}
    for a in assets:
        chain = []
        cur = a
        while cur["location_id"] in by_id and cur["item_id"] not in roots and len(chain) < 20:
            chain.append(cur["item_id"])
            cur = by_id[cur["location_id"]]
        root = roots.get(cur["item_id"], cur["location_id"])
        roots[cur["item_id"]] = root
        for item in chain:
            roots[item] = root
    return roots


async def sync_assets(esi, cid, token):
    data = await esi.get_pages(f"/characters/{cid}/assets/", token=token)
    roots = resolve_roots(data)
    # Names of ships/containers: assembled (singleton) items that hold other items.
    container_ids = {a["location_id"] for a in data}
    named_ids = [a["item_id"] for a in data if a.get("is_singleton") and a["item_id"] in container_ids]
    names = {}
    for i in range(0, len(named_ids), 900):
        try:
            for n in await esi.post(f"/characters/{cid}/assets/names/", named_ids[i:i + 900], token=token):
                if n.get("name") and n["name"] != "None":
                    names[n["item_id"]] = n["name"]
        except ESIError as e:
            log.info("asset names failed: %s", e)
    with db.tx() as c:
        c.execute("DELETE FROM assets WHERE character_id = ?", (cid,))
        c.executemany(
            "INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(a["item_id"], cid, a["type_id"], a["location_id"], a["location_flag"], a["location_type"],
              a["quantity"], int(a["is_singleton"]), int(a.get("is_blueprint_copy", False)),
              roots[a["item_id"]], names.get(a["item_id"])) for a in data])
    await resolve_structures(esi, {r for r in roots.values()}, token)
    return f"{len(data)} items"


async def resolve_structures(esi, location_ids, token):
    """Look up names of player structures (Upwell). Requires docking access; failures are cached."""
    week_ago = time.time() - 7 * 86400
    known = {r[0] for r in db.q("SELECT structure_id FROM structures WHERE fetched_at > ?", (week_ago,))}
    for sid in location_ids:
        if sid is None or sid < 100000000 or sid in known:
            continue
        try:
            s = await esi.get(f"/universe/structures/{sid}/", token=token)
            db.ex("INSERT OR REPLACE INTO structures VALUES (?, ?, ?, ?, ?, 1)",
                  (sid, s["name"], s.get("solar_system_id"), s.get("type_id"), time.time()))
        except ESIError as e:
            if e.status not in (401, 403, 404):
                raise
            db.ex("INSERT OR REPLACE INTO structures VALUES (?, NULL, NULL, NULL, ?, 0)", (sid, time.time()))


async def sync_industry(esi, cid, token):
    data = await esi.get(f"/characters/{cid}/industry/jobs/", {"include_completed": "true"}, token=token)
    with db.tx() as c:
        c.execute("DELETE FROM industry_jobs WHERE character_id = ?", (cid,))
        c.executemany(
            "INSERT OR REPLACE INTO industry_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(j["job_id"], cid, j["activity_id"], j["blueprint_type_id"], j.get("product_type_id"), j["runs"],
              j["status"], j["start_date"], j["end_date"], j["facility_id"], j.get("cost")) for j in data])
    return f"{len(data)} jobs"


async def sync_blueprints(esi, cid, token):
    data = await esi.get_pages(f"/characters/{cid}/blueprints/", token=token)
    with db.tx() as c:
        c.execute("DELETE FROM blueprints WHERE character_id = ?", (cid,))
        c.executemany(
            "INSERT OR REPLACE INTO blueprints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(b["item_id"], cid, b["type_id"], b["location_id"], b["material_efficiency"], b["time_efficiency"],
              b["runs"], b["quantity"]) for b in data])
    return f"{len(data)} blueprints"


async def sync_skills(esi, cid, token):
    data = await esi.get(f"/characters/{cid}/skills/", token=token)
    with db.tx() as c:
        c.execute("DELETE FROM skills WHERE character_id = ?", (cid,))
        c.executemany("INSERT INTO skills VALUES (?, ?, ?)",
                      [(cid, s["skill_id"], s["active_skill_level"]) for s in data["skills"]])
    return f"{len(data['skills'])} skills"


# ---------- net worth ----------

def character_networth(cid: int, prices: dict | None = None) -> dict:
    prices = prices or market.price_map()
    wallet = db.val("SELECT balance FROM wallet WHERE character_id = ?", (cid,), 0.0)
    assets = 0.0
    for type_id, qty, bpc in db.q(
            "SELECT type_id, SUM(quantity), is_blueprint_copy FROM assets WHERE character_id = ? GROUP BY type_id, is_blueprint_copy",
            (cid,)):
        if not bpc:
            assets += prices.get(type_id, 0.0) * qty
    sell = db.val("SELECT SUM(price * volume_remain) FROM orders WHERE character_id = ? AND is_buy = 0", (cid,), 0.0)
    escrow = db.val("SELECT SUM(escrow) FROM orders WHERE character_id = ? AND is_buy = 1", (cid,), 0.0)
    return {"wallet": wallet, "assets": assets, "sell_orders": sell, "buy_escrow": escrow,
            "total": wallet + assets + sell + escrow}


def snapshot_networth() -> str:
    prices = market.price_map()
    ts = int(time.time() // 3600 * 3600)
    chars = db.q("SELECT character_id FROM characters")
    for (cid,) in chars:
        nw = character_networth(cid, prices)
        db.ex("INSERT OR REPLACE INTO networth VALUES (?, ?, ?, ?, ?, ?)",
              (ts, cid, nw["wallet"], nw["assets"], nw["sell_orders"], nw["buy_escrow"]))
    return f"{len(chars)} characters"
