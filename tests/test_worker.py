from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from remnawave.enums import TrafficLimitStrategy, UserStatus

from app.config import RemnawaveConfig, WorkerConfig
from app.state import StateStore
from app.worker import calculate_extended_expire_at, scan_once


EXPIRED_TARGET_SQUAD = UUID("e9534880-836d-41bc-9dc4-a453056ad5d1")
LIMITED_TARGET_SQUAD = UUID("06e88dc4-6b6e-4db0-8de0-28c68ac12025")


class FakeUsersApi:
    def __init__(self, users: list[object]) -> None:
        self.users = users
        self.updates = []
        self.traffic_resets = []
        self.extensions = []
        self.added_squads = []
        self.removed_squads = []
        self.added_traffic = []

    async def get_all_users(self, start: int | None = None, size: int | None = None):
        start = start or 0
        size = size or len(self.users)
        return SimpleNamespace(
            users=self.users[start:start + size],
            total=len(self.users),
        )

    async def update_user(self, body):
        self.updates.append(body)

    async def reset_user_traffic(self, uuid: str):
        self.traffic_resets.append(uuid)

    async def extend_subscription(self, subscription_id: int, days: int):
        self.extensions.append((subscription_id, days))
        return {
            "id": subscription_id,
            "end_date": "2026-01-04T00:00:00Z",
        }

    async def add_subscription_squad(self, subscription_id: int, squad_uuid: str):
        self.added_squads.append((subscription_id, squad_uuid))

    async def remove_subscription_squad(self, subscription_id: int, squad_uuid: str):
        self.removed_squads.append((subscription_id, squad_uuid))

    async def add_subscription_traffic(self, subscription_id: int, gb: int):
        self.added_traffic.append((subscription_id, gb))


class FakeSdk:
    def __init__(self, users: list[object]) -> None:
        self.users = FakeUsersApi(users)


@pytest.fixture()
def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        api_backend="remnawave",
        remnawave=RemnawaveConfig(
            api_base="https://example.test/api",
            api_token="token",
            caddy_token=None,
            ssl_ignore=False,
        ),
        bot_api=None,
        target_squads_by_status={
            "EXPIRED": EXPIRED_TARGET_SQUAD,
            "LIMITED": LIMITED_TARGET_SQUAD,
        },
        target_statuses=frozenset({"EXPIRED", "LIMITED"}),
        extend_days=3,
        traffic_limit_bytes=1024 ** 3,
        scan_interval_seconds=60,
        page_size=500,
        state_db_path=tmp_path / "state.sqlite3",
        dry_run=False,
        log_level="INFO",
    )


def make_user(*, status: str, uuid: UUID | None = None, squads=None):
    return SimpleNamespace(
        uuid=uuid or uuid4(),
        username="user",
        status=status,
        expire_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        active_internal_squads=[
            SimpleNamespace(uuid=squad_uuid)
            for squad_uuid in (squads or [])
        ],
    )


def make_subscription(*, status: str, subscription_id: int = 123, squads=None):
    return SimpleNamespace(
        uuid=str(subscription_id),
        subscription_id=subscription_id,
        user_id=456,
        username="user:456",
        status=status,
        expire_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        active_internal_squads=[
            SimpleNamespace(uuid=squad_uuid)
            for squad_uuid in (squads or [])
        ],
    )


def test_calculate_extended_expire_at_ignores_existing_future_date() -> None:
    before = datetime.now(timezone.utc)
    future = datetime.now(timezone.utc) + timedelta(days=10)
    result = calculate_extended_expire_at(future, extend_days=3)
    after = datetime.now(timezone.utc)

    assert before + timedelta(days=3) <= result <= after + timedelta(days=3)
    assert result < future


@pytest.mark.asyncio
async def test_user_is_processed_once_until_status_leaves_target_cycle(
    config: WorkerConfig,
) -> None:
    user_uuid = uuid4()
    old_squad = uuid4()
    user = make_user(status="EXPIRED", uuid=user_uuid, squads=[old_squad])
    state = StateStore(config.state_db_path)

    try:
        sdk = FakeSdk([user])
        first = await scan_once(config, sdk, state)
        assert first.processed == 1
        assert len(sdk.users.updates) == 1
        assert sdk.users.traffic_resets == [str(user_uuid)]
        assert sdk.users.updates[0].active_internal_squads == [EXPIRED_TARGET_SQUAD]
        assert sdk.users.updates[0].status == UserStatus.ACTIVE
        assert sdk.users.updates[0].traffic_limit_bytes == 1024 ** 3
        assert sdk.users.updates[0].traffic_limit_strategy == TrafficLimitStrategy.NO_RESET
        saved_state = state.get(str(user_uuid))
        assert saved_state is not None
        assert saved_state.last_extended_at is not None
        assert saved_state.extension_count == 1

        active_target_user = make_user(
            status="ACTIVE",
            uuid=user_uuid,
            squads=[EXPIRED_TARGET_SQUAD],
        )
        sdk = FakeSdk([active_target_user])
        second = await scan_once(config, sdk, state)
        assert second.processed == 0
        assert len(sdk.users.updates) == 0

        active_user_elsewhere = make_user(status="ACTIVE", uuid=user_uuid, squads=[uuid4()])
        reset = await scan_once(config, FakeSdk([active_user_elsewhere]), state)
        assert reset.matched == 0

        expired_again = make_user(
            status="LIMITED",
            uuid=user_uuid,
            squads=[uuid4()],
        )
        sdk = FakeSdk([expired_again])
        third = await scan_once(config, sdk, state)
        assert third.processed == 1
        assert len(sdk.users.updates) == 1
        assert sdk.users.updates[0].active_internal_squads == [LIMITED_TARGET_SQUAD]
    finally:
        state.close()


@pytest.mark.asyncio
async def test_active_user_in_target_squad_is_renewed_after_extend_period(
    config: WorkerConfig,
) -> None:
    user_uuid = uuid4()
    old_extended_at = datetime.now(timezone.utc) - timedelta(days=3, minutes=1)
    state = StateStore(config.state_db_path)
    state.record_extension(
        str(user_uuid),
        "ACTIVE",
        expire_at=datetime.now(timezone.utc),
        extended_at=old_extended_at,
    )
    user = make_user(status="ACTIVE", uuid=user_uuid, squads=[LIMITED_TARGET_SQUAD])
    sdk = FakeSdk([user])

    try:
        stats = await scan_once(config, sdk, state)
        assert stats.processed == 1
        assert stats.renewed == 1
        assert len(sdk.users.updates) == 1
        assert sdk.users.traffic_resets == [str(user_uuid)]
        assert sdk.users.updates[0].active_internal_squads == [LIMITED_TARGET_SQUAD]
        assert sdk.users.updates[0].traffic_limit_bytes == 1024 ** 3
        saved_state = state.get(str(user_uuid))
        assert saved_state is not None
        assert saved_state.extension_count == 2
    finally:
        state.close()


@pytest.mark.asyncio
async def test_active_user_in_target_squad_without_state_is_not_renewed(
    config: WorkerConfig,
) -> None:
    user = make_user(status="ACTIVE", squads=[EXPIRED_TARGET_SQUAD])
    state = StateStore(config.state_db_path)
    sdk = FakeSdk([user])

    try:
        stats = await scan_once(config, sdk, state)
        assert stats.processed == 0
        assert stats.renewed == 0
        assert len(sdk.users.updates) == 0
        assert len(sdk.users.traffic_resets) == 0
    finally:
        state.close()


@pytest.mark.asyncio
async def test_managed_user_in_service_squad_is_not_topped_up_before_period_ends(
    config: WorkerConfig,
) -> None:
    user_uuid = uuid4()
    state = StateStore(config.state_db_path)
    state.record_extension(
        str(user_uuid),
        "EXPIRED",
        expire_at=datetime.now(timezone.utc) + timedelta(days=3),
        extended_at=datetime.now(timezone.utc),
    )
    user = make_user(
        status="LIMITED",
        uuid=user_uuid,
        squads=[EXPIRED_TARGET_SQUAD],
    )
    sdk = FakeSdk([user])

    try:
        stats = await scan_once(config, sdk, state)
        assert stats.processed == 0
        assert stats.skipped_processed == 1
        assert len(sdk.users.updates) == 0
        assert len(sdk.users.traffic_resets) == 0
    finally:
        state.close()


@pytest.mark.asyncio
async def test_user_who_left_target_squad_is_not_renewed_after_returning_manually(
    config: WorkerConfig,
) -> None:
    user_uuid = uuid4()
    old_extended_at = datetime.now(timezone.utc) - timedelta(days=5)
    state = StateStore(config.state_db_path)
    state.record_extension(
        str(user_uuid),
        "ACTIVE",
        expire_at=datetime.now(timezone.utc),
        extended_at=old_extended_at,
    )

    try:
        user_elsewhere = make_user(status="ACTIVE", uuid=user_uuid, squads=[uuid4()])
        await scan_once(config, FakeSdk([user_elsewhere]), state)

        user_back_in_target = make_user(
            status="ACTIVE",
            uuid=user_uuid,
            squads=[EXPIRED_TARGET_SQUAD],
        )
        sdk = FakeSdk([user_back_in_target])
        stats = await scan_once(config, sdk, state)

        assert stats.processed == 0
        assert stats.renewed == 0
        assert len(sdk.users.updates) == 0
        assert len(sdk.users.traffic_resets) == 0
    finally:
        state.close()


@pytest.mark.asyncio
async def test_active_user_outside_target_squad_is_not_renewed_even_when_due(
    config: WorkerConfig,
) -> None:
    user_uuid = uuid4()
    old_extended_at = datetime.now(timezone.utc) - timedelta(days=5)
    state = StateStore(config.state_db_path)
    state.record_extension(
        str(user_uuid),
        "ACTIVE",
        expire_at=datetime.now(timezone.utc),
        extended_at=old_extended_at,
    )
    user = make_user(status="ACTIVE", uuid=user_uuid, squads=[uuid4()])
    sdk = FakeSdk([user])

    try:
        stats = await scan_once(config, sdk, state)
        assert stats.processed == 0
        assert len(sdk.users.updates) == 0
        saved_state = state.get(str(user_uuid))
        assert saved_state is not None
        assert saved_state.managed_by_worker is False
    finally:
        state.close()


@pytest.mark.asyncio
async def test_dry_run_marks_processed_without_calling_update(
    config: WorkerConfig,
) -> None:
    dry_config = replace(config, dry_run=True)
    state = StateStore(dry_config.state_db_path)
    sdk = FakeSdk([make_user(status="LIMITED")])

    try:
        stats = await scan_once(dry_config, sdk, state)
        assert stats.processed == 1
        assert len(sdk.users.updates) == 0
        assert len(sdk.users.traffic_resets) == 0
        saved_state = state.get(str(sdk.users.users[0].uuid))
        assert saved_state is not None
        assert saved_state.last_extended_at is None
    finally:
        state.close()


@pytest.mark.asyncio
async def test_bot_backend_extends_subscription_and_sets_target_squad(
    config: WorkerConfig,
) -> None:
    old_squad = uuid4()
    bot_config = replace(
        config,
        api_backend="bot",
        page_size=200,
        traffic_limit_bytes=1024 ** 3,
    )
    subscription = make_subscription(
        status="expired",
        subscription_id=123,
        squads=[old_squad],
    )
    state = StateStore(bot_config.state_db_path)
    sdk = FakeSdk([subscription])

    try:
        stats = await scan_once(bot_config, sdk, state)

        assert stats.processed == 1
        assert sdk.users.extensions == [(123, 3)]
        assert sdk.users.added_squads == [(123, str(EXPIRED_TARGET_SQUAD))]
        assert sdk.users.removed_squads == [(123, str(old_squad))]
        assert sdk.users.added_traffic == [(123, 1)]
        saved_state = state.get("123")
        assert saved_state is not None
        assert saved_state.extension_count == 1
    finally:
        state.close()
