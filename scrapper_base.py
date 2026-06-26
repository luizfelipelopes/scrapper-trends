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
from google.genai import types
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# AI model selection. Driven by env (shared across niches, like the API keys);
# these literals are the fallback when the var is unset. A niche may still
# override by passing the argument explicitly to NicheConfig.
DEFAULT_AI_PROVIDER = "anthropic"   # "anthropic" or "gemini"
DEFAULT_AI_MODEL = "claude-sonnet-4-6"
DEFAULT_REVIEW_MODEL = "claude-sonnet-4-6"

BLOCKED_DOMAINS = ['nsctotal.com.br']

# Network / retry tuning
REQUEST_TIMEOUT = 30          # seconds for every outbound HTTP call

# Fallback cover sources, tried (in order) when the source articles yield no
# usable image. Both return properly licensed images — safe for a monetized
# (AdSense) blog: Wikimedia gives the free-licensed lead image of the best
# matching pt.wikipedia article; Openverse aggregates CC/commercial-use images.
WIKIMEDIA_API = "https://pt.wikipedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
# Wikimedia's API policy requires a descriptive, contactable User-Agent.
COVER_API_USER_AGENT = "scrapper-trends/1.0 (https://github.com/luizfelipelopes/scrapper-trends)"
# Browser-like UA for binary image downloads: upload.wikimedia.org and several
# news CDNs reject requests that lack a User-Agent with 403.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
RETRY_COUNT = 5               # attempts per publish
MAX_TREND_ROWS = 25           # upper bound on trend table rows to probe

# Anthropic generation is grounded with the server-side web_search tool, so bound
# how much it can search/loop to keep cost predictable. (Gemini grounds with
# Google Search, which has no equivalent per-call cap; the review is a lighter
# pass and does not search — facts are already verified at generation time.)
GEN_MAX_SEARCHES = 4          # cap web_search calls per generation (Anthropic)
GEN_MAX_CONTINUATIONS = 4     # cap server-tool pause_turn resumes per generation (Anthropic)

# Local state (committed back to the repo by the cron workflow) keeps a rolling
# log of source articles we've already published so we never spend an AI call
# re-generating a trend that's already live. WordPress slugs remain the
# correctness source of truth; this file is purely a cost optimization.
STATE_DIR = "state"
STATE_HISTORY_LIMIT = 900     # cap published-href history so the file stays small
                              # (~3 entries per trend, so ~300 trends remembered)

# Meta tags that declare the article's canonical cover image. Checked before
# the CSS-based <img> fallback below because they point at the real cover (the
# image the publisher chose for social sharing) instead of whatever <img>
# happens to match a selector — logos, avatars, ads, etc.
OG_IMAGE_SELECTORS = [
    'meta[property="og:image"]',
    'meta[property="og:image:url"]',
    'meta[name="og:image"]',
    'meta[name="twitter:image"]',
    'meta[name="twitter:image:src"]',
]

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

# On a 429 the SDK waits out the server's `retry-after` before retrying. The
# input-token limit resets per minute, so allow enough attempts to ride out a
# full window when generation + review land in the same minute.
AI_MAX_RETRIES = 5

# Lazily-reusable AI clients (instantiation does TLS/auth setup, so do it once).
_anthropic_client = (
    anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=AI_MAX_RETRIES)
    if ANTHROPIC_API_KEY else None
)
_gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

logger = logging.getLogger("scrapper")


class BlockedDomainError(Exception):
    """Source article lives on a domain we explicitly reject."""


class ImageNotFoundError(Exception):
    """No cover image matched any of the configured selectors."""


class CoverImageError(Exception):
    """The cover image element had no usable URL or could not be downloaded."""


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


# Editorial-voice block injected into the generation prompt so the article reads
# like a real human columnist instead of a neutral, encyclopedic summary. One per
# niche — each entry script passes the matching persona to NicheConfig. The shared
# tail (_PERSONA_SHARED_TAIL) keeps the "don't break the rules / don't invent
# facts" guardrail identical across niches.
_PERSONA_SHARED_TAIL = """
        Para soar como um autor de verdade:
        - Comece com um gancho que prende o leitor (uma cena, uma pergunta provocativa, uma reação) em vez de uma definição genérica do assunto.
        - Conte o assunto como uma narrativa, com contexto, bastidores e a sua leitura do que aquilo significa — não apenas os fatos secos.
        - Traga detalhes concretos e específicos (nomes, falas, números, momentos marcantes) em vez de afirmações vagas e genéricas.
        - Varie o ritmo das frases e dos parágrafos. Evite jargão de IA e frases-clichê de preenchimento como "em conclusão", "vale ressaltar", "no mundo de hoje", "é importante notar".
        - Cada subtítulo deve avançar a história com um ângulo novo, não apenas reembalar o que já foi dito.

        Importante: este tom não substitui as regras abaixo — o texto deve ter personalidade E cumprir todas as regras de SEO, AdSense, estrutura HTML e JSON descritas a seguir, além de continuar fiel aos fatos das fontes.
        """

PERSONA_ENTERTAINMENT = """
        Escreva como um(a) colunista humano(a) experiente de um portal de entretenimento e cultura pop — alguém que cobre celebridades, novelas, realities e fofocas há anos, conhece os bastidores e tem opinião própria. NÃO escreva como uma enciclopédia, um resumo neutro ou um robô. O texto deve ter voz própria, personalidade e um ponto de vista bem fundamentado.

        Use um tom acessível e envolvente, mas elegante e bem cuidado, como quem domina o assunto e conta a novidade com naturalidade — sem gírias, exageros ou intimidade forçada. Pode se dirigir ao leitor de forma pontual e discreta ("você"), sem bordões nem coloquialismos excessivos. Traga as reações do público e das redes sociais e a sua leitura do que aquilo significa, com leveza e respeito, sem inventar fatos.
        """ + _PERSONA_SHARED_TAIL

PERSONA_SPORTS = """
        Escreva como um(a) cronista esportivo(a) humano(a) e experiente — alguém que acompanha clubes, atletas e competições de perto, entende de tática e vive a emoção do jogo. NÃO escreva como uma enciclopédia, um boletim de resultados ou um robô. O texto deve ter VOZ, paixão e ponto de vista.

        Use um tom envolvente e enérgico, como quem narra e analisa a partida para o torcedor. Traga a emoção do lance, o contexto da competição, o peso do resultado, a reação da torcida e a sua análise do que aquilo significa para o time ou o atleta — sempre fiel aos fatos, sem inventar placares, estatísticas ou declarações.
        """ + _PERSONA_SHARED_TAIL

PERSONA_FINANCE = """
        Escreva como um(a) colunista de economia e finanças humano(a) e experiente — alguém que traduz o mercado para o leitor comum, com clareza e autoridade, sem economês desnecessário. NÃO escreva como uma enciclopédia, um relatório frio ou um robô. O texto deve ter VOZ, clareza e ponto de vista.

        Use um tom confiável, didático e direto, explicando o que o assunto significa no bolso e nas decisões do leitor. Traga contexto de mercado, causas e possíveis consequências, e a sua leitura do cenário — sempre fiel aos fatos, sem inventar números, cotações ou previsões, e deixando claro quando algo é incerto.
        """ + _PERSONA_SHARED_TAIL


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
    # Editorial voice injected into the generation prompt. Defaults to the
    # entertainment columnist persona; sports/finance pass PERSONA_SPORTS /
    # PERSONA_FINANCE.
    persona: str = PERSONA_ENTERTAINMENT
    # AI provider/model default from env (AI_PROVIDER / AI_MODEL); a niche may
    # override by passing them explicitly. "gemini" or "anthropic".
    ai_provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", DEFAULT_AI_PROVIDER))
    ai_model: str = field(default_factory=lambda: os.getenv("AI_MODEL", DEFAULT_AI_MODEL))
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
    # Reviewer model (env REVIEW_MODEL). Empty falls back to ai_model. The review
    # is a light pass (cover-image relevance via vision + a glaring-error sanity
    # check, no web search); a capable vision model like Sonnet handles it well.
    review_model: str = field(default_factory=lambda: os.getenv("REVIEW_MODEL", DEFAULT_REVIEW_MODEL))


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


def _build_prompt(niche: str, persona: str, href: str, href2: str, href3: str, links_wordpress: list) -> str:
    return f"""
        {_PROMPT_ADSENSE_RULES}

        {persona}

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


def _new_source_hrefs(hrefs: tuple[str, ...]) -> list[str]:
    """The non-empty extracted source hrefs of a trend (its dedup keys)."""
    return [h for h in hrefs if h]


def _matching_published_href(hrefs: tuple[str, ...], seen: set) -> str | None:
    """First extracted href that is already published (in `seen`), else None.

    All of a trend's source articles are checked — not just the first — because
    a trend whose post is already live can resurface with its sources listed in
    a different order.
    """
    return next((h for h in _new_source_hrefs(hrefs) if h in seen), None)


async def _get_image_element(page):
    for selector in IMAGE_SELECTORS:
        img = await page.query_selector(selector)
        if img:
            return img
    raise ImageNotFoundError("Nenhuma imagem encontrada com os seletores fornecidos.")


async def _get_og_image_url(page, href: str) -> str | None:
    """Return the cover URL declared in og:image/twitter:image, or None."""
    for selector in OG_IMAGE_SELECTORS:
        meta = await page.query_selector(selector)
        if not meta:
            continue
        content = await meta.get_attribute("content")
        if content and content.strip():
            return urljoin(href, content.strip())
    return None


async def _resolve_cover_image_url(page, href: str) -> str:
    """Resolve the cover image URL: og:image first, CSS <img> selectors as fallback."""
    og_url = await _get_og_image_url(page, href)
    if og_url:
        return og_url

    img_locator = await _get_image_element(page)
    srcset = await img_locator.get_attribute("srcset")
    if srcset:
        return urljoin(href, srcset.split(",")[-1].split()[0].strip())

    raw_src = await img_locator.get_attribute('src')
    if not raw_src:
        raise CoverImageError(f"Imagem de capa sem URL utilizável em {href}.")
    return urljoin(href, raw_src.replace('x240', 'x720'))


def _safe_title(text: str) -> str:
    """Filesystem-safe slug for a cover filename."""
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', text or 'cover')


def _download_image_to_cover(img_src: str, safe_title: str) -> str:
    """Download `img_src` and persist it under covers/. Returns the file path.

    A browser-like User-Agent is mandatory: upload.wikimedia.org (and several
    news CDNs) answer UA-less requests with 403.
    """
    response = requests.get(img_src, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": BROWSER_USER_AGENT})
    if response.status_code != 200:
        raise CoverImageError(
            f"Falha ao baixar a imagem ({img_src}): status {response.status_code}."
        )

    # Reject formats WordPress won't accept (e.g. SVG logos from Wikimedia) so
    # the fallback chain tries the next source instead of failing at upload.
    if _sniff_raster_media_type(response.content) is None:
        raise CoverImageError(f"Formato de imagem não suportado em {img_src}.")

    img_path = f'covers/{safe_title}.jpg'
    with open(img_path, 'wb') as f:
        f.write(response.content)
    return img_path


async def _download_cover_image(page, href: str) -> tuple[str, str]:
    await page.goto(href, wait_until="domcontentloaded", timeout=120000)
    title = await page.locator('h1').first.text_content()
    safe_title = _safe_title(title)

    img_src = await _resolve_cover_image_url(page, href)
    return _download_image_to_cover(img_src, safe_title), safe_title


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


def _is_landscape(width, height) -> bool:
    """True if the image is wider than tall (landscape hero), per its dimensions.

    Unknown dimensions return False so a candidate with no size metadata is not
    mistaken for a landscape.
    """
    try:
        return int(width) > int(height)
    except (TypeError, ValueError):
        return False


def _fetch_wikimedia_cover(keyword: str) -> tuple[str, str]:
    """Landscape lead image of the best-matching pt.wikipedia article (free-licensed).

    pageimages can't filter by orientation, but ``piprop=original`` returns the
    image dimensions, so a portrait lead image is rejected (the chain then
    tries Openverse). ``gsrlimit`` is widened so a landscape can be found among
    the top search matches rather than only the single best one.
    """
    params = {
        "action": "query", "format": "json",
        "generator": "search", "gsrsearch": keyword, "gsrlimit": 5,
        "prop": "pageimages", "piprop": "original", "pilicense": "free",
    }
    resp = requests.get(WIKIMEDIA_API, params=params, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": COVER_API_USER_AGENT})
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page in pages.values():
        original = page.get("original") or {}
        if original.get("source") and _is_landscape(original.get("width"), original.get("height")):
            safe_title = _safe_title(keyword)
            return _download_image_to_cover(original["source"], safe_title), safe_title
    raise CoverImageError(f"Wikimedia não retornou imagem paisagem para '{keyword}'.")


def _fetch_openverse_cover(keyword: str) -> tuple[str, str]:
    """First commercial-use (CC) landscape image matching `keyword` from Openverse."""
    params = {"q": keyword, "license_type": "commercial",
              "aspect_ratio": "wide", "page_size": 1}
    resp = requests.get(OPENVERSE_API, params=params, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": COVER_API_USER_AGENT})
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if results and results[0].get("url"):
        safe_title = _safe_title(keyword)
        return _download_image_to_cover(results[0]["url"], safe_title), safe_title
    raise CoverImageError(f"Openverse não retornou imagem paisagem para '{keyword}'.")


async def _acquire_cover_image(page, hrefs: tuple[str, str, str], keyword: str,
                               link: int) -> tuple[str, str]:
    """Cover image via fallback chain: source articles → Wikimedia → Openverse.

    The source articles are tried first (og:image, then CSS <img> selectors).
    If none yield a usable image, fall back to the keyword-based, properly
    licensed sources. Raises CoverImageError only if every source fails.
    """
    for ref in range(1, len(hrefs) + 1):
        try:
            return await _download_for_ref(page, hrefs, ref, link)
        except Exception as e:
            logger.warning("Capa via fonte falhou (trend %s, ref %s): %s", link, ref, e)

    for name, fetch in (("Wikimedia", _fetch_wikimedia_cover),
                        ("Openverse", _fetch_openverse_cover)):
        try:
            logger.info("Buscando capa em %s para '%s'", name, keyword)
            return fetch(keyword)
        except Exception as e:
            logger.warning("Capa via %s falhou para '%s': %s", name, keyword, e)

    raise CoverImageError(f"Nenhuma capa encontrada para a trend {link}.")


# Appended to the generation prompt on the Anthropic path, where the web_search
# tool is available, so the article is grounded in verified facts from the start.
# Provider-neutral wording: Anthropic exposes `web_search`, Gemini grounds with
# Google Search — both satisfy this instruction.
_GEN_WEBSEARCH_CLAUSE = """

        Antes de escrever, use a ferramenta de busca na web para confirmar os fatos mais importantes do assunto — especialmente nomes próprios, relações entre pessoas, datas e acontecimentos — garantindo que o conteúdo esteja factualmente correto. Não invente fatos: se não conseguir confirmar uma informação, não a afirme.
        """


async def _generate_content(config: NicheConfig, href: str, href2: str, href3: str, links_wordpress: list) -> dict:
    prompt = _build_prompt(config.prompt_niche, config.persona, href, href2, href3, links_wordpress)

    if config.ai_provider == "anthropic":
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": GEN_MAX_SEARCHES}]
        messages = [{"role": "user", "content": prompt + _GEN_WEBSEARCH_CLAUSE}]

        # web_search runs a server-side loop that may pause with
        # stop_reason="pause_turn"; resume by re-sending the response.
        for _ in range(GEN_MAX_CONTINUATIONS):
            async with _anthropic_client.messages.stream(
                model=config.ai_model,
                max_tokens=config.max_tokens,
                tools=tools,
                messages=messages,
            ) as stream:
                response = await stream.get_final_message()
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})

        text = "\n".join(b.text for b in response.content if b.type == "text")
        return _parse_ai_json(text)

    # Gemini grounds with Google Search server-side in a single call (no
    # pause/continuation loop). We parse JSON from prose, so we intentionally do
    # not set a response schema — that can't be combined with search grounding.
    grounding = types.Tool(google_search=types.GoogleSearch())
    response = _gemini_client.models.generate_content(
        model=config.ai_model,
        contents=prompt + _GEN_WEBSEARCH_CLAUSE,
        config=types.GenerateContentConfig(tools=[grounding]),
    )
    return _parse_ai_json(response.text)


_REVIEW_PROMPT = """
Você é um revisor de IMAGEM DE CAPA de um blog de notícias. Sua ÚNICA tarefa é avaliar se a
imagem de capa anexada tem relação com o assunto do texto abaixo.

NÃO avalie o texto em si: não cheque fatos, datas, lançamentos ou veracidade; não avalie estilo,
SEO, HTML, tamanho do texto, título chamativo (clickbait) nem qualidade editorial. O texto serve
APENAS como referência do assunto para você julgar a imagem. NÃO faça busca na web.

Foi anexada a IMAGEM DE CAPA deste post. Verifique se ela tem relação com o assunto do texto
(mesmo tema, pessoa ou contexto). Seja conservador: só aponte problema se a imagem for
claramente NÃO relacionada ao conteúdo (ex.: pessoa ou assunto totalmente diferente).
Imagens genéricas, porém coerentes com o tema, devem ser aceitas.

Texto (apenas referência de assunto):
title: {title}
keyword: {keyword}
body:
{body}

Responda APENAS com um único objeto JSON, sem nenhum texto fora dele, com os campos:
- "approved": true se a imagem de capa condiz com o assunto do texto; false caso contrário.
- "issues": lista de strings descrevendo o problema apenas se a imagem for incompatível com o
  texto (lista vazia se nenhum).
"""


def _sniff_raster_media_type(raw: bytes) -> str | None:
    """Positively identify a WordPress-supported raster type, or None.

    Only the formats WP accepts by default (JPEG/PNG/GIF/WebP) are recognised.
    Anything else — notably SVG, which Wikimedia serves for logos — returns None
    so callers can reject it instead of mislabelling it as JPEG.
    """
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _detect_image_media_type(raw: bytes) -> str:
    """Sniff the image media type from its magic bytes, defaulting to JPEG.

    Cover images are saved with a ``.jpg`` extension regardless of their real
    format, but the Anthropic vision API validates the declared ``media_type``
    against the actual bytes and 400s on a mismatch. Source CDNs commonly serve
    WebP, so detect the true type and fall back to JPEG for the unknown case.
    """
    return _sniff_raster_media_type(raw) or "image/jpeg"


# Extension WordPress expects for each media type. The filename extension and
# Content-Type must match the actual bytes or the media endpoint rejects the
# upload with rest_upload_sideload_error ("sem permissão para esse tipo").
_MEDIA_TYPE_EXTENSION = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _read_cover_image(image_path: str) -> tuple[bytes, str] | None:
    """Read a cover image for a vision request.

    Returns ``(raw_bytes, media_type)`` with the media type sniffed from the
    bytes, or ``None`` if the file is unreadable. Both providers consume the raw
    bytes (Anthropic base64-encodes them, Gemini wraps them in a ``Part``).
    """
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("Não foi possível ler a imagem de capa %s: %s", image_path, e)
        return None
    return raw, _detect_image_media_type(raw)


async def _review_with_anthropic(model: str, prompt: str,
                                 image: tuple[bytes, str] | None) -> str:
    """Run the review on Anthropic (vision via base64 image block)."""
    content = [{"type": "text", "text": prompt}]
    if image:
        raw, media_type = image
        content.insert(0, {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(raw).decode("utf-8"),
            },
        })
    response = await _anthropic_client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return "\n".join(b.text for b in response.content if b.type == "text")


def _review_with_gemini(model: str, prompt: str,
                        image: tuple[bytes, str] | None) -> str:
    """Run the review on Gemini (vision via an inline image Part)."""
    contents = [prompt]
    if image:
        raw, media_type = image
        contents.insert(0, types.Part.from_bytes(data=raw, mime_type=media_type))
    response = _gemini_client.models.generate_content(model=model, contents=contents)
    return response.text


async def _review_content(config: NicheConfig, match: dict,
                          image_path: str | None = None) -> dict:
    """Cover-image review of a generated post (soft gate).

    Because the article is already fact-checked at generation time (see
    ``_generate_content``), this pass does **not** review the text at all: it
    does not check facts and does not search the web. Its sole job is the cover
    image — it is attached so the reviewer can flag a cover that is clearly
    unrelated to the article. Style/SEO/HTML/clickbait and factual accuracy are
    explicitly out of scope. When no cover image is available there is nothing
    to review, so the post is approved.

    The review runs on the same provider as generation (``config.ai_provider``).
    Returns ``{"approved": bool, "issues": list[str]}``. Fails **open**
    (``approved=True``) whenever review is disabled, no cover image is present,
    the provider's client is unavailable, or the call/parse errors, so a
    reviewer outage never blocks the pipeline.
    """
    if not config.review_enabled:
        return {"approved": True, "issues": []}

    image = _read_cover_image(image_path) if image_path else None
    if image is None:
        return {"approved": True, "issues": []}

    prompt = _REVIEW_PROMPT.format(
        title=match.get('title', ''),
        keyword=match.get('keyword', ''),
        body=match.get('body', ''),
    )
    review_model = config.review_model or config.ai_model

    try:
        if config.ai_provider == "anthropic":
            if _anthropic_client is None:
                logger.warning("Revisão desativada: ANTHROPIC_API_KEY ausente. Publicando sem revisar.")
                return {"approved": True, "issues": []}
            text = await _review_with_anthropic(review_model, prompt, image)
        else:
            if _gemini_client is None:
                logger.warning("Revisão desativada: GEMINI_API_KEY ausente. Publicando sem revisar.")
                return {"approved": True, "issues": []}
            text = _review_with_gemini(review_model, prompt, image)
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

    # The file is always saved as .jpg, but fallback covers (Wikimedia/Openverse)
    # are often PNG/WebP. Declare the real type so WP doesn't reject the mismatch.
    media_type = _detect_image_media_type(image_data)
    extension = _MEDIA_TYPE_EXTENSION.get(media_type, "jpg")

    media_resp = requests.post(
        f'{config.wp_url}/wp-json/wp/v2/media',
        headers={**headers,
                 'Content-Disposition': f'attachment; filename="imagem.{extension}"',
                 'Content-Type': media_type},
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

    A trend is skipped before any AI call if *any* of its source hrefs is
    already in `published_hrefs`. Trends whose generated slug already exists in
    WordPress have all their source hrefs appended to `published_hrefs` (so we
    never re-generate them) and are skipped. Returns the ready-to-publish item
    (carrying all its `source_hrefs`), or None if nothing new was found.
    """
    seen = set(published_hrefs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context()).new_page()
        await page.set_extra_http_headers({"User-Agent": BROWSER_USER_AGENT})

        try:
            for link in range(1, MAX_TREND_ROWS + 1):
                try:
                    hrefs = await _extract_source_hrefs(page, config.trends_url, link)
                except Exception as e:
                    logger.warning("Falha ao ler a linha de trend %s: %s. Próxima.", link, e)
                    continue

                already = _matching_published_href(hrefs, seen)
                if already:
                    logger.info("Trend %s já publicada (%s). Pulando.", link, already)
                    continue

                logger.info("Gerando conteúdo da trend %s", link)
                try:
                    match = await _generate_content(config, *hrefs, links_wordpress)
                except Exception as e:
                    logger.warning("Erro ao gerar conteúdo da trend %s: %s", link, e)
                    continue

                if match['slug'] in slugs:
                    logger.info('Slug "%s" já existe no WordPress. Marcando trend %s como publicada.',
                                match['slug'], link)
                    new_hrefs = _new_source_hrefs(hrefs)
                    published_hrefs.extend(new_hrefs)
                    seen.update(new_hrefs)
                    continue  # this trend is spent; move to the next row

                try:
                    image_path, safe_title = await _acquire_cover_image(
                        page, hrefs, match['keyword'], link)
                except Exception as e:
                    logger.warning("Sem capa para a trend %s: %s. Próxima.", link, e)
                    continue

                review = await _review_content(config, match, image_path)

                return {
                    'link': link,
                    'source_hrefs': _new_source_hrefs(hrefs),
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
            published_hrefs.extend(item['source_hrefs'])
            _save_published_hrefs(config.prompt_niche, published_hrefs)
            return
        except Exception as e:
            logger.error("run_task() falhou na tentativa %s/%s: %s", attempt + 1, RETRY_COUNT, e)

    # Persist any slug-exists discoveries even though this publish failed, then
    # signal failure so the cron run is visibly red.
    _save_published_hrefs(config.prompt_niche, published_hrefs)
    raise SystemExit(f"Falha ao publicar trend {item['link']} após {RETRY_COUNT} tentativas.")
