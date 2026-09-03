import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sys
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.sync_api import sync_playwright
import requests

PORTAL_URL = os.environ["AURIGA_PORTAL_URL"].strip().strip('"').strip("'")
SCHEDULE_API_URL = os.environ["AURIGA_API_URL"].strip().strip('"').strip("'")
AUTH_STATE_B64 = os.environ["AURIGA_AUTH_STATE"].strip()
CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"].strip()
SA_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])


def get_token_from_session() -> str:
    """Restores session state in headless browser and intercepts the Keycloak Bearer token."""
    auth_json_path = "auth.json"
    with open(auth_json_path, "wb") as f:
        f.write(base64.b64decode(AUTH_STATE_B64))

    captured_token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=auth_json_path)
        page = context.new_page()

        def handle_response(response):
            nonlocal captured_token
            if "/protocol/openid-connect/token" in response.url and response.status == 200:
                try:
                    data = response.json()
                    if "access_token" in data:
                        captured_token = data["access_token"]
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"Navigation warning: {e}", file=sys.stderr)

        browser.close()

    if os.path.exists(auth_json_path):
        os.remove(auth_json_path)

    if not captured_token:
        raise RuntimeError(
            "Failed to retrieve access token. Your session has likely expired. "
            "Please re-run `python setup_auth.py` locally and update the AURIGA_AUTH_STATE secret."
        )

    return captured_token


def fetch_interventions(token: str, start_date: datetime, end_date: datetime) -> list:
    """Queries the timetable API for the given date window."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    params = [
        ("days", "1"),
        ("days", "2"),
        ("days", "3"),
        ("days", "4"),
        ("days", "5"),
        ("days", "6"),
        ("days", "7"),
        ("startDate", start_date.strftime("%Y-%m-%d")),
        ("endDate", end_date.strftime("%Y-%m-%d")),
    ]

    resp = requests.get(SCHEDULE_API_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("interventions", [])


def parse_event(item: dict) -> dict:
    """Maps Auriga REST JSON schema to a Google Calendar event payload."""
    pedagogy = item.get("interventionPedagogicalUnits", [])
    title = "Cours"
    if pedagogy:
        pu_caption = pedagogy[0].get("pedagogicalUnit", {}).get("caption", {})
        title = pu_caption.get("fr") or pu_caption.get("en") or title

    rooms = [
        res.get("resource", {}).get("caption", {}).get("fr", "")
        for res in item.get("interventionResources", [])
        if res.get("resource", {}).get("isRoom")
    ]
    location = ", ".join(filter(None, rooms))

    teachers = [
        f"{i.get('person', {}).get('currentFirstName', '')} {i.get('person', {}).get('currentLastName', '')}".strip()
        for i in item.get("interventionInstructors", [])
    ]

    groups = [
        p.get("population", {}).get("caption", {}).get("fr", "")
        for p in item.get("interventionPopulations", [])
    ]

    activity = item.get("activityType", {}).get("caption", {}).get("fr", "")
    status = item.get("interventionStatus", {}).get("caption", {}).get("fr", "Planifié")

    desc_lines = [
        f"Matière: {title}",
        f"Type: {activity}" if activity else "",
        f"Salle: {location}" if location else "",
        f"Enseignant(s): {', '.join(teachers)}" if teachers else "",
        f"Groupe: {', '.join(groups)}" if groups else "",
        f"Statut: {status}",
    ]
    description = "\n".join(filter(None, desc_lines))

    event_id = hashlib.sha256(f"auriga_{item['id']}".encode()).hexdigest()[:32]

    return {
        "id": event_id,
        "summary": f"{title} ({location})" if location else title,
        "location": location,
        "description": description,
        "start": {"dateTime": item["startDateTime"]},
        "end": {"dateTime": item["endDateTime"]},
    }


def sync_to_google(events: list):
    """Inserts or updates events in the specified Google Calendar with backoff."""
    creds = service_account.Credentials.from_service_account_info(
        SA_INFO, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    service = build("calendar", "v3", credentials=creds)

    synced_count = 0
    for event in events:
        eid = event["id"]
        max_retries = 5

        for attempt in range(max_retries):
            try:
                service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                synced_count += 1
                break
            except HttpError as e:
                if e.resp.status == 409:
                    try:
                        service.events().patch(
                            calendarId=CALENDAR_ID, eventId=eid, body=event
                        ).execute()
                        synced_count += 1
                    except Exception as patch_err:
                        print(f"Error patching event {eid}: {patch_err}", file=sys.stderr)
                    break
                elif e.resp.status in [500, 502, 503, 429]:
                    wait_time = (2**attempt) * 0.5
                    time.sleep(wait_time)
                    if attempt == max_retries - 1:
                        print(
                            f"Failed event {eid} after {max_retries} attempts: {e}",
                            file=sys.stderr,
                        )
                else:
                    print(f"Error syncing event {eid}: {e}", file=sys.stderr)
                    break
            except Exception as e:
                print(f"Unexpected error on event {eid}: {e}", file=sys.stderr)
                break

        time.sleep(0.2)

    print(f"Successfully processed {synced_count}/{len(events)} events.")


if __name__ == "__main__":
  print("Retrieving access token...")
  token = get_token_from_session()

  now = datetime.now(timezone.utc)

  # Fetch schedule across a 16-week window (~4 months) in 4-week chunks
  total_weeks = 16
  chunk_days = 28
  all_interventions = []
  seen_ids = set()

  for chunk_offset in range(0, total_weeks * 7, chunk_days):
    start = (now - timedelta(days=1)) + timedelta(days=chunk_offset)
    end = start + timedelta(days=chunk_days)

    print(f"Fetching schedule from {start.date()} to {end.date()}...")
    try:
      chunk = fetch_interventions(token, start, end)
      for item in chunk:
        if item["id"] not in seen_ids:
          seen_ids.add(item["id"])
          all_interventions.append(item)
    except Exception as e:
      print(f"Failed to fetch chunk {start.date()} to {end.date()}: {e}")

  print(
      f"Found a total of {len(all_interventions)} unique classes across"
      f" {total_weeks} weeks."
  )

  parsed = [parse_event(item) for item in all_interventions]
  sync_to_google(parsed)
