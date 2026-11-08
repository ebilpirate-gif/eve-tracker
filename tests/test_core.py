import asyncio
import json
import time
from datetime import date

import httpx
import pytest

from evetracker import db, industry, market, pnl, sync
from evetracker.esi import ESI


def test_material_quantity():
    assert industry.material_quantity(100, 1, 10) == 90
    assert industry.material_quantity(1, 10, 10) == 10          # never below one per run
    assert industry.material_quantity(3, 10, 10) == 27
    assert industry.material_quantity(150, 1, 10, 1.0) == 134   # 133.65 -> 134


def test_fifo_basic_and_unmatched():
    tx = [
        {"type_id": 1, "quantity": 10, "unit_price": 100, "is_buy": 1, "date": "2026-01-01"},
        {"type_id": 1, "quantity": 10, "unit_price": 200, "is_buy": 1, "date": "2026-01-02"},
        {"type_id": 1, "quantity": 15, "unit_price": 300, "is_buy": 0, "date": "2026-01-03"},
        {"type_id": 2, "quantity": 5, "unit_price": 50, "is_buy": 0, "date": "2026-01-03"},
    ]
    s = pnl.fifo(tx, sales_tax=0.0)
    assert s[1]["cost"] == 10 * 100 + 5 * 200
    assert s[1]["revenue"] == 15 * 300
    assert s[1]["profit"] == 4500 - 2000
    assert s[2]["unmatched"] == 5 and s[2]["sold"] == 0 and s[2]["profit"] == 0


def test_fifo_window_consumes_earlier_lots():
    tx = [
        {"type_id": 1, "quantity": 10, "unit_price": 100, "is_buy": 1, "date": "2026-01-01"},
        {"type_id": 1, "quantity": 10, "unit_price": 200, "is_buy": 1, "date": "2026-01-02"},
        {"type_id": 1, "quantity": 10, "unit_price": 300, "is_buy": 0, "date": "2026-01-03"},  # before window
        {"type_id": 1, "quantity": 10, "unit_price": 300, "is_buy": 0, "date": "2026-02-01"},
    ]
    s = pnl.fifo(tx, since="2026-01-15")
    assert s[1]["sold"] == 10 and s[1]["cost"] == 2000  # matched to the second lot


def test_resolve_roots_nested():
    assets = [
        {"item_id": 1, "location_id": 60003760},  # ship in Jita
        {"item_id": 2, "location_id": 1},          # container in ship
        {"item_id": 3, "location_id": 2},          # item in container
        {"item_id": 4, "location_id": 1030000000000},  # item in a structure
    ]
    roots = sync.resolve_roots(assets)
    assert roots == {1: 60003760, 2: 60003760, 3: 60003760, 4: 1030000000000}


def test_summarize_history():
    entries = [{"date": "2026-01-20", "volume": 300, "average": 10.0},
               {"date": "2026-01-25", "volume": 100, "average": 20.0},
               {"date": "2025-06-01", "volume": 9999, "average": 1.0}]
    vol, price = market.summarize_history(entries, days=30, today=date(2026, 1, 30))
    assert vol == pytest.approx(400 / 30)
    assert price == pytest.approx(12.5)


def test_hub_aggregator():
    agg = market._HubAggregator(60003760)
    agg([
        {"location_id": 60003760, "type_id": 34, "is_buy_order": True, "price": 4.0, "volume_remain": 100},
        {"location_id": 60003760, "type_id": 34, "is_buy_order": True, "price": 4.5, "volume_remain": 50},
        {"location_id": 60003760, "type_id": 34, "is_buy_order": False, "price": 5.0, "volume_remain": 10},
        {"location_id": 60003760, "type_id": 34, "is_buy_order": False, "price": 4.8, "volume_remain": 20},
        {"location_id": 99, "type_id": 34, "is_buy_order": False, "price": 1.0, "volume_remain": 20},
    ])
    assert agg.data[34] == [4.5, 4.8, 150, 30, 2, 2]


# ---------- character sync against a fake ESI ----------

CID = 90000001

FAKE = {
    f"/characters/{CID}/wallet/": 1_234_567.89,
    f"/characters/{CID}/orders/": [
        {"order_id": 1, "type_id": 34, "region_id": 10000002, "location_id": 60003760, "is_buy_order": False,
         "price": 5.0, "volume_total": 1000, "volume_remain": 500, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "range": "station"},
        {"order_id": 2, "type_id": 35, "region_id": 10000002, "location_id": 60003760, "is_buy_order": True,
         "price": 10.0, "volume_total": 100, "volume_remain": 100, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "escrow": 1000.0, "range": "station"},
    ],
    f"/characters/{CID}/wallet/transactions/": [
        {"transaction_id": 11, "date": "2026-11-01T00:00:00Z", "type_id": 34, "quantity": 100, "unit_price": 4.0,
         "is_buy": True, "location_id": 60003760, "client_id": 1, "journal_ref_id": 1, "is_personal": True},
        {"transaction_id": 12, "date": "2026-11-02T00:00:00Z", "type_id": 34, "quantity": 50, "unit_price": 6.0,
         "is_buy": False, "location_id": 60003760, "client_id": 1, "journal_ref_id": 2, "is_personal": True},
    ],
    f"/characters/{CID}/wallet/journal/": [
        {"id": 1, "date": "2026-11-02T00:00:00Z", "ref_type": "brokers_fee", "amount": -15.0, "balance": 1.0,
         "description": "fee"}],
    f"/characters/{CID}/assets/": [
        {"item_id": 500, "type_id": 587, "location_id": 60003760, "location_flag": "Hangar",
         "location_type": "station", "quantity": 1, "is_singleton": True},
        {"item_id": 501, "type_id": 34, "location_id": 500, "location_flag": "Cargo",
         "location_type": "item", "quantity": 1000, "is_singleton": False},
        {"item_id": 502, "type_id": 35, "location_id": 1035000000000, "location_flag": "Hangar",
         "location_type": "item", "quantity": 10, "is_singleton": False},
    ],
    f"/characters/{CID}/industry/jobs/": [
        {"job_id": 7, "activity_id": 1, "blueprint_type_id": 691, "product_type_id": 587, "runs": 1,
         "status": "active", "start_date": "2026-11-01T00:00:00Z", "end_date": "2026-11-01T01:00:00Z",
         "facility_id": 60003760, "cost": 100.0}],
    f"/characters/{CID}/blueprints/": [
        {"item_id": 800, "type_id": 691, "location_id": 60003760, "material_efficiency": 10,
         "time_efficiency": 20, "runs": -1, "quantity": -1}],
    f"/characters/{CID}/skills/": {"skills": [{"skill_id": 16622, "active_skill_level": 4, "trained_skill_level": 4,
                                               "skillpoints_in_skill": 1}]},
    "/universe/structures/1035000000000/": None,  # 403: no docking access
    "/markets/10000002/orders/": [
        {"order_id": 99, "type_id": 34, "location_id": 60003760, "is_buy_order": False, "price": 4.9,
         "volume_remain": 1, "volume_total": 1},
        {"order_id": 1, "type_id": 34, "location_id": 60003760, "is_buy_order": False, "price": 5.0,
         "volume_remain": 500, "volume_total": 1000},
    ],
}


def fake_handler(request: httpx.Request):
    path = request.url.path
    if path == f"/characters/{CID}/assets/names/":
        ids = json.loads(request.content)
        return httpx.Response(200, json=[{"item_id": i, "name": "My Rifter"} for i in ids])
    if path not in FAKE:
        return httpx.Response(404, json={"error": "not found"})
    if FAKE[path] is None:
        return httpx.Response(403, json={"error": "forbidden"})
    if path.endswith("/orders/") and path.startswith("/markets") and request.url.params.get("type_id") != "34":
        return httpx.Response(200, json=[])
    return httpx.Response(200, json=FAKE[path], headers={"x-pages": "1"})


@pytest.fixture
def fake_char():
    db.ex("DELETE FROM characters")
    db.ex("INSERT INTO characters (character_id, name, refresh_token, access_token, access_expires, scopes) "
          "VALUES (?, ?, 'r', 'a', ?, ?)",
          (CID, "Test Pilot", time.time() + 3600, " ".join(__import__("evetracker.sso").sso.SCOPES)))
    db.ex("INSERT OR REPLACE INTO types (type_id, name, published) VALUES (34, 'Tritanium', 1), (35, 'Pyerite', 1), (587, 'Rifter', 1)")
    db.ex("INSERT OR REPLACE INTO prices VALUES (34, 4.0, 4.0), (35, 10.0, 10.0), (587, 500000, 500000)")
    return CID


def test_sync_character_end_to_end(fake_char):
    esi = ESI(httpx.AsyncClient(transport=httpx.MockTransport(fake_handler), base_url="https://esi.evetech.net"))

    async def go():
        res = await sync.sync_character(esi, CID, force=True)
        msg = await market.check_orders(esi)
        return res, msg

    res, msg = asyncio.run(go())
    assert all(not str(v).startswith("error") for v in res.values()), res
    assert db.val("SELECT balance FROM wallet WHERE character_id = ?", (CID,)) == pytest.approx(1234567.89)
    # nested cargo resolves to the station; the named ship got its name
    assert db.val("SELECT root_location_id FROM assets WHERE item_id = 501") == 60003760
    assert db.val("SELECT item_name FROM assets WHERE item_id = 500") == "My Rifter"
    assert db.val("SELECT accessible FROM structures WHERE structure_id = 1035000000000") == 0
    assert db.val("SELECT level FROM skills WHERE skill_id = 16622") == 4
    # our sell order at 5.0 is undercut by a 4.9 rival (our own order 1 is ignored)
    assert db.val("SELECT undercut FROM orders WHERE order_id = 1") == 1
    assert db.val("SELECT competitor_price FROM orders WHERE order_id = 1") == 4.9
    assert "1 undercut" in msg
    nw = sync.character_networth(CID)
    assert nw["wallet"] == pytest.approx(1234567.89)
    assert nw["assets"] == pytest.approx(500000 + 1000 * 4.0 + 10 * 10.0)
    assert nw["sell_orders"] == pytest.approx(2500) and nw["buy_escrow"] == pytest.approx(1000)
    items, totals = pnl.report(days=3650)
    assert totals["profit"] == pytest.approx(50 * 6.0 - 50 * 4.0 - 50 * 6.0 * market.fees()["sales_tax"])
    assert totals["broker_fees"] == pytest.approx(15.0)


def test_pages_render(fake_char):
    from fastapi.testclient import TestClient
    from evetracker.web import app
    with TestClient(app) as client:
        for path in ["/", "/assets", "/assets?q=rif", "/trade", "/trade?mode=haul", "/orders", "/orders?days=7",
                     "/industry", "/industry?activity=11", "/industry/691", "/settings", "/healthz"]:
            r = client.get(path)
            assert r.status_code == 200, (path, r.text[:500])
        r = client.post("/settings", data={"hubs": ["jita", "amarr"], "valuation_hub": "jita", "sales_tax_pct": "3.6"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert db.get_setting("sales_tax_pct") == 3.6
        assert db.get_setting("hubs") == ["jita", "amarr"]
        r = client.post("/settings", data={"hubs": ["jita"]}, headers={"origin": "http://evil.example"},
                        follow_redirects=False)
        assert r.status_code == 403
        r = client.get("/sso/login", follow_redirects=False)
        assert r.status_code == 303 and "login.eveonline.com" in r.headers["location"]
