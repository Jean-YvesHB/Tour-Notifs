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
from collections import defaultdict
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

API_KEY = os.environ["TOURDASH_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
# Comma-separated list, e.g. "you@example.com, boss@example.com"
NOTIFY_EMAILS = [
    addr.strip()
    for addr in os.environ.get("NOTIFY_EMAIL", GMAIL_ADDRESS).split(",")
    if addr.strip()
]

STATE_FILE = "state.json"
BASE_URL = "https://www.tourdash.app/api/v1/bookings"

# How far ahead to look for upcoming tours each run.
LOOKAHEAD_DAYS = 60

# TourDash gives tour start times with no UTC offset, so we have to know
# which city each tour code runs in to interpret the time correctly.
# Add an entry here whenever you or your boss start covering a new tour.
TOUR_TIMEZONES = {
    "SydWalk1": ZoneInfo("Australia/Sydney"),
    "SydWalk2": ZoneInfo("Australia/Sydney"),
}

# How many hours before a tour starts to send the reminder.
REMINDER_LEAD_HOURS = 2

# How long after a tour's start time to keep it in the "already
# reminded" list, so that list doesn't grow forever.
REMINDER_MEMORY_HOURS = 24


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("reminded_tours", [])
            return state
    # First-ever run: only flag bookings received from now on.
    return {
        "last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reminded_tours": [],
    }


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


def format_start_time(raw):
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%a, %b %d, %Y at %I:%M %p").replace(" 0", " ")
    except ValueError:
        return raw  # fall back to raw value if TourDash ever changes the format


def send_email(new_bookings):
    lines = []
    for b in new_bookings:
        tour = b["tour"]["name"]
        start = format_start_time(b["tour"]["start_time"])
        booked = b["booked"]
        platform = b["platform"]
        lines.append(
            f"- {tour}\n"
            f"  Starts: {start}\n"
            f"  Guests: {booked['adults']} adults, {booked['children']} children, {booked['infants']} infants\n"
            f"  Platform: {platform}"
        )

    body = "New TourDash booking(s):\n\n" + "\n\n".join(lines)
    count = len(new_bookings)
    subject = f"TourDash: {count} new booking" + ("" if count == 1 else "s")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())


def parse_tour_start_utc(tour_name, raw):
    """TourDash gives start_time with no offset; interpret it in that
    tour's own timezone."""
    tz = TOUR_TIMEZONES[tour_name]
    naive = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    local = naive.replace(tzinfo=tz)
    return local.astimezone(timezone.utc)


def group_into_tour_occurrences(bookings):
    """Combine bookings that belong to the same tour session (same name + start time)."""
    groups = defaultdict(list)
    for b in bookings:
        if b["status"] == "cancelled":
            continue
        key = (b["tour"]["name"], b["tour"]["start_time"])
        groups[key].append(b)
    return groups


def send_reminder_email(tour_name, start_time_raw, bookings):
    total_adults = sum(b["booked"]["adults"] for b in bookings)
    total_children = sum(b["booked"]["children"] for b in bookings)
    total_infants = sum(b["booked"]["infants"] for b in bookings)
    platforms = sorted(set(b["platform"] for b in bookings))

    body = (
        f"Upcoming tour reminder:\n\n"
        f"- {tour_name}\n"
        f"  Starts: {format_start_time(start_time_raw)}\n"
        f"  Total guests: {total_adults} adults, {total_children} children, {total_infants} infants\n"
        f"  Bookings: {len(bookings)} ({', '.join(platforms)})"
    )
    subject = f"Reminder: {tour_name} starts soon"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(NOTIFY_EMAILS)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())


def send_reminders(bookings, state, now_utc):
    reminded = set(tuple(item) for item in state["reminded_tours"])
    lead = timedelta(hours=REMINDER_LEAD_HOURS)
    memory_cutoff = now_utc - timedelta(hours=REMINDER_MEMORY_HOURS)

    occurrences = group_into_tour_occurrences(bookings)
    sent_count = 0

    for (tour_name, start_time_raw), group in occurrences.items():
        # Only remind for tours you actually work -- skip anything else
        # the company runs elsewhere.
        if tour_name not in TOUR_TIMEZONES:
            continue

        start_utc = parse_tour_start_utc(tour_name, start_time_raw)
        key = (tour_name, start_time_raw)

        # Drop this tour from memory once it's well in the past.
        if start_utc < memory_cutoff:
            reminded.discard(key)
            continue

        already_reminded = key in reminded
        due = now_utc <= start_utc <= now_utc + lead

        if due and not already_reminded:
            send_reminder_email(tour_name, start_time_raw, group)
            reminded.add(key)
            sent_count += 1

    state["reminded_tours"] = [list(item) for item in reminded]
    return sent_count


def main():
    state = load_state()
    last_checked_dt = datetime.fromisoformat(state["last_checked"].replace("Z", "+00:00"))

    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")

    bookings = fetch_all_bookings(date_from, date_to)
    bookings = [b for b in bookings if b["tour"]["name"] in TOUR_TIMEZONES]

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

    reminder_count = send_reminders(bookings, state, now)
    if reminder_count:
        print(f"Sent {reminder_count} tour reminder(s).")
    else:
        print("No reminders due.")

    state["last_checked"] = max_received.strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)


if __name__ == "__main__":
    main()
