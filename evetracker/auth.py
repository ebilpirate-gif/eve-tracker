"""Web UI password. Stored as a salted scrypt hash in the database (set via the setup wizard),
or taken from APP_PASSWORD in .env, which wins if present."""
import hashlib
import hmac
import secrets

from . import config, db

MIN_LENGTH = 6


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def from_env() -> bool:
    return bool(config.APP_PASSWORD)


def password_set() -> bool:
    return from_env() or bool(db.get_kv("app_password"))


def check_password(password: str) -> bool:
    if from_env():
        return hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode())
    stored = db.get_kv("app_password")
    if not stored:
        return False
    salt_hex, digest = stored.split("$")
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)).hex(), digest)


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    db.set_kv("app_password", f"{salt.hex()}${_hash(password, salt).hex()}")


def session_token() -> str:
    """Value stored in the login cookie. Changing the password changes it, logging out all browsers."""
    source = config.APP_PASSWORD if from_env() else (db.get_kv("app_password") or "")
    return hashlib.sha256((config.SECRET_KEY + source).encode()).hexdigest()[:24]


def validate_new(password: str, confirm: str) -> str | None:
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if password != confirm:
        return "The two passwords don't match."
    return None
