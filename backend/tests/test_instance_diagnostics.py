from unittest.mock import Mock

from app.api import instance


def _healthy_check(check_id: str, label: str) -> dict[str, str]:
    return {
        "id": check_id,
        "label": label,
        "status": "ok",
        "message": f"{label} 正常。",
    }


def test_diagnostics_reports_ready_without_exposing_service_details(monkeypatch):
    checks = [
        ("_check_database", "database", "数据库"),
        ("_check_redis", "redis", "Redis 队列"),
        ("_check_worker", "worker", "Celery Worker"),
        ("_check_comfyui", "comfyui", "ComfyUI"),
        ("_check_storage", "storage", "媒体存储"),
        ("_check_llm", "llm", "默认语言模型"),
    ]
    for function_name, check_id, label in checks:
        monkeypatch.setattr(
            instance,
            function_name,
            lambda check_id=check_id, label=label: _healthy_check(check_id, label),
        )

    result = instance.get_instance_diagnostics(_user=Mock())

    assert result["status"] == "ready"
    assert {check["id"] for check in result["checks"]} == {
        "database", "redis", "worker", "comfyui", "storage", "llm"
    }
    serialized = str(result)
    assert "127.0.0.1" not in serialized
    assert "api_key" not in serialized.lower()


def test_diagnostics_degrades_if_required_dependency_is_unavailable(monkeypatch):
    monkeypatch.setattr(instance, "_check_database", lambda: _healthy_check("database", "数据库"))
    monkeypatch.setattr(instance, "_check_redis", lambda: _healthy_check("redis", "Redis"))
    monkeypatch.setattr(
        instance,
        "_check_worker",
        lambda: {
            "id": "worker",
            "label": "Celery Worker",
            "status": "error",
            "message": "未检测到可用 Worker。",
        },
    )
    monkeypatch.setattr(instance, "_check_comfyui", lambda: _healthy_check("comfyui", "ComfyUI"))
    monkeypatch.setattr(instance, "_check_storage", lambda: _healthy_check("storage", "媒体存储"))
    monkeypatch.setattr(
        instance,
        "_check_llm",
        lambda: {
            "id": "llm",
            "label": "默认语言模型",
            "status": "warning",
            "message": "默认模型未配置。",
        },
    )

    result = instance.get_instance_diagnostics(_user=Mock())

    assert result["status"] == "degraded"
    assert next(check for check in result["checks"] if check["id"] == "worker")["status"] == "error"
