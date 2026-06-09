import asyncio
import re
import json
import base64
import os
import random
import logging
from logging.handlers import RotatingFileHandler
import requests
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from datetime import time as dtime
from typing import Callable
from urllib.parse import urljoin

import anthropic
from google import genai
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

BLOCKED_DOMAINS = ['nsctotal.com.br']

# Network / retry tuning
REQUEST_TIMEOUT = 30          # seconds for every outbound HTTP call
RETRY_COUNT = 5               # attempts per trend row / per publish
MAX_TREND_ROWS = 25           # upper bound on trend table rows to probe
FAILURE_BACKOFF_SECONDS = 60  # pause after a failed publish before moving on
EMPTY_BATCH_BACKOFF_SECONDS = 300  # pause before retrying when a batch comes back empty

IMAGE_SELECTORS = [
    ".content-media-container figure img",
    ".article__content--body figure picture img",
    ".body-container figure picture img",
    "figure img",
    "picture img",
    "section img",
    "article img",
    "div a img",
]

# Lazily-reusable AI clients (instantiation does TLS/auth setup, so do it once).
_anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
_gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

logger = logging.getLogger("scrapper")


class BlockedDomainError(Exception):
    """Source article lives on a domain we explicitly reject."""


class ImageNotFoundError(Exception):
    """No cover image matched any of the configured selectors."""


class WordPressError(Exception):
    """A WordPress REST call returned a non-success status."""


_PROMPT_ADSENSE_RULES = """
De acordo com com as políticas do Google Adsense, o conteúdo superficial com pouco ou nenhum valor agregado são conteúdos de baixa qualidade como por exemplo:

Páginas afiliadas sem valor agregado:

A afiliação sem valor agregado é a prática de publicar conteúdo com links de produtos afiliados em que as descrições e avaliações são copiadas diretamente do comerciante original, sem nenhum conteúdo original ou valor agregado.

As páginas afiliadas podem ser consideradas sem valor agregado se fizerem parte de um programa que distribui o próprio conteúdo em uma rede de afiliados sem oferecer valor adicional. Muitas vezes, eles aparentam ser sites comuns ou criados com base em modelos que exibem conteúdo duplicado igual ou similar dentro do mesmo site ou em diversos domínios ou idiomas. Se uma página de resultados da Pesquisa retorna vários desses sites, todos com o mesmo conteúdo, as páginas afiliadas sem valor agregado tornam a experiência do usuário frustrante.

Nem todo site que participa de um programa de afiliados é um afiliado sem valor agregado. Bons sites afiliados agregam valor oferecendo conteúdo ou recursos significativos. Exemplos de boas páginas afiliadas incluem informações adicionais sobre preço, avaliações originais de produtos, testes e classificações rigorosos, navegação em produtos ou categorias e comparações de produtos.

Conteúdo de outras fontes, por exemplo: conteúdo copiado ou postagens de baixa qualidade em blogs de convidados (raspagem de dados):

A raspagem de dados se refere à prática de copiar conteúdo de outros sites, muitas vezes de forma automatizada, e hospedar esse conteúdo com o objetivo de manipular as classificações de pesquisa. Exemplos de raspagem abusiva de dados:

Republicar conteúdo de outros sites sem adicionar conteúdo original ou valor, ou nem mesmo citar a fonte original
Copiar conteúdo de outros sites, fazer pequenas modificações (por exemplo, usando sinônimos ou técnicas automatizadas) e publicar novamente
Reprodução de feeds de conteúdo de outros sites sem oferecer qualquer tipo de benefício exclusivo ao usuário
Criação de sites voltados à incorporação ou compilação de conteúdo, como vídeos, imagens ou mídias diversas de outros sites, sem acrescentar valor significativo para o usuário


Abuso de Doorways: O abuso de doorways é quando sites ou páginas são criados para a classificação em consultas de pesquisa específicas e semelhantes. Elas levam os usuários a páginas intermediárias que não são tão úteis quanto o destino final. Exemplos de abuso de doorways incluem:

Ter vários sites com pequenas variações no URL e na página inicial para maximizar o alcance em qualquer consulta específica
Ter várias páginas ou muitos nomes de domínio segmentados por regiões ou cidades específicas que direcionam os usuários a uma página
Gerar páginas para direcionar os visitantes à parte utilizável ou relevante de um site
Criar páginas consideravelmente semelhantes e mais próximas aos resultados de pesquisa do que, por exemplo, uma hierarquia claramente definida e navegável

Essas técnicas não oferecem aos usuários conteúdo substancialmente exclusivo ou importante, além de violar as políticas contra spam.
"""


@dataclass(frozen=True)
class NicheConfig:
    wp_url: str
    wp_user: str
    wp_pass: str
    telegram_token: str
    telegram_chat_id: str
    trends_url: str
    batch_size: int
    post_interval_seconds: int
    prompt_niche: str
    get_categories: Callable[[dict], list]
    ai_provider: str  # "gemini" or "anthropic"
    ai_model: str
    author_ids: list = field(default_factory=lambda: [2, 3, 4, 5, 6, 7, 8, 9, 10])
    max_tokens: int = 8192


def _configure_logging(niche: str) -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%d/%m/%Y %H:%M:%S")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    Path("logs").mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        f"logs/{niche}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def _build_prompt(niche: str, href: str, href2: str, href3: str, links_wordpress: list) -> str:
    return f"""
        {_PROMPT_ADSENSE_RULES}

        Baseado nesses conceitos, crie um post para blog de noticias de {niche} com conteúdo original, relevante, magnético e exclusivo. O conteúdo deve possuir um texto já em html (apenas o corpo do texto para inserir no editor do Wordpress) com retorno em formato json (apenas os campos 'title', 'slug', 'meta_description', 'keyword' e 'body'), com no minimo 600 palavras e links externos (deve possuir links de saída!). Os links internos devem vir ao final do post em tópicos com o título 'Outras noticias que podem te interessar:' e devem vir dos seguintes links: {links_wordpress}. Esses links internos devem se limitar até 4 links.
        O campo 'title' deve conter no máximo 57 caracteres.
        A keyword deve ser a mais importante do texto, e deve ser usada no título, no slug, na meta description e no corpo do texto.
        A keyword, ou sinônimos dela, devem ser exibidos em até 70% dos subtítulos H2 e H3 (não em todos os subtítulos!).
        O campo 'meta description' deve conter no mínimo 120 caracteres e no máximo 146 caracteres.
        Todo o texto deve respeitar as regras de SEO e as regras do Google Adsense, se baseando nas seguintes noticias dos links: {href} {href2} {href3}. Os links externos também devem ser baseados nesses links.
        O conteúdo html no campo 'body' deve ser inserido dentro de uma tag <article>. E dentro da tag <article> não deve conter a tag <h1> e nem deve haver sugestões para o autor do post no meio do texto.
        O primeiro parágrafo não deve conter a tag <h2> e deve ser uma introdução ao assunto do post, sem repetir o título do post.
        A idéia é que o texto já esteja pronto para ser publicado no Wordpress.
        """


def _parse_ai_json(text: str) -> dict:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"Nenhum JSON encontrado na resposta da IA: {text[:300]}")


def _wp_auth_headers(wp_user: str, wp_pass: str) -> dict:
    token = base64.b64encode(f'{wp_user}:{wp_pass}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def _is_commercial_hour() -> bool:
    now = datetime.now().time()
    start = dtime(1, 0)   # 01:00
    end = dtime(5, 0)     # 05:00

    if start <= now <= end:
        return False
    else:
        return True


async def _get_image_element(page):
    for selector in IMAGE_SELECTORS:
        img = await page.query_selector(selector)
        if img:
            return img
    raise ImageNotFoundError("Nenhuma imagem encontrada com os seletores fornecidos.")


async def _download_cover_image(page, href: str) -> tuple[str, str]:
    await page.goto(href, wait_until="domcontentloaded", timeout=120000)
    title = await page.locator('h1').first.text_content()
    safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', title)

    img_locator = await _get_image_element(page)
    srcset = await img_locator.get_attribute("srcset")
    if srcset:
        img_src = urljoin(href, srcset.split(",")[-1].split()[0].strip())
    else:
        raw_src = await img_locator.get_attribute('src')
        img_src = urljoin(href, raw_src.replace('x240', 'x720'))

    img_path = f'covers/{safe_title}.jpg'
    response = requests.get(img_src, timeout=REQUEST_TIMEOUT)
    if response.status_code == 200:
        with open(img_path, 'wb') as f:
            f.write(response.content)
    else:
        logger.warning("Falha ao baixar a imagem: Status %s", response.status_code)

    return img_path, safe_title


async def _scrape_trend(config: NicheConfig, link: int, ref: int) -> tuple[str, str, str, str, str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()

        try:
            await page.goto(config.trends_url)
            await page.click(f'//*[@id="trend-table"]/div[1]/table/tbody[2]/tr[{link}]')

            href = await page.locator('.jDtQ5 a').first.get_attribute('href')
            href2 = await page.locator('.jDtQ5 a').nth(1).get_attribute('href')
            href3 = await page.locator('.jDtQ5 a').nth(2).get_attribute('href')

            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })

            link_image = [href, href2, href3][ref - 1]

            for domain in BLOCKED_DOMAINS:
                if link_image and domain in link_image:
                    raise BlockedDomainError(f'Link {link} - {link_image} é de um domínio bloqueado.')

            image_path, safe_title = await _download_cover_image(page, link_image)
        finally:
            await browser.close()

    return href, href2, href3, image_path, safe_title


async def _generate_content(config: NicheConfig, href: str, href2: str, href3: str, links_wordpress: list) -> dict:
    prompt = _build_prompt(config.prompt_niche, href, href2, href3, links_wordpress)

    if config.ai_provider == "anthropic":
        async with _anthropic_client.messages.stream(
            model=config.ai_model,
            max_tokens=config.max_tokens,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            response = await stream.get_final_message()
        text = next(b.text for b in response.content if b.type == "text")
        return _parse_ai_json(text)

    response = _gemini_client.models.generate_content(model=config.ai_model, contents=prompt)
    return _parse_ai_json(response.text)


def _recover_wp_data(config: NicheConfig) -> tuple[list, set]:
    headers = _wp_auth_headers(config.wp_user, config.wp_pass)

    posts_resp = requests.get(f'{config.wp_url}/wp-json/wp/v2/posts', headers=headers, timeout=REQUEST_TIMEOUT)
    cats_resp = requests.get(f'{config.wp_url}/wp-json/wp/v2/categories', headers=headers, timeout=REQUEST_TIMEOUT)

    if posts_resp.status_code != 200:
        logger.warning("Erro ao recuperar posts do WordPress: %s", posts_resp.text)
    if cats_resp.status_code != 200:
        logger.warning("Erro ao recuperar categorias do WordPress: %s", cats_resp.text)

    posts = posts_resp.json() if posts_resp.status_code == 200 else []
    cats = cats_resp.json() if cats_resp.status_code == 200 else []

    links = [p['link'] for p in posts] + [c['link'] for c in cats]
    slugs = {p['slug'] for p in posts}

    return links, slugs


def _upload_image(config: NicheConfig, image_path: str, match: dict) -> int:
    headers = _wp_auth_headers(config.wp_user, config.wp_pass)

    with open(image_path, 'rb') as img:
        image_data = img.read()

    media_resp = requests.post(
        f'{config.wp_url}/wp-json/wp/v2/media',
        headers={**headers, 'Content-Disposition': 'attachment; filename="imagem.jpg"', 'Content-Type': 'image/jpeg'},
        data=image_data,
        timeout=REQUEST_TIMEOUT,
    )
    if media_resp.status_code != 201:
        raise WordPressError(f'Erro ao fazer upload da imagem: {media_resp.text}')

    media_id = media_resp.json()['id']

    meta_resp = requests.put(
        f"{config.wp_url}/wp-json/wp/v2/media/{media_id}",
        headers={**headers, 'Content-Type': 'application/json'},
        json={
            'title': match['title'],
            'alt_text': match['title'],
            'caption': match['title'],
            'description': match['meta_description'],
        },
        timeout=REQUEST_TIMEOUT,
    )
    if meta_resp.status_code != 200:
        logger.warning("Erro ao atualizar metadados da mídia %s: %s", media_id, meta_resp.text)

    return media_id


def _create_post(config: NicheConfig, media_id: int, match: dict, trend_index: int) -> str:
    headers = _wp_auth_headers(config.wp_user, config.wp_pass)

    post_resp = requests.post(
        f'{config.wp_url}/wp-json/wp/v2/posts',
        headers={**headers, 'Content-Type': 'application/json'},
        json={
            'title': match['title'],
            'content': match['body'],
            'status': 'publish',
            'featured_media': media_id,
            'slug': match['slug'],
            'yoast_description': match['meta_description'],
            'yoast_keyword': match['keyword'],
            'categories': config.get_categories(match),
            'author': random.choice(config.author_ids),
        },
        timeout=REQUEST_TIMEOUT,
    )

    if post_resp.status_code == 201:
        msg = f'✅ Post criado com sucesso! Link {trend_index}: {post_resp.json()["link"]}'
        logger.info(msg)
    else:
        msg = f'❌ Erro ao criar o post (Link {trend_index}): {post_resp.text}'
        logger.error(msg)

    return msg


def _send_telegram(config: NicheConfig, message: str) -> None:
    resp = requests.post(
        f'https://api.telegram.org/bot{config.telegram_token}/sendMessage',
        data={'chat_id': config.telegram_chat_id, 'text': message},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("Falha ao enviar mensagem Telegram: %s", resp.text)


def _remove_image(safe_title: str) -> None:
    path = os.path.join("covers", f'{safe_title}.jpg')
    if os.path.exists(path):
        os.remove(path)


def _run_task(config: NicheConfig, image_path: str, safe_title: str, match: dict, trend_index: int) -> None:
    media_id = _upload_image(config, image_path, match)
    log_message = _create_post(config, media_id, match, trend_index)
    _remove_image(safe_title)
    _send_telegram(config, log_message)


async def _load_batch(config: NicheConfig) -> list:
    links_wordpress, slugs = _recover_wp_data(config)
    batch = []
    link = 1
    ref = 1
    attempts = 0

    while len(batch) < config.batch_size and link <= MAX_TREND_ROWS:
        try:
            logger.info("Tentando carregar o link %s, ref: %s", link, ref)
            href, href2, href3, image_path, safe_title = await _scrape_trend(config, link, ref)
            match = await _generate_content(config, href, href2, href3, links_wordpress)

            if match['slug'] in slugs:
                logger.info('O slug "%s" já existe no WordPress. Pulando.', match["slug"])
                link += 1
                ref = 1
                attempts = 0
                continue

            batch.append({
                'link': link,
                'image_path': image_path,
                'safe_title': safe_title,
                'match': match,
            })
            link += 1
            ref = 1
            attempts = 0

        except Exception as e:
            attempts += 1
            logger.warning("Erro ao carregar link %s (ref %s), tentativa %s/%s: %s",
                           link, ref, attempts, RETRY_COUNT, e)
            if attempts >= RETRY_COUNT:
                attempts = 0
                if ref < 3:
                    ref += 1
                else:
                    ref = 1
                    link += 1

    if link > MAX_TREND_ROWS and len(batch) < config.batch_size:
        logger.error("Tabela de trends esgotada (%s linhas) com apenas %s/%s posts carregados.",
                     MAX_TREND_ROWS, len(batch), config.batch_size)

    return batch


async def run_niche(config: NicheConfig):
    _configure_logging(config.prompt_niche)
    Path("covers").mkdir(exist_ok=True)
    batch = []
    index = 0

    while True:
        if not _is_commercial_hour():
            logger.info(f"❌ Fora do horário comercial (forbid to run 01:00-05:00). Em {datetime.now()}")
            await asyncio.sleep(30)
            continue

        if index >= len(batch):
            index = 0
            logger.info("Carregando novo lote de %s posts.", config.batch_size)
            batch = await _load_batch(config)
            if not batch:
                logger.warning("Lote vazio. Aguardando %ss antes de tentar novamente.",
                               EMPTY_BATCH_BACKOFF_SECONDS)
                await asyncio.sleep(EMPTY_BATCH_BACKOFF_SECONDS)
                continue

        link_info = batch[index]

        if not Path(link_info['image_path']).exists():
            logger.warning("Imagem não encontrada: %s. Pulando.", link_info['image_path'])
            index += 1
            continue

        published = False
        for attempt in range(RETRY_COUNT):
            try:
                if not _is_commercial_hour():
                    logger.info("Saiu do horário comercial durante a execução.")
                    break
                logger.info("Publicando %s/%s - %s", index + 1, len(batch), link_info['image_path'])
                _run_task(config, link_info['image_path'], link_info['safe_title'],
                          link_info['match'], link_info['link'])
                published = True
                break
            except Exception as e:
                logger.error("run_task() falhou na tentativa %s/%s: %s", attempt + 1, RETRY_COUNT, e)

        index += 1
        if published:
            await asyncio.sleep(config.post_interval_seconds)
        else:
            # Don't hammer a flaky WordPress endpoint — back off before the next item.
            await asyncio.sleep(FAILURE_BACKOFF_SECONDS)
