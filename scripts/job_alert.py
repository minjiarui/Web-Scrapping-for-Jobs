"""
Business Analyst / Data Analyst Job Alert Bot
------------------------------------------------
Fetches new job postings from the Adzuna API across multiple search terms
(e.g. "business analyst", "data analyst"), ranks them by relevance to your
resume/skillset, and sends the top N most relevant new ones to Telegram.

Relevance is scored using a BLEND of:
  1. Weighted keyword matching (RELEVANCE_KEYWORDS) - exact skill/term hits
  2. Semantic similarity (sentence-transformers) - how close the job's
     title+description is in *meaning* to your profile, even if it doesn't
     use the exact same words.

Runs either on a daily schedule, or instantly when triggered by a Cloudflare
Worker reacting to a "/refresh" message in Telegram (see README.md).

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
    MAX_JOBS_PER_DIGEST - how many top-ranked jobs to send per run (default: 10)
    TRIGGERED_BY      - set to "refresh" when triggered on-demand, for message wording
"""

import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import requests
from sentence_transformers import SentenceTransformer, util

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
MAX_JOBS_PER_DIGEST = int(os.environ.get("MAX_JOBS_PER_DIGEST", "10"))

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_JOBS_FILE = DATA_DIR / "seen_jobs.json"

ADZUNA_URL = f"https://api.adzuna.com/v1/api/jobs/{SEARCH_COUNTRY}/search/1"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

QUERY_TAGS = {
    "business analyst": "📊 Business Analyst",
    "data analyst": "📈 Data Analyst",
}

# ---------- Resume + LinkedIn-based relevance keywords ----------
# Weighted by how strongly each skill/keyword reflects your resume and
# LinkedIn profile. Title matches count double a description match.
RELEVANCE_KEYWORDS = {
    # Core technical skills - highest weight
    "python": 5, "sql": 5, "power bi": 5, "julia": 5,
    "operations research": 5, "business analytics": 5,
    "data analytics": 5, "data analysis": 5,
    # Explicitly stated target areas (from LinkedIn summary: "actively
    # seeking internship roles in data analytics, automation, or
    # quantitative finance")
    "automation": 5, "quantitative finance": 5,
    # You're specifically looking for internships right now
    "internship": 4, "intern": 4,
    # Related tools / methods
    "excel": 3, "power query": 3, "machine learning": 3,
    "simulation": 3, "optimization": 3, "forecasting": 3,
    "dashboard": 3, "nlp": 3, "statistics": 3, "r": 3, "quant": 2,
    # Domain-adjacent (finance/sales background)
    "financial analysis": 1, "valuation": 1, "wealth management": 1,
    "risk management": 1, "financial planning": 1, "quantitative": 1,
    "portfolio": 1, "digital transformation": 1,
}

# ---------- Semantic similarity profile ----------
# This paragraph is embedded once per run and compared against each job's
# title+description using a local, free sentence-embedding model. Unlike
# keyword matching, this can catch jobs that describe similar work using
# different words (e.g. "built BI tooling to replace manual spreadsheets"
# matching your Power BI/Power Query automation experience at KONE, even
# without the exact words "Power BI" appearing).
PROFILE_TEXT = """
Final year Engineering Systems and Design student at SUTD, specialising in
Business Analytics and Operations Research, graduating May 2027. Completed
a 4-month data analyst internship at KONE, automating manual Excel
reporting into Power BI dashboards, building a market analysis tool to
identify high-potential sales leads, and using Power Query to clean and
standardise large financial datasets across an 8,000+ asset portfolio.
Academic projects in statistical modelling, discrete event simulation,
machine learning, and optimisation using Python, R, SQL, and Julia/JuMP.
Primarily interested in data analytics, business analytics, and finance
roles.
"""

# How much weight each scoring method gets when combined (should sum to 1.0)
KEYWORD_WEIGHT = 0.4
SEMANTIC_WEIGHT = 0.6

# Lazily loaded so the model is only downloaded/loaded once per run, and
# only if there are actually new jobs to score.
_semantic_model = None


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


def compute_relevance(job: dict) -> tuple:
    """Scores a job against RELEVANCE_KEYWORDS. Title matches count double.
    Returns (score, matched_keywords_list)."""
    title = job.get("title", "").lower()
    description = job.get("description", "").lower()

    score = 0
    matched = []
    for keyword, weight in RELEVANCE_KEYWORDS.items():
        pattern = r"\b" + re.escape(keyword) + r"\b"
        title_hits = len(re.findall(pattern, title))
        desc_hits = len(re.findall(pattern, description))
        hits = title_hits * 2 + desc_hits
        if hits:
            score += weight * hits
            matched.append(keyword)

    return score, matched


def get_semantic_model() -> SentenceTransformer:
    """Loads the sentence-embedding model once and reuses it. Uses a small
    (~90MB) pre-trained model that runs fine on CPU within GitHub Actions."""
    global _semantic_model
    if _semantic_model is None:
        print("Loading semantic similarity model (first run may take a moment)...")
        _semantic_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _semantic_model


def compute_semantic_scores(jobs: list) -> list:
    """Returns a list of cosine-similarity scores (roughly 0-1, higher =
    more semantically similar) between PROFILE_TEXT and each job's
    title+description, computed in a single batch for efficiency."""
    if not jobs:
        return []

    model = get_semantic_model()
    profile_embedding = model.encode(PROFILE_TEXT, convert_to_tensor=True)

    job_texts = [f"{j.get('title', '')}. {j.get('description', '')}" for j in jobs]
    job_embeddings = model.encode(job_texts, convert_to_tensor=True)

    similarities = util.cos_sim(profile_embedding, job_embeddings)[0]
    return [float(s) for s in similarities]


def normalize(values: list) -> list:
    """Min-max normalizes a list of numbers to a 0-1 range so two
    differently-scaled scores (keyword counts vs. cosine similarity) can be
    fairly combined. If all values are equal, returns 0.5 for each."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


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

    matched_keywords = job.get("_matched_keywords", [])
    match_line = ""
    if matched_keywords:
        shown = ", ".join(matched_keywords[:4])
        match_line = f"\n🎯 Matches your skills: {html.escape(shown)}"

    search_query = quote_plus(f"{raw_title} {raw_company} Singapore")
    google_search_url = f"https://www.google.com/search?q={search_query}"

    return (
        f"{tag_line}"
        f"📋 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}{salary_line}{match_line}\n"
        f"🗓 Posted: {created}\n"
        f'🔗 <a href="{url}">Apply here (Adzuna)</a>\n'
        f'🔎 <a href="{google_search_url}">Search on Google</a>'
    )


def build_digest_chunks(new_jobs: list, max_length: int = 3500):
    separator = "\n\n➖➖➖➖➖➖➖➖\n\n"
    chunks = []
    current_text = ""
    current_jobs = []

    for job in new_jobs:
        entry = format_job_message(job)
        addition = (separator if current_jobs else "") + entry
        if current_jobs and len(current_text) + len(addition) > max_length:
            chunks.append((current_text, current_jobs))
            current_text = entry
            current_jobs = [job]
        else:
            current_text += addition
            current_jobs.append(job)

    if current_jobs:
        chunks.append((current_text, current_jobs))

    return chunks


def send_telegram_message(text: str, disable_preview: bool = True) -> bool:
    url = f"{TELEGRAM_API}/sendMessage"
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


def run_job_check(triggered_by_refresh: bool = False):
    """Fetches jobs, ranks them by relevance, and sends the top N most
    relevant new ones to Telegram. Jobs outside the top N are deliberately
    left un-marked as seen, so they get re-considered (and re-scored) on a
    future day, in case they become more competitive once today's stronger
    matches have already been sent."""
    seen_ids = load_seen_jobs()
    jobs = fetch_all_jobs()
    print(f"Fetched {len(jobs)} unique jobs across queries: {', '.join(SEARCH_QUERIES)}")

    new_jobs = [j for j in jobs if str(j.get("id")) not in seen_ids]
    print(f"{len(new_jobs)} of these are new")

    if not new_jobs:
        if triggered_by_refresh:
            send_telegram_message("🔄 Refreshed - no new jobs found right now.")
        return

    # --- Step 1: keyword-based score (exact skill/term matches) ---
    keyword_scores = []
    matched_lists = []
    for job in new_jobs:
        score, matched = compute_relevance(job)
        keyword_scores.append(score)
        matched_lists.append(matched)

    # --- Step 2: semantic similarity score (meaning-based match) ---
    print("Computing semantic similarity scores...")
    semantic_scores = compute_semantic_scores(new_jobs)

    # --- Step 3: normalize both to 0-1, then blend ---
    normalized_keyword = normalize(keyword_scores)
    normalized_semantic = normalize(semantic_scores)

    for job, matched, kw_norm, sem_norm, sem_raw in zip(
        new_jobs, matched_lists, normalized_keyword, normalized_semantic, semantic_scores
    ):
        job["_matched_keywords"] = matched
        job["_semantic_score"] = round(sem_raw, 3)
        job["_relevance_score"] = (KEYWORD_WEIGHT * kw_norm) + (SEMANTIC_WEIGHT * sem_norm)

    new_jobs.sort(key=lambda j: j["_relevance_score"], reverse=True)

    top_jobs = new_jobs[:MAX_JOBS_PER_DIGEST]
    print(f"Sending top {len(top_jobs)} of {len(new_jobs)} new job(s) by relevance")

    chunks = build_digest_chunks(top_jobs)
    prefix = "🔄 Refresh: " if triggered_by_refresh else "🔔 "
    header = f"{prefix}Top {len(top_jobs)} job(s) today, ranked by relevance to you:\n\n"

    sent_count = 0
    for i, (chunk_text, jobs_in_chunk) in enumerate(chunks):
        text = (header if i == 0 and len(top_jobs) > 1 else "") + chunk_text
        if send_telegram_message(text):
            for job in jobs_in_chunk:
                seen_ids.add(str(job.get("id")))
                sent_count += 1

    print(f"Successfully sent {sent_count} of {len(top_jobs)} job(s) in {len(chunks)} message(s)")
    save_seen_jobs(seen_ids)


def main():
    require_env_vars()

    triggered_by_refresh = os.environ.get("TRIGGERED_BY", "").strip().lower() == "refresh"
    if triggered_by_refresh:
        print("Triggered instantly via /refresh")
        send_telegram_message("🔄 Refreshing job search now...")
    run_job_check(triggered_by_refresh=triggered_by_refresh)


if __name__ == "__main__":
    main()
