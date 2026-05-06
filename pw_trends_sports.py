import asyncio
import os
from dotenv import load_dotenv
from scrapper_base import NicheConfig, run_niche

load_dotenv()

config = NicheConfig(
    wp_url=os.getenv("WP_BLOG_SPORT_URL"),
    wp_user=os.getenv("WP_BLOG_SPORT_USER"),
    wp_pass=os.getenv("WP_BLOG_SPORT_PASS"),
    telegram_token=os.getenv("TELEGRAM_SPORT_TOKEN"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    trends_url=os.getenv("TRENDS_URL_SPORTS"),
    batch_size=2,
    post_interval_seconds=60 * 60,
    prompt_niche="esportes",
    get_categories=lambda match: [1],
    ai_provider="gemini",
    ai_model=os.getenv("GEMINI_API_GPT_2_5"),
)

if __name__ == "__main__":
    asyncio.run(run_niche(config))
