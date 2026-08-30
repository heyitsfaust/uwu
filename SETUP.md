# How this works, in plain terms

Twice a day (7am and 7pm UTC by default — tell me your timezone and I'll adjust
the times), GitHub opens the real HPL and Maersk public tracking pages in a
headless browser (same pages you use, just without a human watching), types in
each tracking number, and reads the events table. Every run, you get ONE email
listing every tracking number you're still actively watching, each with either
"no change" or a short line on what happened (e.g. "vessel departed from
Shanghai on 2026-08-01").

Once a shipment's tracking shows it's been picked up by rail, that shipment is
automatically retired — it stops being checked and stops appearing in the
email, since you'll be switching to checking the rail site manually for that
one specifically. Other shipments keep going as normal.

This reads the public tracking pages the way a browser does — it doesn't use
either carrier's official API, since that needs a developer account. That
means it's more likely to need occasional fixes if HPL or Maersk redesign
their tracking page, but nothing you'd need to touch yourself — just forward
me what broke.

## One-time setup

1. **Create a private GitHub repo** and upload these files to it.
2. **Add your tracking numbers** to `shipments.json` — replace the two example
   entries, add as many as you want. This is the only file you'll routinely
   edit yourself; it's plain text, no code.
3. **Add secrets** (Settings → Secrets and variables → Actions → New repository
   secret) — these are the credentials the script needs but that should never
   be visible in the code itself:
   - `EMAIL_SENDER` — the Gmail address sending the alerts
   - `EMAIL_APP_PASSWORD` — the app password you generate in that Gmail
     account's security settings (not your normal password)
   - `EMAIL_RECIPIENT` — where the alerts go (can be the same address)
4. **Run it once manually** (Actions tab → "Check shipment status" → "Run
   workflow") once your tracking numbers are in. If a shipment fails to read,
   the run will still finish and attach a screenshot ("debug-screenshots" in
   the run's summary) — download it and send it to me, and I'll adjust the
   script to match what the page actually looks like. This is the one round
   of back-and-forth this approach needs, since it reads the live page rather
   than a clean API.

## Changing the schedule

Edit the two `cron` lines in `.github/workflows/check-shipments.yml`. Cron
times are UTC. You can also trigger a check manually anytime from the
"Actions" tab on GitHub, no waiting for the schedule.

## Adding or removing shipments later

Just edit `shipments.json` directly on GitHub (click the file, click the
pencil icon, edit, commit) — no need to touch anything else.
