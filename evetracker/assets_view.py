"""Builds the assets tree: location -> owner -> hangar / division -> ships & containers -> contents,
with values rolled up at every level, like the in-game inventory."""
import re

from . import corp, db, market, sde

# Where an item sits inside a ship or container, grouped into readable sections.
SLOT_SECTIONS = [
    (r"^HiSlot\d", "High slots"), (r"^MedSlot\d", "Mid slots"), (r"^LoSlot\d", "Low slots"),
    (r"^RigSlot\d", "Rigs"), (r"^SubSystemSlot\d", "Subsystems"), (r"^ServiceSlot\d", "Service slots"),
    (r"^Cargo$", "Cargo"), (r"^DroneBay$", "Drone bay"), (r"^FighterBay$", "Fighter bay"),
    (r"^FighterTube\d", "Fighter tubes"), (r"^ShipHangar$", "Ship hangar"), (r"^FleetHangar$", "Fleet hangar"),
    (r"^SpecializedFuelBay$", "Fuel bay"), (r"^Specialized\w+Hold$", "Specialized holds"),
    (r"^FrigateEscapeBay$", "Frigate escape bay"), (r"^QuafeBay$", "Quafe bay"), (r"^Implant$", "Implants"),
    (r"^Locked$|^Unlocked$", "Contents"), (r"^AutoFit$", "Contents"),
]
SECTION_ORDER = [s for _, s in SLOT_SECTIONS]
TOP_LEVEL_LABELS = {
    "Hangar": "Item hangar", "Deliveries": "Deliveries", "CorpDeliveries": "Corp deliveries",
    "AssetSafety": "Asset safety", "HangarAll": "Hangar", "Unlocked": "Hangar", "Locked": "Hangar",
}
THIN_MARKET_FACTOR = 3.0  # flag prices this many times above CCP's average price
OFFICE_TYPE_ID = 27


def section_for(flag: str) -> str:
    for pattern, label in SLOT_SECTIONS:
        if re.match(pattern, flag or ""):
            return label
    return (flag or "Other").replace("Specialized", "").strip() or "Other"


def _price_info(prices: dict, averages: dict, type_id: int, bpc: bool):
    if bpc:
        return 0.0, False
    unit = prices.get(type_id, 0.0)
    avg = averages.get(type_id)
    thin = bool(avg and unit > avg * THIN_MARKET_FACTOR and unit > 1_000_000)
    return unit, thin


def build(owner: int = 0, side: str | None = None) -> dict:
    """Returns {"locations": [...], "total": float, "count": int}. Each node:
    {name, label, type_id, qty, unit, value, total, thin, bpc, children: [section...]};
    sections: {label, total, nodes}."""
    prices = market.price_map(side=side)
    averages = {r[0]: r[1] for r in db.q("SELECT type_id, average FROM prices WHERE average > 0")}
    params, where = [], ""
    if owner:
        where, params = "WHERE a.character_id = ?", [owner]
    rows = [dict(r) for r in db.q(
        f"""SELECT a.*, t.name AS type_name, o.name AS owner_name, o.is_corp
            FROM assets a LEFT JOIN types t ON t.type_id = a.type_id
            LEFT JOIN owners o ON o.owner_id = a.character_id {where}""", params)]
    by_id = {r["item_id"]: r for r in rows}
    children: dict[int, list] = {}
    for r in rows:
        if r["location_id"] in by_id:
            children.setdefault(r["location_id"], []).append(r)
    divisions = {}

    def node(r) -> dict:
        unit, thin = _price_info(prices, averages, r["type_id"], bool(r["is_blueprint_copy"]))
        n = {"item_id": r["item_id"], "type_id": r["type_id"], "name": r["type_name"] or f"Type {r['type_id']}",
             "label": r["item_name"], "qty": r["quantity"], "unit": unit, "value": unit * r["quantity"],
             "thin": thin, "bpc": bool(r["is_blueprint_copy"]), "children": []}
        kids = children.get(r["item_id"], [])
        if kids:
            sections: dict[str, list] = {}
            for k in kids:
                sections.setdefault(section_for(k["location_flag"]), []).append(node(k))
            n["children"] = _sections(sections, SECTION_ORDER)
        n["total"] = n["value"] + sum(s["total"] for s in n["children"])
        n["count"] = 1 + sum(s["count"] for s in n["children"])
        return n

    # Top level: items not inside another item, grouped by root location -> owner -> section.
    locations: dict[int, dict] = {}
    for r in rows:
        if r["location_id"] in by_id:
            continue
        loc = locations.setdefault(r["root_location_id"], {"owners": {}})
        own = loc["owners"].setdefault(r["character_id"], {"name": r["owner_name"] or str(r["character_id"]),
                                                           "is_corp": r["is_corp"], "sections": {}})
        if r["type_id"] == OFFICE_TYPE_ID and r["location_flag"] == "OfficeFolder":
            # A corp office: show its hangar divisions directly under the corporation.
            if r["character_id"] not in divisions:
                divisions[r["character_id"]] = corp.division_names(r["character_id"])
            for k in children.get(r["item_id"], []):
                label = corp.flag_label(k["location_flag"], divisions[r["character_id"]])
                own["sections"].setdefault(label, []).append(node(k))
            continue
        flag = r["location_flag"]
        label = TOP_LEVEL_LABELS.get(flag) or (corp.flag_label(flag, None) if r["is_corp"] else flag)
        if r["location_type"] == "solar_system":
            label = "In space"
        own["sections"].setdefault(label, []).append(node(r))

    out = []
    for loc_id, loc in locations.items():
        owners = []
        for oid, own in loc["owners"].items():
            sections = _sections(own["sections"], order=None)
            owners.append({"id": oid, "name": own["name"], "is_corp": own["is_corp"], "sections": sections,
                           "total": sum(s["total"] for s in sections), "count": sum(s["count"] for s in sections)})
        owners.sort(key=lambda o: o["total"], reverse=True)
        out.append({"id": loc_id, "name": sde.location_name(loc_id), "owners": owners,
                    "total": sum(o["total"] for o in owners), "count": sum(o["count"] for o in owners)})
    out.sort(key=lambda x: x["total"], reverse=True)
    return {"locations": out, "total": sum(x["total"] for x in out), "count": sum(x["count"] for x in out)}


def _sections(sections: dict[str, list], order) -> list[dict]:
    result = []
    for label, nodes in sections.items():
        nodes.sort(key=lambda n: n["total"], reverse=True)
        result.append({"label": label, "nodes": nodes, "total": sum(n["total"] for n in nodes),
                       "count": sum(n["count"] for n in nodes)})
    if order:
        rank = {lab: i for i, lab in enumerate(order)}
        result.sort(key=lambda s: (rank.get(s["label"], len(rank)), s["label"]))
    else:
        # Hangar divisions in number order, everything else by value.
        def key(s):
            m = re.match(r"Hangar (\d)", s["label"])
            return (0, int(m.group(1)), 0) if m else (1, 0, -s["total"])
        result.sort(key=key)
    return result
