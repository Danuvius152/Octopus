"""Installation identity and the process that exclusively owns its storage."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .account_baseline import persistent_database_file
from .service_health import ServiceHealth, file_effect
from .service_scope import ScopeError, current_scope

if TYPE_CHECKING:
    from .config import Settings


class InstallationError(RuntimeError):
    pass


_DIRECTORIES = (
    "users_root", "attachments_dir", "large_prompts_dir", "codex_home_dir",
    "research_dir", "wiki_dir", "agents_dir", "applications_dir",
    "default_working_dir",
)
_FILES = ("db_path", "metrics_db_path", "master_key_file")
_PROVIDER_HOMES = {
    "codex_default_home": ("CODEX_HOME", ".codex"),
    "claude_default_home": ("CLAUDE_CONFIG_DIR", ".claude"),
}


class ServiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    unit: str = Field(pattern=r"^[A-Za-z0-9_.@:-]+\.service$")
    manager: Literal["user", "system"]
    paths: dict[str, str]
    host: str
    port: int = Field(ge=1, le=65535)
    enable_tunnel: bool


def _paths(config: Settings) -> dict[str, str]:
    paths = {
        name: str(Path(getattr(config, name)).expanduser().resolve())
        for name in (*_DIRECTORIES, *_FILES)
        if name not in _FILES or getattr(config, name) != ":memory:"
    }
    paths.update({name: str(Path(os.environ.get(variable) or Path.home() / default).expanduser().resolve())
                  for name, (variable, default) in _PROVIDER_HOMES.items()})
    return paths


def profile_path(config: Settings) -> Path:
    db = Path(config.db_path).expanduser().resolve()
    return db.with_name(db.name + ".service.json")


def environment_path(config: Settings) -> Path:
    db = Path(config.db_path).expanduser().resolve()
    return db.with_name(db.name + ".service.env")


def _service_environment(config: Settings) -> str:
    values = config.model_dump(mode="json")
    values.update({name: value for name, value in _paths(config).items() if name in values})
    lines = []
    for name, value in values.items():
        # An explicit empty optional value overrides inherited manager state;
        # its cancellation semantics must not depend on a separate unit file.
        if value is None:
            value = ""
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if any(character in raw for character in "\n\r\0"):
            raise InstallationError(f"Service configuration {name} must not contain line breaks or NUL")
        quoted = raw.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'OCTOPUS_{name.upper()}="{quoted}"')
    return "\n".join(lines) + "\n"


def _markers(paths: dict[str, str]):
    for name in _DIRECTORIES:
        if name in paths:
            yield Path(paths[name]) / ".octopus-installation"
    for name in ("metrics_db_path", "master_key_file", *_PROVIDER_HOMES):
        if name not in paths:
            continue
        path = Path(paths[name])
        yield path.with_name(path.name + ".installation")


def _account_work_roots(config: Settings) -> list[Path] | None:
    database = persistent_database_file(config.db_path)
    if database is None:
        return None
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='users'").fetchone() is None:
                return None
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                return None
            roots = [Path(row[0]).expanduser().resolve() for row in connection.execute(
                "SELECT DISTINCT working_dir FROM sessions WHERE working_dir IS NOT NULL AND working_dir != ''")]
            for (raw,) in connection.execute("SELECT extra_roots FROM users"):
                if raw is None:
                    continue
                granted = json.loads(raw)
                if not isinstance(granted, list) or any(
                    not isinstance(root, str) or not Path(root).is_absolute() for root in granted
                ):
                    raise InstallationError("Cannot verify the installation's granted work roots")
                # Grants are canonical when saved. Following today's symlinks
                # would change which approved path this cleanup preserves.
                roots.extend(Path(root) for root in granted)
            return roots
    except (TypeError, ValueError) as error:
        raise InstallationError("Cannot verify the installation's granted work roots") from error
    except sqlite3.Error as error:
        raise InstallationError("Cannot verify the installation's account storage layout") from error


def _is_legacy_shell(root: Path, target: Path) -> bool:
    if root.is_symlink():
        return False
    if not root.exists():
        return True
    if not root.is_dir():
        return False
    reference = root / ".octopus-installation"
    if not reference.is_file() or reference.is_symlink():
        return False

    def unavailable(error):
        raise error

    for directory, folders, files in os.walk(root, followlinks=False, onerror=unavailable):
        if any((Path(directory) / name).is_symlink() for name in folders):
            return False
        for name in files:
            marker = Path(directory) / name
            if name != ".octopus-installation" or marker.is_symlink():
                return False
            try:
                if marker.read_text().strip() != str(target):
                    return False
            except (OSError, UnicodeError):
                return False
    return True


def _claimed_paths(config: Settings, paths: dict[str, str]) -> dict[str, str]:
    work_roots = _account_work_roots(config)
    if work_roots is None:
        return paths
    protected = [Path(raw) for name, raw in paths.items() if name not in _DIRECTORIES or name == "users_root"]
    target = profile_path(config)
    return {
        name: raw for name, raw in paths.items()
        if name not in _DIRECTORIES or name == "users_root"
        or Path(getattr(config, name)).expanduser().is_symlink()
        or any(_overlaps(Path(raw), root) for root in protected)
        or any(Path(raw) == directory or Path(raw) in directory.parents for directory in work_roots)
        or not _is_legacy_shell(Path(raw), target)
    }


def legacy_shells(config: Settings) -> list[Path]:
    """Unused single-user directories; account roots and real data stay owned."""
    paths = _paths(config)
    claimed = set(_claimed_paths(config, paths).values())
    return sorted({Path(raw) for raw in paths.values() if raw not in claimed})


def prune_legacy_shells(config: Settings) -> list[Path]:
    """Remove only this managed installation's unused, empty legacy trees."""
    if read_service_profile(config) is None:
        raise InstallationError("Legacy cleanup requires the existing managed installation")
    target = profile_path(config)
    removed = []
    for root in legacy_shells(config):
        if not root.exists():
            continue
        with _claim_range(root):
            if not _is_legacy_shell(root, target):
                continue
            directories = [Path(directory) for directory, _, _ in os.walk(root, followlinks=False)]
            try:
                for directory in reversed(directories):
                    marker = directory / ".octopus-installation"
                    if marker.exists():
                        if marker.is_symlink() or marker.read_text().strip() != str(target):
                            raise InstallationError("Legacy cleanup found another resource owner")
                        marker.unlink()
                    directory.rmdir()
            except OSError:
                # A concurrent new file is retained and restores the ordinary
                # ownership check; recursive deletion is never permitted.
                if root.is_dir() and not root.is_symlink():
                    _claim(root / ".octopus-installation", target)
                raise
            descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            removed.append(root)
    return removed


def _resource_markers(path: Path):
    for current in (path, *path.parents):
        yield current / ".octopus-installation"
        if current.name:
            yield current.with_name(current.name + ".installation")


@contextmanager
def _claim_range(path: Path, *, directory: bool = True):
    """Coordinate overlapping claims on the actual directory inodes.

    A parent's exclusive lock conflicts with every child's shared ancestor
    lock. Claims are short and never wait while holding another range.
    """
    scope = path if directory else path.parent
    while not scope.exists():
        scope = scope.parent
    handles = []
    try:
        for current in (*reversed(scope.parents), scope):
            descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            handles.append(descriptor)
            mode = fcntl.LOCK_EX if current == scope else fcntl.LOCK_SH
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise InstallationError("An overlapping resource claim is in progress; retry after it completes") from error
        yield
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)


def _descendant_markers(root: Path):
    if not root.is_dir():
        return

    def unavailable(error):
        raise error

    for directory, _, files in os.walk(root, followlinks=False, onerror=unavailable):
        for name in files:
            if name == ".octopus-installation" or name.endswith(".installation"):
                yield Path(directory) / name


def _vault_markers(root: Path):
    for current in (root, *root.parents):
        marker = current / ".octopus-vault"
        if marker.exists():
            yield marker
    if root.is_dir():
        def unavailable(error):
            raise error

        for directory, _, files in os.walk(root, followlinks=False, onerror=unavailable):
            if Path(directory) != root and ".octopus-vault" in files:
                yield Path(directory) / ".octopus-vault"


def _check_installation_range(root: Path, target: Path | None, *, directory=True):
    markers = list(_resource_markers(root))
    if directory:
        markers.extend(_descendant_markers(root))
    references = _references(markers)
    if references - ({target} if target is not None else set()):
        raise InstallationError("Resource belongs to another managed installation; use its existing registration")
    return references


def _references(markers) -> set[Path]:
    references = set()
    for marker in markers:
        if not os.path.lexists(marker):
            continue
        if not marker.is_file():
            raise InstallationError(f"Invalid ownership reference: {marker}")
        try:
            target = Path(marker.read_text().strip())
        except (OSError, UnicodeError) as error:
            raise InstallationError(f"Ownership reference is unreadable: {marker}") from error
        if not target.is_absolute():
            raise InstallationError(f"Invalid ownership reference: {marker}")
        references.add(target.resolve())
    return references


def read_service_profile(config: Settings) -> ServiceProfile | None:
    expected = profile_path(config)
    paths = _paths(config)
    claimed = _claimed_paths(config, paths)
    markers = set(_markers(claimed))
    # Descendants remain part of the same installation. A different DB name
    # or a narrower Wiki/user root must not silently reuse managed resources.
    for path in claimed.values():
        markers.update(_resource_markers(Path(path)))
    references = {expected} if expected.exists() else set()
    try:
        references.update(_references(markers))
        if not references:
            return None
        if references != {expected}:
            raise InstallationError("Resources belong to another managed installation; use its configured service or fully isolated paths")
        profile = ServiceProfile.model_validate_json(expected.read_text())
    except (OSError, ValidationError) as error:
        raise InstallationError(f"Managed installation configuration is unavailable: {expected}") from error
    differences = sorted(name for name in paths.keys() | profile.paths.keys() if paths.get(name) != profile.paths.get(name))
    differences.extend(name for name in ("host", "port", "enable_tunnel") if getattr(config, name) != getattr(profile, name))
    if config.debug:
        differences.append("debug/reload")
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        differences.append("workers (WEB_CONCURRENCY)")
    if differences:
        raise InstallationError(f"Configuration differs from managed installation {profile.unit}: {', '.join(differences)}; update its environment and rerun service setup")
    if any(not marker.is_file() or marker.read_text().strip() != str(expected) for marker in _markers(claimed)):
        raise InstallationError("Managed installation setup is incomplete; rerun service setup before startup")
    return profile


def guard_vault(config: Settings, root: Path, state: Path, *, bind: bool = False,
                health: ServiceHealth | None = None) -> None:
    """Claim one authoritative ledger for a non-overlapping body range."""
    root = root.expanduser().resolve()
    state = state.expanduser().resolve()
    profile = read_service_profile(config)
    target = profile_path(config) if profile is not None else None
    _claim_vault(config, root, state, target, bind=bind, health=health)


def _claim_vault(config: Settings, root: Path, state: Path, target: Path | None, *, bind: bool,
                 health: ServiceHealth | None = None):
    if not root.is_dir():
        raise InstallationError("Wiki vault root is unavailable; retained ownership requires verification")
    with _claim_range(root):
        _check_registrations(config, root=root, state=state)
        references = _check_installation_range(root, target)
        for marker in _vault_markers(root):
            if marker.parent != root or _references([marker]) != {state}:
                raise InstallationError("Wiki vault overlaps another authoritative state; use its existing registration")
        if target is not None and not references and not bind:
            raise InstallationError("Managed Wiki vault ownership is missing; rerun service setup for its existing registration")
        _claim(root / ".octopus-vault", state, health=health)
        if target is not None:
            _claim(root / ".octopus-installation", target, health=health)




def _registered_vaults(config: Settings):
    from .wiki.protocols import read_registration
    from .wiki.store import WikiError

    states = [Path(config.wiki_dir).expanduser()]
    users = Path(config.users_root).expanduser()
    if users.is_dir():
        states.extend(path / "wiki" for path in users.iterdir() if path.is_dir())
    for state in states:
        file = state / "config.json"
        if not file.is_file():
            continue
        try:
            # A damaged ledger cannot release a valid historical root for a
            # different account. This discovery never opens a writer.
            value = read_registration(state, require_root=False, binding_only=True)
            root = Path(value["root"])
        except WikiError:
            # Registration diagnostics own malformed configuration. Do not
            # create a guessed vault or prevent the rest of the workbench booting.
            continue
        if root.is_absolute():
            yield root.resolve(), state.resolve()


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _check_registrations(config: Settings, *, root: Path | None = None, state: Path | None = None):
    registrations = list(_registered_vaults(config))
    if root is not None:
        assert state is not None
        # Registry forbids changing an existing state's root without migration.
        # A writer only needs to check its own range; setup checks every pair.
        if any(other_state != state and _overlaps(root, other_root) for other_root, other_state in registrations):
            raise InstallationError("Wiki has conflicting registrations for overlapping body ranges; verify their authority before opening a writer")
        return
    for index, (first_root, first_state) in enumerate(registrations):
        for second_root, second_state in registrations[index + 1:]:
            if first_state != second_state and _overlaps(first_root, second_root):
                raise InstallationError("Wiki has conflicting registrations for overlapping body ranges; verify their authority before opening a writer")


def _verify_service(profile: ServiceProfile) -> None:
    try:
        scope = current_scope(profile.unit)
        required = {
            "MainPID": str(os.getpid()), "ControlGroup": str(scope.unit_path),
            "Type": "simple", "ExitType": "main",
            "KillMode": "control-group", "SendSIGKILL": "yes", "Delegate": "no", "TasksAccounting": "yes",
        }
        command = ["systemctl", *( ["--user"] if profile.manager == "user" else [] ), "show", "--no-pager"]
        command.extend("--property=" + key for key in required)
        result = subprocess.run([*command, "--", profile.unit], check=True, capture_output=True, text=True, timeout=5)
        actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if any(actual.get(key) != value for key, value in required.items()):
            raise InstallationError(f"{profile.unit} must directly own this single server process with the configured cgroup policy; reload and multiple workers are unsupported")
    except (ScopeError, OSError, subprocess.SubprocessError) as error:
        raise InstallationError(f"Start this managed installation through {profile.unit}: {error}") from error


def _publish(path: Path, text: str, *, health: ServiceHealth | None = None) -> None:
    with file_effect(health) if health is not None else nullcontext():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _claim(marker: Path, target: Path, *, health: ServiceHealth | None = None) -> None:
    """Publish a complete ownership reference without replacing another owner."""
    with file_effect(health) if health is not None else nullcontext():
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=marker.parent, mode="w", encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(str(target) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, marker)
            except FileExistsError:
                if not marker.is_file() or marker.read_text().strip() != str(target):
                    raise InstallationError("Resource is already bound to another managed installation") from None
            directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def configure_service(config: Settings, *, unit: str, manager: Literal["user", "system"]) -> ServiceProfile:
    if config.db_path == ":memory:" or config.debug:
        raise InstallationError("A managed installation requires a persistent database and reload disabled")
    target = profile_path(config)
    environment = _service_environment(config)
    active_environment = environment_path(config)
    if active_environment.exists():
        from dotenv import dotenv_values

        token = dotenv_values(active_environment, interpolate=False).get("OCTOPUS_AUTH_TOKEN")
        if token != config.auth_token:
            raise InstallationError("Setup token differs from the active service; supply its current token to setup")
    paths = _paths(config)
    profile = ServiceProfile(unit=unit, manager=manager, paths=paths, host=config.host, port=config.port, enable_tunnel=config.enable_tunnel)
    owner = InstallationOwner(config)
    owner._acquire_lock()
    try:
        claimed = _claimed_paths(config, paths)
        markers = set(_markers(claimed))
        for path in claimed.values():
            markers.update(_resource_markers(Path(path)))
        _check_registrations(config)
        vaults = {(root, state) for root, state in _registered_vaults(config) if root.is_dir()}
        for root, _ in vaults:
            markers.update(_resource_markers(root))
        if _references(markers) - {target}:
            raise InstallationError("Resource is already bound to another managed installation")
        for marker in _markers(claimed):
            if marker.exists() and marker.read_text().strip() != str(target):
                raise InstallationError("Resource is already bound to another managed installation")
        # References contain no copied settings. The central profile is the
        # authority; interrupted setup is closed to startup and repeatable.
        for name in (*_DIRECTORIES, "metrics_db_path", "master_key_file", *_PROVIDER_HOMES):
            if name not in claimed:
                continue
            resource = Path(paths[name])
            directory = name not in _FILES
            with _claim_range(resource, directory=directory):
                _check_installation_range(resource, target, directory=directory)
                for marker in _vault_markers(resource) if directory else ():
                    if not _references([marker]) <= {state for _, state in vaults}:
                        raise InstallationError("Resource overlaps another authoritative Wiki state")
                marker = resource / ".octopus-installation" if name in _DIRECTORIES else resource.with_name(resource.name + ".installation")
                _claim(marker, target)
        for root, state in vaults:
            _claim_vault(config, root, state, target, bind=True)
        _publish(environment_path(config), environment)
        _publish(target, profile.model_dump_json(indent=2) + "\n")
    finally:
        owner.close()
    return profile


def render_service_unit(project: Path, config: Settings, *, manager: Literal["user", "system"], service_user: str | None = None) -> str:
    """Render the production contract without changing or starting a service."""
    project = project.expanduser().absolute()
    python = Path(sys.executable).absolute()

    def word(value, *, command=False, quoted=True):
        value = str(value)
        if any(c in value for c in "\n\r\0"):
            raise InstallationError("Unit values must not contain line breaks or NUL")
        value = value.replace("%", "%%")
        if command:
            value = value.replace("$", "$$")
        return json.dumps(value, ensure_ascii=False) if quoted else value

    if manager == "system" and not service_user:
        raise InstallationError("A system service requires --service-user for the existing installation owner")
    user = f"User={word(service_user, quoted=False)}\n" if manager == "system" else ""
    checker = " ".join(word(part, command=True) for part in (python, "-I", "-S", project / "server/service_scope.py"))
    start = " ".join(word(part, command=True) for part in (python, "-m", "server.cli", "serve", "--service-process"))
    return (
        "[Unit]\nDescription=Octopus Server\nAfter=network.target\n"
        "StartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
        "[Service]\nType=simple\nExitType=main\n" + user
        + f"WorkingDirectory={word(project, quoted=False)}\nEnvironmentFile={word(environment_path(config), quoted=False)}\n"
        + f"Environment={word('OCTOPUS_ENV_FILE=' + str(environment_path(config)))}\n"
        + f"Environment={word('PATH=' + os.environ.get('PATH', '/usr/bin:/bin'))}\n"
        + "".join(f"Environment={word(variable + '=' + _paths(config)[name])}\n" for name, (variable, _) in _PROVIDER_HOMES.items())
        + "KillMode=control-group\nSendSIGKILL=yes\nDelegate=no\nTasksAccounting=yes\n"
        "TimeoutStopSec=10s\nRestart=on-failure\nRestartSec=3s\n"
        + f"ExecStartPre={checker} --unit=%n\nExecStart={start}\n\n"
        + "[Install]\nWantedBy=" + ("default.target" if manager == "user" else "multi-user.target") + "\n"
    )


def main() -> int:
    import argparse

    from .config import settings

    parser = argparse.ArgumentParser(description="Preview or install Octopus's managed service after natural drain")
    parser.add_argument("--unit", default="octopus.service")
    parser.add_argument("--manager", choices=("user", "system"), default="user")
    parser.add_argument("--service-user")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true", help="Bind storage and install the unit; never stop or start existing work")
    args = parser.parse_args()
    try:
        ServiceProfile(unit=args.unit, manager=args.manager, paths=_paths(settings),
                       host=settings.host, port=settings.port, enable_tunnel=settings.enable_tunnel)
        text = render_service_unit(args.project, settings, manager=args.manager, service_user=args.service_user)
        if not args.apply:
            print(text, end="")
            return 0
        if args.manager == "system":
            import pwd

            try:
                service_uid = pwd.getpwnam(args.service_user).pw_uid
            except KeyError as error:
                raise InstallationError("The system service owner does not exist") from error
            if os.geteuid() != service_uid:
                raise InstallationError("Run setup as the configured service owner; only unit installation and daemon-reload use sudo")
        command = ["systemctl", *(["--user"] if args.manager == "user" else [])]
        result = subprocess.run([*command, "show", "--property=ActiveState", "--property=DropInPaths", "--", args.unit],
                                check=True, capture_output=True, text=True)
        actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if actual.get("ActiveState") not in {"inactive", "failed"}:
            raise InstallationError("Wait for natural drain and stop the existing service before installing its managed profile")
        if actual.get("DropInPaths"):
            raise InstallationError("Existing unit drop-ins must be reconciled with the rendered contract before setup")
        directory = Path.home() / ".config/systemd/user" if args.manager == "user" else Path("/etc/systemd/system")
        with tempfile.TemporaryDirectory(prefix="octopus-unit-") as scratch:
            candidate = Path(scratch) / args.unit
            candidate.write_text(text)
            subprocess.run(["systemd-analyze", *(["--user"] if args.manager == "user" else []), "verify", str(candidate)], check=True)
            configure_service(settings, unit=args.unit, manager=args.manager)
            privileged = args.manager == "system" and not os.access(directory, os.W_OK)
            if privileged:
                subprocess.run(["sudo", "-n", "install", "-m", "0644", "--", str(candidate), str(directory / args.unit)], check=True)
            else:
                _publish(directory / args.unit, text)
            subprocess.run([*(["sudo", "-n"] if privileged else []), *command, "daemon-reload"], check=True)
        print(f"Installed {args.unit}; start it after the candidate has passed release checks. Readiness: /ready")
        return 0
    except (InstallationError, ValidationError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 78


class InstallationOwner:
    def __init__(self, config: Settings):
        self.config = config
        self.db_path = None if config.db_path == ":memory:" else Path(config.db_path).expanduser().resolve()
        self._lock: BinaryIO | None = None
        self.profile: ServiceProfile | None = None
        self.offline_request: str | None = None

    def acquire(self) -> None:
        self._acquire(service_process=True)

    def acquire_offline(self) -> None:
        """Own stopped storage without impersonating systemd's server process."""
        self._acquire(service_process=False)

    def _acquire(self, *, service_process: bool) -> None:
        self._acquire_lock()
        try:
            if self.db_path is not None:
                incomplete = self.db_path.with_name(self.db_path.name + ".importing")
                if incomplete.exists() or incomplete.is_symlink():
                    raise InstallationError(f"Incomplete account import; inspect its owned-path inventory before cleanup: {incomplete}")
            self.profile = read_service_profile(self.config)
            if self.profile is not None:
                if service_process:
                    _verify_service(self.profile)
                else:
                    command = ["systemctl", *(["--user"] if self.profile.manager == "user" else []),
                               "show", "--no-pager", "--property=MainPID,ActiveState,Job", "--", self.profile.unit]
                    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
                    actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                    if actual.get("MainPID") != "0" or actual.get("ActiveState") not in {"inactive", "failed"} or actual.get("Job"):
                        raise InstallationError("Managed installation must finish stopping before offline storage access")
            else:
                for name, raw in _claimed_paths(self.config, _paths(self.config)).items():
                    path = Path(raw)
                    with _claim_range(path, directory=name not in _FILES):
                        _check_installation_range(path, None, directory=name not in _FILES)
        except BaseException:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        if self._lock is not None:
            raise InstallationError("Installation ownership was already acquired")
        if self.db_path is None:
            return
        lock_path = self.db_path.with_name(self.db_path.name + ".owner")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise InstallationError("Installation is already owned by another service process") from error
        except BaseException:
            handle.close()
            raise
        # Python-created file descriptors are non-inheritable. Hold this
        # descriptor until writers are retired; never unlink the lock inode.
        self._lock = handle

    def close(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None


if __name__ == "__main__":
    raise SystemExit(main())
