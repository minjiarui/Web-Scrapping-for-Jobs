"""
Business Analyst / Data Analyst Job Alert Bot
------------------------------------------------
Fetches new job postings from Adzuna, Jooble, and Careerjet across multiple
search terms (e.g. "business analyst", "data analyst", "quantitative
analyst"), ranks them by relevance to your resume/skillset, and sends the
top N most relevant new ones to Telegram.

Relevance is scored using a BLEND of:
  1. Weighted keyword matching (RELEVANCE_KEYWORDS) - exact skill/term hits
  2. Semantic similarity (sentence-transformers) - how close the job's
     title+description is in *meaning* to your profile, even if it doesn't
     use the exact same words.

Runs either on a daily schedule, or instantly when triggered by a Cloudflare
Worker reacting to a "/refresh" message in Telegram (see README.md).

Every new job considered in a run (whether sent, dropped by the AI fit
check, or filtered out for experience) is also appended to
data/powerbi_export.csv - a flat, append-only history file for building a
Power BI dashboard on top of this pipeline. See README.md for the Power BI
connection steps.

Required environment variables (set as GitHub Secrets, see README.md):
    ADZUNA_APP_ID     - your Adzuna API app ID
    ADZUNA_APP_KEY    - your Adzuna API app key
    TELEGRAM_BOT_TOKEN - your Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID   - your personal chat ID (see README.md for how to get this)

Optional environment variables:
    JOOBLE_API_KEY    - if set, also fetches jobs from Jooble. Skipped
                        entirely (no error) if not set.
    CAREERJET_AFFID   - if set, also fetches jobs from Careerjet. Skipped
                        entirely (no error) if not set.
    SEARCH_LOCATION_NAME - location string passed to Jooble/Careerjet
                        (default: "Singapore"). Adzuna uses SEARCH_COUNTRY
                        instead, since it takes a country code.
    CAREERJET_LOCALE - Careerjet locale code, determines which country
                        site is queried (default: "en_SG")
    SEARCH_QUERIES    - comma-separated search terms (default: see below)
    SEARCH_COUNTRY    - Adzuna country code (default: "sg" for Singapore)
    RESULTS_PER_PAGE  - how many jobs to pull per search term per run (default: 20)
    MAX_JOB_AGE_DAYS  - only consider jobs posted in the last N days (default: 3)
    MAX_JOBS_PER_DIGEST - how many top-ranked jobs to send per run (default: 10)
    MAX_EXPERIENCE_YEARS - filter out jobs requiring more than this many years
                           of experience (default: 0, i.e. entry-level/fresh-
                           grad/internship roles only)
    ANTHROPIC_API_KEY - if set, enables a deeper AI-based fit check for the
                        top-ranked jobs: fetches the real job posting page
                        and asks Claude to honestly assess fit against your
                        profile. If not set, this step is skipped entirely
                        and everything else works as before.
    TRIGGERED_BY      - set to "refresh" when triggered on-demand, for message wording
"""

import csv
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests
import trafilatura
from sentence_transformers import SentenceTransformer, util

# ---------- Configuration ----------
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Jooble and Careerjet are optional extra sources - each is only queried if
# its key/affid is set, so leaving these unset just means "skip that source".
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY")
CAREERJET_AFFID = os.environ.get("CAREERJET_AFFID")
SEARCH_LOCATION_NAME = os.environ.get("SEARCH_LOCATION_NAME", "Singapore")
CAREERJET_LOCALE = os.environ.get("CAREERJET_LOCALE", "en_SG")

# Expanded beyond "business analyst"/"data analyst" to cover BI, operations
# research, and finance/quant roles that also fit your resume (KONE Power BI
# work, WorldQuant Alphathon, CFA Research Challenge). Ten queries across
# three sources is ~30 API calls per run - if you hit rate limits on
# Jooble/Careerjet's free tiers, trim this list via the SEARCH_QUERIES
# GitHub Actions variable rather than editing code.
SEARCH_QUERIES = [
    q.strip()
    for q in os.environ.get(
        "SEARCH_QUERIES",
        "business analyst,data analyst,business intelligence analyst,"
        "data science analyst,operations research analyst,quantitative analyst,"
        "investment analyst,risk analyst,financial analyst,graduate analyst",
    ).split(",")
    if q.strip()
]
SEARCH_COUNTRY = os.environ.get("SEARCH_COUNTRY", "sg")
RESULTS_PER_PAGE = int(os.environ.get("RESULTS_PER_PAGE", "20"))
MAX_JOB_AGE_DAYS = int(os.environ.get("MAX_JOB_AGE_DAYS", "3"))
MAX_JOBS_PER_DIGEST = int(os.environ.get("MAX_JOBS_PER_DIGEST", "10"))
MAX_EXPERIENCE_YEARS = int(os.environ.get("MAX_EXPERIENCE_YEARS", "0"))

# --- AI-based fit verification (optional - only runs if ANTHROPIC_API_KEY is set) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

JOB_PAGE_FETCH_TIMEOUT = 15
JOB_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
MAX_JOB_PAGE_TEXT_CHARS = 6000  # keeps the amount of text sent to Claude bounded

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_JOBS_FILE = DATA_DIR / "seen_jobs.json"
APPLIED_JOBS_FILE = DATA_DIR / "applied_jobs.json"

# ---------- Power BI export ----------
# Append-only flat history of every new job the bot has ever scored, for
# building a Power BI dashboard on top of this pipeline (keyword trends,
# company frequency, score distribution, AI fit-check outcomes). Unlike
# seen_jobs.json (just bare IDs, used for dedup), this keeps the actual
# scoring details.
EXPORT_FILE = DATA_DIR / "powerbi_export.csv"
EXPORT_FIELDNAMES = [
    "job_id",
    "date_posted",
    "date_scraped",
    "title",
    "company",
    "location",
    "query",
    "keyword_score",
    "semantic_score",
    "relevance_score",
    "matched_keywords",
    "experience_filtered",
    "ai_suitability_note",
    "sent_in_digest",
    "application_status",
]

ADZUNA_URL = f"https://api.adzuna.com/v1/api/jobs/{SEARCH_COUNTRY}/search/1"
JOOBLE_URL = f"https://jooble.org/api/{JOOBLE_API_KEY}" if JOOBLE_API_KEY else None
CAREERJET_URL = "https://search.api.careerjet.net/v4/query"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

QUERY_TAGS = {
    "business analyst": "📊 Business Analyst",
    "data analyst": "📈 Data Analyst",
    "business intelligence analyst": "📊 BI Analyst",
    "data science analyst": "🧠 Data Science Analyst",
    "operations research analyst": "🧮 Operations Research Analyst",
    "quantitative analyst": "📐 Quant Analyst",
    "investment analyst": "💹 Investment Analyst",
    "risk analyst": "⚠️ Risk Analyst",
    "financial analyst": "💰 Financial Analyst",
    "graduate analyst": "🎓 Graduate Analyst",
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
    "dashboard": 3, "nlp": 3, "statistics": 3, "r": 3, "quant": 3,
    # Quant/finance terms - weighted up now that quant analyst, investment
    # analyst, and risk analyst are active search targets. Ties to your
    # WorldQuant Alphathon (alphas, portfolio optimisation, Sharpe) and CFA
    # Research Challenge (valuation, investment thesis) experience.
    "quantitative": 3, "financial modeling": 3, "portfolio": 2,
    "valuation": 2, "investment thesis": 2, "alpha": 2,
    "risk management": 2,
    # Domain-adjacent (finance/sales background)
    "financial analysis": 1, "wealth management": 1,
    "financial planning": 1, "digital transformation": 1,
}

# ---------- Experience requirement filtering ----------
# You're a final-year student with a 4-month internship, not someone with
# years of full-time experience - so postings requiring more than
# MAX_EXPERIENCE_YEARS are filtered out entirely rather than just ranked lower.

# Seniority words in the TITLE are the most reliable signal that a role
# needs more experience than you have, regardless of how the description
# phrases things. Uses \b word boundaries (not manual spacing) so a word at
# the very end of a title (e.g. "Business Analyst - Team Lead") is still
# caught correctly.
SENIOR_TITLE_PATTERNS = [
    r"\bsenior\b", r"\bsr\.?\b", r"\blead\b", r"\bmanager\b", r"\bdirector\b",
    r"\bhead of\b", r"\bprincipal\b", r"\bvp\b", r"\bvice president\b",
    r"\bchief\b", r"\bstaff\b", r"\bexecutive\b", r"\bavp\b", r"\bsvp\b",
    r"\bassociate director\b", r"\bgroup head\b", r"\bexpert\b",
    r"\b(?:analyst|manager|consultant)\s*(?:ii|iii|iv)\b",
]

# Some words for years/experience phrasing, to catch both "years" and "yrs"
_YEARS_WORD = r"(?:years?|yrs?)"

# Explicit years-of-experience phrasing to look for in the description.
# Each pattern's first capture group is the minimum number of years required.
EXPERIENCE_YEAR_PATTERNS = [
    rf"(?:minimum|min\.?|at least)\s*(\d+)\+?\s*{_YEARS_WORD}",
    rf"(\d+)\+?\s*(?:to|-)\s*\d+\+?\s*{_YEARS_WORD}\s*(?:of\s*)?(?:relevant\s*)?experience",
    rf"(\d+)\+?\s*{_YEARS_WORD}\s*(?:of\s*)?(?:relevant\s*|working\s*|professional\s*)?experience",
    rf"(\d+)\+?\s*{_YEARS_WORD}['\u2019]?\s*experience",
    rf"experience\s*(?:of\s*)?(?:at least\s*)?(\d+)\+?\s*{_YEARS_WORD}",
]

# Non-numeric phrasing that still signals "we want someone experienced",
# even without a specific year count the regex above could extract.
EXPERIENCE_PHRASE_RED_FLAGS = [
    r"proven track record",
    r"extensive experience",
    r"seasoned professional",
    r"several years of experience",
    r"deep expertise",
    r"significant experience",
    r"substantial experience",
]

# If any of these appear, treat the job as entry-level regardless of any
# stray year-number matched elsewhere in the text (e.g. company history).
ENTRY_LEVEL_OVERRIDE_PATTERNS = [
    r"entry[\s-]?level",
    r"fresh grad",
    r"no experience (?:required|necessary)",
    r"0\s*[-to]*\s*1\s*years?",
    r"recent graduate",
    r"graduate (?:program|programme|trainee)",
    r"\bintern(?:ship)?\b",
]


def exceeds_experience_threshold(job: dict) -> bool:
    """Returns True if a job appears to require more than MAX_EXPERIENCE_YEARS
    of experience, based on its title and description. Used to filter out
    roles that are a poor fit given your current experience level.

    Note: this runs against each source's description snippet, which is
    sometimes truncated - so it won't catch every senior role (e.g. if the
    "5 years experience" line falls outside the snippet). The AI fit-check
    step on your shortlisted top jobs (if ANTHROPIC_API_KEY is set) acts as
    a second pass using the full page text, which can catch some of what
    slips through here."""
    title = job.get("title", "").lower()
    description = job.get("description", "").lower()
    combined = f"{title} {description}"

    # Explicit entry-level language always wins, even over a stray year match
    for pattern in ENTRY_LEVEL_OVERRIDE_PATTERNS:
        if re.search(pattern, combined):
            return False

    # Seniority words in the title are a strong, reliable signal on their own
    for pattern in SENIOR_TITLE_PATTERNS:
        if re.search(pattern, title):
            return True

    # Look for explicit "X years experience" style phrasing anywhere
    for pattern in EXPERIENCE_YEAR_PATTERNS:
        match = re.search(pattern, combined)
        if match:
            years_required = int(match.group(1))
            if years_required > MAX_EXPERIENCE_YEARS:
                return True

    # Look for non-numeric "we want someone experienced" phrasing
    for pattern in EXPERIENCE_PHRASE_RED_FLAGS:
        if re.search(pattern, combined):
            return True

    return False


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
Also has quantitative finance experience: built and tested trading alphas
in the WorldQuant BRAIN Alphathon, and conducted equity valuation and
investment thesis work in the CFA Institute Research Challenge. Primarily
interested in data analytics, business analytics, and finance roles.
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


# ---------- Power BI export helpers ----------

def load_exported_job_ids() -> set:
    """Reads just the job_id column of the existing export, so we know
    which jobs have already been written and never duplicate a row."""
    if not EXPORT_FILE.exists():
        return set()
    with open(EXPORT_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return {row["job_id"] for row in reader if row.get("job_id")}


def build_export_row(job: dict, *, experience_filtered: bool, sent_in_digest: bool) -> dict:
    """Flattens one job dict (plus scoring fields attached elsewhere in the
    pipeline) into a single CSV row for the Power BI export."""
    company = job.get("company", {}).get("display_name", "Unknown company")
    location = job.get("location", {}).get("display_name", "")
    matched_keywords = "|".join(job.get("_matched_keywords", []))
    matched_queries = "|".join(job.get("_matched_queries", []))

    return {
        "job_id": str(job.get("id")),
        "date_posted": job.get("created", "")[:10],
        "date_scraped": datetime.now(timezone.utc).date().isoformat(),
        "title": job.get("title", ""),
        "company": company,
        "location": location,
        "query": matched_queries,
        "keyword_score": job.get("_keyword_score", ""),
        "semantic_score": job.get("_semantic_score", ""),
        "relevance_score": job.get("_relevance_score", ""),
        "matched_keywords": matched_keywords,
        "experience_filtered": experience_filtered,
        "ai_suitability_note": job.get("_ai_note", ""),
        "sent_in_digest": sent_in_digest,
        "application_status": "not_applied",
    }


def append_jobs_to_export(rows: list):
    """Appends new rows to powerbi_export.csv, writing a header first if the
    file doesn't exist yet. Rows for job_ids already in the file should be
    filtered out by the caller before this is called."""
    if not rows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = EXPORT_FILE.exists()
    with open(EXPORT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def finalize_export(export_rows: list):
    """Deduplicates against what's already on disk and writes the rest."""
    if not export_rows:
        return
    already_exported = load_exported_job_ids()
    new_rows = [r for r in export_rows if r["job_id"] not in already_exported]
    if new_rows:
        append_jobs_to_export(new_rows)
        print(f"Appended {len(new_rows)} new row(s) to {EXPORT_FILE.name}")


def backfill_applied_job_titles():
    """Repairs any applied_jobs.json entries stuck showing "Unknown title" /
    "Unknown company" - this happens if a status button was tapped within
    seconds of a digest arriving, before that day's export commit had
    actually landed on GitHub, so update_status.py's lookup came up empty
    at the time. Re-checking here, after this run's own export is written,
    catches both today's races and any left over from previous days."""
    if not APPLIED_JOBS_FILE.exists() or not EXPORT_FILE.exists():
        return

    applied_jobs = load_json(APPLIED_JOBS_FILE, {})
    unknown_ids = {jid for jid, entry in applied_jobs.items() if entry.get("title") == "Unknown title"}
    if not unknown_ids:
        return

    with open(EXPORT_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        lookup = {row["job_id"]: row for row in reader if row.get("job_id") in unknown_ids}

    fixed_count = 0
    for job_id, row in lookup.items():
        entry = applied_jobs[job_id]
        entry["title"] = row.get("title") or entry["title"]
        entry["company"] = row.get("company") or entry["company"]
        fixed_count += 1

    if fixed_count:
        save_json(APPLIED_JOBS_FILE, applied_jobs)
        print(f"Backfilled title/company for {fixed_count} previously-unknown applied job(s)")


def _within_max_age(created: str) -> bool:
    """Adzuna filters by max_days_old server-side, but Jooble and Careerjet
    don't support that param, so this applies the same freshness rule
    client-side to their results. If a date can't be parsed, the job is
    kept rather than dropped (better to show a possibly-stale job than
    silently lose a good one to a parsing quirk)."""
    if not created:
        return True
    try:
        created_date = datetime.strptime(created[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (date.today() - created_date).days <= MAX_JOB_AGE_DAYS


def _parse_careerjet_date(raw_date: str) -> str:
    """Careerjet returns dates as RFC 822 strings (e.g. 'Wed,15 Nov 2025
    19:13:43 GMT'). The rest of the pipeline expects 'YYYY-MM-DD' (it just
    slices the first 10 characters of Adzuna's ISO date), so this converts
    to match."""
    if not raw_date:
        return ""
    try:
        return parsedate_to_datetime(raw_date).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def fetch_adzuna_jobs_for_query(query: str) -> list:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "results_per_page": RESULTS_PER_PAGE,
        "sort_by": "date",
        "max_days_old": MAX_JOB_AGE_DAYS,
        "content-type": "application/json",
    }
    try:
        resp = requests.get(ADZUNA_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except (requests.RequestException, ValueError) as e:
        print(f"WARNING: Adzuna fetch failed for '{query}': {e}")
        return []


def fetch_jooble_jobs_for_query(query: str) -> list:
    """Fetches jobs from Jooble for a single query, normalized to the same
    schema used throughout the rest of the pipeline (title, company,
    location, description, salary_min/max, redirect_url, created, id).
    Returns an empty list on any failure or if JOOBLE_API_KEY isn't set, so
    a Jooble outage or missing key never breaks the rest of the run."""
    if not JOOBLE_API_KEY:
        return []
    payload = {"keywords": query, "location": SEARCH_LOCATION_NAME, "page": "1"}
    try:
        resp = requests.post(JOOBLE_URL, json=payload, timeout=30)
        resp.raise_for_status()
        raw_jobs = resp.json().get("jobs", [])
    except (requests.RequestException, ValueError) as e:
        print(f"WARNING: Jooble fetch failed for '{query}': {e}")
        return []

    normalized = []
    for raw in raw_jobs:
        created = (raw.get("updated") or "")[:10]
        if not _within_max_age(created):
            continue
        normalized.append({
            "id": f"jooble_{raw.get('id')}",
            "title": raw.get("title", ""),
            "company": {"display_name": raw.get("company") or "Unknown company"},
            "location": {"display_name": raw.get("location") or "Location not specified"},
            "description": raw.get("snippet", ""),
            "salary_min": None,
            "salary_max": None,
            "redirect_url": raw.get("link", ""),
            "created": created,
        })
    return normalized


def fetch_careerjet_jobs_for_query(query: str) -> list:
    """Fetches jobs from Careerjet for a single query, normalized the same
    way as fetch_jooble_jobs_for_query. Returns an empty list on any
    failure or if CAREERJET_AFFID isn't set."""
    if not CAREERJET_AFFID:
        return []
    params = {
        "keywords": query,
        "location": SEARCH_LOCATION_NAME,
        "affid": CAREERJET_AFFID,
        "locale_code": CAREERJET_LOCALE,
        "user_ip": "127.0.0.1",
        "user_agent": "job-alert-bot",
    }
    try:
        resp = requests.get(CAREERJET_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"WARNING: Careerjet fetch failed for '{query}': {e}")
        return []

    if data.get("type") != "JOBS":
        return []  # e.g. ambiguous "LOCATIONS" response - no results this call

    normalized = []
    for raw in data.get("jobs", []):
        created = _parse_careerjet_date(raw.get("date", ""))
        if not _within_max_age(created):
            continue
        job_url = raw.get("url", "")
        normalized.append({
            "id": f"careerjet_{abs(hash(job_url))}",
            "title": raw.get("title", ""),
            "company": {"display_name": raw.get("company") or "Unknown company"},
            "location": {"display_name": raw.get("locations") or "Location not specified"},
            "description": raw.get("description", ""),
            "salary_min": raw.get("salary_min"),
            "salary_max": raw.get("salary_max"),
            "redirect_url": job_url,
            "created": created,
        })
    return normalized


def fetch_all_jobs() -> list:
    """Fetches jobs from every configured source (Adzuna always; Jooble and
    Careerjet only if their keys are set) across all SEARCH_QUERIES, and
    deduplicates by ID. A job matched by more than one query keeps a
    combined list of which queries it matched, so the digest message can
    show all relevant tags."""
    jobs_by_id = {}

    def add_job(job: dict, query: str):
        job_id = str(job.get("id"))
        if job_id in jobs_by_id:
            if query not in jobs_by_id[job_id]["_matched_queries"]:
                jobs_by_id[job_id]["_matched_queries"].append(query)
        else:
            job["_matched_queries"] = [query]
            jobs_by_id[job_id] = job

    for query in SEARCH_QUERIES:
        for job in fetch_adzuna_jobs_for_query(query):
            add_job(job, query)
        for job in fetch_jooble_jobs_for_query(query):
            add_job(job, query)
        for job in fetch_careerjet_jobs_for_query(query):
            add_job(job, query)

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


def fetch_job_page_text(url: str) -> str:
    """Attempts to fetch the real job posting page and extract its clean
    main text (stripping nav bars, ads, cookie banners, etc). Many job
    boards (LinkedIn, Indeed especially) block automated requests - this is
    the same limitation that led to the Google Search fallback link in each
    Telegram message. On any failure, returns None rather than raising, so
    one blocked/broken page doesn't affect the rest of the digest."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers=JOB_PAGE_HEADERS, timeout=JOB_PAGE_FETCH_TIMEOUT, allow_redirects=True)
        if not resp.ok:
            return None
        text = trafilatura.extract(resp.text)
        if not text or len(text) < 200:
            return None
        return text[:MAX_JOB_PAGE_TEXT_CHARS]
    except requests.RequestException:
        return None


def get_ai_suitability_note(job: dict, page_text: str) -> dict:
    """Sends the fetched job page text plus your profile to Claude (Haiku,
    for low cost) and asks for a structured fit assessment - specifically
    flagging experience-level or skill mismatches missed by the earlier
    regex-based filter (which only sees each source's, sometimes truncated,
    description snippet). Returns None on any failure so a single bad API
    call never breaks the rest of the digest.

    Returns a dict: {"fit": "good" | "poor", "note": "short explanation"}"""
    prompt = f"""You are helping a final-year student evaluate whether a job posting is a good fit.

STUDENT PROFILE:
{PROFILE_TEXT.strip()}

JOB TITLE: {job.get("title", "Unknown")}
COMPANY: {job.get("company", {}).get("display_name", "Unknown")}

FULL JOB POSTING TEXT:
{page_text}

Assess how well this role fits the student's profile above. Specifically
check whether it actually needs more experience than a 4-month internship
(e.g. states 2+ years, "senior", "experienced professional" language), or
whether the core required skills don't really match.

Respond with ONLY a JSON object, no other text, no markdown formatting:
{{"fit": "good" or "poor", "note": "1-2 short direct sentences explaining why"}}"""

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if not resp.ok:
            print(f"WARNING: Anthropic API call failed ({resp.status_code}): {resp.text[:200]}")
            return None
        data = resp.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        raw_text = " ".join(text_blocks).strip()
        raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text.strip()).strip()
        parsed = json.loads(raw_text)
        if parsed.get("fit") not in ("good", "poor") or not parsed.get("note"):
            return None
        return parsed
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"WARNING: Anthropic API call/parse failed: {e}")
        return None


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

    matched_queries = job.get("_matched_queries", [])
    primary_tag = QUERY_TAGS.get(matched_queries[0], matched_queries[0]) if matched_queries else ""
    tag_line = f"{primary_tag}\n" if primary_tag else ""

    matched_keywords = job.get("_matched_keywords", [])
    match_line = ""
    if matched_keywords:
        shown = ", ".join(matched_keywords[:4])
        match_line = f"\n🎯 Matches your skills: {html.escape(shown)}"

    ai_note = job.get("_ai_note")
    ai_note_line = f"\n🤖 {html.escape(ai_note)}" if ai_note else ""

    search_query = quote_plus(f"{raw_title} {raw_company} Singapore")
    google_search_url = f"https://www.google.com/search?q={search_query}"

    return (
        f"{tag_line}"
        f"📋 <b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {location}{salary_line}{match_line}{ai_note_line}\n"
        f"🗓 Posted: {created}\n"
        f'🔗 <a href="{url}">Apply here</a>\n'
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


def send_telegram_message(text: str, disable_preview: bool = True, reply_markup: dict = None) -> bool:
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


def build_apply_keyboard(job_id: str) -> dict:
    """Inline button shown under each digest message - lets you mark a job
    as applied with a single tap instead of typing a command. Tapping it
    fires a callback_query Telegram update, which the Cloudflare Worker
    routes to the "Telegram Status Update" workflow (see
    scripts/update_status.py)."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Mark Applied", "callback_data": f"applied:{job_id}"}
        ]]
    }


def run_job_check(triggered_by_refresh: bool = False):
    """Fetches jobs, ranks them by relevance, and sends the top N most
    relevant new ones to Telegram. Jobs outside the top N are deliberately
    left un-marked as seen, so they get re-considered (and re-scored) on a
    future day, in case they become more competitive once today's stronger
    matches have already been sent.

    Every new job the bot looks at this run - sent, AI-dropped, skipped for
    rank, or filtered out for experience - is queued up as a row for
    powerbi_export.csv via export_rows, and written out at the end."""
    seen_ids = load_seen_jobs()
    export_rows = []

    jobs = fetch_all_jobs()
    print(f"Fetched {len(jobs)} unique jobs across queries: {', '.join(SEARCH_QUERIES)}")

    new_jobs = [j for j in jobs if str(j.get("id")) not in seen_ids]
    print(f"{len(new_jobs)} of these are new")

    # Filter out jobs requiring more experience than you have. Excluded jobs
    # are marked as seen immediately so they don't reappear in future digests.
    kept_jobs = []
    excluded_ids = []
    for job in new_jobs:
        if exceeds_experience_threshold(job):
            excluded_ids.append(str(job.get("id")))
            export_rows.append(
                build_export_row(job, experience_filtered=True, sent_in_digest=False)
            )
        else:
            kept_jobs.append(job)

    if excluded_ids:
        print(f"Filtered out {len(excluded_ids)} job(s) requiring more than {MAX_EXPERIENCE_YEARS} year(s) of experience")
        seen_ids.update(excluded_ids)

    new_jobs = kept_jobs

    if not new_jobs:
        if triggered_by_refresh:
            send_telegram_message("🔄 Refreshed - no new jobs found right now.")
        save_seen_jobs(seen_ids)
        finalize_export(export_rows)
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

    for job, matched, kw_raw, kw_norm, sem_norm, sem_raw in zip(
        new_jobs, matched_lists, keyword_scores, normalized_keyword, normalized_semantic, semantic_scores
    ):
        job["_matched_keywords"] = matched
        job["_keyword_score"] = round(kw_raw, 3)
        job["_semantic_score"] = round(sem_raw, 3)
        job["_relevance_score"] = round((KEYWORD_WEIGHT * kw_norm) + (SEMANTIC_WEIGHT * sem_norm), 3)

    new_jobs.sort(key=lambda j: j["_relevance_score"], reverse=True)

    top_jobs = new_jobs[:MAX_JOBS_PER_DIGEST]
    remaining_jobs = new_jobs[MAX_JOBS_PER_DIGEST:]
    print(f"Sending top {len(top_jobs)} of {len(new_jobs)} new job(s) by relevance")

    # --- Step 4: deeper AI-based fit check, only for the shortlisted top jobs ---
    # Unlike the earlier regex filter (which only sees each source's often-
    # truncated description snippet), this reads the full job page - so it
    # catches senior/experienced-required roles that slipped through Step 0.
    # Jobs flagged "poor" fit are dropped and backfilled from the next-best
    # ranked jobs, so the digest still has up to MAX_JOBS_PER_DIGEST entries.
    if ANTHROPIC_API_KEY:
        print("Fetching job pages and running AI fit check on shortlisted jobs...")
        confirmed_jobs = []
        candidates = list(top_jobs)
        while candidates and len(confirmed_jobs) < MAX_JOBS_PER_DIGEST:
            job = candidates.pop(0)
            page_text = fetch_job_page_text(job.get("redirect_url", ""))
            if not page_text:
                print(f"Could not fetch full page for '{job.get('title')}' - keeping as-is (no AI check)")
                confirmed_jobs.append(job)
                continue
            verdict = get_ai_suitability_note(job, page_text)
            if verdict is None:
                confirmed_jobs.append(job)
                continue
            job["_ai_note"] = verdict["note"]
            if verdict["fit"] == "poor":
                print(f"AI check flagged '{job.get('title')}' as a poor fit - dropping and backfilling")
                seen_ids.add(str(job.get("id")))
                if remaining_jobs:
                    candidates.append(remaining_jobs.pop(0))
            else:
                confirmed_jobs.append(job)
        top_jobs = confirmed_jobs
    else:
        print("ANTHROPIC_API_KEY not set - skipping AI-based fit check step")

    # Each job now gets its own message (rather than several jobs sharing
    # one batched message) so the "Mark Applied" button under it is
    # unambiguous - Telegram buttons apply to a whole message, so batching
    # would make a tap ambiguous about which job it referred to.
    prefix = "🔄 Refresh: " if triggered_by_refresh else "🔔 "
    if top_jobs:
        send_telegram_message(
            f"{prefix}Top {len(top_jobs)} job(s) today, ranked by relevance to you. "
            f"Tap ✅ under a job once you've applied:"
        )

    sent_count = 0
    sent_ids = set()
    for job in top_jobs:
        job_id = str(job.get("id"))
        text = format_job_message(job)
        keyboard = build_apply_keyboard(job_id)
        if send_telegram_message(text, reply_markup=keyboard):
            seen_ids.add(job_id)
            sent_ids.add(job_id)
            sent_count += 1

    print(f"Successfully sent {sent_count} of {len(top_jobs)} job(s), one message each")

    # Export every scored job this run, not just the ones sent - AI-dropped
    # and lower-ranked jobs matter for the score-distribution chart in
    # Power BI, and AI-dropped jobs carry their ai_suitability_note too.
    for job in new_jobs:
        export_rows.append(
            build_export_row(
                job,
                experience_filtered=False,
                sent_in_digest=str(job.get("id")) in sent_ids,
            )
        )

    save_seen_jobs(seen_ids)
    finalize_export(export_rows)


def main():
    require_env_vars()

    triggered_by_refresh = os.environ.get("TRIGGERED_BY", "").strip().lower() == "refresh"
    if triggered_by_refresh:
        print("Triggered instantly via /refresh")
        send_telegram_message("🔄 Refreshing job search now...")
    run_job_check(triggered_by_refresh=triggered_by_refresh)
    backfill_applied_job_titles()


if __name__ == "__main__":
    main()
