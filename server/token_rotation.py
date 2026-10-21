"""Token files precede a database rekey with one encrypted prepared intent.

Rekey and intent removal share a short transaction. Startup restores old file
values while the intent remains; uncertain commits exit instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .aio import complete_cleanup
from .config import settings, startup_env_file
from .crypto import decrypt, encrypt, master_key
from .database import Database
from .db.transaction import Transaction
from .service_installation import ServiceProfile

logger = logging.getLogger(__name__)
ENV_KEY = "OCTOPUS_AUTH_TOKEN"
MIN_TOKEN_LENGTH = 12
_FORBIDDEN = re.compile(r"[\s;,\"'\\]")
_TOKEN_LINE = re.compile(r"^([ \t]*(?:export[ \t]+)?OCTOPUS_AUTH_TOKEN[ \t]*=[ \t]*).*$", re.M | re.I)
_ACCOUNT_SECRETS = (
    ("credential_secrets", "backend_credentials", "credential_id"),
    ("connector_installation_secrets", "connector_installations", "installation_id"),
)


class TokenRotationError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    database: str
    old_token: str = Field(min_length=1, repr=False)
    new_token: str = Field(min_length=MIN_TOKEN_LENGTH, repr=False)
    files: tuple[str, ...] = Field(min_length=1)


@dataclass
class RotationResult:
    env_files: list[str] = field(default_factory=list)
    reencrypted: dict[str, int] = field(default_factory=dict)


def validate_new_token(new_token: str) -> str:
    token = (new_token or "").strip()
    if not token:
        raise TokenRotationError("The new token can't be empty")
    if len(token) < MIN_TOKEN_LENGTH:
        raise TokenRotationError(f"The new token must be at least {MIN_TOKEN_LENGTH} characters")
    if _FORBIDDEN.search(token):
        raise TokenRotationError("The new token can't contain spaces, quotes, backslashes, semicolons or commas — it travels in a URL query and a cookie")
    if "${" in token:
        raise TokenRotationError("The new token cannot contain dotenv interpolation (${...}); choose a literal value")
    if token == "changeme":
        raise TokenRotationError("Pick something other than the default token")
    if token == settings.auth_token:
        raise TokenRotationError("That's already the current token")
    return token


def env_files_defining_token() -> list[Path]:
    override = os.environ.get("OCTOPUS_ENV_FILE")
    primary = Path(startup_env_file()).expanduser()
    candidates = [primary] if override else [primary, Path(__file__).resolve().parent.parent / ".env"]
    found: list[Path] = []
    for candidate in candidates:
        if candidate.is_file():
            path = candidate.resolve()
            if path not in found and _TOKEN_LINE.search(path.read_text()):
                found.append(path)
    return found


def _env_token(text: str, *, interpolate: bool) -> str:
    # Count bindings before dotenv's dictionary folds repeated or quoted keys.
    definitions = [binding for binding in parse_stream(StringIO(text))
                   if binding.key is not None and binding.key.lower() == ENV_KEY.lower()]
    if len(definitions) != 1:
        raise TokenRotationError("The token must have one unambiguous, single-line environment definition", status_code=409)
    # The sole binding must also match the existing single-line rewrite format.
    lines = list(_TOKEN_LINE.finditer(text))
    # Settings normalizes names to lowercase, including aliases in dotenv.
    values = {name.lower(): value for name, value in dotenv_values(stream=StringIO(text), interpolate=interpolate).items()}
    token = values.get(ENV_KEY.lower())
    single = {name.lower(): value for name, value in dotenv_values(stream=StringIO(lines[0].group()), interpolate=interpolate).items()} if len(lines) == 1 else {}
    if (len(lines) != 1 or token is None
            or single.get(ENV_KEY.lower()) != token):
        raise TokenRotationError("The token must have one unambiguous, single-line environment definition", status_code=409)
    return token


def _render_environment(text: str, token: str, *, interpolate: bool) -> str:
    value = json.dumps(token, ensure_ascii=False) if _FORBIDDEN.search(token) else token
    updated = _TOKEN_LINE.sub(lambda match: match[1] + value, text)
    if _env_token(updated, interpolate=interpolate) != token:
        raise TokenRotationError("The token cannot round-trip through the startup configuration", status_code=409)
    return updated


def _startup_source(db_path: str, paths: list[Path]) -> bool:
    """Prove the effective next-start source, including loaded unit overrides."""
    database = Path(db_path).expanduser().resolve()
    profile_file = database.with_name(database.name + ".service.json")
    aliases = [name for name in os.environ if name.lower() == ENV_KEY.lower()]
    if not profile_file.exists():
        if aliases:
            raise TokenRotationError("An external OCTOPUS_AUTH_TOKEN would override the rotated file on restart; start from the env file before rotating", status_code=409)
        if Path(startup_env_file()).expanduser().resolve() not in paths:
            raise TokenRotationError("Settings' selected env file does not define the token; configure OCTOPUS_ENV_FILE or its working-directory .env before rotating", status_code=409)
        if paths != env_files_defining_token():
            raise TokenRotationError("Startup now uses different token files; preserve the retained rotation and restore its original startup source", status_code=409)
        return True  # Standalone Settings uses python-dotenv interpolation.
    if any(name != ENV_KEY for name in aliases):
        raise TokenRotationError("A case alias of OCTOPUS_AUTH_TOKEN can override the generated service source; remove it before rotating", status_code=409)
    try:
        profile = ServiceProfile.model_validate_json(profile_file.read_text())
    except (OSError, ValidationError) as error:
        raise TokenRotationError("The managed startup profile cannot be verified", status_code=409) from error
    active = database.with_name(database.name + ".service.env")
    if profile.paths.get("db_path") != str(database) or paths != [active]:
        raise TokenRotationError("Rotation must use the installation's active environment file", status_code=409)
    try:
        command = ["systemctl", "--user" if profile.manager == "user" else "--system", "show", profile.unit,
                   "-p", "EnvironmentFiles", "-p", "UnsetEnvironment", "-p", "ExecStart"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise TokenRotationError("The loaded service configuration cannot be verified; rotation was not started", status_code=409) from error
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    expected_command = f"argv[]={sys.executable} -m server.cli serve --service-process ;"
    unset = properties.get("UnsetEnvironment", "")
    if (properties.get("EnvironmentFiles") != f"{active} (ignore_errors=no)"
            or expected_command not in properties.get("ExecStart", "")
            or re.search(r"(?:^|[\s\"'])OCTOPUS_AUTH_TOKEN(?:[=\s\"']|$)", unset)):
        raise TokenRotationError("Loaded service overrides do not preserve the generated token source; rerun service setup before rotating", status_code=409)
    return False  # systemd passes the EnvironmentFile's literal token to Settings.


def _preflight(db_path: str, old_token: str, new_token: str) -> tuple[list[Path], bool]:
    paths = env_files_defining_token()
    if not paths:
        raise TokenRotationError(f"No env file defines {ENV_KEY}, so a new token couldn't survive a restart. Add it to a .env file first.", status_code=409)
    interpolate = _startup_source(db_path, paths)
    for path in paths:
        text = path.read_text()
        if _env_token(text, interpolate=interpolate) != old_token:
            raise TokenRotationError("An environment file differs from the live token; verify its configuration before rotating", status_code=409)
        _render_environment(text, old_token, interpolate=interpolate)
        _render_environment(text, new_token, interpolate=interpolate)
    return paths, interpolate


def _rewrite_env_file(path: Path, token: str, allowed: set[str], *, interpolate: bool) -> None:
    if path.resolve() != path:
        raise TokenRotationError("A retained token file no longer has its original canonical path", status_code=409)
    original = path.read_text()
    if _env_token(original, interpolate=interpolate) not in allowed:
        raise TokenRotationError("A token file contains an unknown third value; retained rotation evidence needs operator attention", status_code=409)
    updated = _render_environment(original, token, interpolate=interpolate)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-rotate-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        for directory in path.parents:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_files(intent: _Intent, token: str, interpolate: bool) -> None:
    allowed = {intent.old_token, intent.new_token}
    # Reject an unknown later file before changing any earlier file.
    for name in intent.files:
        path = Path(name)
        if path.resolve() != path or _env_token(path.read_text(), interpolate=interpolate) not in allowed:
            raise TokenRotationError("A retained token file cannot be reconciled; preserve the intent for operator attention", status_code=409)
    for name in intent.files:
        _rewrite_env_file(Path(name), token, allowed, interpolate=interpolate)


async def _file_step[T](db: Database, action: Callable[..., T], *args: Any) -> T:
    task = asyncio.create_task(asyncio.to_thread(action, *args))
    try:
        async with asyncio.timeout(5):
            return await asyncio.shield(task)
    except TokenRotationError:
        raise
    except BaseException as error:
        db.health.fail(error)


async def _rekey(tx: Transaction, old: str, new: str, wrapping_key: str, *, write: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, parents, identity in _ACCOUNT_SECRETS:
        rows = await tx.fetch_all(
            f"SELECT s.{identity},s.secret_encrypted FROM {table} s JOIN {parents} p ON p.id=s.{identity} WHERE p.user_id IS NULL",  # noqa: S608
        )
        counts[table] = 0
        for key, ciphertext in rows:
            if not ciphertext:
                continue
            try:
                plaintext = decrypt(ciphertext, old)
            except ValueError as error:
                raise TokenRotationError(f"{table} record {key!r} cannot be decrypted with the current token; verify the retained secret before rotating", status_code=409) from error
            if write:
                await tx.execute(f"UPDATE {table} SET secret_encrypted=? WHERE {identity}=?", (encrypt(plaintext, new), key))  # noqa: S608
            counts[table] += 1
    counts["connector_oauth_clients"] = 0
    for kind, ciphertext in await tx.fetch_all("SELECT kind,client_secret_encrypted FROM connector_oauth_clients"):
        if not ciphertext:
            continue
        try:
            decrypt(ciphertext, wrapping_key)
            continue
        except ValueError:
            pass
        try:
            plaintext = decrypt(ciphertext, old)
        except ValueError as error:
            raise TokenRotationError("An OAuth client secret has an unknown encryption key; verify it before rotating", status_code=409) from error
        if write:
            await tx.execute("UPDATE connector_oauth_clients SET client_secret_encrypted=? WHERE kind=?", (encrypt(plaintext, wrapping_key), kind))
        counts["connector_oauth_clients"] += 1
    return counts


async def _recover_locked(db: Database) -> None:
    async with db.transaction(write=False) as tx:
        row = await tx.fetch_one("SELECT encrypted_payload FROM token_rotation_intent WHERE id=1")
    if row is None:
        return
    try:
        intent = _Intent.model_validate_json(decrypt(row[0], master_key()))
        if (intent.database != str(Path(db._db_path).expanduser().resolve())
                or len(set(intent.files)) != len(intent.files)
                or any(not Path(name).is_absolute() for name in intent.files)
                or settings.auth_token not in {intent.old_token, intent.new_token}):
            raise TokenRotationError("Retained token rotation does not match this startup; preserve its evidence for operator attention", status_code=409)
        interpolate = await _file_step(db, _startup_source, db._db_path, [Path(name) for name in intent.files])
        await _file_step(db, _write_files, intent, intent.old_token, interpolate)
    except (TokenRotationError, ValueError, ValidationError) as error:
        db.health.fail(error)
    async with db.transaction() as tx:
        await tx.execute("DELETE FROM token_rotation_intent WHERE id=1")
    settings.auth_token = intent.old_token
    if ENV_KEY in os.environ:
        os.environ[ENV_KEY] = intent.old_token


async def recover_auth_token(db: Database) -> None:
    """Startup owner: reconcile files before any secret consumer is admitted."""
    async with db.installation_key_lock:
        await _recover_locked(db)


async def rotate_auth_token(db: Database, new_token: str, *, session_mgr: Any | None = None) -> RotationResult:
    captured_token = settings.auth_token

    async def command() -> RotationResult:
        async with db.installation_key_lock:
            if settings.auth_token != captured_token:
                raise TokenRotationError("Another rotation has already changed this token; sign in again", status_code=409)
            token = validate_new_token(new_token)
            paths, interpolate = await _file_step(db, _preflight, db._db_path, captured_token, token)
            wrapping_key = await _file_step(db, master_key)
            intent = _Intent(database=str(Path(db._db_path).expanduser().resolve()), old_token=captured_token,
                             new_token=token, files=tuple(str(path) for path in paths))
            sealed = encrypt(intent.model_dump_json(), wrapping_key)
            try:
                # C0: validate without effects, then retain the sole cross-resource intent.
                async with db.transaction() as tx:
                    if await db._count(tx, "SELECT COUNT(*) FROM users"):
                        raise TokenRotationError("The installation token has retired; use the owning account", status_code=409)
                    if await tx.fetch_one("SELECT id FROM token_rotation_intent WHERE id=1"):
                        db.health.fail(RuntimeError("An unresolved token intent reached an admitted rotation"))
                    await _rekey(tx, captured_token, token, wrapping_key, write=False)
                    await tx.execute("INSERT INTO token_rotation_intent(id,encrypted_payload) VALUES(1,?)", (sealed,))
                try:
                    # F1: every file and its directory precedes the database commit.
                    await _file_step(db, _write_files, intent, token, interpolate)
                    # C1: re-read actual secrets, rekey, and remove the intent atomically.
                    async with db.transaction() as tx:
                        counts = await _rekey(tx, captured_token, token, wrapping_key, write=True)
                        await tx.execute("DELETE FROM token_rotation_intent WHERE id=1")
                except TokenRotationError:
                    await _recover_locked(db)
                    raise
            except TokenRotationError:
                raise
            except BaseException as error:
                # A cancelled owner can have a confirmed C0/C1 commit. Never
                # release its key scope without handoff or startup recovery.
                db.health.fail(error)
            settings.auth_token = token
            if ENV_KEY in os.environ:
                os.environ[ENV_KEY] = token
            try:
                if session_mgr is not None:
                    async with asyncio.timeout(5):
                        await session_mgr.stop_all_held_processes()
            except BaseException as error:
                db.health.fail(error)
            result = RotationResult([str(path) for path in paths], counts)
            logger.info("auth token rotated; rewrote %s, re-encrypted %s", result.env_files, counts)
            return result

    return await complete_cleanup(command())
