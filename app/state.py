from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class UserState:
    user_uuid: str
    last_status: str
    managed_by_worker: bool
    in_target_squad: bool
    last_processed_at: str | None
    last_extended_at: str | None
    last_expire_at: str | None
    extension_count: int


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def get(self, user_uuid: str) -> UserState | None:
        row = self._conn.execute(
            """
            SELECT
                user_uuid,
                last_status,
                managed_by_worker,
                in_target_squad,
                last_processed_at,
                last_extended_at,
                last_expire_at,
                extension_count
            FROM user_status_state
            WHERE user_uuid = ?
            """,
            (user_uuid,),
        ).fetchone()
        if row is None:
            return None
        return UserState(
            user_uuid=row[0],
            last_status=row[1],
            managed_by_worker=bool(row[2]),
            in_target_squad=bool(row[3]),
            last_processed_at=row[4],
            last_extended_at=row[5],
            last_expire_at=row[6],
            extension_count=int(row[7]),
        )

    def mark_observed(
        self,
        user_uuid: str,
        status: str,
        *,
        in_target_squad: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO user_status_state (
                user_uuid,
                last_status,
                managed_by_worker,
                in_target_squad,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_uuid) DO UPDATE SET
                last_status = excluded.last_status,
                managed_by_worker = CASE
                    WHEN excluded.in_target_squad = 1
                    THEN user_status_state.managed_by_worker
                    ELSE 0
                END,
                in_target_squad = excluded.in_target_squad,
                updated_at = excluded.updated_at
            """,
            (
                user_uuid,
                status,
                0,
                1 if in_target_squad else 0,
                _utc_now_iso(),
            ),
        )
        self._conn.commit()

    def record_extension(
        self,
        user_uuid: str,
        status: str,
        *,
        expire_at: datetime,
        extended_at: datetime | None = None,
    ) -> None:
        extended_at_iso = _to_utc_iso(extended_at or datetime.now(timezone.utc))
        expire_at_iso = _to_utc_iso(expire_at)
        now_iso = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO user_status_state (
                user_uuid,
                last_status,
                managed_by_worker,
                in_target_squad,
                last_processed_at,
                last_extended_at,
                last_expire_at,
                extension_count,
                updated_at
            )
            VALUES (?, ?, 1, 1, ?, ?, ?, 1, ?)
            ON CONFLICT(user_uuid) DO UPDATE SET
                last_status = excluded.last_status,
                managed_by_worker = 1,
                in_target_squad = 1,
                last_processed_at = excluded.last_processed_at,
                last_extended_at = excluded.last_extended_at,
                last_expire_at = excluded.last_expire_at,
                extension_count = user_status_state.extension_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                user_uuid,
                status,
                extended_at_iso,
                extended_at_iso,
                expire_at_iso,
                now_iso,
            ),
        )
        self._conn.commit()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_status_state (
                user_uuid TEXT PRIMARY KEY,
                last_status TEXT NOT NULL,
                processed_in_target_cycle INTEGER NOT NULL DEFAULT 0,
                last_processed_at TEXT,
                managed_by_worker INTEGER NOT NULL DEFAULT 0,
                in_target_squad INTEGER NOT NULL DEFAULT 0,
                last_extended_at TEXT,
                last_expire_at TEXT,
                extension_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._add_missing_columns()
        self._conn.commit()

    def _add_missing_columns(self) -> None:
        existing_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(user_status_state)")
        }
        columns = {
            "managed_by_worker": "INTEGER NOT NULL DEFAULT 0",
            "in_target_squad": "INTEGER NOT NULL DEFAULT 0",
            "last_extended_at": "TEXT",
            "last_expire_at": "TEXT",
            "extension_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing_columns:
                self._conn.execute(
                    f"ALTER TABLE user_status_state ADD COLUMN {name} {definition}"
                )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
