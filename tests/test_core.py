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
