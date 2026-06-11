from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Protocol
from uuid import UUID

from remnawave.enums import TrafficLimitStrategy, UserStatus
from remnawave.models import UpdateUserRequestDto

from app.config import WorkerConfig
from app.state import StateStore, UserState

logger = logging.getLogger(__name__)


class UsersApi(Protocol):
    async def get_all_users(self, start: int | None = None, size: int | None = None):
        ...

    async def update_user(self, body: UpdateUserRequestDto):
        ...

    async def reset_user_traffic(self, uuid: str):
        ...

    async def extend_subscription(self, subscription_id: int, days: int):
        ...

    async def add_subscription_squad(self, subscription_id: int, squad_uuid: str):
        ...

    async def remove_subscription_squad(self, subscription_id: int, squad_uuid: str):
        ...

    async def add_subscription_traffic(self, subscription_id: int, gb: int):
        ...


class RemnawaveApi(Protocol):
    users: UsersApi


@dataclass(frozen=True, slots=True)
class ScanStats:
    scanned: int = 0
    matched: int = 0
    processed: int = 0
    skipped_processed: int = 0
    renewed: int = 0
    failed: int = 0


async def run_forever(config: WorkerConfig, sdk: RemnawaveApi, state: StateStore) -> None:
    while True:
        try:
            stats = await scan_once(config, sdk, state)
            logger.info(
                "Scan finished: scanned=%s matched=%s processed=%s "
                "renewed=%s skipped_processed=%s failed=%s",
                stats.scanned,
                stats.matched,
                stats.processed,
                stats.renewed,
                stats.skipped_processed,
                stats.failed,
            )
        except Exception:
            logger.exception("Scan failed")

        await asyncio.sleep(config.scan_interval_seconds)


async def scan_once(
    config: WorkerConfig,
    sdk: RemnawaveApi,
    state: StateStore,
) -> ScanStats:
    scanned = 0
    matched = 0
    processed = 0
    skipped_processed = 0
    renewed = 0
    failed = 0

    async for user in iter_users(sdk, page_size=config.page_size):
        scanned += 1
        status = normalize_status(getattr(user, "status", ""))
        user_uuid = str(getattr(user, "uuid"))
        squad_uuids = extract_internal_squad_uuids(user)
        current_target_squad = find_current_target_squad(config, squad_uuids)
        in_target_squad = current_target_squad is not None
        current_state = state.get(user_uuid)

        should_process_status = status in config.target_statuses
        status_target_squad = get_target_squad_for_status(config, status)
        should_renew_target = (
            not should_process_status
            and current_target_squad is not None
            and is_managed_by_worker(current_state)
            and should_extend_again(current_state, extend_days=config.extend_days)
        )

        if not should_process_status and not should_renew_target:
            state.mark_observed(
                user_uuid,
                status,
                in_target_squad=in_target_squad,
            )
            continue

        matched += 1
        if (
            should_process_status
            and current_target_squad is not None
            and is_managed_by_worker(current_state)
            and not should_extend_again(current_state, extend_days=config.extend_days)
        ):
            skipped_processed += 1
            state.mark_observed(
                user_uuid,
                status,
                in_target_squad=in_target_squad,
            )
            continue

        try:
            target_squad_uuid = status_target_squad or current_target_squad
            if target_squad_uuid is None:
                raise RuntimeError(f"No target squad selected for status {status}")

            expire_at = await process_user(
                config,
                sdk,
                user,
                target_squad_uuid=target_squad_uuid,
            )
            if not config.dry_run:
                state.record_extension(user_uuid, status, expire_at=expire_at)
            else:
                state.mark_observed(user_uuid, status, in_target_squad=in_target_squad)
            processed += 1
            if should_renew_target:
                renewed += 1
        except Exception:
            failed += 1
            logger.exception(
                "Failed to process user uuid=%s username=%s status=%s",
                user_uuid,
                getattr(user, "username", None),
                status,
            )

    return ScanStats(
        scanned=scanned,
        matched=matched,
        processed=processed,
        skipped_processed=skipped_processed,
        renewed=renewed,
        failed=failed,
    )


async def iter_users(sdk: RemnawaveApi, *, page_size: int) -> AsyncIterator[object]:
    start = 0
    while True:
        response = await sdk.users.get_all_users(start=start, size=page_size)
        users = list(getattr(response, "users", []) or [])
        total = int(getattr(response, "total", len(users)) or 0)

        for user in users:
            yield user

        start += len(users)
        if not users or start >= total:
            break


async def process_user(
    config: WorkerConfig,
    sdk: RemnawaveApi,
    user: object,
    *,
    target_squad_uuid: UUID,
) -> datetime:
    if config.api_backend == "bot":
        return await process_bot_subscription(
            config,
            sdk,
            user,
            target_squad_uuid=target_squad_uuid,
        )
    return await process_remnawave_user(
        config,
        sdk,
        user,
        target_squad_uuid=target_squad_uuid,
    )


async def process_remnawave_user(
    config: WorkerConfig,
    sdk: RemnawaveApi,
    user: object,
    *,
    target_squad_uuid: UUID,
) -> datetime:
    user_uuid = UUID(str(getattr(user, "uuid")))
    expire_at = calculate_extended_expire_at(
        getattr(user, "expire_at"),
        extend_days=config.extend_days,
    )

    logger.info(
        "%s user uuid=%s username=%s squads=%s status=%s expire_at=%s traffic_limit_bytes=%s",
        "DRY-RUN would update" if config.dry_run else "Updating",
        user_uuid,
        getattr(user, "username", None),
        [str(target_squad_uuid)],
        UserStatus.ACTIVE.value,
        expire_at.isoformat(),
        config.traffic_limit_bytes,
    )

    if config.dry_run:
        return expire_at

    await sdk.users.reset_user_traffic(str(user_uuid))
    await sdk.users.update_user(
        UpdateUserRequestDto(
            uuid=user_uuid,
            status=UserStatus.ACTIVE,
            active_internal_squads=[target_squad_uuid],
            expire_at=expire_at,
            traffic_limit_bytes=config.traffic_limit_bytes,
            traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        )
    )
    return expire_at


async def process_bot_subscription(
    config: WorkerConfig,
    sdk: RemnawaveApi,
    user: object,
    *,
    target_squad_uuid: UUID,
) -> datetime:
    subscription_id = int(getattr(user, "subscription_id", getattr(user, "uuid")))
    current_squads = extract_internal_squad_uuids(user)
    expire_at = calculate_extended_expire_at(
        getattr(user, "expire_at", None),
        extend_days=config.extend_days,
    )
    traffic_limit_gb = bytes_to_gib(config.traffic_limit_bytes)

    logger.info(
        "%s subscription id=%s user_id=%s squads=%s extend_days=%s traffic_limit_gb=%s",
        "DRY-RUN would update" if config.dry_run else "Updating",
        subscription_id,
        getattr(user, "user_id", None),
        [str(target_squad_uuid)],
        config.extend_days,
        traffic_limit_gb,
    )

    if config.dry_run:
        return expire_at

    users_api = sdk.users
    response = await users_api.extend_subscription(subscription_id, config.extend_days)
    await users_api.add_subscription_squad(subscription_id, str(target_squad_uuid))
    for squad_uuid in current_squads:
        if squad_uuid != target_squad_uuid:
            await users_api.remove_subscription_squad(subscription_id, str(squad_uuid))
    if traffic_limit_gb > 0:
        await users_api.add_subscription_traffic(subscription_id, traffic_limit_gb)

    return parse_response_expire_at(response) or expire_at


def bytes_to_gib(value: int) -> int:
    if value <= 0:
        return 0
    return math.ceil(value / (1024 ** 3))


def parse_response_expire_at(response: object) -> datetime | None:
    if not isinstance(response, dict):
        return None
    raw = response.get("end_date")
    if not isinstance(raw, str) or not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def get_target_squad_for_status(config: WorkerConfig, status: str) -> UUID | None:
    return config.target_squads_by_status.get(status)


def find_current_target_squad(config: WorkerConfig, squad_uuids: list[UUID]) -> UUID | None:
    target_squad_uuids = set(config.target_squads_by_status.values())
    for squad_uuid in squad_uuids:
        if squad_uuid in target_squad_uuids:
            return squad_uuid
    return None


def extract_internal_squad_uuids(user: object) -> list[UUID]:
    result: list[UUID] = []
    for squad in getattr(user, "active_internal_squads", []) or []:
        raw_uuid = getattr(squad, "uuid", squad)
        squad_uuid = UUID(str(raw_uuid))
        if squad_uuid not in result:
            result.append(squad_uuid)
    return result


def calculate_extended_expire_at(current_expire_at: datetime, *, extend_days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=extend_days)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def should_extend_again(state: UserState | None, *, extend_days: int) -> bool:
    if state is None or not state.last_extended_at:
        return True

    last_extended_at = datetime.fromisoformat(state.last_extended_at)
    last_extended_at = ensure_aware_utc(last_extended_at)
    return datetime.now(timezone.utc) >= last_extended_at + timedelta(days=extend_days)


def is_managed_by_worker(state: UserState | None) -> bool:
    return state is not None and state.managed_by_worker


def normalize_status(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).upper().rsplit(".", maxsplit=1)[-1]
