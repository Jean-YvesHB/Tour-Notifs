"""
Polls the TourDash Bookings API and emails you whenever a booking
appears that wasn't there last time this script ran.

Relies on the `received_at` field TourDash stamps on every booking,
so it never needs to remember individual booking IDs -- just the
timestamp of the last booking it already told you about (state.json).
"""

import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ["TOURDASH_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_ADDRESS)

STATE_FILE = "state.json"
BASE_URL = "https://tourdash.app/api/v1/bookings"

# How far ahead to look for upcoming tours each run.
LOOKAHEAD_DAYS = 60


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    # First-ever run: only flag bookings received from now on.
    return {"last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_all_bookings(date_from, date_to):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    bookings = []
    page = 1
    while True:
        resp = requests.get(
            BASE_URL,
            headers=headers,
            params={"from": date_from, "to": date_to, "page": page, "page_size": 200},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        bookings.extend(payload["data"])
        pagination = payload["pagination"]
        if page >= pagination["total_pages"]:
            break
        page += 1
    return bookings


def send_email(new_bookings):
    lines = []
    for b in new_bookings:
        tour = b["tour"]["name"]
        start = b["tour"]["start_time"]
        booked = b["booked"]
        platform = b["platform"]
        lines.append(
            f"- {tour}\n"
            f"  Starts: {start}\n"
            f"  Guests: {booked['adults']} adults, {booked['children']} children, {booked['infants']} infants\n"
            f"  Platform: {platform}"
        )

    body = "New TourDash booking(s):\n\n" + "\n\n".join(lines)
    subject = f"TourDash: {len(new_bookings)} new booking(s)"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_EMAIL

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [NOTIFY_EMAIL], msg.as_string())


def main():
    state = load_state()
    last_checked_dt = datetime.fromisoformat(state["last_checked"].replace("Z", "+00:00"))

    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")

    bookings = fetch_all_bookings(date_from, date_to)

    new_bookings = []
    max_received = last_checked_dt

    for b in bookings:
        received_at = datetime.fromisoformat(b["received_at"].replace("Z", "+00:00"))
        if received_at > last_checked_dt:
            new_bookings.append(b)
        if received_at > max_received:
            max_received = received_at

    if new_bookings:
        new_bookings.sort(key=lambda b: b["received_at"])
        send_email(new_bookings)
        print(f"Sent email for {len(new_bookings)} new booking(s).")
    else:
        print("No new bookings.")

    state["last_checked"] = max_received.strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)


if __name__ == "__main__":
    main()
