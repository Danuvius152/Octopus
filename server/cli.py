"""CLI entry point for Octopus: serve (default) and handoff subcommands."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path


def get_project_dir(cwd: str | None = None) -> Path:
    """Return the Claude Code project directory for the given (or current) cwd."""
    if cwd is None:
        cwd = str(Path.cwd())
    escaped = cwd.replace("/", "-").replace("\\", "-")
    return Path.home() / ".claude" / "projects" / escaped


def discover_sessions(project_dir: Path) -> list[dict]:
    """Find JSONL session files and return basic info about each."""
    if not project_dir.is_dir():
        return []

    sessions = []
    for jsonl_path in sorted(project_dir.glob("*.jsonl")):
        session_id = jsonl_path.stem
        preview = _get_session_preview(jsonl_path)
        sessions.append({
            "session_id": session_id,
            "path": jsonl_path,
            "preview": preview,
        })
    return sessions


def _get_session_preview(path: Path) -> str:
    """Extract first user message from a JSONL file as a preview."""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "user":
                    continue
                message = data.get("message", {})
                content = message.get("content")
                if isinstance(content, str):
                    return content[:100]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")[:100]
    except OSError:
        pass
    return "(no preview)"


def build_import_payload(
    jsonl_path: Path, name: str | None = None
) -> dict:
    """Parse a Claude Code JSONL file (via the claude harness's transcript
    codec) and build the import API payload. Handoff reads Claude's on-disk
    JSONL, so the codec is always the claude-code harness's."""
    from .harness import get_harness

    parsed = get_harness("claude-code").transcript_codec.parse_file(str(jsonl_path))
    meta = parsed.metadata

    return {
        "name": name or f"Handoff: {(meta.first_user_message or 'session')[:60]}",
        "working_dir": meta.cwd,
        "claude_session_id": meta.session_id,
        "messages": [msg.model_dump(exclude_none=True) for msg in parsed.messages],
    }


def do_handoff(args: argparse.Namespace) -> None:
    """Execute the handoff subcommand."""
    server = args.server.rstrip("/")
    token = args.token

    # Determine project dir
    project_dir = Path(args.project_dir) if args.project_dir else get_project_dir()

    if args.session_id:
        jsonl_path = project_dir / f"{args.session_id}.jsonl"
        if not jsonl_path.exists():
            print(f"Error: Session file not found: {jsonl_path}", file=sys.stderr)
            sys.exit(1)
    else:
        # Discover and let user pick
        sessions = discover_sessions(project_dir)
        if not sessions:
            print(f"No sessions found in {project_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(sessions)} session(s) in {project_dir}:\n")
        for i, s in enumerate(sessions, 1):
            print(f"  {i}. {s['session_id'][:12]}...  {s['preview']}")
        print()

        try:
            choice = input(f"Select session (1-{len(sessions)}): ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(sessions):
                raise ValueError
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)

        jsonl_path = sessions[idx]["path"]

    # Build payload
    payload = build_import_payload(jsonl_path, name=args.name)
    msg_count = len(payload["messages"])
    print(f"Importing session with {msg_count} messages...")

    # POST to server
    url = f"{server}/api/sessions/import"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"Session imported: {result['id']}")
            print(f"  Name: {result['name']}")
            print(f"  Messages: {result['message_count']}")
            print(f"  URL: {server}/sessions/{result['id']}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error: HTTP {e.code} — {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError:
        print(f"Error: Could not connect to {server}", file=sys.stderr)
        print("  Is the Octopus server running? Start it with:", file=sys.stderr)
        print("    octopus serve", file=sys.stderr)
        print("  Or with Cloudflare Tunnel:", file=sys.stderr)
        print("    octopus serve --tunnel", file=sys.stderr)
        sys.exit(1)


def do_pull(args: argparse.Namespace) -> None:
    """Read fixed members completely before publishing a local native transcript."""
    from .harness import get_harness
    from .models import MessageContent, MessageExportPage

    server = args.server.rstrip("/")
    token = args.token
    session_id = args.session_id

    url = f"{server}/api/sessions/{urllib.parse.quote(session_id, safe='')}/message-export"
    temporary: Path | None = None
    replaced = False

    def fetch(query: dict[str, str | int] | None = None) -> MessageExportPage:
        request = urllib.request.Request(
            url + ("?" + urllib.parse.urlencode(query) if query else ""),
            headers={"Authorization": f"Bearer {token}"}, method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return MessageExportPage.model_validate(json.loads(response.read()))

    try:
        first = fetch()
        snapshot = first.snapshot
        if snapshot.session_id != session_id or (snapshot.expected_count == 0) != (snapshot.max_seq is None):
            raise ValueError("Invalid message export identity or membership")
        harness = get_harness(snapshot.backend)
        codec = harness.transcript_codec
        if codec is None:
            raise ValueError(f"pull isn't supported for the {snapshot.backend.value} harness — it has no on-disk transcript format.")
        claude_session_id = snapshot.claude_session_id
        if not claude_session_id:
            import uuid
            claude_session_id = str(uuid.uuid4())
            print(f"Note: No claude_session_id on server — generated {claude_session_id}")
        if claude_session_id in {".", ".."} or "/" in claude_session_id or "\\" in claude_session_id:
            raise ValueError("Unsafe native session filename")
        working_dir = args.cwd or snapshot.working_dir or str(Path.cwd())
        project_dir = Path(args.project_dir) if args.project_dir else get_project_dir(working_dir)
        out_path = project_dir / f"{claude_session_id}.jsonl"
        count = 0
        verified = False

        def messages() -> Iterator[MessageContent]:
            nonlocal count, verified
            page = first
            last_seq: int | None = None
            while True:
                if page.snapshot != snapshot:
                    raise ValueError("Message export snapshot changed between pages")
                before = count
                for message in page.messages:
                    seq = message.seq
                    if (seq is None or seq < 0 or last_seq is not None and seq <= last_seq
                            or snapshot.max_seq is None or seq > snapshot.max_seq):
                        raise ValueError("Invalid or duplicate message export sequence")
                    count += 1
                    if count > snapshot.expected_count:
                        raise ValueError("Message export exceeds its captured membership")
                    last_seq = seq
                    yield message
                if not page.has_more:
                    if (page.next_after_seq is not None or count != snapshot.expected_count
                            or last_seq != snapshot.max_seq):
                        raise ValueError("Incomplete message export membership")
                    verified = True
                    return
                if (count == before or page.next_after_seq != last_seq
                        or last_seq is None or count >= snapshot.expected_count):
                    raise ValueError("Message export cursor did not advance")
                page = fetch({"snapshot": snapshot.model_dump_json(), "after_seq": last_seq})

        project_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{claude_session_id}.", suffix=".tmp", dir=project_dir)
        temporary = Path(name)
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            codec.write_file(str(temporary), messages(), claude_session_id, working_dir)
            if not verified:
                raise ValueError("Transcript codec did not consume the complete export")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, out_path)
        replaced = True
        directory = os.open(project_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception as error:
        detail = (f"HTTP {error.code} — {error.read().decode('utf-8', errors='replace')}"
                  if isinstance(error, urllib.error.HTTPError) else str(error))
        if replaced:
            detail = f"Export replaced the target, but directory synchronization is unconfirmed; the new file may already be active. {detail}"
        print(f"Error: {detail}", file=sys.stderr)
        sys.exit(1)
    finally:
        if temporary is not None and not replaced:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                print(f"Error: Could not remove temporary export {temporary}: {error}", file=sys.stderr)

    if count == 0:
        print("Warning: Session has no messages.", file=sys.stderr)
    print(f"Pulled session with {count} messages → {out_path}")
    print(f"\nResume with:\n  claude --resume {claude_session_id}")


def do_serve(args: argparse.Namespace) -> None:
    """Execute the serve subcommand (default)."""
    import os
    import subprocess

    # Resolve CLI overrides before checking the managed installation contract.
    if getattr(args, "tunnel", None) is not None:
        os.environ["OCTOPUS_ENABLE_TUNNEL"] = str(args.tunnel).lower()

    from .config import settings
    from .service_installation import InstallationError, read_service_profile

    try:
        profile = read_service_profile(settings)
        if getattr(args, "service_process", False) and profile is None:
            raise InstallationError("--service-process requires a configured managed installation")
        if profile is not None and not getattr(args, "service_process", False):
            command = ["systemctl", *(["--user"] if profile.manager == "user" else []), "start", "--", profile.unit]
            subprocess.run(command, check=True)
            print(f"Using {profile.unit}; readiness: http://127.0.0.1:{profile.port}/ready")
            return
    except (InstallationError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78) from error

    dist_index = Path(__file__).resolve().parent.parent / "web" / "dist" / "index.html"
    if not dist_index.exists():
        print(
            "Warning: Built frontend not found (web/dist/index.html).\n"
            "  The API will work, but no UI will be served.\n"
            "  Run `cd web && bun run build` to build the frontend.\n"
        )

    from .main import run
    run()


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="octopus",
        description="Octopus — remote Claude Code controller",
    )
    subparsers = parser.add_subparsers(dest="command")

    export_parser = subparsers.add_parser("export-account", help="Export current account data to a portable archive")
    export_parser.add_argument("user_id", help="Original account ID")
    export_parser.add_argument("archive", type=Path, help="New archive path, outside the selected account")
    export_parser.add_argument("--home", type=Path, default=Path.home(), help="Source home layout prefix")
    export_parser.add_argument("--token-file", type=Path, help="Administrator bearer for a running managed source")
    export_parser.add_argument("--wait", type=float, default=1800, help="Seconds to wait for natural source idle")
    export_parser.add_argument("--pause-command", help="Program and arguments to drain/stop external writers")
    export_parser.add_argument("--resume-command", help="Program and arguments to restore those writers")
    import_parser = subparsers.add_parser("import-account", help="Import an account archive into empty data locations")
    import_parser.add_argument("archive", type=Path)
    import_parser.add_argument("--home", type=Path, default=Path.home(), help="Target home layout prefix")

    # serve (default)
    serve_parser = subparsers.add_parser("serve", help="Start the Octopus server")
    serve_parser.add_argument(
        "--tunnel",
        action="store_true",
        default=None,
        help="Enable Cloudflare Tunnel for public HTTPS access",
    )
    serve_parser.add_argument(
        "--service-process", action="store_true",
        help="Run the managed server inside its configured systemd unit",
    )

    # handoff
    handoff_parser = subparsers.add_parser(
        "handoff", help="Import a local Claude Code session"
    )
    handoff_parser.add_argument(
        "--session-id",
        help="Claude Code session UUID (skips interactive selection)",
    )
    handoff_parser.add_argument(
        "--project-dir",
        help="Path to Claude Code project directory (default: auto-detect from cwd)",
    )
    handoff_parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="Octopus server URL (default: http://localhost:8000)",
    )
    handoff_parser.add_argument(
        "--token",
        default="changeme",
        help="Auth token for the Octopus server",
    )
    handoff_parser.add_argument(
        "--name",
        help="Name for the imported session",
    )

    # pull
    pull_parser = subparsers.add_parser(
        "pull", help="Pull a session from Octopus and write as Claude Code JSONL"
    )
    pull_parser.add_argument(
        "session_id",
        help="Octopus session ID to pull",
    )
    pull_parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="Octopus server URL (default: http://localhost:8000)",
    )
    pull_parser.add_argument(
        "--token",
        default="changeme",
        help="Auth token for the Octopus server",
    )
    pull_parser.add_argument(
        "--project-dir",
        help="Path to Claude Code project directory (default: auto-detect from working_dir)",
    )
    pull_parser.add_argument(
        "--cwd",
        help="Override the working directory stored in the session",
    )

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Read what the server has recorded about itself",
        description=(
            "Canned reports over octopus-metrics.db. The data is plain SQLite, "
            "so anything not listed here is a sqlite3 query away."
        ),
    )
    monitor_parser.add_argument(
        "report",
        nargs="?",
        default="overview",
        help="overview (default), turns, codex, codex_turns, errors, connectors, schedules, resources, http",
    )
    monitor_parser.add_argument(
        "--since",
        default="7d",
        metavar="WINDOW",
        help="How far back to look: 30m, 24h, 7d (default 7d)",
    )

    return parser


def do_monitor(args) -> None:
    """Print one report as an aligned table.

    Formatting lives here rather than in `monitor/query.py` so the same
    functions serve the API without carrying presentation with them.
    """
    import asyncio
    import os

    import aiosqlite

    from .config import settings
    from .monitor import query

    async def run() -> int:
        if not os.path.exists(settings.metrics_db_path):
            print(f"No metrics file yet at {settings.metrics_db_path}.")
            print("It is created when the server starts; give it a few minutes of traffic.")
            return 1
        conn = await aiosqlite.connect(
            f"file:{settings.metrics_db_path}?mode=ro", uri=True
        )
        try:
            if args.report == "overview":
                data = await query.overview(conn, args.since)
                print(f"Octopus — last {data['window']}\n")
                _table("activity", data["counts"])
                _table("turns", data["turns"])
                _table("Codex effective output TPS (including reasoning)", data["codex"])
                _table("top errors", data["errors"])
                _table("resources", data["resources"])
                return 0
            fn = query.REPORTS.get(args.report)
            if fn is None:
                print(f"Unknown report {args.report!r}.")
                print("Available: overview, " + ", ".join(sorted(query.REPORTS)))
                return 2
            rows = await fn(conn, args.since)
            print(f"{args.report} — last {args.since}\n")
            _table(args.report, rows)
            return 0
        finally:
            await conn.close()

    raise SystemExit(asyncio.run(run()))


def _table(title: str, rows) -> None:
    """Minimal aligned table. Empty is stated rather than shown as blank, so
    "nothing recorded" never reads as "nothing wrong"."""
    if not rows:
        print(f"  {title}: (nothing recorded in this window)\n")
        return
    cols = list(rows[0].keys())
    widths = {
        c: max(len(str(c)), *(len(_fmt(r.get(c))) for r in rows)) for c in cols
    }
    print(f"  {title}")
    print("    " + "  ".join(str(c).ljust(widths[c]) for c in cols))
    print("    " + "  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("    " + "  ".join(_fmt(r.get(c)).ljust(widths[c]) for c in cols))
    print()


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def do_portability(args: argparse.Namespace) -> None:
    """Thin local adapter; domain operations own all package and restore rules."""
    import shlex
    import sqlite3
    import subprocess
    import zipfile
    from contextlib import contextmanager

    from .config import settings
    from .portability import export_account, import_account
    from .portability_lifecycle import managed_source

    try:
        if args.command == "import-account":
            config = import_account(args.archive, home=args.home)
            result = {"imported": True, "environment": config.db_path + ".import.env"}
        else:
            if bool(args.pause_command) != bool(args.resume_command):
                raise ValueError("External writer pause and resume commands must be supplied together")
            if args.wait < 0:
                raise ValueError("Wait must not be negative")
            token = args.token_file.read_text().strip() if args.token_file else ""

            @contextmanager
            def source_lifecycle():
                external_touched = False
                try:
                    if args.pause_command:
                        external_touched = True
                        subprocess.run(shlex.split(args.pause_command), check=True)
                    with managed_source(settings, token=token, timeout=args.wait):
                        yield
                finally:
                    if external_touched:
                        subprocess.run(shlex.split(args.resume_command), check=True)

            result = export_account(settings, args.user_id, args.archive, home=args.home, pause=source_lifecycle())
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(f"Account migration failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        do_serve(args)
    elif args.command == "handoff":
        do_handoff(args)
    elif args.command == "pull":
        do_pull(args)
    elif args.command == "monitor":
        do_monitor(args)
    elif args.command in {"export-account", "import-account"}:
        do_portability(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
