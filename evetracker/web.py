"""FastAPI web app: pages, SSO login and settings."""
import hmac
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, industry, market, pnl, sde, sso, sync
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")
# httpx logs every request at INFO: thousands of lines per market refresh, all hitting the SD card.
logging.getLogger("httpx").setLevel(logging.WARNING)
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    app.state.worker = Worker()
    if not config.DISABLE_WORKER:
        app.state.worker.start()
    yield
    await app.state.worker.stop()


app = FastAPI(title="EVE Tracker", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# ---------- template helpers ----------


def isk(v, digits=2):
    if v is None:
        return "–"
    a = abs(v)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= limit:
            return f"{v / limit:,.{digits}f}{suffix}"
    return f"{v:,.2f}"


def ago(ts):
    if not ts:
        return "never"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    d = time.time() - ts
    future = d < 0
    d = abs(d)
    if d < 90:
        s = f"{int(d)}s"
    elif d < 5400:
        s = f"{int(d // 60)}m"
    elif d < 172800:
        s = f"{d / 3600:.1f}h"
    else:
        s = f"{int(d // 86400)}d"
    return f"in {s}" if future else f"{s} ago"


templates.env.filters["isk"] = isk
templates.env.filters["ago"] = ago
templates.env.filters["num"] = lambda v, d=0: "–" if v is None else f"{v:,.{d}f}"
templates.env.globals["type_name"] = sde.type_name
templates.env.globals["location_name"] = sde.location_name
templates.env.globals["HUBS"] = market.HUBS
templates.env.globals["ACTIVITY_NAMES"] = industry.ACTIVITY_NAMES


def render(request: Request, name: str, **ctx):
    ctx.setdefault("nav", name.split(".")[0])
    ctx["sso_configured"] = sso.configured()
    ctx["sde_loaded"] = sde.is_loaded()
    ctx["password_set"] = auth.password_set()
    return templates.TemplateResponse(request, name, ctx)


def back(request: Request, default="/"):
    ref = request.headers.get("referer") or default
    return RedirectResponse(ref, status_code=303)


# ---------- auth ----------

OPEN_PATHS = ("/login", "/static/", "/healthz")


@app.middleware("http")
async def require_password(request: Request, call_next):
    path = request.url.path
    if not path.startswith(OPEN_PATHS):
        if not auth.password_set():
            # Brand-new install: everything leads to the setup wizard until a password exists.
            if not path.startswith("/setup"):
                return RedirectResponse("/setup", status_code=303)
        elif request.session.get("authed") != auth.session_token():
            return RedirectResponse("/login", status_code=303)
    if request.method == "POST":
        # Same-origin check for form posts (CSRF protection).
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=30 * 86400, same_site="lax")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not auth.password_set():
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if auth.check_password(password):
        request.session["authed"] = auth.session_token()
        return RedirectResponse("/", status_code=303)
    time.sleep(1)
    return render(request, "login.html", error="Wrong password")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- EVE SSO ----------

@app.get("/sso/login")
def sso_login(request: Request):
    if not sso.configured():
        return RedirectResponse("/setup", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["sso_state"] = state
    return RedirectResponse(sso.authorize_url(state), status_code=303)


@app.get("/callback")
async def sso_callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("sso_state", None)
    if not code or not expected or not hmac.compare_digest(state, expected):
        return HTMLResponse("Invalid SSO state. <a href='/settings'>Try again</a>.", status_code=400)
    try:
        char = await sso.exchange_code(code)
    except sso.NeedsReauth as e:
        return HTMLResponse(f"SSO failed: {e}. <a href='/settings'>Back</a>", status_code=400)
    log.info("added character %s", char["name"])
    worker = request.app.state.worker
    worker.trigger("characters")
    return RedirectResponse("/settings?added=" + str(char["character_id"]), status_code=303)


@app.post("/characters/{character_id}/delete")
def delete_character(character_id: int):
    with db.tx() as c:
        for table in ("characters", "sync_state", "assets", "wallet", "orders", "industry_jobs", "blueprints",
                      "skills", "networth", "transactions", "journal"):
            c.execute(f"DELETE FROM {table} WHERE character_id = ?", (character_id,))
    return RedirectResponse("/settings", status_code=303)


@app.post("/jobs/{name}")
async def run_job(request: Request, name: str):
    worker = request.app.state.worker
    if name == "characters-force":
        import asyncio
        asyncio.create_task(worker.run_job("characters", lambda: worker.job_characters(force=True)))
    else:
        worker.trigger(name)
    return back(request)


# ---------- pages ----------

def characters():
    return [dict(r) for r in db.q("SELECT * FROM characters ORDER BY name")]


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    prices = market.price_map()
    chars = characters()
    for c in chars:
        c["nw"] = sync.character_networth(c["character_id"], prices)
    totals = {k: sum(c["nw"][k] for c in chars) for k in ("wallet", "assets", "sell_orders", "buy_escrow", "total")}
    series = db.q("SELECT ts, SUM(wallet + assets + sell_orders + buy_escrow) AS v FROM networth "
                  "WHERE ts > ? GROUP BY ts ORDER BY ts", (time.time() - 90 * 86400,))
    undercut = db.val("SELECT COUNT(*) FROM orders WHERE undercut = 1", default=0)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs_ready = db.val("SELECT COUNT(*) FROM industry_jobs WHERE status = 'ready' OR (status = 'active' AND end_date <= ?)",
                        (now_iso,), 0)
    jobs_active = db.val("SELECT COUNT(*) FROM industry_jobs WHERE status = 'active' AND end_date > ?", (now_iso,), 0)
    return render(request, "dashboard.html", chars=chars, totals=totals, chart=svg_chart(series),
                  undercut=undercut, jobs_ready=jobs_ready, jobs_active=jobs_active,
                  hub_updated=db.get_kv("hub_updated:" + db.get_setting("valuation_hub")))


def svg_chart(series, w=800, h=180):
    pts = [(r[0], r[1]) for r in series if r[1] is not None]
    if len(pts) < 2:
        return None
    x0, x1 = pts[0][0], pts[-1][0]
    lo, hi = min(p[1] for p in pts), max(p[1] for p in pts)
    if hi == lo:
        hi = lo + 1
    pad = 8
    coords = " ".join(f"{pad + (x - x0) / (x1 - x0) * (w - 2 * pad):.1f},{h - pad - (y - lo) / (hi - lo) * (h - 2 * pad):.1f}"
                      for x, y in pts)
    return {"w": w, "h": h, "points": coords, "lo": lo, "hi": hi,
            "start": datetime.fromtimestamp(x0).strftime("%b %d"), "end": datetime.fromtimestamp(x1).strftime("%b %d")}


@app.get("/assets", response_class=HTMLResponse)
def assets_page(request: Request, char: int = 0, q: str = "", min_value: float = 0):
    prices = market.price_map()
    where, params = [], []
    if char:
        where.append("a.character_id = ?")
        params.append(char)
    rows = db.q(f"""SELECT a.*, t.name AS type_name, COALESCE(t.packaged_volume, t.volume) AS m3, c.name AS char_name
                    FROM assets a LEFT JOIN types t ON t.type_id = a.type_id
                    LEFT JOIN characters c ON c.character_id = a.character_id
                    {'WHERE ' + ' AND '.join(where) if where else ''}""", params)
    by_id = {r["item_id"]: r for r in rows}
    ql = q.lower()
    locations = defaultdict(lambda: {"value": 0.0, "items": [], "count": 0})
    total = 0.0
    for r in rows:
        name = r["type_name"] or f"Type {r['type_id']}"
        unit = 0.0 if r["is_blueprint_copy"] else prices.get(r["type_id"], 0.0)
        value = unit * r["quantity"]
        if ql and ql not in name.lower() and ql not in (r["item_name"] or "").lower():
            continue
        if value < min_value:
            continue
        parent = by_id.get(r["location_id"])
        inside = None
        if parent:
            inside = parent["item_name"] or parent["type_name"]
        loc = locations[r["root_location_id"]]
        loc["value"] += value
        loc["count"] += 1
        loc["items"].append({"type_id": r["type_id"], "name": name, "qty": r["quantity"], "unit": unit,
                             "value": value, "char": r["char_name"], "flag": r["location_flag"],
                             "inside": inside, "label": r["item_name"], "bpc": r["is_blueprint_copy"]})
        total += value
    locs = []
    for lid, loc in locations.items():
        loc["items"].sort(key=lambda i: i["value"], reverse=True)
        locs.append({"id": lid, "name": sde.location_name(lid), **loc})
    locs.sort(key=lambda x: x["value"], reverse=True)
    return render(request, "assets.html", locations=locs, total=total, chars=characters(), char=char, q=q,
                  min_value=min_value, item_count=sum(l["count"] for l in locs))


@app.get("/trade", response_class=HTMLResponse)
def trade_page(request: Request, mode: str = "station", hub: str = "jita", src: str = "jita", dst: str = "amarr",
               haul_mode: str = "instant", min_margin: float = 5, min_volume: float = 10,
               max_price: float = 0, max_m3: float = 0, max_investment: float = 0, min_profit: float = 0):
    enabled = [h for h in db.get_setting("hubs") if h in market.HUBS]
    rows = []
    if mode == "station":
        hub = hub if hub in enabled else enabled[0]
        rows = market.station_trades(hub, min_margin, min_volume, max_price or None)
    else:
        src = src if src in enabled else enabled[0]
        dst = dst if dst in enabled else enabled[-1]
        rows = market.hauling_trades(src, dst, haul_mode, min_profit, max_m3 or None, max_investment or None, min_volume)
    updated = {h: db.get_kv("hub_updated:" + h) for h in enabled}
    return render(request, "trade.html", rows=rows, mode=mode, hub=hub, src=src, dst=dst, haul_mode=haul_mode,
                  min_margin=min_margin, min_volume=min_volume, max_price=max_price, max_m3=max_m3,
                  max_investment=max_investment, min_profit=min_profit, enabled=enabled, updated=updated,
                  fees=market.fees())


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, days: int = 30, char: int = 0):
    orders = db.q("""SELECT o.*, t.name AS type_name, c.name AS char_name FROM orders o
                     LEFT JOIN types t ON t.type_id = o.type_id LEFT JOIN characters c ON c.character_id = o.character_id
                     ORDER BY o.is_buy, o.undercut DESC, t.name""")
    items, totals = pnl.report(days, char or None)
    txs = db.q("""SELECT x.*, t.name AS type_name, c.name AS char_name FROM transactions x
                  LEFT JOIN types t ON t.type_id = x.type_id LEFT JOIN characters c ON c.character_id = x.character_id
                  ORDER BY date DESC LIMIT 200""")
    wallets = db.q("SELECT c.name, w.balance, w.updated FROM wallet w JOIN characters c USING (character_id) ORDER BY c.name")
    last_check = db.val("SELECT MAX(checked_at) FROM orders")
    return render(request, "orders.html", orders=orders, items=items[:300], totals=totals, txs=txs, days=days,
                  wallets=wallets, chars=characters(), char=char, last_check=last_check)


@app.get("/industry", response_class=HTMLResponse)
def industry_page(request: Request, activity: int = 1, runs: int = 10, owned: int = 0, q: str = "",
                  min_volume: float = 5, unseeded: int = 0):
    runs = max(1, min(runs, 1000))
    rows = industry.all_profitability(activity, runs, bool(owned), q, min_volume, bool(unseeded))
    jobs = db.q("""SELECT j.*, c.name AS char_name FROM industry_jobs j LEFT JOIN characters c USING (character_id)
                   WHERE j.status IN ('active', 'ready', 'paused') ORDER BY j.end_date""")
    return render(request, "industry.html", rows=rows[:300], total_rows=len(rows), activity=activity, runs=runs,
                  owned=owned, q=q, min_volume=min_volume, unseeded=unseeded, jobs=jobs,
                  cost_index=industry.cost_index(activity), system=db.get_setting("industry_system"),
                  now_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


@app.get("/industry/{bp_type_id}", response_class=HTMLResponse)
def blueprint_page(request: Request, bp_type_id: int, runs: int = 10, me: int = -1, te: int = -1):
    activity = industry.MANUFACTURING if db.q1(
        "SELECT 1 FROM bp_products WHERE type_id = ? AND activity_id = 1", (bp_type_id,)) else industry.REACTION
    own = industry.owned_blueprints().get(bp_type_id)
    default_me = db.get_setting("industry_default_me") or 0
    me = me if me >= 0 else (own["me"] if own else default_me)
    te = te if te >= 0 else (own["te"] if own else default_me * 2)
    r = industry.evaluate(bp_type_id, activity, max(1, runs), me, te)
    return render(request, "blueprint.html", nav="industry", r=r, own=own, runs=runs, activity=activity,
                  bp_type_id=bp_type_id)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, added: int = 0):
    settings = {k: db.get_setting(k) for k in db.DEFAULT_SETTINGS}
    jobs = db.q("SELECT * FROM job_log ORDER BY job")
    steps = db.q("SELECT s.*, c.name FROM sync_state s JOIN characters c USING (character_id) ORDER BY c.name, s.step")
    return render(request, "settings.html", settings=settings, chars=characters(), jobs=jobs, steps=steps,
                  fees=market.fees(), added=added, callback_url=sso.callback_url(), scopes=sso.SCOPES,
                  client_id=sso.client_id(), has_secret=bool(sso.client_secret()), sso_from_env=sso.from_env(),
                  password_from_env=auth.from_env(), suggested_callback=suggested_callback(request),
                  msg=request.query_params.get("msg"), err=request.query_params.get("err"),
                  running=request.app.state.worker.running)


def _opt_float(v: str):
    v = (v or "").strip()
    return float(v) if v else None


@app.post("/settings")
def save_settings(request: Request, sales_tax_pct: str = Form(""), broker_fee_pct: str = Form(""),
                  valuation_hub: str = Form("jita"), valuation_side: str = Form("sell"),
                  hubs: list[str] = Form([]), hub_refresh_minutes: int = Form(30),
                  industry_hub: str = Form("jita"), industry_system: str = Form("Jita"),
                  industry_facility_tax_pct: float = Form(0.25), industry_structure_me_bonus_pct: float = Form(0),
                  industry_default_me: int = Form(10), industry_material_side: str = Form("sell")):
    hubs = [h for h in hubs if h in market.HUBS] or ["jita"]
    if valuation_hub not in hubs:
        hubs.append(valuation_hub)
    if industry_hub not in hubs:
        hubs.append(industry_hub)
    values = {
        "sales_tax_pct": _opt_float(sales_tax_pct), "broker_fee_pct": _opt_float(broker_fee_pct),
        "valuation_hub": valuation_hub, "valuation_side": valuation_side, "hubs": hubs,
        "hub_refresh_minutes": max(10, hub_refresh_minutes), "industry_hub": industry_hub,
        "industry_system": industry_system.strip() or "Jita",
        "industry_facility_tax_pct": industry_facility_tax_pct,
        "industry_structure_me_bonus_pct": industry_structure_me_bonus_pct,
        "industry_default_me": max(0, min(10, industry_default_me)), "industry_material_side": industry_material_side,
    }
    for k, v in values.items():
        db.set_setting(k, v)
    return RedirectResponse("/settings", status_code=303)


# ---------- setup wizard ----------

def suggested_callback(request: Request) -> str:
    """The callback URL matching how the browser is reaching the app right now."""
    return str(request.base_url).rstrip("/") + "/callback"


def setup_step() -> str:
    if not auth.password_set():
        return "password"
    if not sso.configured():
        return "eve"
    if not db.val("SELECT COUNT(*) FROM characters", default=0):
        return "character"
    return "done"


def _callback_problem(callback: str) -> str | None:
    if not callback.startswith(("http://", "https://")) or not callback.rstrip("/").endswith("/callback"):
        return "The callback URL should look like http://192.168.0.10:8000/callback"
    return None


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    step = setup_step()
    if step == "done":
        return RedirectResponse("/", status_code=303)
    from urllib.parse import urlsplit
    callback = sso.callback_url()
    mismatch = bool(callback) and urlsplit(callback).netloc != request.headers.get("host")
    return render(request, "setup.html", nav="setup", step=step, error=error, scopes=sso.SCOPES,
                  suggested_callback=suggested_callback(request), callback_url=callback, mismatch=mismatch,
                  client_id=sso.client_id(), sso_from_env=sso.from_env(), min_length=auth.MIN_LENGTH)


@app.post("/setup/password")
def setup_password(request: Request, password: str = Form(...), confirm: str = Form(...)):
    if auth.password_set():
        return JSONResponse({"error": "a password is already set"}, status_code=403)
    problem = auth.validate_new(password, confirm)
    if problem:
        return setup_page(request, error=problem)
    auth.set_password(password)
    request.session["authed"] = auth.session_token()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/eve")
def setup_eve(request: Request, client_id: str = Form(...), client_secret: str = Form(""),
              callback_url: str = Form(...), next: str = Form("/setup")):
    target = next if next in ("/setup", "/settings") else "/setup"
    if sso.from_env():
        return RedirectResponse(target, status_code=303)
    problem = _callback_problem(callback_url.strip())
    if not problem and not client_id.strip():
        problem = "Paste the Client ID from your EVE application."
    if not problem and not (client_secret.strip() or sso.client_secret()):
        problem = "Paste the Secret Key from your EVE application."
    if problem:
        if target == "/settings":
            return RedirectResponse("/settings?err=" + quote(problem), status_code=303)
        return setup_page(request, error=problem)
    sso.save_app(client_id, client_secret, callback_url)
    if target == "/settings":
        return RedirectResponse("/settings?msg=" + quote("EVE application saved."), status_code=303)
    return RedirectResponse(target, status_code=303)


@app.post("/settings/password")
def change_password(request: Request, current: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    if auth.from_env():
        return RedirectResponse("/settings?err=" + quote("The password is set in .env; change it there."), status_code=303)
    if not auth.check_password(current):
        time.sleep(1)
        return RedirectResponse("/settings?err=" + quote("Current password is wrong."), status_code=303)
    problem = auth.validate_new(password, confirm)
    if problem:
        return RedirectResponse("/settings?err=" + quote(problem), status_code=303)
    auth.set_password(password)
    request.session["authed"] = auth.session_token()  # other browsers are logged out
    return RedirectResponse("/settings?msg=" + quote("Password changed."), status_code=303)
