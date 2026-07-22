"""
Telegram inline-button -> application status updater
------------------------------------------------------
Run by the "Telegram Status Update" GitHub Actions workflow whenever you
tap a status button (✅ Mark Applied / 🟠 Interview / 🟢 Offer / ❌ Rejected)
under a job in your Telegram digest.

Updates data/applied_jobs.json (the full application history) and the
matching row's application_status column in data/powerbi_export.csv (so
Power BI's funnel visual has a single source of truth), then edits the
original Telegram message's buttons to show the next logical stage.

Required environment variables (passed as workflow_dispatch inputs from
the Cloudflare Worker, see README.md):
    JOB_ID              - the job's ID, from the button's callback_data
    ACTION              - one of: applied, interview, offer, rejected
    TELEGRAM_CHAT_ID    - chat to edit the message in
    TELEGRAM_MESSAGE_ID - which message's buttons to update
    TELEGRAM_BOT_TOKEN  - your bot token (GitHub Secret)
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent.parent / "data"
APPLIED_JOBS_FILE = DATA_DIR / "applied_jobs.json"
EXPORT_FILE = DATA_DIR / "powerbi_export.csv"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

VALID_ACTIONS = {"applied", "interview", "offer", "rejected"}

# Buttons shown after each stage - only the logical next steps. A terminal
# stage (offer/rejected) removes the buttons entirely rather than looping
# back, since there's nowhere further for a job to progress from there.
NEXT_STAGE_KEYBOARDS = {
    "applied": {
        "inline_keyboard": [[
            {"text": "🟠 Interview", "callback_data": "interview:{job_id}"},
            {"text": "❌ Rejected", "callback_data": "rejected:{job_id}"},
        ]]
    },
    "interview": {
        "inline_keyboard": [[
            {"text": "🟢 Offer", "callback_data": "offer:{job_id}"},
            {"text": "❌ Rejected", "callback_data": "rejected:{job_id}"},
        ]]
    },
    "offer": {"inline_keyboard": []},
    "rejected": {"inline_keyboard": []},
}


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def find_job_in_export(job_id: str) -> dict:
    """Looks up a job's title/company from powerbi_export.csv, so a newly
    tracked job doesn't need you to type that info in by hand."""
    if not EXPORT_FILE.exists():
        return {}
    with open(EXPORT_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("job_id") == job_id:
                return {"title": row.get("title", ""), "company": row.get("company", "")}
    return {}


def update_applied_jobs(job_id: str, action: str) -> dict:
    applied_jobs = load_json(APPLIED_JOBS_FILE, {})
    entry = applied_jobs.get(job_id)
    if entry is None:
        job_info = find_job_in_export(job_id)
        entry = {
            "title": job_info.get("title", "Unknown title"),
            "company": job_info.get("company", "Unknown company"),
            "status": None,
            "date_applied": None,
            "date_interview": None,
            "date_offer": None,
            "date_rejected": None,
        }

    entry["status"] = action
    entry[f"date_{action}"] = datetime.now(timezone.utc).date().isoformat()
    applied_jobs[job_id] = entry
    save_json(APPLIED_JOBS_FILE, applied_jobs)
    return entry


def update_export_status(job_id: str, action: str):
    """Rewrites powerbi_export.csv with the matching row's
    application_status updated. CSVs don't support editing a single row in
    place, so this reads every row, patches the one that matches, and
    writes the whole file back."""
    if not EXPORT_FILE.exists():
        print(f"WARNING: {EXPORT_FILE.name} not found - skipping CSV status update")
        return

    with open(EXPORT_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    updated = False
    for row in rows:
        if row.get("job_id") == job_id:
            row["application_status"] = action
            updated = True
            break

    if not updated:
        print(f"WARNING: job_id {job_id} not found in {EXPORT_FILE.name} - status only saved to applied_jobs.json")
        return

    with open(EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def update_telegram_buttons(job_id: str, action: str, chat_id: str, message_id: str):
    """Swaps the tapped message's buttons to the next logical stage (or
    removes them entirely for a terminal stage like offer/rejected)."""
    keyboard_template = NEXT_STAGE_KEYBOARDS.get(action, {"inline_keyboard": []})
    keyboard = json.loads(json.dumps(keyboard_template).replace("{job_id}", job_id))

    url = f"{TELEGRAM_API}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(keyboard),
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"WARNING: Failed to update Telegram buttons ({resp.status_code}): {resp.text}")


def main():
    job_id = os.environ.get("JOB_ID", "").strip()
    action = os.environ.get("ACTION", "").strip().lower()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    message_id = os.environ.get("TELEGRAM_MESSAGE_ID", "").strip()

    if not job_id or action not in VALID_ACTIONS:
        print(f"ERROR: invalid job_id ({job_id!r}) or action ({action!r})")
        sys.exit(1)

    entry = update_applied_jobs(job_id, action)
    update_export_status(job_id, action)

    if chat_id and message_id:
        update_telegram_buttons(job_id, action, chat_id, message_id)

    print(f"Updated job {job_id} ({entry['title']} @ {entry['company']}) -> {action}")


if __name__ == "__main__":
    main()
