"""RQ worker entrypoint (jobs are registered by feature sets)."""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.logging import configure_logging


def run_worker() -> None:
    """Start an RQ worker consuming the configured queue."""
    configure_logging(get_settings().log_level)
    from redis import Redis
    from rq import Queue, Worker

    redis = Redis.from_url(get_settings().redis_url)
    worker = Worker([Queue("default", connection=redis)], connection=redis)
    logging.getLogger(__name__).info("worker.start", extra={"event": "worker.start"})
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    run_worker()
