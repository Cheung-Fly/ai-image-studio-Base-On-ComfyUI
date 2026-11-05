"""Persist and emit progress events for automatic image processing."""

import logging

from sqlalchemy.orm import Session

from ..models import AutoImageAsset, AutoImageEvent

logger = logging.getLogger(__name__)

_LOG_METHODS = {
    "info": logger.info,
    "warning": logger.warning,
    "error": logger.error,
}


def add_auto_image_event(
    db: Session,
    batch_id: int,
    event_type: str,
    message: str,
    *,
    level: str = "info",
    duration_ms: int | None = None,
) -> AutoImageEvent:
    """Persist a concise progress message and mirror it to service logs."""
    log_method = _LOG_METHODS.get(level)
    if log_method is None:
        raise ValueError(f"Unsupported automatic image log level: {level}")

    event = AutoImageEvent(
        batch_id=batch_id,
        event_type=event_type,
        level=level,
        message=message[:2000],
        duration_ms=duration_ms,
    )
    db.add(event)
    log_method(
        "auto_image batch_id=%s stage=%s duration_ms=%s message=%s",
        batch_id,
        event_type,
        duration_ms,
        event.message,
    )
    return event


def add_auto_image_task_event(
    db: Session,
    task_id: int,
    event_type: str,
    message: str,
    *,
    level: str = "info",
    duration_ms: int | None = None,
) -> AutoImageEvent | None:
    asset = (
        db.query(AutoImageAsset)
        .filter(AutoImageAsset.task_id == task_id)
        .first()
    )
    if asset is None:
        return None
    return add_auto_image_event(
        db,
        asset.batch_id,
        event_type,
        message,
        level=level,
        duration_ms=duration_ms,
    )
