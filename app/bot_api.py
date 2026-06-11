from __future__ import annotations

import asyncio
import json
import ssl
from datetime import datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.config import BotApiConfig


class BotApiError(RuntimeError):
    pass


class BotApiClient:
    def __init__(self, config: BotApiConfig) -> None:
        self._base_url = config.api_base.rstrip("/")
        self._api_key = config.api_key
        self._ssl_context = (
            ssl._create_unverified_context() if config.ssl_ignore else None
        )
        self.users = BotSubscriptionsApi(self)

    async def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
        return await self._request_json("GET", path, params=params)

    async def post_json(
        self,
        path: str,
        body: dict[str, object] | None = None,
    ) -> object:
        return await self._request_json("POST", path, body=body or {})

    async def delete_json(self, path: str) -> object:
        return await self._request_json("DELETE", path)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> object:
        return await asyncio.to_thread(
            self._request_json_sync,
            method,
            path,
            params,
            body,
        )

    def _request_json_sync(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None,
        body: dict[str, object] | None,
    ) -> object:
        url = f"{self._base_url}{path}"
        if params:
            query = urlencode(
                {
                    key: value
                    for key, value in params.items()
                    if value is not None
                }
            )
            if query:
                url = f"{url}?{query}"

        data = None
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30, context=self._ssl_context) as response:
                raw = response.read()
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise BotApiError(
                f"Bot API {method} {path} failed: {exc.code} {message}"
            ) from exc

        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    async def close(self) -> None:
        return None


class BotSubscriptionsApi:
    def __init__(self, client: BotApiClient) -> None:
        self._client = client

    async def get_all_users(self, start: int | None = None, size: int | None = None):
        offset = start or 0
        limit = size or 200
        payload = await self._client.get_json(
            "/subscriptions",
            {"offset": offset, "limit": limit},
        )
        subscriptions = [
            _subscription_to_user(item)
            for item in payload if isinstance(item, dict)
        ]
        total = offset + len(subscriptions)
        if len(subscriptions) == limit:
            total += 1
        return SimpleNamespace(users=subscriptions, total=total)

    async def extend_subscription(self, subscription_id: int, days: int) -> object:
        return await self._client.post_json(
            f"/subscriptions/{subscription_id}/extend",
            {"days": days},
        )

    async def add_subscription_squad(
        self,
        subscription_id: int,
        squad_uuid: str,
    ) -> object:
        return await self._client.post_json(
            f"/subscriptions/{subscription_id}/squads",
            {"squad_uuid": squad_uuid},
        )

    async def add_subscription_traffic(self, subscription_id: int, gb: int) -> object:
        return await self._client.post_json(
            f"/subscriptions/{subscription_id}/traffic",
            {"gb": gb},
        )

    async def remove_subscription_squad(
        self,
        subscription_id: int,
        squad_uuid: str,
    ) -> object:
        escaped_squad_uuid = quote(squad_uuid, safe="")
        return await self._client.delete_json(
            f"/subscriptions/{subscription_id}/squads/{escaped_squad_uuid}",
        )


def _subscription_to_user(item: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=str(item["id"]),
        subscription_id=int(item["id"]),
        user_id=item.get("user_id"),
        username=f"user:{item.get('user_id')}",
        status=item.get("actual_status") or item.get("status") or "",
        expire_at=_parse_datetime(item.get("end_date")),
        active_internal_squads=list(item.get("connected_squads") or []),
        raw=item,
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
