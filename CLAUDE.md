# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the scripts

```bash
# Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# Publish ONE post for a niche, then exit (no loop — cron drives the cadence)
python pw_trends.py          # Entertainment/gossip (blog: Fofocando)
python pw_trends_finance.py  # Finance
python pw_trends_sports.py   # Sports

# Run the unit tests
pip install -r requirements-dev.txt
python -m pytest
```

Each script is **single-shot**: it publishes at most one post and exits 0 (or
exits non-zero on a hard publish failure). Scheduling is owned by the GitHub
Actions workflows under `.github/workflows/`.

## Required `.env` variables (and GitHub Secrets)

The same names are read from `.env` locally and from GitHub Secrets in CI.

```
ANTHROPIC_API_KEY=         # AI provider in use (see scrapper_base ai_provider)
GEMINI_API_KEY=            # only needed if AI_PROVIDER is "gemini"

# AI model selection (shared across niches; optional — defaults shown). A niche
# can still override by passing the arg explicitly to NicheConfig.
AI_PROVIDER=               # "anthropic" (default) or "gemini"
AI_MODEL=                  # generation model (default claude-sonnet-4-6)
REVIEW_MODEL=              # review model (default claude-sonnet-4-6)

WP_BLOG_FOFOCANDO_URL=
WP_BLOG_FOFOCANDO_USER=
WP_BLOG_FOFOCANDO_PASS=

WP_BLOG_SPORT_URL=
WP_BLOG_SPORT_USER=
WP_BLOG_SPORT_PASS=

WP_BLOG_FINANCE_URL=
WP_BLOG_FINANCE_USER=
WP_BLOG_FINANCE_PASS=

TELEGRAM_TOKEN=
TELEGRAM_SPORT_TOKEN=
TELEGRAM_FINANCE_TOKEN=
TELEGRAM_CHAT_ID=

TRENDS_URL_ENTERTEINMENT=
TRENDS_URL_SPORTS=
TRENDS_URL_FINANCE=
```

## Architecture

Each entry script (`pw_trends*.py`) is a thin config: it builds a `NicheConfig`
and calls `run_once(config)`. All shared logic lives in `scrapper_base.py`.

`run_once(config)` does exactly one publish cycle:

1. **Commercial-hours gate** — `_is_commercial_hour(tz)` evaluates the
   01:00–05:00 pause in `config.timezone` (default `America/Sao_Paulo`). Outside
   the window it logs and returns (exit 0). The timezone matters because CI cron
   fires in **UTC**.
2. **`_recover_wp_data`** — fetches recent WP posts/categories for internal
   links and the set of existing slugs (the dedup source of truth).
3. **`_find_publishable`** — opens Playwright once and walks trend rows:
   - `_extract_source_hrefs` clicks a trend row and reads up to 3 source `href`s.
   - If **any** of those hrefs is already in `state/<niche>.json` it's skipped
     **with no AI call** (cost optimization) — a published trend can resurface
     with its sources in a different order.
   - Otherwise `_download_for_ref` scrapes the cover image (srcset → src
     fallback, saved to `covers/<safe_title>.jpg`) and `_generate_content` calls
     the AI, returning `{title, slug, meta_description, keyword, body}`. The
     generator is grounded with web search on both providers: Anthropic uses the
     server-side `web_search` tool (bounded by `GEN_MAX_SEARCHES` /
     `GEN_MAX_CONTINUATIONS`), Gemini grounds with Google Search (single call, no
     per-call cap), so it can confirm key facts before writing. The generation
     prompt also injects a per-niche **editorial persona** (`config.persona`) so
     the article reads like a human columnist instead of a neutral summary,
     without relaxing the SEO/AdSense/HTML/JSON rules. If the
     slug already exists in WP, the href is recorded in state and the row is
     skipped. The first genuinely new post is then reviewed (see step 4) and
     returned.
4. **Content review (soft gate)** — `_review_content` runs a **light** review
   on the **same provider as generation** (`config.ai_provider`), using
   `config.review_model` (default `claude-sonnet-4-6`; set `REVIEW_MODEL` to a
   model that matches the provider), returning `{approved, issues}`. Because the
   article is already fact-checked at generation time (step 3), this pass does
   **not** search the web. Its main job is the **cover image**: when one is
   available it is attached (vision — base64 block on Anthropic, inline `Part`
   on Gemini) so the reviewer can flag a cover that is **clearly unrelated** to
   the article. It also does a quick, conservative sanity check for *glaring*
   factual errors against the source articles, and ignores style, SEO, HTML and
   clickbait entirely. This is **soft blocking**: a flagged post is still
   created in WordPress but as a **draft** (`status=draft`) for human review
   instead of going live, and the Telegram message lists the issues. The step
   **fails open** — if `review_enabled` is `False`, the provider's client is
   missing, or the call/parse errors, the post publishes as usual.
5. **Publish** — `_run_task` = `_upload_image` → `_create_post` →
   `_remove_image` → `_send_telegram`. `_create_post` honours the review verdict
   (`publish` vs `draft`). Up to `RETRY_COUNT` (5) attempts.
6. **State** — all of the published trend's source hrefs are appended to
   `state/<niche>.json` (capped to the last `STATE_HISTORY_LIMIT` entries; note
   each published trend now contributes up to 3 entries). In CI the workflow commits
   this file back to the repo. This file is purely a cost optimization; WordPress
   slugs remain the correctness source of truth, so a lost state file can at
   worst cause a duplicate *generation*, never a duplicate *published post*.

## Scheduling (GitHub Actions)

One workflow per niche (`entertainment.yml`, `sports.yml`, `finance.yml`) calls
the reusable `_publish.yml`, which installs deps, runs the script, and commits
the updated `state/` file back to the repo. Crons are defined in **UTC**; the
script's own commercial-hours gate handles the 01:00–05:00 pause, so cron may
fire 24/7 (off-hours runs just exit 0). Cadence: entertainment every 2h, sports
every 1h, finance every 4h. Add every `.env` value above as a repository Secret.

## Key behavioural differences between scripts

| Script | WP Blog env prefix | Telegram token | Cron cadence | WP category logic |
|---|---|---|---|---|
| `pw_trends.py` | `WP_BLOG_FOFOCANDO_*` | `TELEGRAM_TOKEN` | every 2h | 9 (Novelas) if title matches the novela regex, else 6 (Notícias) |
| `pw_trends_sports.py` | `WP_BLOG_SPORT_*` | `TELEGRAM_SPORT_TOKEN` | every 1h | Category 1 always |
| `pw_trends_finance.py` | `WP_BLOG_FINANCE_*` | `TELEGRAM_FINANCE_TOKEN` | every 4h | Category 1 always |

The AI provider and models are shared across niches via the `AI_PROVIDER`,
`AI_MODEL`, and `REVIEW_MODEL` env vars (defaults: `anthropic`,
`claude-sonnet-4-6`, `claude-sonnet-4-6`). A niche can override by passing the
argument explicitly to `NicheConfig`.

Each niche also sets its own **editorial persona** — the voice the generator
writes in — via the `persona` field on `NicheConfig`. `scrapper_base` exports
`PERSONA_ENTERTAINMENT` (the default), `PERSONA_SPORTS`, and `PERSONA_FINANCE`;
the sports/finance entry scripts pass theirs explicitly while entertainment uses
the default. Each persona shares a common tail that forbids the voice from
overriding the SEO/AdSense/HTML/JSON rules or inventing facts. A niche can supply
any custom `persona=` string.

## Known skipped sources

Domains in `BLOCKED_DOMAINS` (currently `nsctotal.com.br`) are rejected in
`_download_for_ref`, causing the script to try the next `ref` (source article).
