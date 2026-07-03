"""
Business Analyst / Data Analyst Job Alert Bot
------------------------------------------------
Fetches new job postings from the Adzuna API across multiple search terms
(e.g. "business analyst", "data analyst") and sends any jobs it hasn't seen
before to a Telegram chat as a digest. Each job includes a "Mark Applied"
button that logs it to a simple applied-jobs tracker.

Designed to run once a day via GitHub Actions (see .github/workflows/daily-job-check.yml)
but works fine run manually or via a local cron job too.

Required environment variables (set as GitHub Secrets, see README.md):
    ADZUNA_APP_ID     - your Adzuna API app ID
    ADZUNA_APP_KEY    - your Adzuna API app key
    TELEGRAM_BOT_TOKEN - your Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID   - your personal chat ID (see README.md for how to get this)

Optional environment variables:
    SEARCH_QUERIES    - comma-separated search terms (default: "business analyst,data analyst")
    SEARCH_COUNTRY    - Adzuna country code (default: "sg" for Singapore)
    RESULTS_PER_PAGE  - how many jobs to pull per search term per run (default: 20)
    MAX_JOB_AGE_DAYS  - only consider jobs posted in the last N days (default: 3)
"""

import html
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

import requests

# ---------- Configuration ----------
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SEARCH_QUERIES = [
    q.strip() for q in os.environ.get("SEARCH_QUERIES", "business analyst,data analyst").split(",") if q.strip()
]
SEARCH_COUNTRY = os.environ.get("SEARCH_COUNTRY", "sg")
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "20"))
MAX_JOB_AGE_DAYS = int(os.environ.get("MAX_JOB_AGE_DAYS", "3"))

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_JOBS_FILE = DATA_DIR / "seen_jobs.json"
APPLIED_JOBS_FILE = DATA_DIR / "applied_jobs.json"
TELEGRAM_OFFSET_FILE = DATA_DIR / "telegram_offset.json"

ADZUNA_URL = f"https://api.adzuna.com/v1/api/jobs/{SEARCH_COUNTRY}/search/1"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

QUERY_TAGS = {
    "business analyst": "📊 Business Analyst",
    "data analyst": "📈 Data Analyst",
}


def require_env_vars():
    missing = [
        name
        for name, val in [
            ("ADZUNA_APP_ID", ADZUNA_APP_ID),
            ("ADZUNA_APP_KEY", ADZUNA_APP_KEY),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        print(f"ERROR: missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def load_seen_jobs() -> set:
    return set(load_json(SEEN_JOBS_FILE, []))


def save_seen_jobs(seen_ids: set):
    save_json(SEEN_JOBS_FILE, sorted(seen_ids)[-1000:])


def load_applied_jobs() -> dict:
    return load_json(APPLIED_JOBS_FILE, {})


def save_applied_jobs(applied: dict):
    save_json(APPLIED_JOBS_FILE, applied)


def load_telegram_offset() -> int:
    return load_json(TELEGRAM_OFFSET_FILE, {}).get("offset", 0)


def save_telegram_offset(offset: int):
    save_json(TELEGRAM_OFFSET_FILE, {"offset": offset})


def fetch_jobs_for_query(query: str) -> list:
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "results_per_page": RESULTS_PER_PAGE,
        "sort_by": "date",
        "max_days_old": MAX_JOB_AGE_DAYS,
        "content-type": "application/json",
    }
    resp = requests.get(ADZUNA_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def fetch_all_jobs() -> list:
    jobs_by_id = {}
    for query in SEARCH_QUERIES:
        for job in fetch_jobs_for_query(query):
            job_id = str(job.get("id"))
            if job_id in jobs_by_id:
                jobs_by_id[job_id]["_matched_queries"].append(query)
            else:
                job["_matched_queries"] = [query]
                jobs_by_id[job_id] = job
    return list(jobs_by_id.values())


def format_job_message(job: dict) -> str:
    raw_title = job.get("title", "Untitled role").strip()
    raw_company = job.get("company", {}).get("display_name", "Unknown company")
    title = html.escape(raw_title)
    company = html.escape(raw_company)
    location = html.escape(job.get("location", {}).get("display_name", "Location not specified"))
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    url = job.get("redirect_url", "")
    created = job.get("created", "")[:10]

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n💰 {salary_min:,.0f} - {salary_max:,.0f}"

    tags = " ".join(QUERY_TAGS.get(q, q) for q in job.get("_matched_queries", []))
    tag_line = f"{tags}\n" if tags else ""

    search_query = quote_plus(f"{raw_title} {raw_company} Singapore")
    google_search_url = f"https://www.google.com/search?q={search_query}"

    return (
        f"{tag_line}"
        f"📋 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}{salary_line}\n"
        f"🗓 Posted: {created}\n"
        f'🔗 <a href="{url}">Apply here (Adzuna)</a>\n'
        f'🔎 <a href="{google_search_url}">Search on Google</a>'
    )


def build_digest_chunks(new_jobs: list, max_length: int = 3200):
    separator = "\n\n➖➖➖➖➖➖➖➖\n\n"
    chunks = []
    current_text = ""
    current_jobs = []

    for job in new_jobs:
        entry = format_job_message(job)
        addition = (separator if current_jobs else "") + entry
        if current_jobs and len(current_text) + len(addition) > max_length:
            chunks.append((current_text, _build_keyboard(current_jobs), current_jobs))
            current_text = entry
            current_jobs = [job]
        else:
            current_text += addition
            current_jobs.append(job)

    if current_jobs:
        chunks.append((current_text, _build_keyboard(current_jobs), current_jobs))

    return chunks


def _build_keyboard(jobs_in_chunk: list) -> dict:
    rows = []
    for job in jobs_in_chunk:
        title = job.get("title", "role").strip()
        short_title = (title[:28] + "…") if len(title) > 28 else title
        rows.append([{
            "text": f"✅ Mark Applied: {short_title}",
            "callback_data": f"applied:{job.get('id')}",
        }])
    return {"inline_keyboard": rows}


def send_telegram_message(text: str, reply_markup: dict = None, disable_preview: bool = True) -> bool:
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"WARNING: Telegram send failed ({resp.status_code}): {resp.text}")
        return False
    return True


def poll_telegram_updates(offset: int) -> tuple:
    url = f"{TELEGRAM_API}/getUpdates"
    params = {"offset": offset, "timeout": 0}
    resp = requests.get(url, params=params, timeout=15)
    if not resp.ok:
        print(f"WARNING: Failed to poll Telegram updates ({resp.status_code}): {resp.text}")
        return [], offset

    updates = resp.json().get("result", [])
    new_offset = offset
    for update in updates:
        new_offset = max(new_offset, update.get("update_id", 0) + 1)
    return updates, new_offset


def answer_callback_query(callback_query_id: str, text: str):
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": False}
    requests.post(url, data=payload, timeout=15)


def process_applied_button_taps(applied: dict) -> dict:
    offset = load_telegram_offset()
    updates, new_offset = poll_telegram_updates(offset)

    tap_count = 0
    for update in updates:
        callback = update.get("callback_query")
        if not callback:
            continue
        data = callback.get("data", "")
        if not data.startswith("applied:"):
            continue

        job_id = data.split(":", 1)[1]
        if job_id not in applied:
            applied[job_id] = {
                "applied_at": callback.get("message", {}).get("date"),
            }
            tap_count += 1
            answer_callback_query(callback["id"], "✅ Marked as applied!")
        else:
            answer_callback_query(callback["id"], "Already marked as applied.")

    if tap_count:
        print(f"Recorded {tap_count} new 'applied' button tap(s)")

    save_telegram_offset(new_offset)
    return applied


def main():
    require_env_vars()

    applied = load_applied_jobs()
    applied = process_applied_button_taps(applied)
    save_applied_jobs(applied)

    seen_ids = load_seen_jobs()
    jobs = fetch_all_jobs()
    print(f"Fetched {len(jobs)} unique jobs across queries: {', '.join(SEARCH_QUERIES)}")

    new_jobs = [j for j in jobs if str(j.get("id")) not in seen_ids]
    print(f"{len(new_jobs)} of these are new")

    if not new_jobs:
        return

    chunks = build_digest_chunks(new_jobs)
    header = f"🔔 {len(new_jobs)} new job(s) found today:\n\n"

    sent_count = 0
    for i, (chunk_text, keyboard, jobs_in_chunk) in enumerate(chunks):
        text = (header if i == 0 and len(new_jobs) > 1 else "") + chunk_text
        if send_telegram_message(text, reply_markup=keyboard):
            for job in jobs_in_chunk:
                seen_ids.add(str(job.get("id")))
                sent_count += 1

    print(f"Successfully sent {sent_count} of {len(new_jobs)} new job(s) in {len(chunks)} message(s)")
    save_seen_jobs(seen_ids)


if __name__ == "__main__":
    main()
