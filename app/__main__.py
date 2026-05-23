from __future__ import annotations

import asyncio
import logging

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
        "Starting worker: target_squads=%s statuses=%s extend_days=%s interval=%ss dry_run=%s",
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
    sdk = create_sdk(config.remnawave)
    try:
        await run_forever(config, sdk, state)
    finally:
        await close_sdk(sdk)
        state.close()


if __name__ == "__main__":
    asyncio.run(main())
