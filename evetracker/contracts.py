"""Contracts (item exchange, courier, auction) for characters and corporations, plus a small
cache of character/corporation names for issuers and acceptors."""
import logging
import time

from . import db
from .esi import ESIError

log = logging.getLogger("contracts")

COLS = ("contract_id", "character_id", "type", "status", "title", "issuer_id", "issuer_corporation_id",
        "assignee_id", "acceptor_id", "for_corporation", "availability", "price", "reward", "collateral",
        "buyout", "volume", "days_to_complete", "start_location_id", "end_location_id", "date_issued",
        "date_expired", "date_accepted", "date_completed")
UPDATE = ", ".join(f"{c} = excluded.{c}" for c in COLS if c not in ("contract_id", "character_id"))
OPEN_STATUSES = ("outstanding", "in_progress")
TYPE_LABELS = {"item_exchange": "Item exchange", "courier": "Courier", "auction": "Auction", "loan": "Loan",
               "unknown": "Unknown"}
ITEMS_PER_SYNC = 60  # be gentle: item lists are one request each and never change


def _row(c: dict, owner_id: int) -> tuple:
    return (c["contract_id"], owner_id, c.get("type"), c.get("status"), c.get("title") or "",
            c.get("issuer_id"), c.get("issuer_corporation_id"), c.get("assignee_id"), c.get("acceptor_id"),
            int(bool(c.get("for_corporation"))), c.get("availability"), c.get("price"), c.get("reward"),
            c.get("collateral"), c.get("buyout"), c.get("volume"), c.get("days_to_complete"),
            c.get("start_location_id"), c.get("end_location_id"), c.get("date_issued"), c.get("date_expired"),
            c.get("date_accepted"), c.get("date_completed"))


def store(owner_id: int, data: list, corp: bool) -> None:
    """Upsert contracts. History is kept (ESI only returns ~30 days). A contract seen by both a
    character and its corporation belongs to the corporation."""
    placeholders = ", ".join("?" * len(COLS))
    if corp:
        sql = (f"INSERT INTO contracts ({', '.join(COLS)}) VALUES ({placeholders}) "
               f"ON CONFLICT(contract_id) DO UPDATE SET character_id = excluded.character_id, {UPDATE}, "
               # taken over from a member's list: fetch items again with the corp's access if we have none
               "items_fetched = CASE WHEN contracts.character_id != excluded.character_id AND NOT EXISTS "
               "(SELECT 1 FROM contract_items i WHERE i.contract_id = contracts.contract_id) "
               "THEN 0 ELSE contracts.items_fetched END")
    else:
        sql = (f"INSERT INTO contracts ({', '.join(COLS)}) VALUES ({placeholders}) "
               f"ON CONFLICT(contract_id) DO UPDATE SET {UPDATE} "
               f"WHERE contracts.character_id = excluded.character_id")
    with db.tx() as c:
        c.executemany(sql, [_row(x, owner_id) for x in data])


async def fetch_items(esi, owner_id: int, token: str, base: str) -> int:
    """Fetch item lists for contracts that don't have them yet (newest first)."""
    todo = db.q("""SELECT contract_id FROM contracts WHERE character_id = ? AND items_fetched = 0
                   AND type IN ('item_exchange', 'auction', 'courier') ORDER BY date_issued DESC LIMIT ?""",
                (owner_id, ITEMS_PER_SYNC))
    n = 0
    for (cid,) in todo:
        try:
            items = await esi.get(f"{base}/contracts/{cid}/items/", token=token, cache=False)
        except ESIError as e:
            if e.status not in (403, 404):
                raise
            items = []  # expired/deleted or no longer visible: don't ask again
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO contract_items VALUES (?, ?, ?, ?, ?, ?)",
                          [(cid, i["record_id"], i["type_id"], i["quantity"], int(i.get("is_included", True)),
                            int(i.get("is_singleton", False))) for i in items])
            c.execute("UPDATE contracts SET items_fetched = 1 WHERE contract_id = ?", (cid,))
        n += 1
    return n


async def sync_for_character(esi, cid: int, token: str) -> str:
    data = await esi.get_pages(f"/characters/{cid}/contracts/", token=token)
    store(cid, data, corp=False)
    n = await fetch_items(esi, cid, token, f"/characters/{cid}")
    await resolve_names(esi, _party_ids(data))
    return f"{len(data)} contracts ({n} item lists fetched)"


async def sync_for_corporation(esi, corp_id: int, token: str) -> str:
    data = await esi.get_pages(f"/corporations/{corp_id}/contracts/", token=token)
    store(corp_id, data, corp=True)
    n = await fetch_items(esi, corp_id, token, f"/corporations/{corp_id}")
    await resolve_names(esi, _party_ids(data))
    return f"{len(data)} contracts ({n} item lists fetched)"


def _party_ids(data) -> set[int]:
    ids = set()
    for c in data:
        for k in ("issuer_id", "issuer_corporation_id", "assignee_id", "acceptor_id"):
            if c.get(k):
                ids.add(c[k])
    return ids


# ---------- names ----------

async def resolve_names(esi, ids) -> None:
    """Look up names for character/corporation/alliance ids we haven't seen (public endpoint)."""
    known = {r[0] for r in db.q("SELECT id FROM names")}
    todo = [i for i in ids if i and i not in known]
    for i in range(0, len(todo), 500):
        chunk = todo[i:i + 500]
        try:
            result = await esi.post("/universe/names/", chunk)
        except ESIError as e:
            if e.status != 404:
                raise
            # One bad id fails the whole batch; fall back to asking one at a time.
            result = []
            for one in chunk:
                try:
                    result += await esi.post("/universe/names/", [one])
                except ESIError:
                    result.append({"id": one, "name": f"#{one}", "category": "unknown"})
        db.exmany("INSERT OR REPLACE INTO names VALUES (?, ?, ?)",
                  [(r["id"], r["name"], r.get("category")) for r in result])


def name(entity_id) -> str:
    if not entity_id:
        return ""
    row = db.q1("SELECT name FROM owners WHERE owner_id = ?", (entity_id,))
    if row and row[0]:
        return row[0]
    return db.val("SELECT name FROM names WHERE id = ?", (entity_id,), f"#{entity_id}")


# ---------- page data ----------

def listing(view: str, owner: int = 0, prices: dict | None = None) -> list[dict]:
    """view: 'open' (outstanding / in progress) or 'finished' (everything else)."""
    where = ["status IN ('outstanding', 'in_progress')" if view == "open"
             else "status NOT IN ('outstanding', 'in_progress')"]
    params: list = []
    if owner:
        where.append("character_id = ?")
        params.append(owner)
    rows = [dict(r) for r in db.q(
        f"""SELECT x.*, o.name AS owner_name FROM contracts x LEFT JOIN owners o ON o.owner_id = x.character_id
            WHERE {' AND '.join(where)} ORDER BY COALESCE(date_completed, date_issued) DESC LIMIT 500""", params)]
    prices = prices or {}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for r in rows:
        items = db.q("SELECT type_id, quantity, is_included FROM contract_items WHERE contract_id = ?",
                     (r["contract_id"],))
        r["items"] = [dict(i) for i in items]
        r["item_value"] = sum(prices.get(i["type_id"], 0) * i["quantity"] for i in items if i["is_included"])
        r["wanted_value"] = sum(prices.get(i["type_id"], 0) * i["quantity"] for i in items if not i["is_included"])
        r["mine"] = r["issuer_id"] == r["character_id"] or r["issuer_corporation_id"] == r["character_id"]
        r["expired"] = r["status"] == "outstanding" and (r["date_expired"] or "9") < now_iso
        r["type_label"] = TYPE_LABELS.get(r["type"], r["type"])
    return rows


def expiring_soon(hours: float = 24) -> int:
    now = time.time()
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + hours * 3600))
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return db.val("""SELECT COUNT(*) FROM contracts WHERE status = 'outstanding' AND date_expired BETWEEN ? AND ?
                     AND (issuer_id = character_id OR issuer_corporation_id = character_id)""",
                  (now_iso, soon), 0)
