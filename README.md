# EVE Tracker

A self-hosted EVE Online asset tracker and trading tool, built to run on a Raspberry Pi.

- **Assets & net worth:** every item across all your characters, grouped by station/structure, with ships and containers resolved and everything valued at hub prices. Hourly net-worth history.
- **Trade finder:** station-trading margins at Jita, Amarr, Dodixie, Rens and Hek, and hauling routes between them (instant-sell or listing), all after taxes and broker fees and filtered by real 30-day volume.
- **Orders & wallet:** your open orders with undercut/outbid detection, recent transactions, and FIFO profit & loss matched across all your characters (buy on one alt, sell on another).
- **Industry:** profit and profit/hour for every manufacturing blueprint and reaction, using your owned blueprints' ME/TE, live system cost indices, the SCC surcharge, facility tax and structure bonuses. Shows your running jobs and which ones are ready.

It's a single Python process (FastAPI + SQLite) with a built-in background scheduler, so no Redis, Postgres or Docker is needed.

## Requirements

- Raspberry Pi 3B+ or newer (a Pi 4/5 is recommended; tracking all five hubs downloads ~1,000 pages of market orders every 30 minutes). 64-bit Raspberry Pi OS (Bookworm) is recommended.
- Python 3.10+ (Bookworm ships 3.11).
- Roughly 300 MB of disk for the database.

## 1. Register an EVE application

1. Go to <https://developers.eveonline.com/applications> and log in.
2. Create a new application with **Authentication & API Access**.
3. **Callback URL:** the address you'll open the app at, plus `/callback`, e.g. `http://raspberrypi.local:8000/callback`. It must match `EVE_CALLBACK_URL` in `.env` exactly.
4. **Scopes:**
   - `esi-assets.read_assets.v1`
   - `esi-wallet.read_character_wallet.v1`
   - `esi-markets.read_character_orders.v1`
   - `esi-markets.structure_markets.v1`
   - `esi-universe.read_structures.v1`
   - `esi-industry.read_character_jobs.v1`
   - `esi-characters.read_blueprints.v1`
   - `esi-skills.read_skills.v1`
5. Copy the **Client ID** and **Secret Key**.

## 2. Install on the Pi

```bash
git clone <this repo> eve-tracker
cd eve-tracker
./deploy/install.sh        # creates .env on the first run
nano .env                  # fill in EVE_CLIENT_ID, EVE_CLIENT_SECRET, EVE_CALLBACK_URL, APP_PASSWORD
./deploy/install.sh        # installs and starts the systemd service
```

Open `http://raspberrypi.local:8000` (or the Pi's IP address) from any device on your network. On first start it downloads the static game data (about a minute) and then the market data (a few minutes on a Pi). Go to **Settings → Add character** and log in with each of your characters.

Useful commands:

```bash
journalctl -u eve-tracker -f      # live logs
sudo systemctl restart eve-tracker
```

To update: `git pull && ./deploy/install.sh`.

### Running without systemd (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set EVE_CALLBACK_URL=http://localhost:8000/callback
.venv/bin/uvicorn evetracker.web:app --port 8000
```

Run the tests with `.venv/bin/pip install pytest && .venv/bin/python -m pytest tests`.

## Security notes

- **Set `APP_PASSWORD`.** Without it, anyone on your network can see your wallets and assets.
- Don't port-forward the app to the internet. For access away from home, use a VPN such as Tailscale or WireGuard.
- Your EVE refresh tokens live in `data/evetracker.db`. The app can only *read* data (every scope is read-only), but keep the file private. Removing a character in Settings deletes its tokens and data, and you can revoke access at any time at <https://community.eveonline.com/support/third-party-applications/>.

## How the numbers are calculated

- **Fees:** sales tax = 7.5% × (1 − 11% × Accounting level), broker fee = 3% − 0.3% × Broker Relations level, using the best skills across your characters. Override both in Settings if you have standings or trade in player structures.
- **Asset value:** best sell (or buy) price at your valuation hub, falling back to CCP's average price. Blueprint copies count as zero.
- **Station trading:** buy via a buy order at the best bid and sell via a sell order at the best ask, both paying broker fees, and the sale pays tax. "Potential/day" assumes you capture 10% of the daily volume.
- **Hauling:** buy from sell orders at the source. "Instant" sells into the destination's buy orders, while "listing" places sell orders there.
- **Industry:** materials = max(runs, ⌈base × runs × (1 − ME) × (1 − structure bonus)⌉). Job cost = EIV × (system cost index + facility tax + 4% SCC). By default it only lists blueprints you can actually buy (BPOs) or already own, and products that trade at least 5 units a day.
- **P&L:** FIFO across all your characters. Sales with no recorded purchase (loot, manufactured goods, stock bought before you added the character) are shown as "unmatched" and left out of profit. ESI only exposes about the last 30 days of transactions, so the history builds up from the day you install the app.

## Limitations / ideas for later

- Corporation assets, wallets and orders (needs director-level corp scopes).
- Invention / T2 manufacturing chains and building components yourself.
- Player-structure markets (e.g. Perimeter Keepstar) as trade hubs.
- Discord/push notifications for undercuts and finished jobs.
