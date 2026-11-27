# 🎄 Świąteczna Pracownia LEGO® — Appointedd Slot Monitor

Automated appointment availability monitor for **Świąteczna Pracownia LEGO®** (`04–06.12.2026`, Park Świętokrzyski, Warsaw):
- Target booking page: https://swiateczna-pracownia.appointedd.com/booking-widget/6af497ce58a51703b9fb8202

## How It Works

Instead of fragile browser scraping, this monitor queries **Appointedd's own GraphQL API** (`https://graphql.services.appointedd.com`) used by the booking widget:

1. **`GetBookingToolData`** resolves the widget (`6af497ce58a51703b9fb8202`), organisation (`6af491d258a51703b9fb80ae`), services, and default duration (`60 min`).
2. **`GetOrganisationAvailability`** queries both `availabilityDates` and `availabilityIntervals` for **December 4–6, 2026** (`Europe/Warsaw`), parsing both **`AvailableInterval`** and **`GroupBookingInterval`** (including `remainingSpaces`).
3. **Self-healing Access-Token**: if Appointedd ever rotates the public widget `Access-Token`, the client automatically extracts the fresh token from `https://booking-tools.appointedd.com/widget/<widget_id>`.
4. **Smart Deduplication (`data/slots_state.json` on orphan `state` branch)**:
   - Sends an instant **Telegram alert** whenever a new slot opens, a previously booked slot reopens, or `remainingSpaces` increases on an existing slot.
   - Uses push-only Telegram Bot API (`sendMessage`), so it never conflicts with other bots sharing the same token.
5. **3-Hour GitHub Actions Loop (Every 1 Minute)**:
   - [.github/workflows/check_slots.yml](.github/workflows/check_slots.yml) runs for **179 minutes** (`timeout-minutes: 185`), polling every **1 minute**, and is designed to be triggered every 3 hours via `workflow_dispatch` from [cron-job.org](https://console.cron-job.org/jobs).

## GitHub Secrets

Configure in **Settings → Secrets and variables → Actions**:
- `TELEGRAM_BOT_TOKEN` — Telegram Bot token from `@BotFather`
- `TELEGRAM_CHAT_ID` — Your numeric Telegram Chat ID (or comma-separated IDs)

Optional Repository Variables:
- `DATE_FROM` (default: `2026-12-04`)
- `DATE_TO` (default: `2026-12-06`)
- `TIMEZONE` (default: `Europe/Warsaw`)
- `MIN_SPACES` (default: `1`)

## Local Usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Run unit tests
pytest

# Run a single live check
python -m monitor.main --once

# Run a 3-hour loop checking every 1 minute
python -m monitor.main --daemon-minutes 179 --interval-minutes 1
```

## Triggering Every 3 Hours from cron-job.org

Create a job on [console.cron-job.org/jobs](https://console.cron-job.org/jobs):
- **URL**: `https://api.github.com/repos/AntonBelski/lego-christmas-workshop-slots-bot/actions/workflows/check_slots.yml/dispatches`
- **Method**: `POST`
- **Schedule**: Every 3 hours (`0 */3 * * *`)
- **Headers**:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <GITHUB_PAT_WITH_ACTIONS_WRITE>`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Request body**:
  ```json
  {"ref": "main"}
  ```
