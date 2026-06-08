import asyncio
import os
from dotenv import load_dotenv
from scrapper_base import NicheConfig, run_niche

load_dotenv()

config = NicheConfig(
    wp_url=os.getenv("WP_BLOG_FINANCE_URL"),
    wp_user=os.getenv("WP_BLOG_FINANCE_USER"),
    wp_pass=os.getenv("WP_BLOG_FINANCE_PASS"),
    telegram_token=os.getenv("TELEGRAM_FINANCE_TOKEN"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    trends_url=os.getenv("TRENDS_URL_FINANCE"),
    batch_size=5,
    post_interval_seconds=4 * 60 * 60,
    prompt_niche="finanças",
    get_categories=lambda match: [1],
    ai_provider="anthropic",
    ai_model="claude-opus-4-6",
)

if __name__ == "__main__":
    asyncio.run(run_niche(config))
