"""EVE Online SSO (OAuth2 v2). Handles login, token refresh and refresh-token rotation."""
import asyncio
import base64
import json
import time
from urllib.parse import urlencode

import httpx

from . import config, db

AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

SCOPES = [
    "esi-assets.read_assets.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-markets.read_character_orders.v1",
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-characters.read_blueprints.v1",
    "esi-skills.read_skills.v1",
]


class NeedsReauth(Exception):
    pass


_locks: dict[int, asyncio.Lock] = {}


def authorize_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "redirect_uri": config.EVE_CALLBACK_URL,
        "client_id": config.EVE_CLIENT_ID,
        "scope": " ".join(SCOPES),
        "state": state,
    })


def _basic_auth() -> str:
    raw = f"{config.EVE_CLIENT_ID}:{config.EVE_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data=data, headers={
            "Authorization": _basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "login.eveonline.com",
        })
    if resp.status_code >= 400:
        raise NeedsReauth(f"SSO token request failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def decode_jwt(token: str) -> dict:
    """Decode the JWT payload. The token comes straight from CCP's token endpoint over TLS,
    so we only need its claims (character id, name, scopes, expiry)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if claims.get("iss") not in ("login.eveonline.com", "https://login.eveonline.com"):
        raise ValueError("unexpected token issuer")
    return claims


def _store_tokens(tokens: dict) -> dict:
    claims = decode_jwt(tokens["access_token"])
    char_id = int(claims["sub"].split(":")[-1])
    scopes = claims.get("scp", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    db.ex(
        """INSERT INTO characters (character_id, name, refresh_token, access_token, access_expires, scopes, added_at, needs_reauth)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(character_id) DO UPDATE SET name=excluded.name, refresh_token=excluded.refresh_token,
             access_token=excluded.access_token, access_expires=excluded.access_expires,
             scopes=excluded.scopes, needs_reauth=0, sync_error=NULL""",
        (char_id, claims.get("name"), tokens["refresh_token"], tokens["access_token"],
         time.time() + int(tokens.get("expires_in", 1199)), " ".join(scopes), time.time()),
    )
    return {"character_id": char_id, "name": claims.get("name")}


async def exchange_code(code: str) -> dict:
    tokens = await _token_request({"grant_type": "authorization_code", "code": code})
    return _store_tokens(tokens)


async def get_access_token(character_id: int) -> str:
    lock = _locks.setdefault(character_id, asyncio.Lock())
    async with lock:
        row = db.q1("SELECT * FROM characters WHERE character_id = ?", (character_id,))
        if row is None:
            raise NeedsReauth("unknown character")
        if row["access_token"] and (row["access_expires"] or 0) > time.time() + 60:
            return row["access_token"]
        try:
            tokens = await _token_request({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
        except NeedsReauth as e:
            db.ex("UPDATE characters SET needs_reauth = 1, sync_error = ? WHERE character_id = ?",
                  (str(e), character_id))
            raise
        _store_tokens(tokens)  # CCP rotates refresh tokens; always persist the new one.
        return tokens["access_token"]


def has_scope(character_id: int, scope: str) -> bool:
    scopes = db.val("SELECT scopes FROM characters WHERE character_id = ?", (character_id,), "")
    return scope in scopes.split()
