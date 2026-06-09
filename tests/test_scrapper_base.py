"""Unit tests for the pure (non-IO) logic in scrapper_base.

Scraping, AI generation and WordPress calls are IO-heavy and intentionally
out of scope here; these tests cover the commercial-hours gate, the local
state file, and AI-JSON parsing.
"""
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
