"""worker: Celery tasks (broker: Valkey). Run with the thread pool so OTel works without fork hooks."""

import logging
import time
from typing import Any

import psycopg
from celery import Celery
from celery.signals import after_setup_logger, worker_ready

from aviro.common.config import get_settings
from aviro.common.logs import setup_logging

settings = get_settings().model_copy(update={"service": "worker"})
log = logging.getLogger("aviro.worker")

celery = Celery("aviro", broker=settings.broker_url)
celery.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=4,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)


@after_setup_logger.connect
def _json_logs(**_: Any) -> None:
    setup_logging("worker", settings.log_level)


@worker_ready.connect
def _ready(**_: Any) -> None:
    log.info("worker ready")


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        settings.database_url,
        options=f"-c statement_timeout={settings.db_statement_timeout_ms}",
        application_name="aviro-worker",
    )


@celery.task(name="aviro.fulfil_order", bind=True, max_retries=3, default_retry_delay=2)
def fulfil_order(
    self: Any,
    order: dict[str, Any] | None = None,
    order_id: str | None = None,
    customer_email: str | None = None,
) -> dict[str, str]:
    """Pick, pack and "email" the customer. Marks the order fulfilled.

    Accepts the full ``order`` payload and the older ``order_id`` form.
    """
    if order is not None:
        order_id, customer_email = order["order_id"], order["customer_email"]
    if order_id is None or customer_email is None:
        raise ValueError("fulfil_order needs an order")
    time.sleep(0.05)  # pretend to talk to the warehouse
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = 'fulfilled', fulfilled_at = now() "
            "WHERE id = %s AND status = 'paid' RETURNING id",
            (order_id,),
        )
        updated = cur.fetchone() is not None
    log.info(
        "order fulfilled" if updated else "order already fulfilled",
        extra={"order_id": order_id, "customer_domain": customer_email.split("@")[-1]},
    )
    return {"order_id": order_id, "status": "fulfilled"}
