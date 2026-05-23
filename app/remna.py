from __future__ import annotations

from remnawave import RemnawaveSDK

from app.config import RemnawaveConfig


def create_sdk(config: RemnawaveConfig) -> RemnawaveSDK:
    return RemnawaveSDK(
        base_url=config.api_base,
        token=config.api_token,
        caddy_token=config.caddy_token,
        ssl_ignore=config.ssl_ignore,
    )


async def close_sdk(sdk: RemnawaveSDK) -> None:
    await sdk._client.aclose()
