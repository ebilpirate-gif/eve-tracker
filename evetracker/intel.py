"""Intel: DOTLAN-style system statistics and sovereignty, routes, the local checker (paste the Local
member list), zKillboard pilot stats, standings from your contacts, and a d-scan parser."""
import asyncio
import json
import logging
import re
import time

import httpx

from . import config, contracts, db
from .esi import ESIError

log = logging.getLogger("intel")

ZKILL = "https://zkillboard.com/api/stats/characterID/{}/"
ZKILL_TTL = 6 * 3600
ENTITY_TTL = 3 * 86400
MAX_PILOTS = 1000


# ---------- system statistics & sovereignty (hourly) ----------

async def update_universe(esi) -> str:
    jumps, kills, sov, incursions = await asyncio.gather(
        esi.get("/universe/system_jumps/"), esi.get("/universe/system_kills/"),
        esi.get("/sovereignty/map/"), esi.get("/incursions/"))
    stats: dict[int, list] = {}
    for j in jumps:
        stats.setdefault(j["system_id"], [0, 0, 0, 0])[0] = j.get("ship_jumps", 0)
    for k in kills:
        s = stats.setdefault(k["system_id"], [0, 0, 0, 0])
        s[1], s[2], s[3] = k.get("ship_kills", 0), k.get("pod_kills", 0), k.get("npc_kills", 0)
    with db.tx() as c:
        c.execute("DELETE FROM system_stats")
        c.executemany("INSERT INTO system_stats VALUES (?, ?, ?, ?, ?)", [(sid, *v) for sid, v in stats.items()])
        c.execute("DELETE FROM sov")
        c.executemany("INSERT INTO sov VALUES (?, ?, ?, ?)",
                      [(s["system_id"], s.get("alliance_id"), s.get("corporation_id"), s.get("faction_id"))
                       for s in sov])
    infested = sorted({sid for i in incursions for sid in i.get("infested_solar_systems", [])})
    db.set_kv("incursions", infested)
    owners = {s.get("alliance_id") or s.get("faction_id") for s in sov} - {None}
    await contracts.resolve_names(esi, owners)
    db.set_kv("universe_updated", time.time())
    return f"{len(stats)} systems with activity, {len(owners)} sov holders, {len(infested)} incursion systems"


def map_intel() -> dict:
    owners = {}
    sov = []
    for sid, alliance, faction in db.q("SELECT system_id, alliance_id, faction_id FROM sov"):
        owner = alliance or faction
        if owner:
            sov.append([sid, owner])
            owners[owner] = None
    for oid in owners:
        owners[oid] = contracts.name(oid)
    return {
        "stats": [list(r) for r in db.q("SELECT system_id, jumps, ship_kills, pod_kills, npc_kills FROM system_stats")],
        "sov": sov, "owners": owners, "incursions": db.get_kv("incursions", []),
        "updated": db.get_kv("universe_updated"),
    }


# ---------- routes ----------

def find_system(text: str):
    text = (text or "").strip()
    if not text:
        return None
    row = db.q1("SELECT system_id, name FROM systems WHERE name = ? COLLATE NOCASE", (text,))
    if row:
        return row
    return db.q1("SELECT system_id, name FROM systems WHERE name LIKE ? COLLATE NOCASE ORDER BY length(name) LIMIT 1",
                 (text + "%",))


async def route(esi, origin: int, destination: int, flag: str = "shortest") -> list[dict]:
    flag = flag if flag in ("shortest", "secure", "insecure") else "shortest"
    path = await esi.get(f"/route/{origin}/{destination}/", {"flag": flag})
    stats = {r[0]: r for r in db.q(f"SELECT * FROM system_stats WHERE system_id IN ({','.join('?' * len(path))})",
                                   path)} if path else {}
    out = []
    for sid in path:
        s = db.q1("SELECT name, security, region_id FROM systems WHERE system_id = ?", (sid,))
        st = stats.get(sid)
        out.append({"system_id": sid, "name": s["name"] if s else str(sid), "sec": round(s["security"] or 0, 1) if s else 0,
                    "region": db.val("SELECT name FROM regions WHERE region_id = ?", (s["region_id"],)) if s else "",
                    "kills": (st["ship_kills"] + st["pod_kills"]) if st else 0, "jumps": st["jumps"] if st else 0})
    return out


# ---------- standings ----------

def store_contacts(owner_id: int, data: list) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM contacts WHERE owner_id = ?", (owner_id,))
        c.executemany("INSERT OR REPLACE INTO contacts VALUES (?, ?, ?, ?)",
                      [(owner_id, x["contact_id"], x.get("contact_type"), x.get("standing", 0)) for x in data])


def _friendly_ids() -> tuple[set, set, set]:
    chars = {r[0] for r in db.q("SELECT character_id FROM characters")}
    corps = {r[0] for r in db.q("SELECT corporation_id FROM corporations")} | \
        {r[0] for r in db.q("SELECT corporation_id FROM characters WHERE corporation_id IS NOT NULL")}
    alliances = {r[0] for r in db.q("SELECT alliance_id FROM corporations WHERE alliance_id IS NOT NULL")}
    return chars, corps, alliances


def standings_lookup():
    """Returns f(character_id, corporation_id, alliance_id) -> (standing, reason). The most specific
    contact wins (pilot, then corp, then alliance); personal lists beat corp lists beat alliance lists."""
    chars, corps, alliances = _friendly_ids()
    rank = {}  # list owner -> 0 personal, 1 corp, 2 alliance
    for c in chars:
        rank[c] = 0
    for c in corps:
        rank[c] = 1
    for a in alliances:
        rank[a] = 2
    best: dict[int, tuple] = {}
    for owner, contact, standing in db.q("SELECT owner_id, contact_id, standing FROM contacts"):
        r = rank.get(owner, 3)
        if contact not in best or r < best[contact][0]:
            best[contact] = (r, standing)

    def lookup(char_id, corp_id, alliance_id):
        if char_id in chars or corp_id in corps:
            return 10.0, "your corporation"
        if alliance_id and alliance_id in alliances:
            return 10.0, "your alliance"
        for eid, what in ((char_id, "pilot"), (corp_id, "corporation"), (alliance_id, "alliance")):
            if eid and eid in best:
                return best[eid][1], f"{what} standing"
        return 0.0, ""
    return lookup


# ---------- corporation / alliance names and tickers ----------

async def entity_infos(esi, corp_ids, alliance_ids) -> dict[int, dict]:
    ids = {("corporation", i) for i in corp_ids if i} | {("alliance", i) for i in alliance_ids if i}
    fresh = {r["id"]: dict(r) for r in db.q("SELECT * FROM entity_info WHERE fetched_at > ?", (time.time() - ENTITY_TTL,))}
    todo = [(k, i) for k, i in ids if i not in fresh]
    sem = asyncio.Semaphore(6)

    async def one(kind, eid):
        async with sem:
            try:
                info = await esi.get(f"/{'corporations' if kind == 'corporation' else 'alliances'}/{eid}/")
            except ESIError:
                return
            row = {"id": eid, "kind": kind, "name": info.get("name"), "ticker": info.get("ticker"),
                   "alliance_id": info.get("alliance_id"), "fetched_at": time.time()}
            db.ex("INSERT OR REPLACE INTO entity_info VALUES (?, ?, ?, ?, ?, ?)", tuple(row.values()))
            fresh[eid] = row

    await asyncio.gather(*(one(k, i) for k, i in todo))
    return fresh


# ---------- local checker ----------

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-.]{1,36}$")


def parse_names(text: str) -> list[str]:
    """One pilot per line, as copied from the Local member list (Ctrl+A, Ctrl+C)."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        name = line.strip().strip("\t")
        if "\t" in name:  # some lists paste extra columns; the name comes first
            name = name.split("\t")[0].strip()
        if NAME_RE.match(name) and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[:MAX_PILOTS]


async def check_local(esi, text: str) -> dict:
    names = parse_names(text)
    if not names:
        return {"pilots": [], "groups": [], "unknown": [], "count": 0}
    ids: dict[str, int] = {}
    for i in range(0, len(names), 500):
        res = await esi.post("/universe/ids/", names[i:i + 500])
        for c in res.get("characters", []):
            ids[c["name"].lower()] = c["id"]
    unknown = [n for n in names if n.lower() not in ids]
    char_ids = list(dict.fromkeys(ids.values()))
    affiliation = []
    for i in range(0, len(char_ids), 1000):
        affiliation += await esi.post("/characters/affiliation/", char_ids[i:i + 1000])
    infos = await entity_infos(esi, {a["corporation_id"] for a in affiliation},
                               {a.get("alliance_id") for a in affiliation})
    lookup = standings_lookup()
    name_of = {v: k for k, v in ids.items()}
    display = {n.lower(): n for n in names}
    cached = {r["character_id"]: json.loads(r["data"]) for r in db.q(
        f"SELECT character_id, data FROM zkill_stats WHERE fetched_at > ? AND character_id IN "
        f"({','.join('?' * len(char_ids))})", [time.time() - ZKILL_TTL, *char_ids])} if char_ids else {}
    pilots = []
    for a in affiliation:
        corp, alli = infos.get(a["corporation_id"], {}), infos.get(a.get("alliance_id"), {})
        standing, why = lookup(a["character_id"], a["corporation_id"], a.get("alliance_id"))
        pilots.append({
            "id": a["character_id"], "name": display.get(name_of[a["character_id"]], name_of[a["character_id"]]),
            "corporation_id": a["corporation_id"], "corporation": corp.get("name"), "corp_ticker": corp.get("ticker"),
            "alliance_id": a.get("alliance_id"), "alliance": alli.get("name"), "alliance_ticker": alli.get("ticker"),
            "npc_corp": 1000000 <= a["corporation_id"] < 2000000,
            "standing": standing, "standing_reason": why, "zkill": cached.get(a["character_id"]),
        })
    pilots.sort(key=lambda p: (p["standing"], -(p["zkill"] or {}).get("danger", 0), p["alliance"] or "~",
                               p["corporation"] or "", p["name"]))
    groups: dict = {}
    for p in pilots:
        key = p["alliance_id"] or p["corporation_id"]
        g = groups.setdefault(key, {"id": key, "kind": "alliance" if p["alliance_id"] else "corporation",
                                    "name": p["alliance"] or p["corporation"],
                                    "ticker": p["alliance_ticker"] or p["corp_ticker"],
                                    "standing": p["standing"], "count": 0})
        g["count"] += 1
    group_list = sorted(groups.values(), key=lambda g: (-g["count"], g["name"] or ""))
    return {"pilots": pilots, "groups": group_list, "unknown": unknown, "count": len(pilots),
            "hostile": sum(1 for p in pilots if p["standing"] < 0),
            "friendly": sum(1 for p in pilots if p["standing"] > 0)}


# ---------- zKillboard ----------

def summarize_zkill(d: dict) -> dict:
    ships = next((t.get("values") or [] for t in d.get("topLists") or [] if t.get("type") == "shipType"), [])
    return {
        "kills": d.get("shipsDestroyed") or 0, "losses": d.get("shipsLost") or 0,
        "isk_destroyed": d.get("iskDestroyed") or 0, "isk_lost": d.get("iskLost") or 0,
        "danger": d.get("dangerRatio") or 0, "gang": d.get("gangRatio") or 0,
        "solo": d.get("soloKills") or 0, "avg_gang": d.get("avgGangSize") or 0,
        "recent_kills": ((d.get("activepvp") or {}).get("kills") or {}).get("count", 0),
        "ships": [{"type_id": s.get("shipTypeID"), "name": s.get("shipName"), "group": s.get("groupName"),
                   "kills": s.get("kills")} for s in ships[:3]],
    }


async def zkill_stats(ids: list[int], client: httpx.AsyncClient | None = None) -> dict[int, dict]:
    """zKillboard stats for pilots, cached for 6 hours. Requests are kept gentle (2 at a time),
    as zKillboard asks of third-party tools."""
    ids = list(dict.fromkeys(int(i) for i in ids))[:50]
    fresh = {r["character_id"]: json.loads(r["data"]) for r in db.q(
        f"SELECT character_id, data FROM zkill_stats WHERE fetched_at > ? AND character_id IN ({','.join('?' * len(ids))})",
        [time.time() - ZKILL_TTL, *ids])} if ids else {}
    todo = [i for i in ids if i not in fresh]
    if not todo:
        return fresh
    ua = "eve-tracker/0.1 (self-hosted local checker" + (f"; {config.ESI_CONTACT})" if config.ESI_CONTACT else ")")
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": ua,
                                                                                     "Accept-Encoding": "gzip"})
    sem = asyncio.Semaphore(2)

    async def one(cid):
        async with sem:
            try:
                r = await client.get(ZKILL.format(cid))
                if r.status_code != 200:
                    return
                data = r.json() if r.content else {}
            except (httpx.HTTPError, ValueError) as e:
                log.info("zkill %s failed: %s", cid, e)
                return
            summary = summarize_zkill(data if isinstance(data, dict) else {})
            db.ex("INSERT OR REPLACE INTO zkill_stats VALUES (?, ?, ?)", (cid, json.dumps(summary), time.time()))
            fresh[cid] = summary

    try:
        await asyncio.gather(*(one(i) for i in todo))
    finally:
        if own:
            await client.aclose()
    return fresh


# ---------- d-scan ----------

_type_index: dict = {"at": 0, "by_name": {}}
DSCAN_CATEGORIES = {6: "Ship", 65: "Structure", 22: "Deployable", 18: "Drone", 87: "Fighter", 23: "Starbase",
                    40: "Sovereignty structure"}


def _types_by_name():
    if time.time() - _type_index["at"] > 3600:
        _type_index["by_name"] = {r[1].lower(): (r[0], r[1], r[2], r[3]) for r in db.q(
            """SELECT t.type_id, t.name, g.name, g.category_id FROM types t JOIN groups_ g ON g.group_id = t.group_id
               WHERE t.published = 1""")}
        _type_index["at"] = time.time()
    return _type_index["by_name"]


def _distance_km(text: str):
    t = text.strip().replace(",", "").replace(" ", " ")
    m = re.match(r"^([\d.]+)\s*(km|m|AU)$", t, re.I)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2).lower()
    return v / 1000 if unit == "m" else v * 149_597_870.7 if unit == "au" else v


def parse_dscan(text: str) -> dict:
    """D-scan rows are tab separated (id, name, type, distance in recent clients). We find the
    column that is a known item type, so older and newer formats both work."""
    by_name = _types_by_name()
    types: dict[int, dict] = {}
    groups: dict[str, dict] = {}
    total = on_grid = 0
    for line in (text or "").splitlines():
        cols = [c.strip() for c in line.split("\t")]
        match = next((by_name[c.lower()] for c in cols if c.lower() in by_name), None)
        if not match:
            continue
        type_id, name, group, category = match
        if category not in DSCAN_CATEGORIES:
            continue
        dist = next((d for d in (_distance_km(c) for c in cols) if d is not None), None)
        grid = dist is not None and dist < 10000
        t = types.setdefault(type_id, {"type_id": type_id, "name": name, "group": group,
                                       "kind": DSCAN_CATEGORIES[category], "count": 0, "on_grid": 0})
        t["count"] += 1
        t["on_grid"] += grid
        g = groups.setdefault(group, {"group": group, "kind": DSCAN_CATEGORIES[category], "count": 0})
        g["count"] += 1
        total += 1
        on_grid += grid
    return {"total": total, "on_grid": on_grid,
            "ships": sum(t["count"] for t in types.values() if t["kind"] == "Ship"),
            "types": sorted(types.values(), key=lambda t: (t["kind"] != "Ship", -t["count"], t["name"])),
            "groups": sorted(groups.values(), key=lambda g: (g["kind"] != "Ship", -g["count"], g["group"]))}
