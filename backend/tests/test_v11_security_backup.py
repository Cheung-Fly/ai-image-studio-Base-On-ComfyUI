"""V11 secret encryption and data backup/restore regression checks."""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from scripts.backup_data import create_backup, restore_backup  # noqa: E402

from app.api.custom_models import create_config  # noqa: E402
from app.cli.rotate_secrets import rotate_stored_secrets  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import CustomModelConfig, RunningHubConfig, User  # noqa: E402
from app.schemas import CustomModelIn  # noqa: E402
from app.security import (  # noqa: E402
    SecretConfigurationError,
    _derive_legacy_keystream,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_with_current_key,
)

TEST_KEY = "v11-test-key-" + ("x" * 40)


def _legacy_ciphertext(plaintext: str, key: str) -> str:
    nonce = bytes(range(16))
    data = plaintext.encode("utf-8")
    stream = _derive_legacy_keystream(key.encode("utf-8"), nonce, len(data))
    ciphertext = bytes(left ^ right for left, right in zip(data, stream, strict=True))
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


class SecretEncryptionTests(unittest.TestCase):
    def test_authenticated_tokens_round_trip_and_reject_tampering(self):
        with patch.object(settings, "encryption_key", TEST_KEY):
            token = encrypt_secret("provider-api-key")
            self.assertTrue(token.startswith("v1:"))
            self.assertNotEqual(token, encrypt_secret("provider-api-key"))
            self.assertEqual(decrypt_secret(token), "provider-api-key")

            raw = bytearray(base64.urlsafe_b64decode(token[3:]))
            raw[-20] ^= 1
            damaged = "v1:" + base64.urlsafe_b64encode(raw).decode("ascii")
            with self.assertRaisesRegex(ValueError, "完整性校验失败"):
                decrypt_secret(damaged)

    def test_rejects_missing_or_short_encryption_key(self):
        for key in ("", "too-short", "change-me-in-production-encryption-key"):
            with self.subTest(key_length=len(key)), patch.object(
                settings, "encryption_key", key
            ), self.assertRaises(SecretConfigurationError):
                encrypt_secret("provider-api-key")

    def test_custom_model_endpoint_reports_missing_key_as_unavailable(self):
        request = CustomModelIn(
            name="test",
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="provider-api-key",
        )
        with patch.object(settings, "encryption_key", ""), self.assertRaises(
            HTTPException
        ) as raised:
            create_config(request, User(id=1), None)
        self.assertEqual(raised.exception.status_code, 503)

    def test_reads_legacy_ciphertext_with_current_key(self):
        with patch.object(settings, "encryption_key", TEST_KEY):
            token = _legacy_ciphertext("legacy-api-key", TEST_KEY)
            self.assertEqual(decrypt_secret(token), "legacy-api-key")

    def test_reads_legacy_ciphertext_with_migration_key(self):
        old_key = "previous-installation-key"
        with (
            patch.object(settings, "encryption_key", TEST_KEY),
            patch.object(settings, "legacy_encryption_key", old_key),
        ):
            token = _legacy_ciphertext("legacy-api-key", old_key)
            self.assertEqual(decrypt_secret(token), "legacy-api-key")

    def test_reads_versioned_ciphertext_with_migration_key(self):
        old_key = "previous-versioned-key-" + ("y" * 32)
        with patch.object(settings, "encryption_key", old_key):
            token = encrypt_secret("versioned-api-key")
        with (
            patch.object(settings, "encryption_key", TEST_KEY),
            patch.object(settings, "legacy_encryption_key", old_key),
        ):
            self.assertEqual(decrypt_secret(token), "versioned-api-key")


class SecretRotationTests(unittest.TestCase):
    def test_rotation_migrates_all_secret_rows_and_is_idempotent(self):
        old_key = "previous-installation-key-" + ("o" * 32)
        new_key = "current-installation-key-" + ("n" * 32)
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)

        with patch.object(settings, "encryption_key", old_key):
            custom_token = encrypt_secret("custom-api-key")
            runninghub_token = _legacy_ciphertext("runninghub-api-key", old_key)
        with Session(engine) as db:
            db.add(User(id=1, username="rotation-test", password_hash="not-a-secret"))
            db.add(
                CustomModelConfig(
                    user_id=1,
                    name="test",
                    base_url="https://example.invalid/v1",
                    model="test-model",
                    api_key_encrypted=custom_token,
                )
            )
            db.add(
                RunningHubConfig(
                    id=1,
                    workflow_id="workflow",
                    prompt_node_id="1",
                    prompt_field="text",
                    api_key_encrypted=runninghub_token,
                )
            )
            db.commit()

        with (
            patch.object(settings, "encryption_key", new_key),
            patch.object(settings, "legacy_encryption_key", old_key),
            Session(engine) as db,
        ):
            self.assertEqual(rotate_stored_secrets(db), 2)
            self.assertEqual(rotate_stored_secrets(db), 0)
            custom = db.query(CustomModelConfig).one()
            runninghub = db.get(RunningHubConfig, 1)
            self.assertTrue(is_encrypted_with_current_key(custom.api_key_encrypted))
            self.assertTrue(is_encrypted_with_current_key(runninghub.api_key_encrypted))
            self.assertEqual(decrypt_secret(custom.api_key_encrypted), "custom-api-key")
            self.assertEqual(
                decrypt_secret(runninghub.api_key_encrypted), "runninghub-api-key"
            )
            self.assertNotIn("api-key", custom.api_key_encrypted)
        engine.dispose()


class DataBackupTests(unittest.TestCase):
    def test_backup_restore_preserves_data_and_keeps_pre_restore_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source-data"
            source.mkdir()
            (source / "aistudio.db").write_bytes(b"database")
            (source / "images").mkdir()
            (source / "images" / "result.png").write_bytes(b"image-bytes")
            (source / "empty").mkdir()
            archive = root / "backup.zip"
            create_backup(source, archive)

            target = root / "data"
            target.mkdir()
            (target / "old.txt").write_text("preserve me", encoding="utf-8")
            previous = restore_backup(archive, target)

            self.assertIsNotNone(previous)
            self.assertEqual((previous / "old.txt").read_text(encoding="utf-8"), "preserve me")
            self.assertEqual((target / "aistudio.db").read_bytes(), b"database")
            self.assertEqual(
                (target / "images" / "result.png").read_bytes(), b"image-bytes"
            )
            self.assertTrue((target / "empty").is_dir())

    def test_rejects_unsafe_archive_paths_without_changing_live_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "unsafe.zip"
            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
                output.writestr(
                    "backup-manifest.json",
                    json.dumps({"format_version": 1}),
                )
                output.writestr("data/../../outside.txt", b"unsafe")
            target = root / "data"
            target.mkdir()
            (target / "keep.txt").write_text("safe", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
                restore_backup(archive, target)

            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "safe")
            self.assertFalse((root.parent / "outside.txt").exists())

    def test_backup_must_not_be_written_inside_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "data"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "outside the data directory"):
                create_backup(source, source / "backup.zip")


if __name__ == "__main__":
    unittest.main()
