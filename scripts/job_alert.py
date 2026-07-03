"""
Business Analyst Job Alert Bot
--------------------------------
Fetches new "business analyst" job postings from the Adzuna API and sends
any jobs it hasn't seen before to a Telegram chat.

Designed to run once a day via GitHub Actions (see .github/workflows/daily-job-check.yml)
but works fine run manually or via a local cron job too.

Required environment variables (set as GitHub Secrets, see README.md):
    ADZUNA_APP_ID     - your Adzuna API app ID
    ADZUNA_APP_KEY    - your Adzuna API app key
    TELEGRAM_BOT_TOKEN - your Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID   - your personal chat ID (see README.md for how to get this)

Optional environment variables:
    SEARCH_QUERY      - what to search for (default: "business analyst")
    SEARCH_COUNTRY    - Adzuna country code (default: "sg" for Singapore)
    RESULTS_PER_PAGE  - how many jobs to pull per run (default: 20)
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

SEARCH_QUERY = os.environ.get("SEARCH_QUERY", "business analyst")
SEARCH_COUNTRY = os.environ.get("SEARCH_COUNTRY", "sg")
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "20"))
MAX_JOB_AGE_DAYS = int(os.environ.get("MAX_JOB_AGE_DAYS", "3"))

SEEN_JOBS_FILE = Path(__file__).parent.parent / "data" / "seen_jobs.json"

ADZUNA_URL = f"https://api.adzuna.com/v1/api/jobs/{SEARCH_COUNTRY}/search/1"


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


def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_jobs(seen_ids: set):
    SEEN_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_JOBS_FILE, "w") as f:
        # Sort for a deterministic, stable file (avoids spurious git diffs between
        # runs caused by Python's randomized set ordering) and cap growth at 500 IDs.
        json.dump(sorted(seen_ids)[-500:], f, indent=2)


def fetch_jobs() -> list:
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": SEARCH_QUERY,
        "results_per_page": RESULTS_PER_PAGE,
        "sort_by": "date",
        "max_days_old": MAX_JOB_AGE_DAYS,
        "content-type": "application/json",
    }
    resp = requests.get(ADZUNA_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def format_job_message(job: dict) -> str:
    # HTML-escape all job-provided text fields to keep Telegram's HTML parser happy
    # and to avoid characters like < > & breaking the message.
    raw_title = job.get("title", "Untitled role").strip()
    raw_company = job.get("company", {}).get("display_name", "Unknown company")
    title = html.escape(raw_title)
    company = html.escape(raw_company)
    location = html.escape(job.get("location", {}).get("display_name", "Location not specified"))
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    url = job.get("redirect_url", "")
    created = job.get("created", "")[:10]  # just the date part

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n💰 {salary_min:,.0f} - {salary_max:,.0f}"

    # Adzuna's own redirect/details pages can occasionally get blocked by their
    # CDN's bot detection. As a reliable fallback, build a plain Google search
    # link for the job title + company - this always works since it's just a
    # search query, not a link into Adzuna's infrastructure.
    search_query = quote_plus(f"{raw_title} {raw_company} Singapore")
    google_search_url = f"https://www.google.com/search?q={search_query}"

    return (
        f"📋 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}{salary_line}\n"
        f"🗓 Posted: {created}\n"
        f'🔗 <a href="{url}">Apply here (Adzuna)</a>\n'
        f'🔎 <a href="{google_search_url}">Search on Google</a>'
    )


def send_telegram_message(text: str, disable_preview: bool = False) -> bool:
    """Returns True if the message was sent successfully, False otherwise."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"WARNING: Telegram send failed ({resp.status_code}): {resp.text}")
        return False
    return True


def build_digest_chunks(new_jobs: list, max_length: int = 3500):
    """
    Groups job entries into digest messages. Telegram caps messages at 4096
    characters, so this packs as many jobs as possible into each message
    (staying under max_length as a safety buffer) instead of sending one
    message per job.

    Returns a list of (message_text, jobs_in_this_message) tuples.
    """
    separator = "\n\n➖➖➖➖➖➖➖➖\n\n"
    chunks = []
    current_text = ""
    current_jobs = []

    for job in new_jobs:
        entry = format_job_message(job)
        addition = (separator if current_jobs else "") + entry
        if current_jobs and len(current_text) + len(addition) > max_length:
            # Current chunk is full - close it out and start a new one
            chunks.append((current_text, current_jobs))
            current_text = entry
            current_jobs = [job]
        else:
            current_text += addition
            current_jobs.append(job)

    if current_jobs:
        chunks.append((current_text, current_jobs))

    return chunks


def main():
    require_env_vars()

    seen_ids = load_seen_jobs()
    jobs = fetch_jobs()
    print(f"Fetched {len(jobs)} jobs from Adzuna for query='{SEARCH_QUERY}' country={SEARCH_COUNTRY}")

    new_jobs = [j for j in jobs if str(j.get("id")) not in seen_ids]
    print(f"{len(new_jobs)} of these are new")

    if not new_jobs:
        # Nothing to notify about today - stay quiet, don't spam a "no jobs" message daily.
        return

    chunks = build_digest_chunks(new_jobs)
    header = f"🔔 {len(new_jobs)} new Business Analyst job(s) found today:\n\n"

    sent_count = 0
    for i, (chunk_text, jobs_in_chunk) in enumerate(chunks):
        # Only prepend the header to the first message, and only if there's
        # more than one job overall (keeps a single-job day looking clean).
        text = (header if i == 0 and len(new_jobs) > 1 else "") + chunk_text
        # Disable link previews on digest messages - with several jobs per
        # message, a preview card for just the first link looks out of place.
        if send_telegram_message(text, disable_preview=True):
            for job in jobs_in_chunk:
                seen_ids.add(str(job.get("id")))
                sent_count += 1

    print(f"Successfully sent {sent_count} of {len(new_jobs)} new job(s) in {len(chunks)} message(s)")
    save_seen_jobs(seen_ids)


if __name__ == "__main__":
    main()
