# BA Job Alert Bot

Checks Adzuna daily for new "business analyst" job postings in Singapore and
sends you a Telegram message for each new one it finds. Runs for free on
GitHub Actions — no server needed.

## How it works

1. GitHub Actions runs `scripts/job_alert.py` on a daily schedule (default: 9am SGT).
2. The script queries the Adzuna API for BA jobs.
3. It compares the results against `data/seen_jobs.json` (jobs already notified about).
4. Any new job gets sent to you on Telegram.
5. `data/seen_jobs.json` is updated and committed back to the repo, so tomorrow's run
   knows what's already been sent.

## Setup steps

### 1. Get an Adzuna API key (free)
1. Go to https://developer.adzuna.com/ and sign up.
2. Create an app — you'll get an `App ID` and `App Key`.

### 2. Create your Telegram bot
1. In Telegram, message **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and username).
3. BotFather will give you a **bot token** — looks like `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`. Save it.
4. Send your new bot a message (anything, e.g. "hi") — this is required once so
   the bot is allowed to message you back.

### 3. Get your Telegram chat ID
1. After messaging your bot, open this URL in a browser (replace with your token):
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
2. Look for `"chat":{"id":123456789,...}` in the response — that number is your `TELEGRAM_CHAT_ID`.
   (If you see an empty result, make sure you messaged the bot first, then refresh.)

### 4. Push this project to a GitHub repo
1. Create a new **private** repo on GitHub (private is fine — GitHub Actions
   free tier includes private repos).
2. Push these files to it:
   ```bash
   cd ba-job-alert
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```

### 5. Add your secrets to GitHub
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:
| Secret name | Value |
|---|---|
| `ADZUNA_APP_ID` | from step 1 |
| `ADZUNA_APP_KEY` | from step 1 |
| `TELEGRAM_BOT_TOKEN` | from step 2 |
| `TELEGRAM_CHAT_ID` | from step 3 |

### 6. Test it
Go to the **Actions** tab in your repo → select "Daily BA Job Alert" →
**Run workflow** (this uses the `workflow_dispatch` trigger, works anytime,
doesn't wait for the schedule). Check the logs, and check Telegram for messages.

That's it — from now on it runs automatically every day at 9am SGT.

## Customizing

Edit the `env:` block in `.github/workflows/daily-job-check.yml`:
- `SEARCH_QUERY` — change to `"business analyst intern"`, `"data analyst"`, etc.
- `SEARCH_COUNTRY` — Adzuna country code (`sg`, `gb`, `us`, `au`, `in`, etc. — see https://developer.adzuna.com/overview for the full list)
- Cron schedule — edit the `cron:` line. Cron is in UTC. `0 1 * * *` = 1am UTC = 9am SGT.
  Use https://crontab.guru to build other schedules.

You can also change `MAX_JOB_AGE_DAYS` (in the script, or add it as another env var)
if you want to widen or narrow how recent a posting must be to count.

## Adding more sources later

Adzuna is a solid single source to start with. If you want to expand later:
- **Jooble API** (free) — similar setup, different aggregator, good for coverage overlap.
- **JSearch on RapidAPI** — aggregates LinkedIn, Indeed, Glassdoor listings, free tier available.
- Each new source just means fetching its list, tagging job IDs with a source prefix
  (so they don't collide in `seen_jobs.json`), and reusing the same dedupe +
  Telegram-send logic.

A note on LinkedIn specifically: LinkedIn's Terms of Service prohibit scraping the
site directly, and it actively blocks automated traffic. Using an aggregator API
(as this project does) sidesteps that entirely and is far more reliable long-term.

## Troubleshooting

- **No messages arriving**: check the Actions tab → latest run → logs. Most common
  cause is a missing/incorrect secret, or you forgot to message your bot first
  (Telegram bots can't message you until you've messaged them).
- **"No changes to commit" every day but you expect new jobs**: double check
  `SEARCH_QUERY` and `SEARCH_COUNTRY` are returning results — test the Adzuna URL
  directly in a browser with your keys filled in.
- **Getting too many/few results**: adjust `RESULTS_PER_PAGE` and `MAX_JOB_AGE_DAYS`.
