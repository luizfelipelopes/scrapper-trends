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


# --- AI JSON parsing --------------------------------------------------------

def test_parse_ai_json_extracts_object_from_prose():
    raw = 'Claro! Aqui está:\n```json\n{"title": "Olá", "slug": "ola"}\n```'
    assert sb._parse_ai_json(raw) == {"title": "Olá", "slug": "ola"}


def test_parse_ai_json_raises_without_json():
    with pytest.raises(ValueError):
        sb._parse_ai_json("sem json aqui")


# --- content review (soft gate) ---------------------------------------------

def _niche(**overrides):
    base = dict(
        wp_url="https://wp.test", wp_user="u", wp_pass="p",
        telegram_token="t", telegram_chat_id="c", trends_url="https://trends.test",
        prompt_niche="esportes", get_categories=lambda m: [1],
        ai_provider="anthropic", ai_model="claude-opus-4-8",
    )
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
    def __init__(self, text):
        self._text = text
        self.messages = self

    async def create(self, **kwargs):
        block = type("Block", (), {"type": "text", "text": self._text})()
        return type("Msg", (), {"content": [block]})()


def test_review_flags_post_with_issues(monkeypatch):
    reply = '{"approved": false, "issues": ["título não condiz com o corpo"]}'
    monkeypatch.setattr(sb, "_anthropic_client", _FakeAnthropic(reply))
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": False, "issues": ["título não condiz com o corpo"]}


def test_review_malformed_response_fails_open(monkeypatch):
    monkeypatch.setattr(sb, "_anthropic_client", _FakeAnthropic("não é json"))
    verdict = asyncio.run(sb._review_content(_niche(), _MATCH, _HREFS))
    assert verdict == {"approved": True, "issues": []}


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
