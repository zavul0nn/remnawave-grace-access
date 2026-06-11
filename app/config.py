from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from environs import Env


@dataclass(frozen=True, slots=True)
class RemnawaveConfig:
    api_base: str
    api_token: str
    caddy_token: str | None
    ssl_ignore: bool


@dataclass(frozen=True, slots=True)
class BotApiConfig:
    api_base: str
    api_key: str
    ssl_ignore: bool


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    api_backend: str
    remnawave: RemnawaveConfig
    bot_api: BotApiConfig | None
    target_squads_by_status: dict[str, UUID]
    target_statuses: frozenset[str]
    extend_days: int
    traffic_limit_bytes: int
    scan_interval_seconds: int
    page_size: int
    state_db_path: Path
    dry_run: bool
    log_level: str


def _parse_statuses(value: str) -> frozenset[str]:
    statuses = frozenset(
        item.strip().upper()
        for item in value.split(",")
        if item.strip()
    )
    if not statuses:
        raise ValueError("TARGET_STATUSES must contain at least one status")
    return statuses


def load_config() -> WorkerConfig:
    env = Env()
    env.read_env()
    api_backend = env.str("API_BACKEND", default="remnawave").strip().lower()
    if api_backend not in {"remnawave", "bot"}:
        raise ValueError("API_BACKEND must be either 'remnawave' or 'bot'")

    target_statuses = _parse_statuses(
        env.str("TARGET_STATUSES", default="EXPIRED,LIMITED")
    )
    target_squads_by_status = _load_target_squads_by_status(env, target_statuses)

    extend_days = env.int("EXTEND_DAYS", default=3)
    if extend_days < 1:
        raise ValueError("EXTEND_DAYS must be greater than 0")

    traffic_limit_bytes = env.int("TRAFFIC_LIMIT_BYTES", default=1024 ** 3)
    if traffic_limit_bytes < 0:
        raise ValueError("TRAFFIC_LIMIT_BYTES must be greater than or equal to 0")

    scan_interval = env.int("SCAN_INTERVAL_SECONDS", default=60)
    if scan_interval < 1:
        raise ValueError("SCAN_INTERVAL_SECONDS must be greater than 0")

    page_size = env.int("PAGE_SIZE", default=500 if api_backend == "remnawave" else 200)
    max_page_size = 500 if api_backend == "remnawave" else 200
    if page_size < 1 or page_size > max_page_size:
        raise ValueError(f"PAGE_SIZE must be between 1 and {max_page_size}")

    return WorkerConfig(
        api_backend=api_backend,
        remnawave=_load_remnawave_config(env, required=api_backend == "remnawave"),
        bot_api=_load_bot_api_config(env) if api_backend == "bot" else None,
        target_squads_by_status=target_squads_by_status,
        target_statuses=target_statuses,
        extend_days=extend_days,
        traffic_limit_bytes=traffic_limit_bytes,
        scan_interval_seconds=scan_interval,
        page_size=page_size,
        state_db_path=Path(env.str("STATE_DB_PATH", default="./data/state.sqlite3")),
        dry_run=env.bool("DRY_RUN", default=False),
        log_level=env.str("LOG_LEVEL", default="INFO").upper(),
    )


def _load_remnawave_config(env: Env, *, required: bool) -> RemnawaveConfig:
    api_base = env.str("REMNAWAVE_API_BASE", default="")
    api_token = env.str("REMNAWAVE_API_TOKEN", default="")
    if required and not api_base:
        raise ValueError("REMNAWAVE_API_BASE is required for API_BACKEND=remnawave")
    if required and not api_token:
        raise ValueError("REMNAWAVE_API_TOKEN is required for API_BACKEND=remnawave")

    return RemnawaveConfig(
        api_base=api_base,
        api_token=api_token,
        caddy_token=env.str("REMNAWAVE_CADDY_TOKEN", default="") or None,
        ssl_ignore=env.bool("REMNAWAVE_SSL_IGNORE", default=False),
    )


def _load_bot_api_config(env: Env) -> BotApiConfig:
    return BotApiConfig(
        api_base=env.str("BOT_API_BASE").rstrip("/"),
        api_key=env.str("BOT_API_KEY"),
        ssl_ignore=env.bool("BOT_API_SSL_IGNORE", default=False),
    )


def _load_target_squads_by_status(
    env: Env,
    target_statuses: frozenset[str],
) -> dict[str, UUID]:
    fallback = env.str("TARGET_SQUAD_UUID", default="")
    raw_by_status = {
        "EXPIRED": env.str("TARGET_EXPIRED_SQUAD_UUID", default="") or fallback,
        "LIMITED": env.str("TARGET_LIMITED_SQUAD_UUID", default="") or fallback,
    }

    result: dict[str, UUID] = {}
    for status in target_statuses:
        raw_uuid = raw_by_status.get(status, fallback)
        if not raw_uuid:
            raise ValueError(
                f"Target squad UUID is not configured for status {status}. "
                f"Set TARGET_{status}_SQUAD_UUID or TARGET_SQUAD_UUID."
            )
        result[status] = UUID(raw_uuid)
    return result
