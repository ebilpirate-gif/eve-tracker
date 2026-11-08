"""Background scheduler: runs inside the web process (use a single uvicorn worker)."""
import asyncio
import logging
import time
import traceback

from . import corp, db, market, sde, sso, sync
from .esi import ESI

log = logging.getLogger("worker")


class Worker:
    def __init__(self):
        self.esi = ESI()
        self.running: set[str] = set()
        self.market_lock = asyncio.Lock()
        self._task = None

    def jobs(self):
        hub_minutes = db.get_setting("hub_refresh_minutes") or 30
        jobs = {
            "sde": (7 * 86400, self.job_sde),
            "prices": (3600, self.job_prices),
            "cost_indices": (3600, self.job_cost_indices),
            "characters": (300, self.job_characters),
            "corporations": (300, self.job_corporations),
            "undercut": (600, self.job_undercut),
            "networth": (3600, self.job_networth),
        }
        for key in db.get_setting("hubs"):
            if key in market.HUBS:
                jobs["hub:" + key] = (hub_minutes * 60, lambda k=key: self.job_hub(k))
        return jobs

    async def run_job(self, name: str, fn):
        if name in self.running:
            return
        self.running.add(name)
        started = time.time()
        try:
            msg = await fn()
            db.log_job(name, started, True, str(msg or ""))
            log.info("job %s ok in %.1fs: %s", name, time.time() - started, msg)
        except Exception as e:  # keep the scheduler alive no matter what
            log.error("job %s failed: %s\n%s", name, e, traceback.format_exc())
            db.log_job(name, started, False, f"{type(e).__name__}: {e}")
        finally:
            self.running.discard(name)

    def trigger(self, name: str):
        jobs = self.jobs()
        if name in jobs:
            asyncio.create_task(self.run_job(name, jobs[name][1]))

    async def loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                log_rows = {r["job"]: r for r in db.q("SELECT job, last_run, ok FROM job_log")}
                for name, (interval, fn) in self.jobs().items():
                    if name != "sde" and not sde.is_loaded():
                        continue  # everything else needs static data first
                    row = log_rows.get(name)
                    if row is not None and not row["ok"]:
                        interval = min(interval, 300)  # retry failures after 5 minutes
                    if time.time() - (row["last_run"] if row else 0) >= interval and name not in self.running:
                        asyncio.create_task(self.run_job(name, fn))
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(30)

    def start(self):
        self._task = asyncio.create_task(self.loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        await self.esi.close()

    # ---------- jobs ----------

    async def job_sde(self):
        return await sde.update_sde()

    async def job_prices(self):
        return await market.update_prices(self.esi)

    async def job_cost_indices(self):
        return await market.update_cost_indices(self.esi)

    async def job_characters(self, force: bool = False):
        out = []
        for row in db.q("SELECT character_id, name FROM characters WHERE needs_reauth = 0"):
            try:
                res = await sync.sync_character(self.esi, row["character_id"], force=force)
                out.append(f"{row['name']}: {len(res)} steps")
            except sso.NeedsReauth as e:
                out.append(f"{row['name']}: needs re-login ({e})")
            except sso.SSOUnavailable as e:
                out.append(f"{row['name']}: skipped, {e}")
        if db.val("SELECT COUNT(*) FROM corporations WHERE enabled = 1 AND last_sync IS NULL", default=0):
            self.trigger("corporations")  # a newly discovered corporation: sync it straight away
        return "; ".join(out) or "no characters"

    async def job_corporations(self, force: bool = False):
        return await corp.sync_all(self.esi, force=force)

    async def job_undercut(self):
        return await market.check_orders(self.esi)

    async def job_hub(self, key: str):
        # One hub at a time: each pulls hundreds of pages, which is plenty for a Pi.
        async with self.market_lock:
            msg = await market.update_hub(self.esi, key)
            region_id = market.HUBS[key]["region_id"]
            types = market.history_candidates().get(region_id, [])
            n = await market.update_history(self.esi, region_id, types)
        return f"{msg}; volume history refreshed for {n} types"

    async def job_networth(self):
        return sync.snapshot_networth()
