"""Account archives: one stable copy, two offline operations, domain-owned facts.

Callers may supply a source lifecycle context. Entering it must drain and stop
all relevant writers; exiting must restore them, including after copy failure.
Without that context only an already stopped installation can be exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import AbstractContextManager, closing, nullcontext
from pathlib import Path, PurePosixPath

from .config import Settings
from .db.portability import read_account, rebind_account, snapshot_account
from .service_installation import (
    _DIRECTORIES,
    _FILES,
    _PROVIDER_HOMES,
    InstallationOwner,
    _service_environment,
)
from .storage_verification import _reader, verify_main

FORMAT = 1
PATH_SETTINGS = (*_DIRECTORIES, *_FILES)


def software_version() -> str:
    """Identify the installed Python implementation, including uncommitted builds."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _relative(path: Path, home: Path) -> str:
    try:
        result = path.relative_to(home).as_posix()
    except ValueError as error:
        raise ValueError(f"Migration supports the selected home layout only: {path}") from error
    if result == ".":
        raise ValueError("The whole home directory cannot be a migration resource")
    _safe_relative(result)
    return result


def _safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or "\\" in value:
        raise ValueError("Archive contains an invalid relative path")
    return Path(*path.parts)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root: Path) -> None:
    for directory, _folders, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
    directories = [Path(directory) for directory, _, _ in os.walk(root)]
    for path in reversed(directories):
        _sync_directory(path)


def _ensure_directory(path: Path, home: Path) -> None:
    """Confirm the involved parent chain, including earlier unconfirmed mkdirs."""
    for parent in (*reversed(path.parents), path):
        if parent == home or parent.is_relative_to(home):
            if parent.is_symlink():
                raise ValueError(f"Target path traverses a symlink: {parent}")
            if not parent.is_dir():
                parent.mkdir(mode=0o700)
            _sync_directory(parent)
            _sync_directory(parent.parent)


def _empty_targets(home: Path, roots: list[str]) -> None:
    for name in roots:
        path = home / _safe_relative(name)
        for parent in (home, *path.parents):
            if parent.is_relative_to(home) and parent.is_symlink():
                raise ValueError(f"Target path traverses a symlink: {parent}")
        if path.exists() or path.is_symlink():
            raise ValueError(f"Migration target is not empty: {path}")


def _minimal_roots(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    for path in sorted(set(paths), key=lambda p: (len(p.parts), str(p))):
        if not any(path == other or path.is_relative_to(other) for other in result):
            result.append(path)
    return result


def _copy_tree(source: Path, destination: Path, home: Path, excluded: set[Path]) -> dict[str, str]:
    links: dict[str, str] = {}

    def copy(path: Path) -> None:
        if path in excluded or path.name == ".octopus-installation":
            return
        relative = _relative(path, home)
        target = destination / relative
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            links[relative] = _relative(path.resolve(strict=True), home)
        elif stat.S_ISDIR(metadata.st_mode):
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            for child in sorted(path.iterdir()):
                copy(child)
            target.chmod(stat.S_IMODE(metadata.st_mode) & 0o777)
        elif stat.S_ISREG(metadata.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, target)
            target.chmod(stat.S_IMODE(metadata.st_mode) & 0o777)
        else:
            raise ValueError(f"Unsupported live or special file in selected data: {path}")

    copy(source)
    return links


def _application_resources(applications: list[dict]) -> tuple[list[Path], set[Path]]:
    from .applications import data_dir_for, runtime_dir_for

    roots = []
    excluded = set()
    for application in applications:
        code = Path(application["app_dir"]).resolve()
        deletion = json.loads(application["deletion"]) if application.get("deletion") else None
        if code.is_dir():
            roots.append(code)
        elif not (deletion and deletion["phase"] == "files" and not deletion["keep_files"] and not code.exists()):
            raise ValueError(f"Application code is missing: {code}")
        data = Path(data_dir_for(str(code)))
        if data.exists():
            roots.append(data)
        excluded.add(Path(runtime_dir_for(str(code))))
    return roots, excluded


def _copy_source(config: Settings, user_id: str, staging: Path, home: Path) -> dict:
    from .crypto import decrypt

    paths = {name: Path(getattr(config, name)).expanduser().resolve() for name in PATH_SETTINGS}
    for path in paths.values():
        _relative(path, home)
    source_key = config.master_key or paths["master_key_file"].read_text().strip()
    if not source_key:
        raise ValueError("The installation master key is missing or empty")
    db_relative = _relative(paths["db_path"], home)
    key_relative = _relative(paths["master_key_file"], home)
    account = paths["users_root"] / user_id
    _relative(account, home)
    with _reader(paths["db_path"]) as source:
        user = source.execute("SELECT dek_wrapped FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None or not user[0]:
            raise ValueError("The selected account or its data key is missing")
        decrypt(user[0], source_key)
        active = source.execute("""SELECT 1 FROM client_submissions c JOIN sessions s ON s.id=c.session_id
            WHERE s.user_id=? AND c.execution_state='running'
            UNION ALL SELECT 1 FROM bg_tasks b JOIN sessions s ON s.id=b.session_id
            WHERE s.user_id=? AND b.status IN ('running','pending')
            UNION ALL SELECT 1 FROM research_jobs r JOIN sessions s ON s.id=r.session_id
            WHERE s.user_id=? AND r.status='running' LIMIT 1""", (user_id, user_id, user_id)).fetchone()
        if active:
            raise ValueError("Selected account has unfinished writes; settle them on the source before exporting")
        applications = snapshot_account(source, staging / db_relative, user_id, key=source_key)
    roots = [account]
    excluded = {account / "codex", *(Path(os.environ.get(variable) or Path.home() / default).expanduser().resolve()
                 for variable, default in _PROVIDER_HOMES.values()),
                *(Path(str(paths["db_path"]) + suffix) for suffix in ("", "-wal", "-shm", "-journal")), paths["master_key_file"]}
    for app in applications:
        if app["status"] == "building" and app.get("deletion") is None:
            raise ValueError("Application build must settle on the source before exporting")
        app_dir = Path(app["app_dir"]).resolve()
        _relative(app_dir, home)
    app_roots, app_exclusions = _application_resources(applications)
    roots.extend(app_roots)
    excluded.update(app_exclusions)
    wiki = None
    state = account / "wiki"
    if (state / "config.json").exists() or (state / "config.json").is_symlink():
        from .wiki.portability import inspect_wiki

        wiki = inspect_wiki(state)
        roots.append(Path(wiki["root"]))
        excluded.update(state / name for name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", "observer.lock", "vault.lock"))
    links = {}
    for root in _minimal_roots(roots):
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"Selected resource is not a regular directory: {root}")
        links.update(_copy_tree(root, staging, home, excluded))
    if wiki:
        target_db = staging / _relative(state / "index.sqlite3", home)
        with _reader(state / "index.sqlite3") as source, closing(sqlite3.connect(target_db)) as copied_db:
            source.backup(copied_db)
    key = staging / key_relative
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key.write_text(source_key)
    key.chmod(0o600)
    entries = {}
    for path in sorted(staging.rglob("*")):
        relative = path.relative_to(staging).as_posix()
        metadata = path.stat()
        entries[relative] = {"kind": "directory" if path.is_dir() else "file", "mode": stat.S_IMODE(metadata.st_mode)}
        if path.is_file():
            entries[relative].update(size=metadata.st_size, sha256=_digest(path))
    for relative, target in links.items():
        if target not in entries:
            raise ValueError(f"Symlink target is outside the selected data: {relative}")
        entries[relative] = {"kind": "symlink", "target": target}
    values = config.model_dump(mode="json")
    values.update({name: str(path) for name, path in paths.items()})
    values.update(master_key="", auth_token="", debug=False)
    resource_roots = [*roots, paths["db_path"], paths["master_key_file"]]
    return {"format": FORMAT, "software": software_version(), "source_home": str(home), "user_id": user_id,
            "config": values, "entries": entries, "roots": [_relative(p, home) for p in _minimal_roots(resource_roots)], "wiki": wiki}


def _preflight_source(config: Settings, user_id: str, home: Path, archive: Path) -> None:
    """Reject static errors before entering the caller's pause context."""
    paths = {name: Path(getattr(config, name)).expanduser().resolve() for name in PATH_SETTINGS}
    for path in paths.values():
        _relative(path, home)
    key = config.master_key or paths["master_key_file"].read_text().strip()
    with _reader(paths["db_path"]) as source:
        rows = read_account(source, user_id, key=key)
    roots = [paths["users_root"] / user_id]
    for value in json.loads(rows["users"][0]["extra_roots"] or "[]"):
        _relative(Path(value), home)
    app_roots, _excluded = _application_resources(rows["applications"])
    roots.extend(app_roots)
    state = roots[0] / "wiki"
    if (state / "config.json").exists():
        from .wiki.protocols import read_registration

        roots.append(Path(read_registration(state)["root"]))
    for root in roots:
        _relative(root, home)
        if archive.is_relative_to(root):
            raise ValueError("Archive destination cannot be inside an exported resource")


def export_account(config: Settings, user_id: str, archive: Path, *, home: Path | None = None,
                   pause: AbstractContextManager | None = None) -> dict:
    """Export current selected account data; compression follows source resumption."""
    home = (home or Path.home()).resolve()
    archive = Path(archive).absolute()
    if archive.exists() or archive.is_symlink():
        raise ValueError("Archive destination already exists")
    archive = archive.parent.resolve() / archive.name
    _preflight_source(config, user_id, home, archive)
    with tempfile.TemporaryDirectory(prefix="octopus-export-") as directory:
        staging = Path(directory)
        with pause if pause is not None else nullcontext():
            owner = InstallationOwner(config)
            owner.acquire_offline()
            try:
                manifest = _copy_source(config, user_id, staging, home)
                if any(archive.is_relative_to(home / root) for root in manifest["roots"]):
                    raise ValueError("Archive destination cannot be inside an exported resource")
            finally:
                owner.close()
        created = False
        try:
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as output:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
                    package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                    for name, entry in manifest["entries"].items():
                        if entry["kind"] == "file":
                            package.write(staging / name, "files/" + name)
                output.flush()
                os.fsync(output.fileno())
            _sync_directory(archive.parent)
        except BaseException:
            if created:
                archive.unlink()
            raise
    return {"archive": str(archive), "user_id": user_id, "entries": len(manifest["entries"])}


def _read_manifest(package: zipfile.ZipFile) -> dict:
    manifest = json.loads(package.read("manifest.json"))
    if manifest["format"] != FORMAT or manifest["software"] != software_version():
        raise ValueError("Archive requires its matching Octopus version")
    expected = {"manifest.json"}
    roots = [PurePosixPath(str(_safe_relative(root))) for root in manifest["roots"]]
    for name, entry in manifest["entries"].items():
        path = PurePosixPath(str(_safe_relative(name)))
        if not any(path == root or path.is_relative_to(root) or root.is_relative_to(path) for root in roots):
            raise ValueError("Archive entry is outside its declared resources")
        if entry["kind"] == "file":
            expected.add("files/" + name)
            info = package.getinfo("files/" + name)
            if info.file_size != entry["size"]:
                raise ValueError("Archive file size mismatch")
            with package.open(info) as stream:
                # ZipFile.open is typed IO[bytes]; its real ZipExtFile supports readinto.
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:  # type: ignore[arg-type]
                    raise ValueError("Archive file integrity mismatch")
        elif entry["kind"] == "symlink":
            target = str(_safe_relative(entry["target"]))
            if target not in manifest["entries"] or manifest["entries"][target]["kind"] == "symlink":
                raise ValueError("Archive contains an undeclared symlink target")
        elif entry["kind"] != "directory":
            raise ValueError("Archive contains an unsupported entry type")
        if entry["kind"] != "symlink" and (type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o777):
            raise ValueError("Archive contains an invalid file mode")
        for parent in Path(name).parents:
            if str(parent) != "." and manifest["entries"].get(parent.as_posix(), {}).get("kind") != "directory":
                raise ValueError("Archive entry traverses an undeclared or non-directory parent")
    names = package.namelist()
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError("Archive inventory is incomplete or contains undeclared entries")
    return manifest


def import_account(archive: Path, *, home: Path | None = None) -> Settings:
    """Restore into empty selected data positions; return its ready-to-use config."""
    home = (home or Path.home()).absolute()
    if home.is_symlink():
        raise ValueError("Target home cannot be a symlink")
    with zipfile.ZipFile(archive) as package:
        try:
            manifest = _read_manifest(package)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Malformed account migration archive") from error
        source_home = Path(manifest["source_home"])

        def map_path(value: str) -> str:
            return str(home / _safe_relative(_relative(Path(value), source_home)))

        values = manifest["config"] | {name: map_path(manifest["config"][name]) for name in PATH_SETTINGS}
        config = Settings.model_validate(values)
        db_relative = _relative(Path(config.db_path), home)
        key_relative = _relative(Path(config.master_key_file), home)
        key = package.read("files/" + key_relative).decode().strip()
        with closing(sqlite3.connect(":memory:")) as reader:
            reader.row_factory = sqlite3.Row
            reader.deserialize(package.read("files/" + db_relative))
            read_account(reader, manifest["user_id"], key=key)
        roots = manifest["roots"]
        env_path = Path(config.db_path).with_name(Path(config.db_path).name + ".import.env")
        _empty_targets(home, [*roots, _relative(env_path, home)])
        existing = home
        while not existing.exists():
            existing = existing.parent
        required = sum(entry.get("size", 0) for entry in manifest["entries"].values())
        if shutil.disk_usage(existing).free < required:
            raise OSError("Not enough disk space to stage the account import")
        _ensure_directory(home, existing)
        with tempfile.TemporaryDirectory(prefix=".octopus-import-", dir=home) as directory:
            staging = Path(directory)
            entries = manifest["entries"]
            for name, entry in sorted(entries.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
                path = staging / name
                if entry["kind"] == "symlink":
                    continue
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if entry["kind"] == "directory":
                    path.mkdir(exist_ok=True)
                else:
                    with package.open("files/" + name) as source, path.open("xb") as target:
                        shutil.copyfileobj(source, target)
                if entry["kind"] != "directory":
                    path.chmod(entry["mode"] & 0o777)
            for name, entry in entries.items():
                if entry["kind"] == "symlink":
                    path = staging / name
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.symlink_to(os.path.relpath(home / entry["target"], (home / name).parent))
            db_relative = _relative(Path(config.db_path), home)
            key = (staging / _relative(Path(config.master_key_file), home)).read_text().strip()
            with _reader(staging / db_relative) as reader:
                from .crypto import decrypt

                verify_main(reader)
                user = reader.execute("SELECT dek_wrapped FROM users WHERE id=?", (manifest["user_id"],)).fetchone()
                if user is None:
                    raise ValueError("Archive is missing its declared account")
                decrypt(user[0], key)
            rebind_account(staging / db_relative, map_path)
            if manifest["wiki"]:
                from .wiki.portability import rebind_wiki

                rebind_wiki(staging, home, source_home, manifest["wiki"], Path(config.users_root) / manifest["user_id"] / "wiki")
            env_relative = _relative(env_path, home)
            staged_env = staging / env_relative
            staged_env.parent.mkdir(parents=True, exist_ok=True)
            staged_env.write_text(_service_environment(config))
            staged_env.chmod(0o600)
            _sync_tree(staging)
            owner = InstallationOwner(config)
            owner.acquire_offline()
            marker = Path(config.db_path).with_name(Path(config.db_path).name + ".importing")
            published: list[Path] = []
            complete = False
            try:
                _empty_targets(home, [*roots, env_relative])
                _ensure_directory(marker.parent, home)
                with marker.open("x") as stream:
                    json.dump({"archive": str(Path(archive).absolute()), "roots": roots, "environment": env_relative}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(marker.parent)
                for name in [*roots, env_relative]:
                    source, target = staging / name, home / name
                    _ensure_directory(target.parent, home)
                    if source.is_dir():
                        target.mkdir(mode=0o700)
                        published.append(target)
                        for child in source.iterdir():
                            os.rename(child, target / child.name)
                        target.chmod(source.stat().st_mode & 0o777)
                        _sync_directory(target)
                    else:
                        os.link(source, target, follow_symlinks=False)
                        published.append(target)
                    _sync_directory(target.parent)
                for name, entry in sorted(entries.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
                    if entry["kind"] == "directory" and any(Path(name).is_relative_to(Path(root)) for root in roots):
                        directory = home / name
                        directory.chmod(entry["mode"])
                        _sync_directory(directory)
                # All published data is durable. From here the only remaining
                # operation enables it; failures must never dismantle it.
                complete = True
                marker.unlink()
                _sync_directory(marker.parent)
            except BaseException:
                if complete:
                    raise
                for path in reversed(published):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    _sync_directory(path.parent)
                if marker.exists():
                    marker.unlink()
                    _sync_directory(marker.parent)
                raise
            finally:
                owner.close()
    return config
