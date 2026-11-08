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
CORP = 98000001

FAKE = {
    f"/characters/{CID}/": {"name": "Test Pilot", "corporation_id": CORP},
    f"/characters/{CID}/roles/": {"roles": ["Director"]},
    f"/corporations/{CORP}/": {"name": "Pilot Holdings", "ticker": "PHLD", "ceo_id": CID, "member_count": 1},
    f"/corporations/{CORP}/divisions/": {
        "hangar": [{"division": 1, "name": "Main"}, {"division": 2, "name": "Minerals"}],
        "wallet": [{"division": 1, "name": "Master Wallet"}, {"division": 3, "name": "Trading"}]},
    f"/corporations/{CORP}/wallets/": [{"division": d, "balance": 1000.0 * d} for d in range(1, 8)],
    f"/corporations/{CORP}/wallets/1/transactions/": [
        {"transaction_id": 11, "date": "2026-11-03T00:00:00Z", "type_id": 34, "quantity": 50, "unit_price": 7.0,
         "is_buy": False, "location_id": 60003760, "client_id": 2, "journal_ref_id": 9}],
    f"/corporations/{CORP}/wallets/1/journal/": [
        {"id": 1, "date": "2026-11-03T00:00:00Z", "ref_type": "brokers_fee", "amount": -5.0, "balance": 1.0,
         "description": "corp fee"}],
    f"/corporations/{CORP}/orders/": [
        {"order_id": 3, "type_id": 35, "region_id": 10000002, "location_id": 60003760, "is_buy_order": False,
         "price": 20.0, "volume_total": 10, "volume_remain": 10, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "range": "station", "wallet_division": 3, "issued_by": CID}],
    f"/corporations/{CORP}/industry/jobs/": [
        {"job_id": 70, "activity_id": 1, "blueprint_type_id": 691, "product_type_id": 587, "runs": 2,
         "status": "active", "start_date": "2026-11-01T00:00:00Z", "end_date": "2099-01-01T00:00:00Z",
         "facility_id": 60003760, "location_id": 60003760, "cost": 10.0, "installer_id": CID}],
    f"/corporations/{CORP}/assets/": [
        {"item_id": 900, "type_id": 27, "location_id": 60003760, "location_flag": "OfficeFolder",
         "location_type": "station", "quantity": 1, "is_singleton": True},
        {"item_id": 901, "type_id": 34, "location_id": 900, "location_flag": "CorpSAG2",
         "location_type": "item", "quantity": 10000, "is_singleton": False},
        {"item_id": 902, "type_id": 587, "location_id": 60008494, "location_flag": "CorpDeliveries",
         "location_type": "station", "quantity": 1, "is_singleton": False},
    ],
    f"/corporations/{CORP}/blueprints/": [
        {"item_id": 950, "type_id": 691, "location_id": 900, "location_flag": "CorpSAG1",
         "material_efficiency": 8, "time_efficiency": 16, "runs": -1, "quantity": -1}],
    f"/characters/{CID}/skillqueue/": [
        {"queue_position": 0, "skill_id": 3446, "finished_level": 5, "start_date": "2026-11-01T00:00:00Z",
         "finish_date": "2099-01-01T00:00:00Z"}],
    f"/characters/{CID}/contracts/": [
        {"contract_id": 500, "type": "item_exchange", "status": "outstanding", "title": "Trit for sale",
         "issuer_id": CID, "issuer_corporation_id": CORP, "assignee_id": 0, "acceptor_id": 0,
         "for_corporation": False, "availability": "public", "price": 50000.0, "volume": 10.0,
         "start_location_id": 60003760, "end_location_id": 60003760,
         "date_issued": "2026-11-01T00:00:00Z", "date_expired": "2099-01-01T00:00:00Z"},
        {"contract_id": 501, "type": "courier", "status": "in_progress", "title": "Haul to Amarr",
         "issuer_id": CID, "issuer_corporation_id": CORP, "assignee_id": 0, "acceptor_id": 92000002,
         "for_corporation": True, "availability": "public", "reward": 5e6, "collateral": 1e8, "volume": 3000.0,
         "days_to_complete": 3, "start_location_id": 60003760, "end_location_id": 60008494,
         "date_issued": "2026-11-01T00:00:00Z", "date_expired": "2099-01-01T00:00:00Z",
         "date_accepted": "2026-11-02T00:00:00Z"}],
    f"/characters/{CID}/contracts/500/items/": [
        {"record_id": 1, "type_id": 34, "quantity": 10000, "is_included": True, "is_singleton": False}],
    f"/characters/{CID}/contracts/501/items/": [],
    f"/corporations/{CORP}/contracts/": [
        {"contract_id": 501, "type": "courier", "status": "in_progress", "title": "Haul to Amarr",
         "issuer_id": CID, "issuer_corporation_id": CORP, "assignee_id": 0, "acceptor_id": 92000002,
         "for_corporation": True, "availability": "public", "reward": 5e6, "collateral": 1e8, "volume": 3000.0,
         "days_to_complete": 3, "start_location_id": 60003760, "end_location_id": 60008494,
         "date_issued": "2026-11-01T00:00:00Z", "date_expired": "2099-01-01T00:00:00Z",
         "date_accepted": "2026-11-02T00:00:00Z"}],
    f"/corporations/{CORP}/contracts/501/items/": [
        {"record_id": 1, "type_id": 587, "quantity": 2, "is_included": True, "is_singleton": False}],
    f"/corporations/{CORP}/structures/": [
        {"structure_id": 1035000000001, "corporation_id": CORP, "name": "PHLD Fort", "type_id": 35832,
         "system_id": 30000142, "state": "shield_vulnerable", "fuel_expires": "2026-11-10T00:00:00Z",
         "services": [{"name": "Manufacturing (Standard)", "state": "online"}]},
        {"structure_id": 1035000000002, "corporation_id": CORP, "name": "PHLD Refinery", "type_id": 35835,
         "system_id": 30000142, "state": "armor_reinforce", "state_timer_end": "2099-01-01T00:00:00Z",
         "fuel_expires": "2099-01-01T00:00:00Z", "services": []}],
    f"/characters/{CID}/wallet/": 1_234_567.89,
    f"/characters/{CID}/orders/": [
        {"order_id": 1, "type_id": 34, "region_id": 10000002, "location_id": 60003760, "is_buy_order": False,
         "price": 5.0, "volume_total": 1000, "volume_remain": 500, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "range": "station"},
        {"order_id": 2, "type_id": 35, "region_id": 10000002, "location_id": 60003760, "is_buy_order": True,
         "price": 10.0, "volume_total": 100, "volume_remain": 100, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "escrow": 1000.0, "range": "station"},
        {"order_id": 3, "type_id": 35, "region_id": 10000002, "location_id": 60003760, "is_buy_order": False,
         "price": 20.0, "volume_total": 10, "volume_remain": 10, "issued": "2026-11-01T00:00:00Z", "duration": 90,
         "range": "station", "is_corporation": True},
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


ROLE_LOCKED: set = set()
UI_CALLS: list = []


def fake_handler(request: httpx.Request):
    path = request.url.path
    if path.startswith(f"/corporations/{CORP}/wallets/") and path not in FAKE:
        return httpx.Response(200, json=[], headers={"x-pages": "1"})  # empty wallet divisions
    if path == "/universe/names/":
        ids = json.loads(request.content)
        return httpx.Response(200, json=[{"id": i, "name": f"Pilot {i}", "category": "character"} for i in ids])
    if path.startswith("/ui/"):
        UI_CALLS.append((path, dict(request.url.params), request.headers.get("authorization")))
        return httpx.Response(204)
    if path in (f"/characters/{CID}/assets/names/", f"/corporations/{CORP}/assets/names/"):
        ids = json.loads(request.content)
        return httpx.Response(200, json=[{"item_id": i, "name": "My Rifter"} for i in ids])
    if path not in FAKE:
        return httpx.Response(404, json={"error": "not found"})
    if path in ROLE_LOCKED:
        return httpx.Response(403, json={"error": "Character does not have required role(s)"})
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
    db.ex("INSERT OR REPLACE INTO types (type_id, name, published) VALUES (34, 'Tritanium', 1), (35, 'Pyerite', 1), "
          "(587, 'Rifter', 1), (27, 'Office', 1)")
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


def reset_setup():
    for k in ("app_password", "eve_client_id", "eve_client_secret", "eve_callback_url"):
        db.ex("DELETE FROM kv WHERE key = ?", (k,))


def logged_in_client():
    """A TestClient for an app that has finished setup, already logged in."""
    from fastapi.testclient import TestClient
    from evetracker import auth, sso
    from evetracker.web import app
    reset_setup()
    auth.set_password("correct horse")
    sso.save_app("abc123", "s3cret", "http://testserver/callback")
    client = TestClient(app)
    client.__enter__()
    r = client.post("/login", data={"password": "correct horse"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def test_pages_render(fake_char):
    with logged_in_client() as client:
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


def test_app_password(monkeypatch):
    from fastapi.testclient import TestClient
    from evetracker import config
    from evetracker.web import app
    monkeypatch.setattr(config, "APP_PASSWORD", "hunter2")
    with TestClient(app) as client:
        r = client.get("/assets", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert client.get("/healthz").status_code == 200
        assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
        r = client.post("/login", data={"password": "hunter2"}, follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/assets").status_code == 200


def test_setup_wizard(fake_char):
    from fastapi.testclient import TestClient
    from evetracker import auth, sso
    from evetracker.web import app
    reset_setup()
    db.ex("DELETE FROM characters")
    with TestClient(app) as client:
        # a fresh install sends everything to the wizard
        for path in ("/", "/settings", "/login"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/setup", path
        assert "Choose a password" in client.get("/setup").text
        assert "match" in client.post("/setup/password", data={"password": "abcdef", "confirm": "abcdeX"}).text
        assert "at least" in client.post("/setup/password", data={"password": "abc", "confirm": "abc"}).text
        r = client.post("/setup/password", data={"password": "abcdef", "confirm": "abcdef"}, follow_redirects=False)
        assert r.status_code == 303 and auth.password_set()
        assert db.get_kv("app_password") != "abcdef"  # stored hashed
        # step 2: the callback URL is suggested from the address we're browsing at
        page = client.get("/setup").text
        assert "Connect your EVE application" in page and "http://testserver/callback" in page
        assert "callback URL should look like" in client.post(
            "/setup/eve", data={"client_id": "id", "client_secret": "sec", "callback_url": "nonsense"}).text
        r = client.post("/setup/eve", data={"client_id": "id1", "client_secret": "sec1",
                                             "callback_url": "http://testserver/callback"}, follow_redirects=False)
        assert r.status_code == 303 and sso.configured()
        # step 3: add a character
        assert "Log in with EVE Online" in client.get("/setup").text
        r = client.get("/sso/login", follow_redirects=False)
        assert "client_id=id1" in r.headers["location"] and "testserver%2Fcallback" in r.headers["location"]
        # nobody can re-run the password step once a password exists
        assert client.post("/setup/password", data={"password": "hacked1", "confirm": "hacked1"}).status_code == 403
        # once a character exists, setup is done
        db.ex("INSERT INTO characters (character_id, name) VALUES (1, 'X')")
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/"
    with TestClient(app) as anon:
        assert anon.get("/assets", follow_redirects=False).headers["location"] == "/login"
        assert anon.get("/setup", follow_redirects=False).headers["location"] == "/login"
        assert anon.post("/setup/eve", data={"client_id": "evil", "client_secret": "x",
                                             "callback_url": "http://evil/callback"},
                         follow_redirects=False).headers["location"] == "/login"
    assert sso.client_id() == "id1"


def test_settings_edit_app_and_password(fake_char):
    from fastapi.testclient import TestClient
    from evetracker import sso
    from evetracker.web import app
    client = logged_in_client()
    # blank secret keeps the old one
    r = client.post("/setup/eve", data={"client_id": "newid", "client_secret": "",
                                        "callback_url": "http://192.168.0.132:8000/callback", "next": "/settings"},
                    follow_redirects=False)
    assert "saved" in r.headers["location"]
    assert sso.client_id() == "newid" and sso.client_secret() == "s3cret"
    assert sso.callback_url() == "http://192.168.0.132:8000/callback"
    r = client.post("/settings/password", data={"current": "wrong", "password": "newpass1", "confirm": "newpass1"},
                    follow_redirects=False)
    assert "wrong" in r.headers["location"]
    with TestClient(app) as other:  # a second browser, logged in with the old password
        other.post("/login", data={"password": "correct horse"})
        assert other.get("/assets", follow_redirects=False).status_code == 200
        r = client.post("/settings/password", data={"current": "correct horse", "password": "newpass1",
                                                     "confirm": "newpass1"}, follow_redirects=False)
        assert "changed" in r.headers["location"]
        assert client.get("/assets", follow_redirects=False).status_code == 200   # this browser stays in
        assert other.get("/assets", follow_redirects=False).status_code == 303    # others are logged out
        assert "Wrong password" in other.post("/login", data={"password": "correct horse"}).text
        other.post("/login", data={"password": "newpass1"})
        assert other.get("/assets", follow_redirects=False).status_code == 200
    client.__exit__(None, None, None)


def test_stale_env_callback_does_not_override(monkeypatch):
    from evetracker import config, sso
    reset_setup()
    monkeypatch.setattr(config, "EVE_CALLBACK_URL", "http://raspberrypi.local:8000/callback")
    sso.save_app("dbid", "dbsecret", "http://192.168.0.132:8000/callback")
    assert not sso.from_env()
    assert sso.callback_url() == "http://192.168.0.132:8000/callback"
    monkeypatch.setattr(config, "EVE_CLIENT_ID", "envid")
    monkeypatch.setattr(config, "EVE_CLIENT_SECRET", "envsecret")
    assert sso.from_env() and sso.client_id() == "envid"
    assert sso.callback_url() == "http://raspberrypi.local:8000/callback"


def test_parse_num():
    from evetracker.web import parse_num
    assert parse_num("") == 0 and parse_num("", 5) == 5 and parse_num(None, 7) == 7
    assert parse_num("1,000,000") == 1e6
    assert parse_num("500k") == 5e5 and parse_num("1.5m") == 1.5e6 and parse_num("2B") == 2e9
    assert parse_num(" 10 m ") == 1e7 and parse_num("5%") == 5 and parse_num("1b isk") == 1e9
    assert parse_num("banana", 3) == 3


def test_blank_and_junk_filters_never_error(fake_char):
    """Every filter form submitted with empty boxes (what browsers send) or junk must still render."""
    blank_forms = [
        "/assets?char=0&q=&min_value=",
        "/assets?char=&q=&min_value=abc",
        "/assets?min_value=10m",
        "/trade?mode=station&hub=jita&min_margin=&max_price=&min_volume=",
        "/trade?mode=haul&src=jita&dst=amarr&haul_mode=instant&max_m3=&max_investment=&min_profit=&min_volume=",
        "/trade?mode=haul&max_investment=1.5b&max_m3=60,000",
        "/industry?activity=1&q=&runs=&min_volume=",
        "/industry?activity=junk&runs=abc&owned=&unseeded=",
        "/industry/691?runs=&me=&te=",
        "/orders?days=&char=",
    ]
    with logged_in_client() as client:
        for url in blank_forms:
            r = client.get(url)
            assert r.status_code == 200, (url, r.text[:300])
        r = client.post("/settings", data={"hubs": ["jita"], "hub_refresh_minutes": "", "industry_default_me": "",
                                           "industry_facility_tax_pct": "", "industry_structure_me_bonus_pct": "",
                                           "sales_tax_pct": "", "broker_fee_pct": "3.5%"}, follow_redirects=False)
        assert r.status_code == 303
        assert db.get_setting("broker_fee_pct") == 3.5 and db.get_setting("sales_tax_pct") is None
        assert db.get_setting("hub_refresh_minutes") == 30


def _fake_esi():
    return ESI(httpx.AsyncClient(transport=httpx.MockTransport(fake_handler), base_url="https://esi.evetech.net"))


def test_corporation_sync_end_to_end(fake_char):
    from evetracker import corp
    for t in ("orders", "assets", "industry_jobs", "blueprints", "transactions", "journal", "wallet",
              "corporations", "corp_wallets", "sync_state", "esi_cache"):
        db.ex(f"DELETE FROM {t}")
    esi = _fake_esi()

    async def go():
        await sync.sync_character(esi, CID, force=True)
        return await corp.sync_corporation(esi, CORP, force=True)

    res = asyncio.run(go())
    assert all(not str(v).startswith(("error", "skipped")) for v in res.values()), res
    # discovered via the character's affiliation, with roles
    c = db.q1("SELECT * FROM corporations WHERE corporation_id = ?", (CORP,))
    assert c["ticker"] == "PHLD" and c["ceo_id"] == CID
    assert json.loads(c["access"]) == {step: "ok" for step in corp.STEPS}
    # wallets: 7 divisions summed
    assert db.val("SELECT balance FROM wallet WHERE character_id = ?", (CORP,)) == 28000.0
    assert corp.wallet_label(CORP, 3) == "Trading" and corp.wallet_label(CORP, 5) == "Division 5"
    # the order the CEO placed for the corp is counted once, as the corp's
    assert db.val("SELECT character_id FROM orders WHERE order_id = 3") == CORP
    assert db.val("SELECT COUNT(*) FROM orders WHERE order_id = 3") == 1
    assert db.val("SELECT wallet_division FROM orders WHERE order_id = 3") == 3
    # assets inside the office resolve to the station and get the hangar's name
    assert db.val("SELECT root_location_id FROM assets WHERE item_id = 901") == 60003760
    assert corp.flag_label("CorpSAG2", corp.division_names(CORP)) == "Hangar 2: Minerals"
    assert db.val("SELECT character_id FROM industry_jobs WHERE job_id = 70") == CORP
    assert db.val("SELECT material_efficiency FROM blueprints WHERE item_id = 950") == 8
    # net worth: wallets + 10000 trit @4 + rifter @500k + sell order 10 x 20
    nw = sync.owner_networth(CORP)
    assert nw["wallet"] == 28000 and nw["assets"] == pytest.approx(40000 + 500000) and nw["sell_orders"] == 200
    assert "2 owners" in sync.snapshot_networth()
    # the corp's sale is matched against the character's purchase (FIFO across owners)
    items, totals = pnl.report(days=3650)
    assert sum(i["sold"] for i in items) == 100  # 50 sold by the character + 50 by the corp
    assert totals["broker_fees"] == pytest.approx(15.0 + 5.0)
    items, totals = pnl.report(days=3650, character_id=CORP)
    assert items[0]["sold"] == 50 and totals["broker_fees"] == pytest.approx(5.0)

    # every page shows the corporation
    with logged_in_client() as client:
        assert "[PHLD] Pilot Holdings" in client.get("/").text
        page = client.get(f"/assets?char={CORP}").text
        assert "Hangar 2: Minerals" in page and "Corp deliveries" in page
        page = client.get("/orders").text
        assert "Trading" in page and "[PHLD] Pilot Holdings" in page
        assert "[PHLD]" in client.get("/industry").text
        page = client.get("/settings").text
        assert "Corporations" in page and "pending" not in page
        # stop tracking hides it from the dashboard
        client.post(f"/corporations/{CORP}/toggle")
        assert "Pilot Holdings" not in client.get("/").text
        client.post(f"/corporations/{CORP}/toggle")


def test_corporation_without_roles(fake_char):
    from evetracker import corp
    db.ex("DELETE FROM sync_state")
    db.ex("DELETE FROM corporations")
    db.ex("DELETE FROM esi_cache")  # earlier tests cached the Director roles
    esi = _fake_esi()
    FAKE[f"/characters/{CID}/roles/"] = {"roles": ["Trader"]}
    FAKE[f"/corporations/{CORP}/"] = {"name": "Big Corp", "ticker": "BIG", "ceo_id": 1}
    try:
        async def go():
            await sync.sync_character(esi, CID, force=True)
            return await corp.sync_corporation(esi, CORP, force=True)
        res = asyncio.run(go())
    finally:
        FAKE[f"/characters/{CID}/roles/"] = {"roles": ["Director"]}
        FAKE[f"/corporations/{CORP}/"] = {"name": "Pilot Holdings", "ticker": "PHLD", "ceo_id": CID}
    access = json.loads(db.val("SELECT access FROM corporations WHERE corporation_id = ?", (CORP,)))
    assert access["orders"] == "ok"  # Trader may read orders
    assert "Director" in access["assets"] and "Accountant" in access["wallets"]
    assert res["assets"].startswith("skipped")


def test_migrates_old_database(tmp_path):
    """A database from before corporation support (single-column keys) upgrades in place."""
    import sqlite3
    from evetracker import db as dbmod
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE characters (character_id INTEGER PRIMARY KEY, name TEXT, corporation_id INTEGER,
            refresh_token TEXT, access_token TEXT, access_expires REAL, scopes TEXT, added_at REAL,
            last_sync REAL, sync_error TEXT, needs_reauth INTEGER DEFAULT 0);
        CREATE TABLE transactions (transaction_id INTEGER PRIMARY KEY, character_id INTEGER, date TEXT,
            type_id INTEGER, quantity INTEGER, unit_price REAL, is_buy INTEGER, location_id INTEGER,
            client_id INTEGER, journal_ref_id INTEGER, is_personal INTEGER);
        CREATE INDEX tx_date ON transactions(date);
        CREATE TABLE journal (id INTEGER PRIMARY KEY, character_id INTEGER, date TEXT, ref_type TEXT,
            amount REAL, balance REAL, description TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, character_id INTEGER, type_id INTEGER,
            region_id INTEGER, location_id INTEGER, is_buy INTEGER, price REAL, volume_total INTEGER,
            volume_remain INTEGER, issued TEXT, duration INTEGER, escrow REAL, range TEXT,
            competitor_price REAL, undercut INTEGER, checked_at REAL);
        INSERT INTO characters (character_id, name) VALUES (1, 'Old Pilot');
        INSERT INTO transactions VALUES (5, 1, '2026-01-01', 34, 10, 4.0, 1, 60003760, 2, 3, 1);
        INSERT INTO journal VALUES (6, 1, '2026-01-01', 'brokers_fee', -1.0, 0, 'x');
    """)
    old.commit()
    old.close()
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(dbmod.SCHEMA)
    dbmod.migrate(c)
    c.executescript(dbmod.SCHEMA)
    assert c.execute("SELECT quantity FROM transactions WHERE transaction_id = 5").fetchone()[0] == 10
    assert c.execute("SELECT division FROM journal WHERE id = 6").fetchone()[0] == 0
    # composite keys now allow the same id for a character and a corporation
    c.execute("INSERT INTO transactions (transaction_id, character_id) VALUES (5, 98000001)")
    c.execute("INSERT INTO journal (id, character_id, division) VALUES (6, 98000001, 1)")
    assert "roles" in {r[1] for r in c.execute("PRAGMA table_info(characters)")}
    assert "wallet_division" in {r[1] for r in c.execute("PRAGMA table_info(orders)")}
    assert c.execute("SELECT name FROM owners WHERE owner_id = 1").fetchone()[0] == "Old Pilot"
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'tx_date'").fetchone()[0] == 1


def test_login_server_down_is_temporary(fake_char, monkeypatch):
    """An unreachable EVE login server must not mark characters as needing to log in again."""
    from evetracker import sso
    from evetracker.worker import Worker
    db.ex("UPDATE characters SET access_expires = 0, needs_reauth = 0 WHERE character_id = ?", (CID,))

    async def down(data):
        raise sso.SSOUnavailable("EVE login server unreachable: test")

    monkeypatch.setattr(sso, "_token_request", down)
    w = Worker.__new__(Worker)
    w.esi = None
    w.running = set()
    msg = asyncio.run(Worker.job_characters(w))
    assert "skipped" in msg
    assert db.val("SELECT needs_reauth FROM characters WHERE character_id = ?", (CID,)) == 0


# ---------- repricing, contracts, skills, structures, health, Open in EVE ----------

def test_reprice_respects_four_significant_digits():
    from evetracker import pricing
    cases = {4.90: (4.89, 4.91), 1234000: (1233000, 1235000), 1000: (999.9, 1001), 999.9: (999.8, 1000),
             2.11: (2.10, 2.12), 187600000: (187500000, 187700000), 0.05: (0.04, 0.06)}
    for competitor, (sell, buy) in cases.items():
        assert pricing.beat_sell(competitor) == pytest.approx(sell), competitor
        assert pricing.beat_buy(competitor) == pytest.approx(buy), competitor
        assert pricing.is_valid(pricing.beat_sell(competitor)) and pricing.is_valid(pricing.beat_buy(competitor))
        assert pricing.beat_sell(competitor) < competitor < pricing.beat_buy(competitor)
    assert not pricing.is_valid(1234500) and pricing.is_valid(1235000)
    assert pricing.suggestion({"undercut": 1, "competitor_price": 4.9, "is_buy": 0}) == pytest.approx(4.89)
    assert pricing.suggestion({"undercut": 0, "competitor_price": 4.9, "is_buy": 0}) is None


def _sync_all_fake(fuel_in_days=2.0):
    from evetracker import corp
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + fuel_in_days * 86400))
    FAKE[f"/corporations/{CORP}/structures/"][0]["fuel_expires"] = soon
    for t in ("orders", "assets", "industry_jobs", "blueprints", "transactions", "journal", "wallet", "contracts",
              "contract_items", "corporations", "corp_wallets", "sync_state", "esi_cache", "corp_structures",
              "skillqueue", "names"):
        db.ex(f"DELETE FROM {t}")
    esi = _fake_esi()

    async def go():
        a = await sync.sync_character(esi, CID, force=True)
        b = await corp.sync_corporation(esi, CORP, force=True)
        return a, b
    return asyncio.run(go())


def test_contracts_skills_structures(fake_char):
    from evetracker import alerts, contracts, corp
    char_res, corp_res = _sync_all_fake(fuel_in_days=2.0)
    assert all(not str(v).startswith(("error", "skipped")) for v in {**char_res, **corp_res}.values()), \
        (char_res, corp_res)
    # the courier contract placed for the corp is the corp's (seen by both, stored once)
    assert db.val("SELECT character_id FROM contracts WHERE contract_id = 501") == CORP
    assert db.val("SELECT character_id FROM contracts WHERE contract_id = 500") == CID
    # a later character sync must not steal it back
    asyncio.run(sync.sync_character(_fake_esi(), CID, force=True))
    assert db.val("SELECT character_id FROM contracts WHERE contract_id = 501") == CORP
    rows = contracts.listing("open", prices={34: 4.0, 587: 500000})
    by_id = {r["contract_id"]: r for r in rows}
    assert by_id[500]["item_value"] == 40000 and by_id[500]["mine"]
    assert by_id[501]["item_value"] == 1000000 and by_id[501]["type_label"] == "Courier"
    assert contracts.name(92000002) == "Pilot 92000002"  # acceptor resolved via /universe/names/
    assert contracts.name(CORP) == "[PHLD] Pilot Holdings"
    # skill queue
    q = {x["character_id"]: x for x in alerts.skill_queues()}[CID]
    assert q["count"] == 1 and q["current"]["level"] == 5 and q["hours_left"] > 1000
    # structures: one low on fuel, one reinforced
    st = {s["name"]: s for s in corp.structures(7)}
    assert st["PHLD Fort"]["fuel_low"] and 1.9 < st["PHLD Fort"]["fuel_days"] < 2.1
    assert st["PHLD Refinery"]["danger"] and st["PHLD Refinery"]["state_label"] == "Armor reinforced"
    assert sde_name(1035000000001) == "PHLD Fort"  # structure names also feed asset locations
    texts = [a["text"] for a in alerts.collect()]
    assert any("PHLD Fort" in t and "days of fuel" in t for t in texts), texts
    assert any("PHLD Refinery: Armor reinforced" in t for t in texts), texts
    # an empty skill queue raises an alert
    db.ex("DELETE FROM skillqueue")
    assert any("skill queue is empty" in a["text"] for a in alerts.collect())


def sde_name(location_id):
    from evetracker import sde
    return sde.location_name(location_id)


def test_new_pages_and_open_in_eve(fake_char):
    _sync_all_fake()
    db.ex("INSERT OR REPLACE INTO orders (order_id, character_id, type_id, region_id, location_id, is_buy, price, "
          "volume_total, volume_remain, issued, duration, competitor_price, undercut, checked_at) "
          "VALUES (77, ?, 34, 10000002, 60003760, 0, 5.0, 10, 10, '2026-11-01T00:00:00Z', 90, 4.9, 1, 1)", (CID,))
    with logged_in_client() as client:
        client.app.state.worker.esi = _fake_esi()
        page = client.get("/").text
        assert "PHLD Fort" in page and "Skill queues" in page and "PHLD Refinery: Armor reinforced" in page
        page = client.get("/orders").text
        assert 'data-copy-text="4.89"' in page          # repricing helper
        assert 'data-market="34"' in page                # Open in EVE button
        page = client.get("/contracts").text
        assert "Trit for sale" in page and "Courier" not in client.get("/contracts?view=finished").text
        assert "Haul to Amarr" in client.get(f"/contracts?char={CORP}").text
        assert "Haul to Amarr" not in client.get(f"/contracts?char={CID}").text
        page = client.get("/system").text
        assert "SD card free" in page and "Background jobs" in page
        assert "Alerts &amp; EVE client" in client.get("/settings").text
        UI_CALLS.clear()
        r = client.post("/ui/market/34")
        assert r.status_code == 200 and r.json()["ok"] and "Tritanium" in r.json()["message"]
        r = client.post("/ui/waypoint/60008494")
        assert r.json()["ok"]
        assert UI_CALLS[0][0] == "/ui/openwindow/marketdetails/" and UI_CALLS[0][1]["type_id"] == "34"
        assert UI_CALLS[1][1]["destination_id"] == "60008494" and UI_CALLS[1][1]["clear_other_waypoints"] == "true"
        # without the permission, a friendly message instead of an error page
        db.ex("UPDATE characters SET scopes = 'esi-assets.read_assets.v1' WHERE character_id = ?", (CID,))
        r = client.post("/ui/market/34")
        assert r.status_code == 400 and "log in again" in r.json()["message"]
        assert 'data-market=' not in client.get("/orders").text  # buttons hidden when unusable


def test_health_and_maintenance(fake_char):
    from evetracker import health
    from evetracker.worker import Worker
    h = health.snapshot()
    assert h["disk_total_gb"] > 0 and h["db_mb"] >= 0 and isinstance(h["warnings"], list)
    fake = dict(h, disk_free_gb=0.4, disk_free_pct=3, temp_c=82, mem_available_mb=50)
    levels = dict((t.split()[0], lvl) for lvl, t in health.warnings(fake))
    assert levels == {"SD": "bad", "Pi": "bad", "Low": "warn"}
    db.ex("INSERT OR REPLACE INTO esi_cache VALUES ('stale', NULL, ?, '[]')", (time.time() - 3 * 86400,))
    db.ex("INSERT OR REPLACE INTO esi_cache VALUES ('fresh', NULL, ?, '[]')", (time.time() + 60,))
    msg = asyncio.run(Worker.job_maintenance(Worker.__new__(Worker)))
    assert "removed 1 stale cache" in msg
    assert db.val("SELECT COUNT(*) FROM esi_cache WHERE key = 'fresh'") == 1
