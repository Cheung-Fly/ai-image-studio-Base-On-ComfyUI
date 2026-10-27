"""Verify singleton ComfyUI preference storage without touching the app database."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AgentMemory
from app.services.agent_memory import clear_preferences, get_preferences, update_preferences


def main() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        defaults = get_preferences(db)
        assert defaults["style"] == "" and defaults["aspect_ratio"] is None, defaults

        updated = update_preferences(
            db,
            {
                "style": "soft watercolor",
                "aspect_ratio": "16:9 (Widescreen)",
                "megapixels": 2.0,
                "notes": "Prefer warm lighting",
            },
        )
        assert updated["style"] == "soft watercolor", updated
        assert updated["megapixels"] == 2.0, updated

        partial = update_preferences(db, {"notes": "Use natural light"})
        assert partial["style"] == "soft watercolor", partial
        assert partial["notes"] == "Use natural light", partial

        try:
            update_preferences(db, {"aspect_ratio": "not-an-aspect"})
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid aspect ratio was accepted")

        cleared = clear_preferences(db)
        assert cleared["style"] == "" and cleared["updated_at"] is None, cleared
        assert db.get(AgentMemory, 1) is None

    print("AGENT MEMORY TEST PASSED")


if __name__ == "__main__":
    main()
