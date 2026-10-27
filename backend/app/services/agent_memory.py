"""Singleton personal preferences for the ComfyUI assistant."""
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import AgentMemory
from ..schemas import AgentMemoryUpdate
from .model_preset import list_presets
from .workflow import list_workflows

DEFAULT_PREFERENCES: dict[str, Any] = {
    "style": "",
    "model_preset": "",
    "workflow": "",
    "aspect_ratio": None,
    "megapixels": None,
    "negative_prompt": "",
    "notes": "",
}


def get_preferences(db: Session) -> dict[str, Any]:
    row = db.get(AgentMemory, 1)
    if row is None:
        return {**DEFAULT_PREFERENCES, "updated_at": None}
    try:
        stored = json.loads(row.preferences)
        validated = AgentMemoryUpdate.model_validate(stored).model_dump()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("个人助手记忆数据损坏，请清除后重新设置") from exc
    updated_at = row.updated_at.isoformat() if row.updated_at else None
    return {**DEFAULT_PREFERENCES, **validated, "updated_at": updated_at}


def update_preferences(db: Session, patch: dict[str, Any]) -> dict[str, Any]:
    validated_patch = AgentMemoryUpdate.model_validate(patch).model_dump(exclude_unset=True)
    current = get_preferences(db)
    values = {key: current[key] for key in DEFAULT_PREFERENCES}
    for key, value in validated_patch.items():
        if value is None and key not in {"aspect_ratio", "megapixels"}:
            value = ""
        values[key] = value

    workflows = set(list_workflows())
    if values["workflow"] and values["workflow"] not in workflows:
        raise ValueError(f"工作流不存在: {values['workflow']}")
    presets = {item["name"] for item in list_presets()}
    if values["model_preset"] and values["model_preset"] not in presets:
        raise ValueError(f"模型预设不存在: {values['model_preset']}")

    row = db.get(AgentMemory, 1)
    if row is None:
        row = AgentMemory(id=1, preferences="{}")
        db.add(row)
    row.preferences = json.dumps(values, ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    updated_at = row.updated_at.isoformat() if row.updated_at else None
    return {**values, "updated_at": updated_at}


def clear_preferences(db: Session) -> dict[str, Any]:
    row = db.get(AgentMemory, 1)
    if row is not None:
        db.delete(row)
        db.commit()
    return {**DEFAULT_PREFERENCES, "updated_at": None}
