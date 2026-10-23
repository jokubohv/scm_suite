"""Outbox relay worker: polls platform.outbox_event and publishes to Redis Streams."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from redis.asyncio import from_url as redis_from_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.outbox import relay_once
from app.db.session import get_engine

log = logging.getLogger("outbox.relay")
POLL_INTERVAL_SECONDS = 0.5


async def run() -> None:
    settings = get_settings()
    redis = redis_from_url(settings.redis_url, decode_responses=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(get_engine(), expire_on_commit=False)
    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows/dev fallback
            signal.signal(sig, _stop)

    log.info("outbox.relay.started")
    while not stop.is_set():
        published = 0
        try:
            async with factory() as session:
                published = await relay_once(session, redis)
                await session.commit()
        except Exception:  # noqa: BLE001 - keep the relay alive; log and retry
            log.exception("outbox.relay.batch_failed")
        if published == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass
    await redis.aclose()
    log.info("outbox.relay.stopped")


def main() -> None:
    configure_logging(get_settings().log_level)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
