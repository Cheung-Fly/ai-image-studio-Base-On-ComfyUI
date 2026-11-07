"""Create and restore complete backups of the Docker Compose data directory."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "backup-manifest.json"
FORMAT_VERSION = 1


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def create_backup(data_dir: Path, output_path: Path) -> Path:
    source_arg = Path(data_dir).expanduser()
    if source_arg.is_symlink():
        raise ValueError("The data directory must not be a symlink.")
    source = source_arg.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Data directory does not exist: {source}")

    output = Path(output_path).expanduser().resolve()
    if _inside(output, source):
        raise ValueError("The backup archive must be outside the data directory.")
    if output.exists():
        raise FileExistsError(f"Backup already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with ZipFile(temp_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Cannot back up symlink: {path}")
                relative = path.relative_to(source).as_posix()
                archive_name = f"data/{relative}"
                if path.is_dir():
                    archive.writestr(f"{archive_name}/", b"")
                elif path.is_file():
                    archive.write(path, archive_name)
        os.replace(temp_path, output)
    finally:
        temp_path.unlink(missing_ok=True)
    return output


def _safe_data_parts(name: str) -> tuple[str, ...]:
    if "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or path.parts[0] != "data":
        raise ValueError(f"Unexpected archive path: {name}")
    parts = path.parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Unsafe archive path: {name}")
    return parts


def _extract_backup(archive_path: Path, destination: Path) -> None:
    with ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(name.casefold() for name in names)):
            raise ValueError("Backup archive contains duplicate paths.")
        if MANIFEST_NAME not in names:
            raise ValueError("Backup archive has no manifest.")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup manifest is invalid.") from exc
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("Unsupported backup format version.")

        destination.mkdir(parents=True, exist_ok=True)
        for info in infos:
            if info.filename == MANIFEST_NAME:
                continue
            parts = _safe_data_parts(info.filename)
            if not parts and not info.is_dir():
                raise ValueError(f"Unsafe archive path: {info.filename}")
            target = destination.joinpath(*parts)
            if not _inside(target.resolve(), destination.resolve()):
                raise ValueError(f"Unsafe archive path: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def restore_backup(archive_path: Path, data_dir: Path) -> Path | None:
    archive = Path(archive_path).expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {archive}")

    target_arg = Path(data_dir).expanduser()
    if target_arg.is_symlink():
        raise ValueError("The data directory must not be a symlink.")
    target = target_arg.absolute()
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"Data path is not a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    stage_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    staged_data = stage_root / "data"
    previous_data = None
    try:
        _extract_backup(archive, staged_data)
        if target.exists():
            previous_data = target.with_name(f"{target.name}.pre-restore-{_utc_stamp()}")
            if previous_data.exists():
                raise FileExistsError(f"Preserved data directory already exists: {previous_data}")
            os.replace(target, previous_data)
        try:
            os.replace(staged_data, target)
        except OSError:
            if previous_data is not None and not target.exists():
                os.replace(previous_data, target)
            raise
        return previous_data
    finally:
        shutil.rmtree(stage_root)


def _require_stopped_services(skip_check: bool) -> None:
    if skip_check:
        return
    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker CLI is unavailable; stop the app manually and use --skip-service-check."
        )
    result = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "--services"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or "docker compose ps failed"
        raise RuntimeError(f"Unable to verify service state: {detail}")
    running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    active = sorted(running & {"api", "worker"})
    if active:
        raise RuntimeError(
            f"Stop these services before backup or restore: {', '.join(active)}."
        )


def _default_backup_path() -> Path:
    return ROOT / "backups" / f"ai-image-studio-data-{_utc_stamp()}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    backup_parser = commands.add_parser("backup", help="Archive the complete data directory.")
    backup_parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    backup_parser.add_argument("--output", type=Path, default=None)
    backup_parser.add_argument("--skip-service-check", action="store_true")

    restore_parser = commands.add_parser("restore", help="Restore a data archive.")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    restore_parser.add_argument("--skip-service-check", action="store_true")

    args = parser.parse_args(argv)
    try:
        _require_stopped_services(args.skip_service_check)
        if args.command == "backup":
            output = create_backup(args.data_dir, args.output or _default_backup_path())
            print(f"Backup created: {output}")
        else:
            previous = restore_backup(args.archive, args.data_dir)
            print(f"Data restored to: {Path(args.data_dir).expanduser().absolute()}")
            if previous is not None:
                print(f"Previous data preserved at: {previous}")
    except (
        BadZipFile,
        FileExistsError,
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
