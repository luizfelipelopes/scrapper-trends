# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the scripts

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run a specific niche script
python pw_trends.py          # Entertainment/gossip (blog: Fofocando)
python pw_trends_finance.py  # Finance
python pw_trends_sports.py   # Sports
```

Each script runs as an infinite loop and must be stopped manually with `Ctrl+C`.

## Required `.env` variables

```
GEMINI_API_KEY=
GEMINI_API_GPT_2_5=        # Model ID used by pw_trends.py
GEMINI_API_GPT=            # Model ID used by pw_trends_sports.py and pw_trends_finance.py

WP_BLOG_FOFOCANDO_URL=
WP_BLOG_FOFOCANDO_USER=
WP_BLOG_FOFOCANDO_PASS=

WP_BLOG_SPORT_URL=
WP_BLOG_SPORT_USER=
WP_BLOG_SPORT_PASS=

TELEGRAM_TOKEN=
TELEGRAM_SPORT_TOKEN=
TELEGRAM_CHAT_ID=

TRENDS_URL_ENTERTEINMENT=
TRENDS_URL_SPORTS=
```

## Architecture

Each script is a single self-contained async Python file. All logic lives inside nested async functions within `main()`. The pipeline for each run is:

1. **`load_contents()`** — Pre-loads a batch of 20 posts before publishing begins:
   - `search_trends(link, ref)` — Playwright opens the Google Trends page (`TRENDS_URL`), clicks a trend row by index, and extracts up to 3 source article `href`s. `ref` controls which article's image is used (fallback mechanism).
   - `dowload_cover_image(page, href)` — Navigates to the article, scrapes the cover image (tries `srcset` first, falls back to `src`), and saves it to `covers/<safe_title>.jpg`.
   - `generate_content_ai(href, href2, href3, links_wordpress)` — Sends all 3 source URLs plus recent WP post/category links to Gemini. Returns a JSON object: `{title, slug, meta_description, keyword, body}`.
   - Skips any trend whose generated slug already exists in WordPress.

2. **Main publishing loop** — iterates through the pre-loaded batch, posting one article every 2 hours (`pw_trends.py`) or 1 hour (`pw_trends_sports.py`). Pauses entirely between 01:00–05:00.
   - `run_task()` → `upload_image_to_wordpress()` → `create_post_wordpress()` → `remove_image()` → `send_telegram_message()`

3. **Retry logic** — up to 5 attempts per trend item; on failure, `ref` is incremented (tries next source article's image) before moving to the next trend row.

## Key behavioural differences between scripts

| Script | WP Blog env prefix | Gemini model env var | Post interval | WP category logic |
|---|---|---|---|---|
| `pw_trends.py` | `WP_BLOG_FOFOCANDO_*` | `GEMINI_API_GPT_2_5` | 2 hours | 6 (Notícias) or 9 (Novelas) based on title regex |
| `pw_trends_sports.py` | `WP_BLOG_SPORT_*` | `GEMINI_API_GPT` | 1 hour | Category 1 always |
| `pw_trends_finance.py` | *(check file)* | `GEMINI_API_GPT` | *(check file)* | *(check file)* |

## Known skipped sources

`nsctotal.com.br` links are explicitly rejected in `search_trends()` and cause the script to try the next `ref` or move to the next trend.
