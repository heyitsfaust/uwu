"""
Shipment status watcher for HPL and MSK — reads their PUBLIC tracking pages
(no login) the same way you do in a browser, since neither offers a public
API without registering for a developer account.

What it does each time it runs:
  1. Reads shipments.json (your list of tracking numbers, in the order
     you want them reported)
  2. For each ACTIVE shipment (see "retirement" below), opens the tracking
     page and searches the tracking number:
       - HPL: reads the events table (Status, Place, Date, Time, Transport,
         Voyage) including whether each row is completed (dark) or still
         planned (grey)
       - MSK: reads the "Latest event" line near the top of the page (e.g.
         "Rail departure • PRINCE RUPERT, CANADA • 24 Aug 2026") — this
         already tells us the most recent CONFIRMED event directly, so
         there's no need to detect line colors on MSK's timeline. The
         estimated arrival date is deliberately ignored since it's known to
         be unreliable — only a change in the confirmed event triggers
         anything.
  3. Compares that to what it saw last run and produces one line per
     shipment: either "no change." or a short phrase of what happened
  4. Sends ONE email every run, one line per tracking number, in the same
     order as shipments.json. Lines with an actual change are bolded so you
     can skip past the "no change" ones at a glance.

Retirement: once a shipment shows a CONFIRMED rail departure event, it's
marked retired — no longer checked, no longer in the email — since you're
switching to checking the rail site manually for that one. Because retired
shipments are just skipped, the remaining ones naturally shift up in the
email, same order otherwise.

If a shipment fails to read, the script saves a screenshot (debug_<carrier>.png)
as a workflow artifact so you can send it over — no debugging needed on your end.
"""

import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

SHIPMENTS_FILE = Path("shipments.json")
STATE_FILE = Path("state.json")

HPL_URL = "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html"
MSK_URL = "https://www.maersk.com/tracking/"

SEARCH_INPUT_CANDIDATES = [
    "input[type='text']",
    "input[type='search']",
    "input[placeholder*='track' i]",
    "input[placeholder*='container' i]",
    "input[placeholder*='booking' i]",
    "input[placeholder*='reference' i]",
]

EVENT_COLUMNS = ["status", "place", "date", "time", "transport", "voyage"]

# How to phrase specific statuses. {place} and {date} get filled in.
# Falls back to a generic "<status> — <place> — <date>" for anything not listed.
STATUS_PHRASES = {
    "vessel departed": "vessel departed from {place} on {date}",
    "vessel arrived": "vessel arrived in {place} on {date}",
    "discharged": "discharged on {date}",
    "loaded": "loaded in {place} on {date}",
    "rail departure": "departed by rail from {place} on {date}",
    "rail arrival": "arrived by rail in {place} on {date}",
}

# Matches MSK's "Latest event" line when it renders as one text node, e.g.:
# "Rail departure • PRINCE RUPERT, CANADA • 24 Aug 2026"
MSK_LATEST_EVENT_PATTERN = re.compile(
    r"^(?P<event>[A-Za-z][A-Za-z /()\-]*?)\s*[•·]\s*(?P<place>[A-Z][A-Z ,.\-]*?)\s*[•·]\s*(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})$"
)
MSK_DATE_PATTERN = re.compile(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}")


def load_shipments():
    data = json.loads(SHIPMENTS_FILE.read_text())
    return [s for s in data["shipments"] if s["tracking_number"] != "REPLACE_ME"]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fill_and_search(page, tracking_number: str) -> bool:
    for selector in SEARCH_INPUT_CANDIDATES:
        try:
            field = page.locator(selector).first
            if field.count() > 0:
                field.click()
                field.fill(tracking_number)
                field.press("Enter")
                return True
        except Exception:
            continue
    return False


def dismiss_cookie_banner(page):
    for label in ["Accept all", "Accept All", "Accept", "I agree", "Got it"]:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                return
        except Exception:
            pass


def extract_events_table(page) -> list[dict]:
    tables = page.locator("table")
    table_count = tables.count()
    target = None
    for i in range(table_count):
        t = tables.nth(i)
        try:
            header_text = t.inner_text()
        except Exception:
            continue
        if "Status" in header_text and "Place of Activity" in header_text:
            target = t
            break

    if target is None:
        raise RuntimeError("couldn't find the events table on the results page")

    rows = target.locator("tbody tr")
    row_count = rows.count()
    if row_count == 0:
        rows = target.locator("tr")
        row_count = rows.count()

    events = []
    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator("td")
        cell_count = cells.count()
        if cell_count == 0:
            continue
        texts = [cells.nth(j).inner_text().strip() for j in range(cell_count)]
        while len(texts) < len(EVENT_COLUMNS):
            texts.append("")
        event = dict(zip(EVENT_COLUMNS, texts[: len(EVENT_COLUMNS)]))
        try:
            style = row.evaluate(
                "el => { const s = getComputedStyle(el); return s.fontWeight + '|' + s.color; }"
            )
        except Exception:
            style = ""
        event["style"] = style
        events.append(event)

    return events


def event_key(e: dict):
    return tuple(e.get(col, "") for col in EVENT_COLUMNS)


def phrase_event(e: dict) -> str:
    status_lower = e["status"].strip().lower()
    template = STATUS_PHRASES.get(status_lower)
    if template:
        return template.format(place=e["place"], date=e["date"])
    # generic fallback for any status not in the lookup above
    parts = [p for p in (e["status"], e["place"], e["date"]) if p]
    return " — ".join(parts)


def is_rail_departure(e: dict) -> bool:
    return "departure" in e["status"].lower() and e.get("transport", "").strip().lower() == "rail"


def diff_events(previous: list[dict], current: list[dict]):
    """Returns (list of phrased new/confirmed events, bool: did a rail departure just get confirmed)."""
    previous_by_key = {event_key(e): e for e in previous}
    phrases = []
    rail_departure_confirmed = False
    for e in current:
        key = event_key(e)
        is_new = key not in previous_by_key
        is_newly_confirmed = (not is_new) and previous_by_key[key].get("style") != e.get("style")
        if is_new or is_newly_confirmed:
            phrases.append(phrase_event(e))
            if is_rail_departure(e):
                rail_departure_confirmed = True
    return phrases, rail_departure_confirmed


def fetch_via_browser(page, url: str, tracking_number: str, carrier: str) -> list[dict]:
    page.goto(url, wait_until="networkidle", timeout=30000)
    dismiss_cookie_banner(page)

    found = fill_and_search(page, tracking_number)
    if not found:
        page.screenshot(path=f"debug_{carrier}.png")
        raise RuntimeError(
            f"Could not find a search field on {url} — saved debug_{carrier}.png, send it over"
        )

    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    try:
        return extract_events_table(page)
    except Exception as e:
        page.screenshot(path=f"debug_{carrier}.png")
        raise RuntimeError(f"{e} — saved debug_{carrier}.png, send it over")


def fetch_hpl_status(page, tracking_number: str) -> list[dict]:
    return fetch_via_browser(page, HPL_URL, tracking_number, "hpl")


def extract_msk_latest_event(page) -> dict | None:
    """
    MSK's page shows a 'Latest event' line near the top, e.g.:
    "Rail departure • PRINCE RUPERT, CANADA • 24 Aug 2026"
    That single line already tells us the most recent CONFIRMED event (as
    opposed to the still-planned ones further down the timeline), so we
    don't need to detect the grey-vs-blue connector line color at all —
    we just watch this one line for change.
    """
    text = page.inner_text("body")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Primary attempt: the whole thing rendered as one bullet-separated line.
    for line in lines:
        m = MSK_LATEST_EVENT_PATTERN.match(line)
        if m:
            return {
                "event": m.group("event").strip(),
                "place": m.group("place").strip(),
                "date": m.group("date").strip(),
            }

    # Fallback: label and value may be split across separate lines/columns.
    if "Latest event" in lines:
        idx = lines.index("Latest event")
        window = lines[idx + 1 : idx + 6]
        date_line = next((l for l in window if MSK_DATE_PATTERN.search(l)), None)
        if date_line:
            date_match = MSK_DATE_PATTERN.search(date_line).group()
            # split on bullet if present, else use the whole line minus the date
            before_date = date_line.split(date_match)[0].strip(" •·")
            parts = [p.strip() for p in re.split(r"[•·]", before_date) if p.strip()]
            event = parts[0] if parts else ""
            place = parts[1] if len(parts) > 1 else ""
            if event:
                return {"event": event, "place": place, "date": date_match}

    return None


def fetch_msk_status(page, tracking_number: str) -> dict | None:
    page.goto(MSK_URL, wait_until="networkidle", timeout=30000)
    dismiss_cookie_banner(page)

    found = fill_and_search(page, tracking_number)
    if not found:
        page.screenshot(path="debug_msk.png")
        raise RuntimeError(
            f"Could not find a search field on {MSK_URL} — saved debug_msk.png, send it over"
        )

    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    result = extract_msk_latest_event(page)
    if result is None:
        page.screenshot(path="debug_msk.png")
        raise RuntimeError(
            f"Couldn't find the 'Latest event' line for {tracking_number} — "
            f"saved debug_msk.png, send it over"
        )
    return result


def send_email(subject: str, report_lines: list[tuple[str, bool]], recipient: str):
    """report_lines is a list of (text, changed) — changed=True gets bolded."""
    sender = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_APP_PASSWORD"]

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html_lines = []
    for text, changed in report_lines:
        safe = esc(text)
        html_lines.append(f"<b>{safe}</b>" if changed else safe)
    html_body = "<br>".join(html_lines)

    msg = MIMEText(html_body, "html")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


def process_hpl(previous_state: dict, current_events: list[dict]):
    phrases, rail_departure_confirmed = diff_events(previous_state.get("events", []), current_events)
    changed = bool(phrases)
    text = "; ".join(phrases) if phrases else "no change."
    new_state = {"events": current_events}
    return changed, text, rail_departure_confirmed, new_state


def process_msk(previous_state: dict, current_latest: dict | None):
    previous_latest = previous_state.get("latest_event")
    changed = current_latest != previous_latest
    rail_departure_confirmed = False
    if changed and current_latest:
        text = phrase_event(
            {"status": current_latest["event"], "place": current_latest["place"], "date": current_latest["date"]}
        )
        event_lower = current_latest["event"].lower()
        rail_departure_confirmed = "rail" in event_lower and "departure" in event_lower
    else:
        text = "no change."
    new_state = {"latest_event": current_latest}
    return changed, text, rail_departure_confirmed, new_state


def main():
    shipments = load_shipments()
    state = load_state()
    # Grouped by recipient tag (e.g. "mma" / "ba") so each gets their own email.
    report_by_group: dict[str, list[tuple[str, bool]]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for shipment in shipments:
            carrier = shipment["carrier"].upper()
            key = f"{carrier}:{shipment['tracking_number']}"
            label = shipment.get("label") or shipment["tracking_number"]
            group = (shipment.get("recipient") or "").strip().lower()
            previous_state = state.get(key, {})

            if not group:
                # No recipient tag — report it, but under its own group so it's
                # obviously not silently dropped rather than guessing where it goes.
                group = "unassigned"

            report_lines = report_by_group.setdefault(group, [])

            if previous_state.get("retired"):
                continue  # on rail now — skip entirely, per your request

            try:
                if carrier == "HPL":
                    current = fetch_hpl_status(page, shipment["tracking_number"])
                    changed, text, rail_departure_confirmed, new_state = process_hpl(previous_state, current)
                elif carrier == "MSK":
                    current = fetch_msk_status(page, shipment["tracking_number"])
                    changed, text, rail_departure_confirmed, new_state = process_msk(previous_state, current)
                else:
                    report_lines.append((f"{label} - unknown carrier '{carrier}'", True))
                    continue
            except Exception as e:
                report_lines.append((f"{label} - couldn't check ({e})", True))
                continue

            if rail_departure_confirmed:
                report_lines.append(
                    (f"{label} - {text} Now on rail — switching to manual tracking, "
                     f"no further updates for this shipment.", True)
                )
                new_state["retired"] = True
            else:
                report_lines.append((f"{label} - {text}", changed))
                new_state["retired"] = False

            state[key] = new_state

        browser.close()

    for group, lines in report_by_group.items():
        if not lines:
            continue
        recipient = os.environ["EMAIL_RECIPIENT"]
        subject = f"Shipment status check — {group.upper()}"
        send_email(subject, lines, recipient)

    save_state(state)


if __name__ == "__main__":
    main()

