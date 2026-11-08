"""Async ESI client: retries, error-limit backoff, ETag caching and parallel pagination."""
import asyncio
import email.utils
import json
import logging
import time

import httpx

from . import config, db

log = logging.getLogger("esi")

BASE = "https://esi.evetech.net"
COMPAT_DATE = "2025-08-26"


class ESIError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"ESI {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


def _expires(headers) -> float:
    exp = headers.get("expires")
    if exp:
        try:
            return email.utils.parsedate_to_datetime(exp).timestamp()
        except (TypeError, ValueError):
            pass
    return time.time() + 60


class ESI:
    def __init__(self, client: httpx.AsyncClient | None = None):
        ua = "eve-tracker/0.1 (self-hosted asset & trading tool"
        ua += f"; {config.ESI_CONTACT})" if config.ESI_CONTACT else ")"
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": ua, "X-Compatibility-Date": COMPAT_DATE, "Accept": "application/json"},
        )
        self._pause_until = 0.0

    async def close(self):
        await self.client.aclose()

    async def _respect_limits(self):
        wait = self._pause_until - time.time()
        if wait > 0:
            log.warning("ESI error limit reached, pausing %.0fs", wait)
            await asyncio.sleep(wait)

    def _track_limits(self, resp: httpx.Response):
        remain = resp.headers.get("x-esi-error-limit-remain")
        reset = resp.headers.get("x-esi-error-limit-reset")
        if remain is not None and reset is not None and int(remain) < 20:
            self._pause_until = max(self._pause_until, time.time() + int(reset) + 1)

    async def request(self, method: str, path: str, *, params=None, json_body=None, token=None,
                      headers=None, retries: int = 3) -> httpx.Response:
        url = path if path.startswith("http") else BASE + path
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        attempt = 0
        while True:
            await self._respect_limits()
            try:
                resp = await self.client.request(method, url, params=params, json=json_body, headers=hdrs)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                attempt += 1
                if attempt > retries:
                    raise ESIError(0, url, str(e)) from e
                await asyncio.sleep(2 ** attempt)
                continue
            self._track_limits(resp)
            if resp.status_code in (420, 429):
                retry_after = int(resp.headers.get("retry-after") or resp.headers.get("x-esi-error-limit-reset") or 30)
                self._pause_until = max(self._pause_until, time.time() + retry_after)
                attempt += 1
                if attempt > retries + 2:
                    raise ESIError(resp.status_code, url, resp.text)
                continue
            if resp.status_code >= 500 and attempt < retries:
                attempt += 1
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400 and resp.status_code != 304:
                raise ESIError(resp.status_code, url, resp.text)
            return resp

    async def get(self, path: str, params=None, token=None, cache: bool = True):
        """GET returning parsed JSON. With cache=True, honours `expires` and uses ETags."""
        if not cache:
            resp = await self.request("GET", path, params=params, token=token)
            return resp.json()
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
        row = db.q1("SELECT etag, expires, body FROM esi_cache WHERE key = ?", (key,))
        if row and row["expires"] > time.time():
            return json.loads(row["body"])
        headers = {"If-None-Match": row["etag"]} if row and row["etag"] else None
        resp = await self.request("GET", path, params=params, token=token, headers=headers)
        if resp.status_code == 304 and row:
            db.ex("UPDATE esi_cache SET expires = ? WHERE key = ?", (_expires(resp.headers), key))
            return json.loads(row["body"])
        body = resp.text
        db.ex("INSERT OR REPLACE INTO esi_cache (key, etag, expires, body) VALUES (?, ?, ?, ?)",
              (key, resp.headers.get("etag"), _expires(resp.headers), body))
        return json.loads(body)

    async def get_pages(self, path: str, params=None, token=None, concurrency: int = 6, on_page=None):
        """Fetch every page of a paginated endpoint. If on_page is given, each page's data is passed
        to it (and not accumulated) so huge endpoints like region orders stay memory friendly."""
        params = dict(params or {})
        first = await self.request("GET", path, params={**params, "page": 1}, token=token)
        pages = int(first.headers.get("x-pages", 1))
        out = []

        def handle(data):
            if on_page:
                on_page(data)
            else:
                out.extend(data)

        handle(first.json())
        sem = asyncio.Semaphore(concurrency)

        async def fetch(page):
            async with sem:
                resp = await self.request("GET", path, params={**params, "page": page}, token=token)
                return resp.json()

        tasks = [asyncio.create_task(fetch(p)) for p in range(2, pages + 1)]
        try:
            for coro in asyncio.as_completed(tasks):
                handle(await coro)
        finally:
            for t in tasks:
                t.cancel()
        return out if not on_page else None

    async def post(self, path: str, body, token=None):
        resp = await self.request("POST", path, json_body=body, token=token)
        return resp.json()
