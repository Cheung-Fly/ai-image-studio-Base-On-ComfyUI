"""Re-encrypt all stored API keys with the active ENCRYPTION_KEY."""

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, init_db
from ..models import CustomModelConfig, RunningHubConfig
from ..security import decrypt_secret, encrypt_secret, is_encrypted_with_current_key


def rotate_stored_secrets(db: Session) -> int:
    """Atomically migrate legacy or old-key ciphertexts to the active key."""
    changed = 0
    with db.begin():
        encrypt_secret("key-validation")
        for model in (CustomModelConfig, RunningHubConfig):
            for record in db.scalars(select(model)):
                token = record.api_key_encrypted
                if is_encrypted_with_current_key(token):
                    continue
                record.api_key_encrypted = encrypt_secret(decrypt_secret(token))
                changed += 1
    return changed


def main() -> int:
    try:
        init_db()
        with SessionLocal() as db:
            changed = rotate_stored_secrets(db)
    except ValueError as exc:
        print(f"Secret rotation aborted: {exc}", file=sys.stderr)
        return 1
    print(f"Re-encrypted {changed} stored API key(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
