"""Manufacturing / reaction profitability."""
import math

from . import db, market, sde

MANUFACTURING = 1
REACTION = 11
ACTIVITY_NAMES = {1: "Manufacturing", 3: "TE research", 4: "ME research", 5: "Copying", 8: "Invention", 11: "Reaction"}
SCC_SURCHARGE = 0.04


def material_quantity(base: int, runs: int, me: int, structure_bonus_pct: float = 0.0) -> int:
    """EVE's material formula: each material needs at least one unit per run."""
    modified = base * runs * (1 - me / 100) * (1 - structure_bonus_pct / 100)
    return max(runs, math.ceil(round(modified, 2)))


def _price_maps():
    hub = market.HUBS[db.get_setting("industry_hub")]
    mat_col = "best_sell" if db.get_setting("industry_material_side") == "sell" else "best_buy"
    rows = db.q(f"SELECT type_id, {mat_col}, best_sell FROM hub_prices WHERE station_id = ?", (hub["station_id"],))
    mats = {r[0]: r[1] for r in rows if r[1]}
    sells = {r[0]: r[2] for r in rows if r[2]}
    adjusted = {r[0]: r[1] for r in db.q("SELECT type_id, adjusted FROM prices")}
    return hub, mats, sells, adjusted


def cost_index(activity_id: int) -> float:
    name = "manufacturing" if activity_id == MANUFACTURING else "reaction"
    system = sde.system_by_name(db.get_setting("industry_system") or "Jita")
    if not system:
        return 0.0
    return db.val("SELECT cost_index FROM cost_indices WHERE system_id = ? AND activity = ?",
                  (system["system_id"], name), 0.0)


def evaluate(bp_type_id: int, activity_id: int, runs: int, me: int, te: int, ctx=None) -> dict | None:
    ctx = ctx or _context(activity_id)
    product = ctx["products"].get(bp_type_id)
    mats = ctx["materials"].get(bp_type_id)
    if not product or not mats:
        return None
    product_id, product_qty = product
    me_eff = me if activity_id == MANUFACTURING else 0
    lines, mat_cost, missing, eiv = [], 0.0, [], 0.0
    for mat_id, base in mats:
        qty = material_quantity(base, runs, me_eff, ctx["structure_bonus"] if activity_id == MANUFACTURING else 0)
        price = ctx["mat_prices"].get(mat_id)
        if price is None:
            missing.append(mat_id)
        cost = (price or 0) * qty
        mat_cost += cost
        eiv += base * runs * (ctx["adjusted"].get(mat_id) or 0)
        lines.append({"type_id": mat_id, "name": ctx["names"].get(mat_id, str(mat_id)), "qty": qty,
                      "price": price, "cost": cost})
    job_cost = eiv * (ctx["cost_index"] + ctx["facility_tax"] + SCC_SURCHARGE)
    sell_price = ctx["sell_prices"].get(product_id)
    f = ctx["fees"]
    revenue = (sell_price or 0) * product_qty * runs * (1 - f["sales_tax"] - f["broker_fee"])
    total_cost = mat_cost + job_cost
    profit = revenue - total_cost
    seconds = (ctx["times"].get(bp_type_id) or 0) * runs * (1 - te / 100)
    return {
        "bp_type_id": bp_type_id, "bp_name": ctx["names"].get(bp_type_id, str(bp_type_id)),
        "product_id": product_id, "product_name": ctx["names"].get(product_id, str(product_id)),
        "runs": runs, "me": me_eff, "te": te, "units": product_qty * runs, "materials": lines,
        "material_cost": mat_cost, "job_cost": job_cost, "total_cost": total_cost,
        "sell_price": sell_price, "revenue": revenue, "profit": profit,
        "margin": (profit / total_cost * 100) if total_cost else 0,
        "seconds": seconds, "profit_per_hour": profit / (seconds / 3600) if seconds else 0,
        "missing_prices": missing or (not sell_price),
        "volume": ctx["volumes"].get(product_id),
    }


def _context(activity_id: int) -> dict:
    hub, mats, sells, adjusted = _price_maps()
    products, materials = {}, {}
    for bp, prod, qty in db.q(
            """SELECT p.type_id, p.product_type_id, p.quantity FROM bp_products p
               JOIN types t ON t.type_id = p.product_type_id
               JOIN types b ON b.type_id = p.type_id
               WHERE p.activity_id = ? AND t.published = 1 AND b.published = 1""", (activity_id,)):
        products[bp] = (prod, qty)
    for bp, mat, qty in db.q("SELECT type_id, material_type_id, quantity FROM bp_materials WHERE activity_id = ?",
                             (activity_id,)):
        materials.setdefault(bp, []).append((mat, qty))
    names = {r[0]: r[1] for r in db.q("SELECT type_id, name FROM types WHERE published = 1")}
    times = {r[0]: r[1] for r in db.q("SELECT type_id, time FROM bp_activity WHERE activity_id = ?", (activity_id,))}
    volumes = {r[0]: r[1] for r in db.q("SELECT type_id, avg_volume FROM market_history WHERE region_id = ?",
                                        (hub["region_id"],))}
    return {
        "products": products, "materials": materials, "names": names, "times": times,
        "mat_prices": mats, "sell_prices": sells, "adjusted": adjusted, "volumes": volumes,
        "cost_index": cost_index(activity_id),
        "facility_tax": (db.get_setting("industry_facility_tax_pct") or 0) / 100,
        "structure_bonus": db.get_setting("industry_structure_me_bonus_pct") or 0,
        "fees": market.fees(),
    }


def owned_blueprints() -> dict[int, dict]:
    """Best owned blueprint (highest ME) per blueprint type."""
    out = {}
    for r in db.q("SELECT type_id, material_efficiency AS me, time_efficiency AS te, runs, quantity FROM blueprints "
                  "ORDER BY material_efficiency DESC, runs = -1 DESC"):
        out.setdefault(r["type_id"], dict(r))
    return out


def all_profitability(activity_id: int = MANUFACTURING, runs: int = 10, only_owned: bool = False,
                      search: str = "", min_volume: float = 1.0, include_unseeded: bool = False) -> list[dict]:
    """Profitability for every blueprint. By default only blueprints sold on the market (BPOs) or
    owned by you are included, and products must trade at least `min_volume` units/day, which
    filters out troll-priced items nobody actually buys."""
    ctx = _context(activity_id)
    owned = owned_blueprints()
    # Blueprint copies can't be sold on the market, so "has a market price" means a real BPO exists.
    seeded = {r[0] for r in db.q("SELECT type_id FROM prices WHERE average > 0 UNION SELECT type_id FROM hub_prices WHERE best_sell > 0")}
    default_me = db.get_setting("industry_default_me") or 0
    out = []
    search = search.lower()
    for bp in ctx["products"]:
        own = owned.get(bp)
        if only_owned and not own:
            continue
        if not own and not include_unseeded and bp not in seeded:
            continue
        name = ctx["names"].get(ctx["products"][bp][0], "")
        if search and search not in name.lower():
            continue
        me = own["me"] if own else default_me
        te = own["te"] if own else (default_me * 2 if activity_id == MANUFACTURING else 0)
        r = evaluate(bp, activity_id, runs, me, te, ctx)
        if r and not r["missing_prices"] and (r["volume"] or 0) >= min_volume:
            r["owned"] = own
            out.append(r)
    out.sort(key=lambda x: x["profit_per_hour"], reverse=True)
    return out
