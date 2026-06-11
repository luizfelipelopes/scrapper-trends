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
from zoneinfo import ZoneInfo

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
RETRY_COUNT = 5               # attempts per publish
MAX_TREND_ROWS = 25           # upper bound on trend table rows to probe

# Local state (committed back to the repo by the cron workflow) keeps a rolling
# log of source articles we've already published so we never spend an AI call
# re-generating a trend that's already live. WordPress slugs remain the
# correctness source of truth; this file is purely a cost optimization.
STATE_DIR = "state"
STATE_HISTORY_LIMIT = 300     # cap published-href history so the file stays small

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
    prompt_niche: str
    get_categories: Callable[[dict], list]
    ai_provider: str  # "gemini" or "anthropic"
    ai_model: str
    author_ids: list = field(default_factory=lambda: [2, 3, 4, 5, 6, 7, 8, 9, 10])
    max_tokens: int = 8192
    # IANA timezone the commercial-hours window is evaluated in. Defaults to
    # Brazil so the 01:00-05:00 pause is correct even when cron runs in UTC.
    timezone: str = "America/Sao_Paulo"
    # Soft content review (LLM-as-judge). When enabled, a flagged post is still
    # created in WordPress but as a *draft* for human review instead of being
    # published live (soft blocking). The reviewer always runs on Anthropic; if
    # no ANTHROPIC_API_KEY is set the step fails open (publishes as usual).
    review_enabled: bool = True
    # Reviewer model. Empty falls back to ai_model. A cheaper/faster model is a
    # good fit for a judge that only emits a short verdict.
    review_model: str = "claude-haiku-4-5-20251001"


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


def _is_commercial_hour(tz: str) -> bool:
    """Whether we're allowed to publish right now (we pause 01:00-05:00 local)."""
    now = datetime.now(ZoneInfo(tz)).time()
    start = dtime(1, 0)   # 01:00
    end = dtime(5, 0)     # 05:00

    return not (start <= now <= end)


def _state_path(niche: str) -> Path:
    return Path(STATE_DIR) / f"{niche}.json"


def _load_published_hrefs(niche: str) -> list[str]:
    path = _state_path(niche)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("published", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Estado em %s ilegível (%s). Recomeçando do zero.", path, e)
        return []


def _save_published_hrefs(niche: str, hrefs: list[str]) -> None:
    path = _state_path(niche)
    path.parent.mkdir(exist_ok=True)
    trimmed = hrefs[-STATE_HISTORY_LIMIT:]
    path.write_text(json.dumps({"published": trimmed}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


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


async def _extract_source_hrefs(page, trends_url: str, link: int) -> tuple[str, str, str]:
    """Open the trends page, click trend row `link`, return its 3 source article hrefs."""
    await page.goto(trends_url)
    await page.click(f'//*[@id="trend-table"]/div[1]/table/tbody[2]/tr[{link}]')

    href = await page.locator('.jDtQ5 a').first.get_attribute('href')
    href2 = await page.locator('.jDtQ5 a').nth(1).get_attribute('href')
    href3 = await page.locator('.jDtQ5 a').nth(2).get_attribute('href')
    return href, href2, href3


async def _download_for_ref(page, hrefs: tuple[str, str, str], ref: int, link: int) -> tuple[str, str]:
    """Download the cover image from the `ref`-th source article (1-indexed)."""
    link_image = hrefs[ref - 1]

    for domain in BLOCKED_DOMAINS:
        if link_image and domain in link_image:
            raise BlockedDomainError(f'Link {link} - {link_image} é de um domínio bloqueado.')

    return await _download_cover_image(page, link_image)


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


_REVIEW_PROMPT = """
Você é um editor revisor de um blog de notícias. Avalie criticamente o post gerado
abaixo ANTES da publicação e decida se ele pode ir ao ar como está.

Critérios:
- Consistência interna: o corpo sustenta o que o título promete (sem clickbait enganoso)?
- Plausibilidade factual: há datas, números, nomes ou afirmações que parecem inventados,
  contraditórios ou improváveis?
- Aderência às fontes: o conteúdo é coerente com as notícias de origem informadas?
- Qualidade e políticas: evita conteúdo raso/duplicado e respeita as regras do Google Adsense?
- HTML: o campo body é HTML coerente dentro de <article>, sem <h1> e sem instruções
  para o autor no meio do texto.

Seja criterioso, mas sinalize apenas problemas REAIS e relevantes — não invente defeitos.

Notícias de origem: {href} {href2} {href3}

Post gerado:
title: {title}
meta_description: {meta_description}
keyword: {keyword}
body:
{body}

Responda APENAS com um JSON, sem texto fora dele, com os campos:
- "approved": true se o post pode ser publicado como está; false se precisa de revisão humana.
- "issues": lista de strings descrevendo os problemas encontrados (lista vazia se nenhum).
"""


async def _review_content(config: NicheConfig, match: dict, hrefs: tuple[str, str, str]) -> dict:
    """LLM-as-judge pass over a generated post (soft gate).

    Returns ``{"approved": bool, "issues": list[str]}``. The check is grounded
    on the same source URLs the generator saw, so it catches title/body
    mismatch, internal inconsistency, implausible claims and policy/HTML issues
    — it is not a real-world fact-checker.

    Fails **open** (``approved=True``) whenever review is disabled, the
    Anthropic client is unavailable, or the call/parse errors, so a reviewer
    outage never blocks the pipeline.
    """
    if not config.review_enabled:
        return {"approved": True, "issues": []}
    if _anthropic_client is None:
        logger.warning("Revisão desativada: ANTHROPIC_API_KEY ausente. Publicando sem revisar.")
        return {"approved": True, "issues": []}

    prompt = _REVIEW_PROMPT.format(
        href=hrefs[0], href2=hrefs[1], href3=hrefs[2],
        title=match.get('title', ''),
        meta_description=match.get('meta_description', ''),
        keyword=match.get('keyword', ''),
        body=match.get('body', ''),
    )

    try:
        response = await _anthropic_client.messages.create(
            model=config.review_model or config.ai_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        verdict = _parse_ai_json(text)
    except Exception as e:
        logger.warning("Revisão falhou (%s). Publicando mesmo assim (fail-open).", e)
        return {"approved": True, "issues": []}

    approved = bool(verdict.get("approved", True))
    issues = verdict.get("issues") or []
    if approved:
        logger.info("Revisão aprovou o post '%s'.", match.get('title', ''))
    else:
        logger.warning("Revisão sinalizou o post '%s' para revisão humana: %s",
                       match.get('title', ''), issues)
    return {"approved": approved, "issues": issues}


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


def _create_post(config: NicheConfig, media_id: int, match: dict, trend_index: int,
                 review: dict | None = None) -> str:
    headers = _wp_auth_headers(config.wp_user, config.wp_pass)

    # Soft blocking: a post the reviewer flagged is still created, but as a
    # draft so a human can vet it before it goes live.
    flagged = bool(review) and not review.get("approved", True)
    status = 'draft' if flagged else 'publish'

    post_resp = requests.post(
        f'{config.wp_url}/wp-json/wp/v2/posts',
        headers={**headers, 'Content-Type': 'application/json'},
        json={
            'title': match['title'],
            'content': match['body'],
            'status': status,
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
        link = post_resp.json()["link"]
        if flagged:
            issues = "; ".join(review.get("issues") or []) or "motivo não especificado"
            msg = (f'⚠️ Post salvo como RASCUNHO para revisão humana (Link {trend_index}): '
                   f'{link}\nProblemas apontados: {issues}')
            logger.warning(msg)
        else:
            msg = f'✅ Post criado com sucesso! Link {trend_index}: {link}'
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


def _run_task(config: NicheConfig, image_path: str, safe_title: str, match: dict, trend_index: int,
              review: dict | None = None) -> None:
    media_id = _upload_image(config, image_path, match)
    log_message = _create_post(config, media_id, match, trend_index, review)
    _remove_image(safe_title)
    _send_telegram(config, log_message)


async def _find_publishable(config: NicheConfig, links_wordpress: list, slugs: set,
                            published_hrefs: list[str]) -> dict | None:
    """Walk the trend table and return the first genuinely new, publishable post.

    Trends already in `published_hrefs` are skipped before any AI call. Trends
    whose generated slug already exists in WordPress are appended to
    `published_hrefs` (so we never re-generate them) and skipped. Returns the
    ready-to-publish item, or None if nothing new was found.
    """
    seen = set(published_hrefs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        try:
            for link in range(1, MAX_TREND_ROWS + 1):
                try:
                    hrefs = await _extract_source_hrefs(page, config.trends_url, link)
                except Exception as e:
                    logger.warning("Falha ao ler a linha de trend %s: %s. Próxima.", link, e)
                    continue

                if hrefs[0] in seen:
                    logger.info("Trend %s (%s) já publicada. Pulando.", link, hrefs[0])
                    continue

                for ref in range(1, len(hrefs) + 1):
                    try:
                        logger.info("Tentando trend %s, ref %s", link, ref)
                        image_path, safe_title = await _download_for_ref(page, hrefs, ref, link)
                        match = await _generate_content(config, *hrefs, links_wordpress)
                    except Exception as e:
                        logger.warning("Erro na trend %s (ref %s): %s", link, ref, e)
                        continue

                    if match['slug'] in slugs:
                        logger.info('Slug "%s" já existe no WordPress. Marcando trend %s como publicada.',
                                    match['slug'], link)
                        published_hrefs.append(hrefs[0])
                        seen.add(hrefs[0])
                        _remove_image(safe_title)
                        break  # this trend is spent; move to the next row

                    review = await _review_content(config, match, hrefs)

                    return {
                        'link': link,
                        'source_href': hrefs[0],
                        'image_path': image_path,
                        'safe_title': safe_title,
                        'match': match,
                        'review': review,
                    }
        finally:
            await browser.close()

    return None


async def run_once(config: NicheConfig) -> None:
    """Publish at most one post, then return. Designed to be driven by cron."""
    _configure_logging(config.prompt_niche)
    Path("covers").mkdir(exist_ok=True)

    if not _is_commercial_hour(config.timezone):
        logger.info("❌ Fora do horário comercial (01:00-05:00 %s). Encerrando.", config.timezone)
        return

    links_wordpress, slugs = _recover_wp_data(config)
    published_hrefs = _load_published_hrefs(config.prompt_niche)

    item = await _find_publishable(config, links_wordpress, slugs, published_hrefs)
    if item is None:
        logger.warning("Nenhuma trend nova publicável encontrada nesta execução.")
        _save_published_hrefs(config.prompt_niche, published_hrefs)
        return

    for attempt in range(RETRY_COUNT):
        try:
            logger.info("Publicando trend %s - %s", item['link'], item['image_path'])
            _run_task(config, item['image_path'], item['safe_title'], item['match'],
                      item['link'], item.get('review'))
            published_hrefs.append(item['source_href'])
            _save_published_hrefs(config.prompt_niche, published_hrefs)
            return
        except Exception as e:
            logger.error("run_task() falhou na tentativa %s/%s: %s", attempt + 1, RETRY_COUNT, e)

    # Persist any slug-exists discoveries even though this publish failed, then
    # signal failure so the cron run is visibly red.
    _save_published_hrefs(config.prompt_niche, published_hrefs)
    raise SystemExit(f"Falha ao publicar trend {item['link']} após {RETRY_COUNT} tentativas.")
