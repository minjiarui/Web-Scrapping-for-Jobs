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
    title = html.escape(job.get("title", "Untitled role").strip())
    company = html.escape(job.get("company", {}).get("display_name", "Unknown company"))
    location = html.escape(job.get("location", {}).get("display_name", "Location not specified"))
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    url = job.get("redirect_url", "")
    created = job.get("created", "")[:10]  # just the date part

    salary_line = ""
    if salary_min and salary_max:
        salary_line = f"\n💰 {salary_min:,.0f} - {salary_max:,.0f}"

    return (
        f"📋 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}{salary_line}\n"
        f"🗓 Posted: {created}\n"
        f'🔗 <a href="{url}">Apply here</a>'
    )


def send_telegram_message(text: str) -> bool:
    """Returns True if the message was sent successfully, False otherwise."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"WARNING: Telegram send failed ({resp.status_code}): {resp.text}")
        return False
    return True


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

    # Send a header if there's more than one
    if len(new_jobs) > 1:
        send_telegram_message(f"🔔 {len(new_jobs)} new Business Analyst job(s) found today:")

    sent_count = 0
    for job in new_jobs:
        # Only mark a job as "seen" if the message actually sent successfully.
        # This way, if Telegram delivery fails (e.g. bad chat ID, network issue),
        # the job will be retried on the next run instead of being silently lost.
        if send_telegram_message(format_job_message(job)):
            seen_ids.add(str(job.get("id")))
            sent_count += 1

    print(f"Successfully sent {sent_count} of {len(new_jobs)} new job(s)")
    save_seen_jobs(seen_ids)


if __name__ == "__main__":
    main()
