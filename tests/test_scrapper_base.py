"""Unit tests for the pure (non-IO) logic in scrapper_base.

Scraping, AI generation and WordPress calls are IO-heavy and intentionally
out of scope here; these tests cover the commercial-hours gate, the local
state file, and AI-JSON parsing.
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scrapper_base as sb


# --- commercial hours -------------------------------------------------------

class _FrozenDateTime:
    """datetime stand-in whose now() honours the requested timezone."""
    fixed_utc = datetime(2026, 6, 9, 6, 0, tzinfo=ZoneInfo("UTC"))

    @classmethod
    def now(cls, tz=None):
        return cls.fixed_utc.astimezone(tz) if tz else cls.fixed_utc


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(sb, "datetime", _FrozenDateTime)


def test_blocked_window_pauses_in_local_time(frozen_clock):
    # 06:00 UTC is 03:00 in São Paulo — inside the 01:00-05:00 pause.
    assert sb._is_commercial_hour("America/Sao_Paulo") is False


def test_same_instant_is_allowed_in_utc(frozen_clock):
    # The very same instant is 06:00 UTC — outside the pause.
    assert sb._is_commercial_hour("UTC") is True


# --- published-hrefs state --------------------------------------------------

@pytest.fixture
def temp_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "STATE_DIR", str(tmp_path))
    return tmp_path


def test_state_round_trip(temp_state_dir):
    sb._save_published_hrefs("esportes", ["https://a.com", "https://b.com"])
    assert sb._load_published_hrefs("esportes") == ["https://a.com", "https://b.com"]


def test_state_missing_file_is_empty(temp_state_dir):
    assert sb._load_published_hrefs("nada") == []


def test_state_history_is_capped(temp_state_dir, monkeypatch):
    monkeypatch.setattr(sb, "STATE_HISTORY_LIMIT", 3)
    sb._save_published_hrefs("x", [f"u{i}" for i in range(10)])
    assert sb._load_published_hrefs("x") == ["u7", "u8", "u9"]


def test_corrupt_state_recovers_as_empty(temp_state_dir):
    (temp_state_dir / "bad.json").write_text("{not json", encoding="utf-8")
    assert sb._load_published_hrefs("bad") == []


# --- href dedup (all extracted sources) -------------------------------------

def test_new_source_hrefs_drops_empty():
    assert sb._new_source_hrefs(("https://a.com", None, "")) == ["https://a.com"]


def test_matching_href_checks_all_three_not_just_first():
    seen = {"https://b.com"}  # the trend's *second* source was published before
    hrefs = ("https://a.com", "https://b.com", "https://c.com")
    assert sb._matching_published_href(hrefs, seen) == "https://b.com"


def test_matching_href_returns_none_when_all_unseen():
    seen = {"https://x.com"}
    hrefs = ("https://a.com", "https://b.com", None)
    assert sb._matching_published_href(hrefs, seen) is None


# --- AI JSON parsing --------------------------------------------------------

def test_parse_ai_json_extracts_object_from_prose():
    raw = 'Claro! Aqui está:\n```json\n{"title": "Olá", "slug": "ola"}\n```'
    assert sb._parse_ai_json(raw) == {"title": "Olá", "slug": "ola"}


def test_parse_ai_json_raises_without_json():
    with pytest.raises(ValueError):
        sb._parse_ai_json("sem json aqui")


# --- AI model selection from env --------------------------------------------

_REQUIRED_NICHE_ARGS = dict(
    wp_url="https://wp.test", wp_user="u", wp_pass="p",
    telegram_token="t", telegram_chat_id="c", trends_url="https://trends.test",
    prompt_niche="esportes", get_categories=lambda m: [1],
)


def test_ai_config_reads_from_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("AI_MODEL", "some-gen-model")
    monkeypatch.setenv("REVIEW_MODEL", "some-review-model")
    cfg = sb.NicheConfig(**_REQUIRED_NICHE_ARGS)
    assert cfg.ai_provider == "gemini"
    assert cfg.ai_model == "some-gen-model"
    assert cfg.review_model == "some-review-model"


def test_ai_config_falls_back_to_defaults(monkeypatch):
    for var in ("AI_PROVIDER", "AI_MODEL", "REVIEW_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg = sb.NicheConfig(**_REQUIRED_NICHE_ARGS)
    assert cfg.ai_provider == sb.DEFAULT_AI_PROVIDER
    assert cfg.ai_model == sb.DEFAULT_AI_MODEL
    assert cfg.review_model == sb.DEFAULT_REVIEW_MODEL


def test_explicit_ai_args_override_env(monkeypatch):
    monkeypatch.setenv("AI_MODEL", "from-env")
    cfg = sb.NicheConfig(**_REQUIRED_NICHE_ARGS, ai_model="explicit")
    assert cfg.ai_model == "explicit"


# --- content review (soft gate) ---------------------------------------------

def _niche(**overrides):
    base = dict(**_REQUIRED_NICHE_ARGS, ai_provider="anthropic", ai_model="claude-opus-4-8")
    base.update(overrides)
    return sb.NicheConfig(**base)


_HREFS = ("https://a.com", "https://b.com", "https://c.com")
_MATCH = {"title": "T", "slug": "t", "meta_description": "m", "keyword": "k", "body": "<article>x</article>"}


def test_review_disabled_fails_open():
    verdict = asyncio.run(sb._review_content(_niche(review_enabled=False), _MATCH, _HREFS))
    assert verdict == {"approved": True, "issues": []}


def test_review_without_client_fails_open(monkeypatch):
    monkeypatch.setattr(sb, "_anthropic_client", None)
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": True, "issues": []}


class _FakeAnthropic:
    """Minimal async stand-in returning a canned text block from messages.create."""
    def __init__(self, text, stop_reason="end_turn"):
        self._text = text
        self._stop_reason = stop_reason
        self.messages = self

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        block = type("Block", (), {"type": "text", "text": self._text})()
        return type("Msg", (), {"content": [block], "stop_reason": self._stop_reason})()


def test_review_flags_post_with_issues(monkeypatch):
    reply = '{"approved": false, "issues": ["título não condiz com o corpo"]}'
    monkeypatch.setattr(sb, "_anthropic_client", _FakeAnthropic(reply))
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": False, "issues": ["título não condiz com o corpo"]}


def test_review_malformed_response_fails_open(monkeypatch):
    monkeypatch.setattr(sb, "_anthropic_client", _FakeAnthropic("não é json"))
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": True, "issues": []}


def test_review_attaches_cover_image(monkeypatch, tmp_path):
    fake = _FakeAnthropic('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_anthropic_client", fake)
    img = tmp_path / "cover.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

    asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS, str(img)))

    content = fake.last_kwargs["messages"][0]["content"]
    assert "image" in [block["type"] for block in content]


def test_review_declares_webp_media_type(monkeypatch, tmp_path):
    # Covers are always saved as .jpg, but a WebP body must be declared as such
    # or the Anthropic vision API 400s on the media-type mismatch.
    fake = _FakeAnthropic('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_anthropic_client", fake)
    img = tmp_path / "cover.jpg"
    img.write_bytes(b"RIFF\x00\x00\x00\x00WEBPfake-webp-bytes")

    asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS, str(img)))

    content = fake.last_kwargs["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/webp"


@pytest.mark.parametrize("magic,expected", [
    (b"\xff\xd8\xff\xe0", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF89a", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
    (b"unknown-bytes", "image/jpeg"),
])
def test_detect_image_media_type(magic, expected):
    assert sb._detect_image_media_type(magic) == expected


@pytest.mark.parametrize("raw", [
    b"<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg'></svg>",
    b"<svg></svg>",
    b"not an image",
])
def test_sniff_raster_rejects_non_raster(raw):
    # SVG/unknown -> None so the download path rejects it instead of mislabelling.
    assert sb._sniff_raster_media_type(raw) is None


def test_download_rejects_svg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "covers").mkdir()
    monkeypatch.setattr(sb.requests, "get",
                        lambda *a, **k: _fake_response(200, b"<svg></svg>"))
    with pytest.raises(sb.CoverImageError):
        sb._download_image_to_cover("https://cdn.wiki/logo.svg", "logo")


def test_review_without_image_sends_text_only(monkeypatch):
    fake = _FakeAnthropic('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_anthropic_client", fake)

    asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))

    content = fake.last_kwargs["messages"][0]["content"]
    assert "image" not in [block["type"] for block in content]


def test_review_unreadable_image_falls_back_to_text(monkeypatch):
    fake = _FakeAnthropic('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_anthropic_client", fake)

    # Non-existent path: image can't be read, review still runs on text alone.
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS, "covers/missing.jpg"))

    assert verdict == {"approved": True, "issues": []}
    content = fake.last_kwargs["messages"][0]["content"]
    assert "image" not in [block["type"] for block in content]


# --- review routes to the configured provider -------------------------------

class _FakeGemini:
    """Minimal stand-in for the Gemini client returning a canned text response."""
    def __init__(self, text):
        self._text = text
        self.models = self

    def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        return type("Resp", (), {"text": self._text})()


def _gemini_niche(**overrides):
    return _niche(ai_provider="gemini", ai_model="gemini-2.5-flash",
                  review_model="gemini-2.5-flash", **overrides)


def test_review_routes_to_gemini(monkeypatch):
    fake = _FakeGemini('{"approved": false, "issues": ["capa não condiz"]}')
    monkeypatch.setattr(sb, "_gemini_client", fake)
    # Anthropic client absent: a Gemini niche must not touch it.
    monkeypatch.setattr(sb, "_anthropic_client", None)

    verdict = asyncio.run(sb._review_content(_gemini_niche(), _MATCH, _HREFS))

    assert verdict == {"approved": False, "issues": ["capa não condiz"]}
    assert fake.last_kwargs["model"] == "gemini-2.5-flash"


def test_review_gemini_without_client_fails_open(monkeypatch):
    monkeypatch.setattr(sb, "_gemini_client", None)
    verdict = asyncio.run(sb._review_content(_gemini_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": True, "issues": []}


def test_review_gemini_attaches_image_part(monkeypatch, tmp_path):
    fake = _FakeGemini('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_gemini_client", fake)
    img = tmp_path / "cover.jpg"
    img.write_bytes(b"RIFF\x00\x00\x00\x00WEBPfake-webp-bytes")

    asyncio.run(sb._review_content(_gemini_niche(), _MATCH, _HREFS, str(img)))

    contents = fake.last_kwargs["contents"]
    # Image Part is prepended; the prompt string follows.
    assert len(contents) == 2
    assert isinstance(contents[0], sb.types.Part)
    assert isinstance(contents[1], str)


def test_review_gemini_without_image_sends_text_only(monkeypatch):
    fake = _FakeGemini('{"approved": true, "issues": []}')
    monkeypatch.setattr(sb, "_gemini_client", fake)

    asyncio.run(sb._review_content(_gemini_niche(), _MATCH, _HREFS))

    contents = fake.last_kwargs["contents"]
    assert len(contents) == 1
    assert isinstance(contents[0], str)


def test_generate_grounds_gemini_with_google_search(monkeypatch):
    fake = _FakeGemini('{"title": "t", "slug": "s", "meta_description": "m", '
                       '"keyword": "k", "body": "<article>x</article>"}')
    monkeypatch.setattr(sb, "_gemini_client", fake)

    result = asyncio.run(
        sb._generate_content(_gemini_niche(), "https://a", "https://b", "https://c", [])
    )

    assert result["slug"] == "s"
    cfg = fake.last_kwargs["config"]
    assert len(cfg.tools) == 1
    assert cfg.tools[0].google_search is not None


# --- soft blocking in _create_post ------------------------------------------

class _FakeResp:
    status_code = 201

    @staticmethod
    def json():
        return {"link": "https://wp.test/post"}


@pytest.fixture
def capture_post(monkeypatch):
    """Capture the JSON payload sent to WordPress and return a 201."""
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["json"] = json
        return _FakeResp()

    monkeypatch.setattr(sb.requests, "post", fake_post)
    monkeypatch.setattr(sb.random, "choice", lambda seq: seq[0])
    return sent


def test_flagged_post_becomes_draft(capture_post):
    review = {"approved": False, "issues": ["data suspeita"]}
    msg = sb._create_post(_niche(), media_id=1, match=_MATCH, trend_index=2, review=review)
    assert capture_post["json"]["status"] == "draft"
    assert "RASCUNHO" in msg
    assert "data suspeita" in msg


def test_approved_post_is_published(capture_post):
    review = {"approved": True, "issues": []}
    msg = sb._create_post(_niche(), media_id=1, match=_MATCH, trend_index=2, review=review)
    assert capture_post["json"]["status"] == "publish"
    assert "sucesso" in msg


def test_missing_review_publishes(capture_post):
    msg = sb._create_post(_niche(), media_id=1, match=_MATCH, trend_index=2)
    assert capture_post["json"]["status"] == "publish"
    assert "sucesso" in msg


# --- cover image download (fail fast) ---------------------------------------

class _FakeLocator:
    """Stands in for a Playwright locator exposing get_attribute."""
    def __init__(self, **attrs):
        self._attrs = attrs

    @property
    def first(self):
        return self

    async def text_content(self):
        return self._attrs.get("text")

    async def get_attribute(self, name):
        return self._attrs.get(name)


class _FakePage:
    """Minimal page: goto is a no-op, locator('h1') yields the article title.

    `meta` maps an og:image/twitter:image selector to its `content` value; an
    empty mapping (the default) means no meta tag, so cover resolution falls
    back to the CSS <img> selectors.
    """
    def __init__(self, title, meta=None):
        self._title = title
        self._meta = meta or {}

    async def goto(self, *args, **kwargs):
        return None

    def locator(self, selector):
        return _FakeLocator(text=self._title)

    async def query_selector(self, selector):
        content = self._meta.get(selector)
        return _FakeLocator(content=content) if content else None


def _patch_image_element(monkeypatch, locator):
    async def fake_get_image(page):
        return locator
    monkeypatch.setattr(sb, "_get_image_element", fake_get_image)


def _fake_response(status_code, content=b""):
    return type("Resp", (), {"status_code": status_code, "content": content})()


def test_download_cover_raises_on_http_error(monkeypatch):
    # A 403 from the CDN must raise, not return a path to a file never written.
    _patch_image_element(monkeypatch, _FakeLocator(srcset="https://cdn.test/x.jpg 1x"))
    monkeypatch.setattr(sb.requests, "get", lambda *a, **k: _fake_response(403))
    with pytest.raises(sb.CoverImageError):
        asyncio.run(sb._download_cover_image(_FakePage("Título"), "https://src.test"))


def test_download_cover_raises_without_image_url(monkeypatch):
    # No srcset and a null src (the source of the '.replace on None' crash).
    _patch_image_element(monkeypatch, _FakeLocator(srcset=None, src=None))
    with pytest.raises(sb.CoverImageError):
        asyncio.run(sb._download_cover_image(_FakePage("Título"), "https://src.test"))


def test_download_cover_writes_file_on_success(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "covers").mkdir()
    _patch_image_element(monkeypatch, _FakeLocator(srcset="https://cdn.test/x.jpg 1x"))
    monkeypatch.setattr(sb.requests, "get", lambda *a, **k: _fake_response(200, b"\xff\xd8\xffbytes"))

    img_path, safe_title = asyncio.run(
        sb._download_cover_image(_FakePage("Olá Mundo"), "https://src.test")
    )

    assert safe_title == "Ol__Mundo"
    assert (tmp_path / img_path).read_bytes() == b"\xff\xd8\xffbytes"


def test_resolve_cover_prefers_og_image(monkeypatch):
    # og:image is declared, so it wins and the <img> selectors are never consulted.
    def boom(page):
        raise AssertionError("_get_image_element should not be called when og:image exists")
    monkeypatch.setattr(sb, "_get_image_element", boom)

    page = _FakePage("T", meta={'meta[property="og:image"]': "https://cdn.test/cover.jpg"})
    url = asyncio.run(sb._resolve_cover_image_url(page, "https://src.test/article"))
    assert url == "https://cdn.test/cover.jpg"


def test_resolve_cover_resolves_relative_og_image(monkeypatch):
    # A protocol-relative / path-only og:image is joined against the article href.
    page = _FakePage("T", meta={'meta[name="twitter:image"]': "/img/cover.jpg"})
    url = asyncio.run(sb._resolve_cover_image_url(page, "https://src.test/article"))
    assert url == "https://src.test/img/cover.jpg"


def test_resolve_cover_falls_back_to_selectors(monkeypatch):
    # No meta tag -> existing srcset-based <img> resolution still works.
    _patch_image_element(monkeypatch, _FakeLocator(srcset="https://cdn.test/x.jpg 1x"))
    page = _FakePage("T")
    url = asyncio.run(sb._resolve_cover_image_url(page, "https://src.test"))
    assert url == "https://cdn.test/x.jpg"


# --- fallback cover sources (Wikimedia / Openverse) -------------------------

def _json_response(payload):
    return type("Resp", (), {
        "status_code": 200,
        "json": lambda self: payload,
        "raise_for_status": lambda self: None,
    })()


def test_wikimedia_cover_downloads_landscape_lead_image(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "covers").mkdir()
    payload = {"query": {"pages": {"123": {"original": {
        "source": "https://cdn.wiki/img.jpg", "width": 1200, "height": 600}}}}}

    calls = {}
    def fake_get(url, *a, **k):
        if url == sb.WIKIMEDIA_API:
            calls["api"] = k.get("params")
            return _json_response(payload)
        return _fake_response(200, b"\xff\xd8\xffimg")
    monkeypatch.setattr(sb.requests, "get", fake_get)

    img_path, safe_title = sb._fetch_wikimedia_cover("Fulano de Tal")
    assert calls["api"]["pilicense"] == "free"  # AdSense-safe: free licenses only
    assert safe_title == "Fulano_de_Tal"
    assert (tmp_path / img_path).read_bytes() == b"\xff\xd8\xffimg"


def test_wikimedia_cover_rejects_portrait_lead_image(monkeypatch):
    # A taller-than-wide lead image is not a landscape hero -> raise (chain moves on).
    payload = {"query": {"pages": {"1": {"original": {
        "source": "https://cdn.wiki/tall.jpg", "width": 600, "height": 1200}}}}}
    monkeypatch.setattr(sb.requests, "get", lambda *a, **k: _json_response(payload))
    with pytest.raises(sb.CoverImageError):
        sb._fetch_wikimedia_cover("Retrato")


def test_wikimedia_cover_raises_when_no_image(monkeypatch):
    monkeypatch.setattr(sb.requests, "get",
                        lambda *a, **k: _json_response({"query": {"pages": {}}}))
    with pytest.raises(sb.CoverImageError):
        sb._fetch_wikimedia_cover("Inexistente")


def test_openverse_cover_filters_commercial_and_landscape(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "covers").mkdir()

    calls = {}
    def fake_get(url, *a, **k):
        if url == sb.OPENVERSE_API:
            calls["params"] = k.get("params")
            return _json_response({"results": [{"url": "https://cdn.ov/img.jpg"}]})
        return _fake_response(200, b"\xff\xd8\xffov")
    monkeypatch.setattr(sb.requests, "get", fake_get)

    img_path, _ = sb._fetch_openverse_cover("Algum Tema")
    assert calls["params"]["license_type"] == "commercial"
    assert calls["params"]["aspect_ratio"] == "wide"
    assert (tmp_path / img_path).read_bytes() == b"\xff\xd8\xffov"


def test_acquire_cover_falls_back_to_wikimedia(monkeypatch):
    # Every source article fails -> chain reaches Wikimedia and returns it.
    async def fail_ref(page, hrefs, ref, link):
        raise sb.CoverImageError("no image")
    monkeypatch.setattr(sb, "_download_for_ref", fail_ref)
    monkeypatch.setattr(sb, "_fetch_wikimedia_cover",
                        lambda kw: ("covers/wiki.jpg", "wiki"))
    # Openverse must not be consulted once Wikimedia succeeds.
    monkeypatch.setattr(sb, "_fetch_openverse_cover",
                        lambda kw: (_ for _ in ()).throw(AssertionError("called")))

    path, title = asyncio.run(
        sb._acquire_cover_image(None, ("a", "b", "c"), "kw", 1))
    assert (path, title) == ("covers/wiki.jpg", "wiki")


def test_acquire_cover_raises_when_all_sources_fail(monkeypatch):
    async def fail_ref(page, hrefs, ref, link):
        raise sb.CoverImageError("no image")
    monkeypatch.setattr(sb, "_download_for_ref", fail_ref)
    monkeypatch.setattr(sb, "_fetch_wikimedia_cover",
                        lambda kw: (_ for _ in ()).throw(sb.CoverImageError("none")))
    monkeypatch.setattr(sb, "_fetch_openverse_cover",
                        lambda kw: (_ for _ in ()).throw(sb.CoverImageError("none")))
    with pytest.raises(sb.CoverImageError):
        asyncio.run(sb._acquire_cover_image(None, ("a", "b", "c"), "kw", 1))


# --- WordPress media upload (content-type matches real bytes) ---------------

def test_upload_image_declares_real_media_type(monkeypatch, tmp_path):
    # A PNG cover saved as .jpg must be uploaded as image/png, else WP rejects it
    # with rest_upload_sideload_error.
    png = tmp_path / "cover.jpg"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"rest")

    captured = {}
    def fake_post(url, *a, **k):
        captured["headers"] = k["headers"]
        return type("R", (), {"status_code": 201, "json": lambda self: {"id": 7}})()
    monkeypatch.setattr(sb.requests, "post", fake_post)
    monkeypatch.setattr(sb.requests, "put",
                        lambda *a, **k: type("R", (), {"status_code": 200})())

    media_id = sb._upload_image(_niche(), str(png), _MATCH)
    assert media_id == 7
    assert captured["headers"]["Content-Type"] == "image/png"
    assert captured["headers"]["Content-Disposition"] == 'attachment; filename="imagem.png"'
