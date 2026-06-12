import asyncio
import os
import re
from dotenv import load_dotenv
from scrapper_base import NicheConfig, run_once

load_dotenv()

_NOVELA_TERMS = re.compile(r'Vale Tudo|A Viagem|Guerreiros do Sol|Garota do Momento')

config = NicheConfig(
    wp_url=os.getenv("WP_BLOG_FOFOCANDO_URL"),
    wp_user=os.getenv("WP_BLOG_FOFOCANDO_USER"),
    wp_pass=os.getenv("WP_BLOG_FOFOCANDO_PASS"),
    telegram_token=os.getenv("TELEGRAM_TOKEN"),
    telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    trends_url=os.getenv("TRENDS_URL_ENTERTEINMENT"),
    prompt_niche="entretenimento",
    get_categories=lambda match: [9 if _NOVELA_TERMS.search(match['title']) else 6],
)

if __name__ == "__main__":
    asyncio.run(run_once(config))
