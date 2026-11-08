"""Where everyone is: live locations of your characters and (with Director) corp member tracking,
plus the data behind the map page."""
import asyncio
import json
import logging
import time

from . import contracts, corp, db, sde, sso
from .esi import ESIError

log = logging.getLogger("locations")

LOCATION = "esi-location.read_location.v1"
SHIP = "esi-location.read_ship_type.v1"
ONLINE = "esi-location.read_online.v1"
WORMHOLE_REGIONS_FROM = 11000000      # wormhole / abyssal / void regions aren't on the stargate map
HIDDEN_REGIONS = {10000004, 10000017, 10000019}  # Jove regions: unreachable


# ---------- syncing ----------

async def sync_characters(esi) -> str:
    """Live location / ship / online status. Runs every minute; ESI caches these for 5-60 seconds."""
    rows = db.q("SELECT character_id, name, scopes FROM characters WHERE needs_reauth = 0")
    done = 0

    async def one(c):
        nonlocal done
        scopes = (c["scopes"] or "").split()
        if LOCATION not in scopes:
            return
        try:
            token = await sso.get_access_token(c["character_id"])
            cid = c["character_id"]
            loc = await esi.get(f"/characters/{cid}/location/", token=token, cache=False)
            ship = await esi.get(f"/characters/{cid}/ship/", token=token, cache=False) if SHIP in scopes else {}
            online = await esi.get(f"/characters/{cid}/online/", token=token, cache=False) if ONLINE in scopes else {}
        except (ESIError, sso.NeedsReauth, sso.SSOUnavailable) as e:
            log.info("location for %s failed: %s", c["name"], e)
            return
        db.ex("INSERT OR REPLACE INTO char_location VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (cid, loc.get("solar_system_id"), loc.get("station_id"), loc.get("structure_id"),
               ship.get("ship_type_id"), ship.get("ship_name"),
               None if not online else int(bool(online.get("online"))),
               online.get("last_login"), online.get("last_logout"), time.time()))
        done += 1

    await asyncio.gather(*(one(c) for c in rows))
    return f"{done} of {len(rows)} characters located"


def system_of(location_id) -> int | None:
    """Solar system for a station, structure or system id."""
    if not location_id:
        return None
    if 30000000 <= location_id < 33000000:
        return location_id
    if 60000000 <= location_id < 64000000:
        return db.val("SELECT system_id FROM stations WHERE station_id = ?", (location_id,))
    return db.val("SELECT system_id FROM structures WHERE structure_id = ?", (location_id,))


async def sync_members(esi, corp_id, token) -> str:
    """Corp member tracking (Director): every member's location, ship and last login."""
    data = await esi.get(f"/corporations/{corp_id}/membertracking/", token=token)
    now = time.time()
    with db.tx() as c:
        c.execute("DELETE FROM corp_members WHERE corporation_id = ?", (corp_id,))
        c.executemany("INSERT OR REPLACE INTO corp_members VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      [(m["character_id"], corp_id, m.get("location_id"), system_of(m.get("location_id")),
                        m.get("ship_type_id"), m.get("logon_date"), m.get("logoff_date"), now) for m in data])
    await contracts.resolve_names(esi, {m["character_id"] for m in data})
    return f"{len(data)} members"


# ---------- map data ----------

def _scale():
    """Project EVE's 3D coordinates top-down (x, -z) into a 0..1000 square."""
    hidden = ",".join(str(r) for r in HIDDEN_REGIONS)
    b = db.q1(f"""SELECT MIN(x), MAX(x), MIN(z), MAX(z) FROM systems
                  WHERE x IS NOT NULL AND region_id < {WORMHOLE_REGIONS_FROM} AND region_id NOT IN ({hidden})""")
    span = max(b[1] - b[0], b[3] - b[2]) or 1
    return lambda x, z: (round((x - b[0]) / span * 1000, 2), round((b[3] - z) / span * 1000, 2))


def static_map() -> dict:
    proj = _scale()
    hidden = ",".join(str(r) for r in HIDDEN_REGIONS)
    systems = []
    for s in db.q(f"""SELECT system_id, name, x, z, security, region_id FROM systems
                      WHERE x IS NOT NULL AND region_id < {WORMHOLE_REGIONS_FROM} AND region_id NOT IN ({hidden})"""):
        px, py = proj(s["x"], s["z"])
        systems.append([s["system_id"], s["name"], px, py, round(s["security"] or 0, 2), s["region_id"]])
    ids = {s[0] for s in systems}
    jumps = sorted({(min(a, b), max(a, b)) for a, b in db.q("SELECT from_system_id, to_system_id FROM jumps")
                    if a in ids and b in ids})
    # Region labels at the middle of their systems (the SDE's region centres are 3D and look off top-down).
    regions = {}
    for s in systems:
        r = regions.setdefault(s[5], [0.0, 0.0, 0])
        r[0] += s[2]
        r[1] += s[3]
        r[2] += 1
    names = {r[0]: r[1] for r in db.q("SELECT region_id, name FROM regions")}
    region_list = [[rid, names.get(rid, str(rid)), round(v[0] / v[2], 1), round(v[1] / v[2], 1)]
                   for rid, v in regions.items()]
    return {"systems": systems, "jumps": [list(j) for j in jumps], "regions": region_list}


def _security_label(system_id):
    s = db.q1("SELECT name, security, region_id FROM systems WHERE system_id = ?", (system_id,))
    if not s:
        return None, None, None
    region = db.val("SELECT name FROM regions WHERE region_id = ?", (s["region_id"],))
    return s["name"], round(s["security"] or 0, 1), region


def live_map(prices: dict) -> dict:
    mine = []
    my_ids = set()
    for r in db.q("""SELECT c.character_id, c.name, c.scopes, l.* FROM characters c
                     LEFT JOIN char_location l ON l.character_id = c.character_id ORDER BY c.name"""):
        my_ids.add(r["character_id"])
        system, sec, region = _security_label(r["system_id"]) if r["system_id"] else (None, None, None)
        docked = r["station_id"] or r["structure_id"]
        mine.append({
            "id": r["character_id"], "name": r["name"], "system_id": r["system_id"], "system": system,
            "sec": sec, "region": region, "docked": sde.location_name(docked) if docked else None,
            "ship": sde.type_name(r["ship_type_id"]) if r["ship_type_id"] else None, "ship_name": r["ship_name"],
            "online": r["online"], "last_login": r["last_login"], "last_logout": r["last_logout"],
            "updated": r["updated"], "has_scope": LOCATION in (r["scopes"] or "").split(),
        })
    members = []
    for m in db.q("""SELECT m.*, n.name FROM corp_members m LEFT JOIN names n ON n.id = m.character_id
                     JOIN corporations c ON c.corporation_id = m.corporation_id AND c.enabled = 1"""):
        if m["character_id"] in my_ids:
            continue
        system, sec, region = _security_label(m["system_id"]) if m["system_id"] else (None, None, None)
        online = bool(m["logon_date"] and (not m["logoff_date"] or m["logon_date"] > m["logoff_date"]))
        members.append({
            "id": m["character_id"], "name": m["name"] or f"#{m['character_id']}", "system_id": m["system_id"],
            "system": system, "sec": sec, "region": region,
            "where": sde.location_name(m["location_id"]) if m["location_id"] else None,
            "ship": sde.type_name(m["ship_type_id"]) if m["ship_type_id"] else None,
            "online": online, "last_login": m["logon_date"], "last_logout": m["logoff_date"],
        })
    members.sort(key=lambda m: (not m["online"], m["last_logout"] or ""), reverse=False)
    # asset value per system
    per_system: dict[int, float] = {}
    for root, type_id, qty, bpc in db.q("SELECT root_location_id, type_id, quantity, is_blueprint_copy FROM assets"):
        if bpc:
            continue
        sid = system_of(root)
        if sid:
            per_system[sid] = per_system.get(sid, 0.0) + prices.get(type_id, 0.0) * qty
    structures = [{"system_id": s["system_id"], "name": s["name"], "state": s["state_label"], "danger": s["danger"],
                   "fuel_days": s["fuel_days"], "low": s["fuel_low"] or s["low_power"]}
                  for s in corp.structures(db.get_setting("alert_fuel_days") or 7)]
    return {"characters": mine, "members": members,
            "assets": [[sid, round(v)] for sid, v in per_system.items() if v > 0],
            "structures": structures, "generated": time.time()}


def map_version() -> str:
    return str(int(db.get_kv("sde_updated", 0) or 0))
