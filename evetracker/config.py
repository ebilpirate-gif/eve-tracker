"""Configuration loaded from environment variables and an optional .env file."""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "evetracker.db"

EVE_CLIENT_ID = os.environ.get("EVE_CLIENT_ID", "")
EVE_CLIENT_SECRET = os.environ.get("EVE_CLIENT_SECRET", "")
EVE_CALLBACK_URL = os.environ.get("EVE_CALLBACK_URL", "http://localhost:8000/callback")

# Optional password protecting the web UI (strongly recommended: wallets are visible).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Contact info sent in the User-Agent, as CCP asks third-party apps to do.
ESI_CONTACT = os.environ.get("ESI_CONTACT", "")

# Disable the background worker (useful for tests).
DISABLE_WORKER = os.environ.get("DISABLE_WORKER", "") == "1"


def _secret_key() -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    key_file = DATA_DIR / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48))
        key_file.chmod(0o600)
    return key_file.read_text().strip()


SECRET_KEY = _secret_key()

SSO_CONFIGURED = bool(EVE_CLIENT_ID and EVE_CLIENT_SECRET)
