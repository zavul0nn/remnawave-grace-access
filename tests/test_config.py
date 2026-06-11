from __future__ import annotations

from uuid import UUID

from app.config import load_config


EXPIRED_SQUAD = "e9534880-836d-41bc-9dc4-a453056ad5d1"
LIMITED_SQUAD = "06e88dc4-6b6e-4db0-8de0-28c68ac12025"


def test_load_config_uses_status_specific_target_squads(monkeypatch) -> None:
    monkeypatch.delenv("TARGET_SQUAD_UUID", raising=False)
    monkeypatch.setenv("REMNAWAVE_API_BASE", "https://example.test/api")
    monkeypatch.setenv("REMNAWAVE_API_TOKEN", "token")
    monkeypatch.setenv("TARGET_EXPIRED_SQUAD_UUID", EXPIRED_SQUAD)
    monkeypatch.setenv("TARGET_LIMITED_SQUAD_UUID", LIMITED_SQUAD)

    config = load_config()

    assert config.target_squads_by_status == {
        "EXPIRED": UUID(EXPIRED_SQUAD),
        "LIMITED": UUID(LIMITED_SQUAD),
    }


def test_load_config_can_use_single_target_squad_as_fallback(monkeypatch) -> None:
    monkeypatch.delenv("TARGET_EXPIRED_SQUAD_UUID", raising=False)
    monkeypatch.delenv("TARGET_LIMITED_SQUAD_UUID", raising=False)
    monkeypatch.setenv("REMNAWAVE_API_BASE", "https://example.test/api")
    monkeypatch.setenv("REMNAWAVE_API_TOKEN", "token")
    monkeypatch.setenv("TARGET_SQUAD_UUID", EXPIRED_SQUAD)

    config = load_config()

    assert config.target_squads_by_status == {
        "EXPIRED": UUID(EXPIRED_SQUAD),
        "LIMITED": UUID(EXPIRED_SQUAD),
    }


def test_load_config_supports_bot_backend(monkeypatch) -> None:
    monkeypatch.setenv("API_BACKEND", "bot")
    monkeypatch.setenv("BOT_API_BASE", "https://bot.example.test/")
    monkeypatch.setenv("BOT_API_KEY", "api-key")
    monkeypatch.setenv("TARGET_EXPIRED_SQUAD_UUID", EXPIRED_SQUAD)
    monkeypatch.setenv("TARGET_LIMITED_SQUAD_UUID", LIMITED_SQUAD)

    config = load_config()

    assert config.api_backend == "bot"
    assert config.bot_api is not None
    assert config.bot_api.api_base == "https://bot.example.test"
    assert config.bot_api.api_key == "api-key"
    assert config.page_size == 200
