"""Corporation data sync: assets, wallets, orders, industry and blueprints.

EVE only exposes corporation data to members holding the right in-game roles, so each step
uses whichever tracked character in the corporation has both the needed role and the ESI scope.
Corporation rows live in the same tables as character data, with the corporation id as owner.
"""
import json
import logging
import time

from . import db, sso
from .esi import ESIError
from .sync import resolve_roots, resolve_structures

log = logging.getLogger("corp")

ACCOUNTING = {"Director", "Accountant", "Junior_Accountant"}

# step -> (min seconds between runs, ESI scope, in-game roles that grant access)
STEPS = {
    "divisions": (6 * 3600, "esi-corporations.read_divisions.v1", {"Director"}),
    "wallets": (300, "esi-wallet.read_corporation_wallets.v1", ACCOUNTING),
    "transactions": (3600, "esi-wallet.read_corporation_wallets.v1", ACCOUNTING),
    "journal": (3600, "esi-wallet.read_corporation_wallets.v1", ACCOUNTING),
    "orders": (1200, "esi-markets.read_corporation_orders.v1", {"Director", "Accountant", "Trader"}),
    "industry": (600, "esi-industry.read_corporation_jobs.v1", {"Director", "Factory_Manager"}),
    "assets": (3600, "esi-assets.read_corporation_assets.v1", {"Director"}),
    "blueprints": (3600, "esi-corporations.read_blueprints.v1", {"Director"}),
}
LABELS = {
    "divisions": "Division names", "wallets": "Wallets", "transactions": "Transactions", "journal": "Journal",
    "orders": "Market orders", "industry": "Industry jobs", "assets": "Assets", "blueprints": "Blueprints",
}
DIVISIONS = range(1, 8)


class NoAccess(Exception):
    pass


def _roles(row) -> set[str]:
    try:
        return set(json.loads(row["roles"] or "[]"))
    except (TypeError, ValueError):
        return set()


def pick_character(corp: dict, step: str):
    """Character whose token we use for this step, or raise NoAccess with a human reason."""
    _, scope, needed = STEPS[step]
    members = db.q("SELECT * FROM characters WHERE corporation_id = ? AND needs_reauth = 0 ORDER BY name",
                   (corp["corporation_id"],))
    if not members:
        raise NoAccess("no logged-in character in this corporation")
    with_scope = [m for m in members if scope in (m["scopes"] or "").split()]
    if not with_scope:
        raise NoAccess("log in again with a character to grant the corporation permissions")
    for m in with_scope:
        if m["character_id"] == corp["ceo_id"] or _roles(m) & needed:
            return m
    # Roles unknown (roles permission missing or not synced yet): just try.
    unknown = [m for m in with_scope if m["roles"] is None]
    if unknown:
        return unknown[0]
    raise NoAccess("needs the in-game role " + " or ".join(sorted(needed)).replace("_", " "))


async def sync_corporation(esi, corp_id: int, force: bool = False) -> dict:
    corp = dict(db.q1("SELECT * FROM corporations WHERE corporation_id = ?", (corp_id,)))
    access = json.loads(corp["access"] or "{}")
    state = {r["step"]: r["last_run"] for r in
             db.q("SELECT step, last_run FROM sync_state WHERE character_id = ?", (corp_id,))}
    results = {}
    for step, (interval, _scope, _roles_needed) in STEPS.items():
        if not force and time.time() - (state.get(step) or 0) < interval:
            continue
        started = time.time()
        err = None
        try:
            char = pick_character(corp, step)
            token = await sso.get_access_token(char["character_id"])
            results[step] = await globals()["sync_" + step](esi, corp_id, token, corp)
            access[step] = "ok"
        except NoAccess as e:
            access[step] = str(e)
            results[step] = "skipped: " + str(e)
        except sso.NeedsReauth as e:
            access[step] = "character needs to log in again"
            results[step] = "skipped: " + str(e)
        except sso.SSOUnavailable as e:
            err = str(e)
            results[step] = "error: " + err  # temporary; access status left as it was
        except ESIError as e:
            if e.status in (401, 403):
                access[step] = "needs the in-game role " + " or ".join(sorted(STEPS[step][2])).replace("_", " ")
            else:
                err = str(e)
                access[step] = "error"
            results[step] = "error: " + str(e)
        db.ex("INSERT OR REPLACE INTO sync_state VALUES (?, ?, ?, ?)", (corp_id, step, started, err))
    errors = [f"{k}: {v}" for k, v in results.items() if str(v).startswith("error")]
    db.ex("UPDATE corporations SET access = ?, last_sync = ?, sync_error = ? WHERE corporation_id = ?",
          (json.dumps(access), time.time(), "; ".join(errors) or None, corp_id))
    return results


async def sync_all(esi, force: bool = False) -> str:
    out = []
    for row in db.q("SELECT corporation_id, ticker FROM corporations WHERE enabled = 1"):
        res = await sync_corporation(esi, row["corporation_id"], force)
        ok = sum(1 for v in res.values() if not str(v).startswith(("error", "skipped")))
        out.append(f"[{row['ticker']}] {ok}/{len(res)} steps ok" if res else f"[{row['ticker']}] up to date")
    return "; ".join(out) or "no corporations"


# ---------- steps ----------

async def sync_divisions(esi, corp_id, token, corp):
    data = await esi.get(f"/corporations/{corp_id}/divisions/", token=token)
    divisions = {kind: {str(d["division"]): d.get("name") for d in data.get(kind, []) if d.get("name")}
                 for kind in ("hangar", "wallet")}
    db.ex("UPDATE corporations SET divisions = ? WHERE corporation_id = ?", (json.dumps(divisions), corp_id))
    return f"{len(divisions['hangar'])} hangar / {len(divisions['wallet'])} wallet names"


async def sync_wallets(esi, corp_id, token, corp):
    data = await esi.get(f"/corporations/{corp_id}/wallets/", token=token)
    total = sum(w["balance"] for w in data)
    with db.tx() as c:
        c.execute("DELETE FROM corp_wallets WHERE corporation_id = ?", (corp_id,))
        c.executemany("INSERT INTO corp_wallets VALUES (?, ?, ?)",
                      [(corp_id, w["division"], w["balance"]) for w in data])
        c.execute("INSERT OR REPLACE INTO wallet VALUES (?, ?, ?)", (corp_id, total, time.time()))
    return f"{total:,.2f} ISK in {len(data)} divisions"


async def sync_transactions(esi, corp_id, token, corp):
    n = 0
    for div in DIVISIONS:
        data = await esi.get(f"/corporations/{corp_id}/wallets/{div}/transactions/", token=token)
        db.exmany(
            "INSERT OR IGNORE INTO transactions (transaction_id, character_id, date, type_id, quantity, unit_price, "
            "is_buy, location_id, client_id, journal_ref_id, is_personal, division) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            [(t["transaction_id"], corp_id, t["date"], t["type_id"], t["quantity"], t["unit_price"],
              int(t["is_buy"]), t["location_id"], t["client_id"], t["journal_ref_id"], div) for t in data])
        n += len(data)
    return f"{n} transactions"


async def sync_journal(esi, corp_id, token, corp):
    n = 0
    for div in DIVISIONS:
        data = await esi.get_pages(f"/corporations/{corp_id}/wallets/{div}/journal/", token=token)
        db.exmany(
            "INSERT OR IGNORE INTO journal (id, character_id, date, ref_type, amount, balance, description, division) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(j["id"], corp_id, j["date"], j["ref_type"], j.get("amount", 0), j.get("balance"),
              j.get("description"), div) for j in data])
        n += len(data)
    return f"{n} journal entries"


async def sync_orders(esi, corp_id, token, corp):
    from .sync import ORDER_COLS
    data = await esi.get_pages(f"/corporations/{corp_id}/orders/", token=token)
    with db.tx() as c:
        old = {r["order_id"]: r for r in c.execute("SELECT * FROM orders WHERE order_id IN (%s)" % ",".join(
            str(int(o["order_id"])) for o in data))} if data else {}
        c.execute("DELETE FROM orders WHERE character_id = ?", (corp_id,))
        for o in data:
            prev = old.get(o["order_id"])
            keep = prev is not None and prev["price"] == o["price"]
            # OR REPLACE: a copy synced from a member's personal order list becomes the corp's.
            c.execute(
                f"INSERT OR REPLACE INTO orders ({ORDER_COLS}) VALUES ({', '.join('?' * 17)})",
                (o["order_id"], corp_id, o["type_id"], o["region_id"], o["location_id"],
                 int(o.get("is_buy_order", False)), o["price"], o["volume_total"], o["volume_remain"], o["issued"],
                 o["duration"], o.get("escrow"), o.get("range"),
                 prev["competitor_price"] if keep else None, prev["undercut"] if keep else None,
                 prev["checked_at"] if keep else None, o.get("wallet_division")))
    return f"{len(data)} orders"


async def sync_industry(esi, corp_id, token, corp):
    data = await esi.get_pages(f"/corporations/{corp_id}/industry/jobs/", {"include_completed": "true"},
                               token=token)
    with db.tx() as c:
        c.execute("DELETE FROM industry_jobs WHERE character_id = ?", (corp_id,))
        c.executemany(
            "INSERT OR REPLACE INTO industry_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(j["job_id"], corp_id, j["activity_id"], j["blueprint_type_id"], j.get("product_type_id"), j["runs"],
              j["status"], j["start_date"], j["end_date"], j["facility_id"], j.get("cost")) for j in data])
    return f"{len(data)} jobs"


async def sync_assets(esi, corp_id, token, corp):
    data = await esi.get_pages(f"/corporations/{corp_id}/assets/", token=token)
    roots = resolve_roots(data)
    container_ids = {a["location_id"] for a in data}
    named_ids = [a["item_id"] for a in data if a.get("is_singleton") and a["item_id"] in container_ids]
    names = {}
    for i in range(0, len(named_ids), 900):
        try:
            for n in await esi.post(f"/corporations/{corp_id}/assets/names/", named_ids[i:i + 900], token=token):
                if n.get("name") and n["name"] != "None":
                    names[n["item_id"]] = n["name"]
        except ESIError as e:
            log.info("corp asset names failed: %s", e)
    with db.tx() as c:
        c.execute("DELETE FROM assets WHERE character_id = ?", (corp_id,))
        c.executemany(
            "INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(a["item_id"], corp_id, a["type_id"], a["location_id"], a["location_flag"], a["location_type"],
              a["quantity"], int(a["is_singleton"]), int(a.get("is_blueprint_copy", False)),
              roots[a["item_id"]], names.get(a["item_id"])) for a in data])
    await resolve_structures(esi, set(roots.values()), token)
    return f"{len(data)} items"


async def sync_blueprints(esi, corp_id, token, corp):
    data = await esi.get_pages(f"/corporations/{corp_id}/blueprints/", token=token)
    with db.tx() as c:
        c.execute("DELETE FROM blueprints WHERE character_id = ?", (corp_id,))
        c.executemany(
            "INSERT OR REPLACE INTO blueprints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(b["item_id"], corp_id, b["type_id"], b["location_id"], b["material_efficiency"],
              b["time_efficiency"], b["runs"], b["quantity"]) for b in data])
    return f"{len(data)} blueprints"


# ---------- display helpers ----------

def division_names(corp_id: int) -> dict:
    raw = db.val("SELECT divisions FROM corporations WHERE corporation_id = ?", (corp_id,))
    try:
        return json.loads(raw) if raw else {"hangar": {}, "wallet": {}}
    except ValueError:
        return {"hangar": {}, "wallet": {}}


def flag_label(flag: str, divisions: dict | None) -> str:
    """'CorpSAG3' -> 'Hangar 3: Minerals' (using the corp's division names)."""
    if flag and flag.startswith("CorpSAG") and flag[7:].isdigit():
        n = flag[7:]
        name = (divisions or {}).get("hangar", {}).get(n)
        return f"Hangar {n}" + (f": {name}" if name else "")
    return {"CorpDeliveries": "Corp deliveries", "OfficeFolder": "Office"}.get(flag, flag)


def wallet_label(corp_id: int, division: int, divisions: dict | None = None) -> str:
    divisions = divisions or division_names(corp_id)
    name = divisions.get("wallet", {}).get(str(division))
    if not name:
        name = "Master wallet" if division == 1 else f"Division {division}"
    return name


def corporations() -> list[dict]:
    out = []
    for r in db.q("SELECT * FROM corporations ORDER BY name"):
        c = dict(r)
        c["access"] = json.loads(c["access"] or "{}")
        c["members"] = [m["name"] for m in db.q(
            "SELECT name FROM characters WHERE corporation_id = ? ORDER BY name", (c["corporation_id"],))]
        out.append(c)
    return out
