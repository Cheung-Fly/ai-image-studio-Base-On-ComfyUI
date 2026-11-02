from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.api.instance import router as instance_router
from app.database import Base, get_db
from app.main import app as main_app


def test_instance_api_is_available_without_credentials():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    test_sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with test_sessions() as db:
            yield db

    test_app = FastAPI()
    test_app.include_router(instance_router, prefix="/api")
    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app) as client:
        response = client.get("/api/instance")

    assert response.status_code == 200
    assert "comfyui_web_url" in response.json()
    assert "/api/auth/login" not in main_app.openapi()["paths"]
    assert "/api/auth/register" not in main_app.openapi()["paths"]
