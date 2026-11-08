"""Static Data Export import from Fuzzwork's CSV dumps (types, blueprints, map, stations)."""
import csv
import io
import logging
import time

import httpx

from . import config, db

log = logging.getLogger("sde")

BASE_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/"
SDE_DIR = config.DATA_DIR / "sde"


def _num(v, cast=float):
    if v in ("", None, "None"):
        return None
    try:
        return cast(float(v)) if cast is int else cast(v)
    except ValueError:
        return None


TABLES = {
    "invTypes": ("types", ["type_id", "group_id", "name", "volume", "packaged_volume", "portion_size",
                           "base_price", "published", "market_group_id", "meta_level"],
                 lambda r: (int(r["typeID"]), _num(r["groupID"], int), r["typeName"], _num(r["volume"]),
                            _num(r["packagedVolume"]), _num(r["portionSize"], int), _num(r["basePrice"]),
                            _num(r["published"], int), _num(r["marketGroupID"], int), _num(r["metaLevel"], int))),
    "invGroups": ("groups_", ["group_id", "category_id", "name"],
                  lambda r: (int(r["groupID"]), int(r["categoryID"]), r["groupName"])),
    "invCategories": ("categories", ["category_id", "name"],
                      lambda r: (int(r["categoryID"]), r["categoryName"])),
    "industryActivity": ("bp_activity", ["type_id", "activity_id", "time"],
                         lambda r: (int(r["typeID"]), int(r["activityID"]), int(r["time"]))),
    "industryActivityMaterials": ("bp_materials", ["type_id", "activity_id", "material_type_id", "quantity"],
                                  lambda r: (int(r["typeID"]), int(r["activityID"]),
                                             int(r["materialTypeID"]), int(r["quantity"]))),
    "industryActivityProducts": ("bp_products", ["type_id", "activity_id", "product_type_id", "quantity"],
                                 lambda r: (int(r["typeID"]), int(r["activityID"]),
                                            int(r["productTypeID"]), int(r["quantity"]))),
    "mapRegions": ("regions", ["region_id", "name"], lambda r: (int(r["regionID"]), r["regionName"])),
    "mapSolarSystems": ("systems", ["system_id", "name", "region_id", "security"],
                        lambda r: (int(r["solarSystemID"]), r["solarSystemName"], int(r["regionID"]),
                                   _num(r["security"]))),
    "staStations": ("stations", ["station_id", "name", "system_id", "region_id"],
                    lambda r: (int(r["stationID"]), r["stationName"], int(r["solarSystemID"]), int(r["regionID"]))),
}


def import_csv(name: str, text: str) -> int:
    table, cols, conv = TABLES[name]
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
    count = 0

    def rows():
        nonlocal count
        for r in reader:
            count += 1
            yield conv(r)

    with db.tx() as c:
        c.execute(f"DELETE FROM {table}")
        c.executemany(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows())
    return count


async def update_sde() -> str:
    SDE_DIR.mkdir(parents=True, exist_ok=True)
    counts = []
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for name in TABLES:
            resp = await client.get(BASE_URL + name + ".csv")
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig")
            counts.append(f"{name}={import_csv(name, text)}")
            log.info("SDE %s imported", name)
    db.set_kv("sde_updated", time.time())
    return ", ".join(counts)


def is_loaded() -> bool:
    return (db.val("SELECT COUNT(*) FROM types", default=0) or 0) > 1000


# ---------- lookups ----------

def type_name(type_id: int) -> str:
    return db.val("SELECT name FROM types WHERE type_id = ?", (type_id,), f"Type {type_id}")


def location_name(location_id: int) -> str:
    if location_id is None:
        return "Unknown"
    if 60000000 <= location_id < 64000000:
        return db.val("SELECT name FROM stations WHERE station_id = ?", (location_id,), f"Station {location_id}")
    if 30000000 <= location_id < 33000000:
        n = db.val("SELECT name FROM systems WHERE system_id = ?", (location_id,), f"System {location_id}")
        return f"{n} (in space)"
    if location_id == 2004:
        return "Asset Safety"
    n = db.val("SELECT name FROM structures WHERE structure_id = ?", (location_id,))
    return n or f"Structure {location_id}"


def system_by_name(name: str):
    return db.q1("SELECT * FROM systems WHERE name = ? COLLATE NOCASE", (name,))
