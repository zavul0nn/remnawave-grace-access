from __future__ import annotations

import asyncio
import logging

from app.bot_api import BotApiClient
from app.config import load_config
from app.logger import setup_logging
from app.remna import close_sdk, create_sdk
from app.state import StateStore
from app.worker import run_forever


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)

    logger = logging.getLogger(__name__)
    logger.info(
        "Starting worker: backend=%s target_squads=%s statuses=%s extend_days=%s interval=%ss dry_run=%s",
        config.api_backend,
        {
            status: str(squad_uuid)
            for status, squad_uuid in config.target_squads_by_status.items()
        },
        ",".join(sorted(config.target_statuses)),
        config.extend_days,
        config.scan_interval_seconds,
        config.dry_run,
    )

    state = StateStore(config.state_db_path)
    sdk = create_api_client(config)
    try:
        await run_forever(config, sdk, state)
    finally:
        await close_api_client(config, sdk)
        state.close()


def create_api_client(config):
    if config.api_backend == "bot":
        if config.bot_api is None:
            raise RuntimeError("Bot API config is not loaded")
        return BotApiClient(config.bot_api)
    return create_sdk(config.remnawave)


async def close_api_client(config, sdk) -> None:
    if config.api_backend == "bot":
        await sdk.close()
        return
    await close_sdk(sdk)


if __name__ == "__main__":
    asyncio.run(main())
