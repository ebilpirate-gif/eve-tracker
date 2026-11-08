"""Static Data Export import from Fuzzwork's CSV dumps (types, blueprints, map, stations)."""
import asyncio
import csv
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


def import_csv(name: str, path) -> int:
    """Stream a CSV file into its table (never holds the whole file in memory)."""
    table, cols, conv = TABLES[name]
    count = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        def rows():
            nonlocal count
            for r in reader:
                count += 1
                yield conv(r)

        with db.tx() as c:
            c.execute(f"DELETE FROM {table}")
            c.executemany(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
                          f"VALUES ({', '.join('?' * len(cols))})", rows())
    return count


async def update_sde() -> str:
    SDE_DIR.mkdir(parents=True, exist_ok=True)
    counts = []
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        for name in TABLES:
            path = SDE_DIR / f"{name}.csv"
            async with client.stream("GET", BASE_URL + name + ".csv") as resp:
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        fh.write(chunk)
            # Import in a thread so the web UI stays responsive on slow hardware.
            counts.append(f"{name}={await asyncio.to_thread(import_csv, name, path)}")
            path.unlink(missing_ok=True)
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
