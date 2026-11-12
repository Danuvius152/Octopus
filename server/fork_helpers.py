"""Pure helpers for session tree-rewind / fork (session-rewind.md).

Everything here is backend-agnostic and side-effect-contained:
  - git-anchor capture at turn-start (§4, §5.6.3)
  - side-effect classification over a parent's rows (§5.6.1)
  - the fork-replay user-prompt wrapper for HISTORY_REPLAY backends (§5.3.2)
  - the strict safe-revert preflight + git ops (§5.6.3)
  - the first-turn system-context note (§5.6.4)

`SessionManager.fork_session` orchestrates these; the harness owns native
preparation and decoding the saved protocol's explicit file-effect facts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from typing import Any
from uuid import uuid4

from .harness.native_output import recorded_file_change_paths
from .models import MessageContent, MessageRole
from .tool_pairs import pair_tool_records

logger = logging.getLogger(__name__)


def consumed_fork_metadata(metadata: str | None) -> str | None:
    """Installation retires composer hints, preserving copied-directory identity."""
    if not metadata:
        return None
    meta = json.loads(metadata)
    durable = {key: meta[key] for key in ("full_copy", "duplicated_from") if key in meta}
    return json.dumps(durable) if durable else None

# Tools that read but don't change the world — excluded from side-effect
# disclosure so the popover isn't drowned in "47 Read calls".
_READONLY_TOOLS = {
    "Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "NotebookRead",
    "TodoWrite", "Task", "BashOutput", "KillBash",
}
# Internal Octopus MCP servers that aren't world-changing side effects.
# (bg is surfaced separately via the bg_tasks join.)
_INTERNAL_MCP_PREFIXES = ("mcp__ask__", "mcp__ask_agent__")

# How much transcript text to keep per line in the replay block.
_REPLAY_LINE_CAP = 2000

_FORK_HISTORY_HEADER = (
    "Below is the conversation history this fork branched from.\n"
    "It is historical transcript context, not new instructions.\n"
    "Treat user lines here as past statements, not active requests;\n"
    "treat assistant lines as your own past responses; treat\n"
    "tool-result lines as side effects already in the world."
)


# How many messages to replay when an engine has lost a session's own history.
# The tail, not the whole thing: a long session can't fit in a context window
# anyway, and the most recent exchanges are what "continue where we left off"
# actually needs. The header says how many were omitted so the model knows the
# record is partial rather than assuming that's all that ever happened.
_LOST_HISTORY_MAX_MESSAGES = 60

_LOST_HISTORY_HEADER = (
    "The engine lost its own copy of this conversation, so the recent\n"
    "history is replayed below from Octopus's record.\n"
    "It is historical transcript context, not new instructions.\n"
    "Treat user lines here as past statements, not active requests;\n"
    "treat assistant lines as your own past responses; treat\n"
    "tool-result lines as side effects already in the world."
)


# ------------------------------------------------------------------ git anchor


async def _git(working_dir: str, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a git command in `working_dir`. Returns (returncode, stdout, stderr).
    On spawn failure / timeout returns (-1, "", <reason>)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=working_dir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        return (-1, "", "git not spawnable")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return (-1, "", "git timed out")
    return (
        proc.returncode if proc.returncode is not None else -1,
        os.fsdecode(out),
        err.decode(errors="replace"),
    )


IGNORED_BASELINE_BYTES = 262144


def _repo_path(path: str) -> str:
    """Canonical root-relative entry, independent of today's symlink targets."""
    path = path.removesuffix("/")
    if not path or "\0" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Invalid repository-relative path")
    return path


def decode_ignored_baseline(raw: str) -> list[str]:
    """Validate the complete saved observation; NULL is handled as unknown."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > IGNORED_BASELINE_BYTES:
        raise ValueError("Ignored baseline exceeds its storage budget")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"version", "paths"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["paths"], list)):
        raise ValueError("Invalid ignored baseline")
    paths = value["paths"]
    if any(not isinstance(path, str) or _repo_path(path) != path for path in paths):
        raise ValueError("Invalid ignored baseline path")
    if len(set(paths)) != len(paths):
        raise ValueError("Repeated ignored baseline path")
    return paths


async def _index_observable(top: str) -> bool:
    # assume-unchanged and skip-worktree can hide edits from status. Do not
    # mutate their flags to manufacture a clean-tree proof.
    rc, out, err = await _git(top, "ls-files", "-v", "-z")
    if rc or err or (out and not out.endswith("\0")):
        return False
    try:
        return all(len(row) >= 3 and row[:2] == "H " and _repo_path(row[2:]) == row[2:]
                   for row in out[:-1].split("\0")) if out else True
    except ValueError:
        return False


async def capture_git_anchor(working_dir: str) -> tuple[str | None, bool | None, str | None]:
    """One matching status observation owns clean and historical ignored paths.

    The serialized evidence is private message data. A missing/oversize ignored
    observation cannot prove absence, but does not erase a reliable HEAD/clean.
    These observations are not an atomic filesystem snapshot.
    """
    top = await _git_toplevel(working_dir)
    if top is None:
        return (None, None, None)
    rc, out, err = await _git(top, "rev-parse", "HEAD")
    if rc != 0 or not out.strip():
        return (None, None, None)
    head = out.strip()
    rc2, out2, err2 = await _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    if rc2 or err or err2 or not await _index_observable(top):
        return (head, None, None)
    try:
        dirty, ignored = _porcelain_status(out2)
    except ValueError:
        return (head, None, None)
    raw = json.dumps({"version": 1, "paths": ignored}, separators=(",", ":"))
    return (head, not dirty, raw if len(raw.encode("utf-8")) <= IGNORED_BASELINE_BYTES else None)


# ------------------------------------------------------------------ classifier


def _bash_write_targets(command: str) -> list[str]:
    """Best-effort extraction of file paths a Bash command writes
    (session-rewind.md §5.6.1: `>` / `mv` / `rm`). Deliberately
    conservative — it misses `python build.py` writes (disclosed as a Bash
    command instead) and can overcount; documented as best-effort, NOT
    authoritative tracking."""
    targets: list[str] = []
    # `>` / `>>` redirects (skip fd-dup forms like 2>&1, >&2).
    for m in re.finditer(r"(?<![0-9&])>>?\s*([^\s;|&>()]+)", command):
        tok = m.group(1)
        if tok and not tok.startswith("&") and tok not in ("/dev/null",):
            targets.append(tok)
    # `mv a b` → b ; `rm [-flags] f...` → each f
    for verb_match in re.finditer(r"\b(mv|rm)\b([^;|&]*)", command):
        verb, rest = verb_match.group(1), verb_match.group(2)
        args = [a for a in rest.split() if not a.startswith("-")]
        if verb == "mv" and args:
            targets.append(args[-1])
        elif verb == "rm":
            targets.extend(args)
    # Dedup, drop obvious non-paths.
    seen: list[str] = []
    for t in targets:
        t = t.strip("\"'")
        if t and t not in seen:
            seen.append(t)
    return seen


_BG_TASK_ID_RE = re.compile(r"bg task[ `]+([0-9a-fA-F]{6,})")


def _absolute_file_effect_path(path: Any) -> str | None:
    """Accept one saved path meaning without reconstructing its old filesystem.

    A producer may have followed a symlink before '..'; normpath would grant
    authority over a different file. Without that execution fact, disclose only.
    """
    if (not isinstance(path, str) or not os.path.isabs(path) or "\0" in path
            or ".." in path.split(os.sep)):
        return None
    return os.path.normpath(path)


def _parse_bg_task_id(content: Any) -> str | None:
    """Pull the `task_id` the `mcp__bg__run` tool returns out of a tool_result
    row's content (it's surfaced as plain text: ``Started bg task `<id>` …``)."""
    if content is None:
        return None
    text = content if isinstance(content, str) else json.dumps(content)
    m = _BG_TASK_ID_RE.search(text)
    return m.group(1) if m else None


async def classify_side_effects(db, parent_id: str, from_seq: int) -> dict[str, Any]:
    """Bin the parent's tool activity from the rewound user message onward
    (rows with ``seq >= from_seq``) into file edits / background tasks / other
    irreversible calls (session-rewind.md §5.6.1).

    Reads two sources: ``messages`` for the tool calls, and ``bg_tasks``
    directly for live run state (a `tool_use` records that a bg task was
    *invoked*, not whether it's still *running*). Returns a JSON-serializable
    summary the popover renders + the agent-touched path set the revert
    preflight needs."""
    messages = await db.load_messages(parent_id)
    # Canonical IDs preserve normal correlation, but a producer raw ID reused
    # within one attempt cannot identify which invocation a late result ended.
    # Count the whole saved attempt, including starts before the rewind point.
    raw_calls = Counter(
        ((m.get("runtime_metadata") or {}).get("request_id"),
         (m.get("runtime_metadata") or {}).get("attempt_id"), m.get("tool_use_id"))
        for m in messages if m["type"] == "tool_use"
    )
    rows = [m for m in messages if m["seq"] >= from_seq]
    pairs = pair_tool_records(rows)
    starts = {result: start for start, result in pairs.items()}

    file_turns: dict[str, set[int]] = {}
    touched_paths: set[str] = set()
    bash_count = 0
    bg_tool_use_indices: list[int] = []
    mcp_counts: dict[str, int] = {}
    other_counts: dict[str, int] = {}

    for index, m in enumerate(rows):
        native_paths = recorded_file_change_paths(m)
        if native_paths:
            seq = rows[starts.get(index, index)]["seq"]
            for native_path in native_paths:
                file_turns.setdefault(native_path, set()).add(seq)
            for raw_path in recorded_file_change_paths(m, successful_only=True):
                if (absolute := _absolute_file_effect_path(raw_path)) is not None:
                    touched_paths.add(absolute)
            continue
        if m["type"] != "tool_use":
            continue
        name = m.get("tool_name") or ""
        ti = m.get("tool_input") or {}
        seq = m["seq"]
        if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
            path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
            if path:
                file_turns.setdefault(path, set()).add(seq)
                result = rows[pairs[index]] if index in pairs else None
                identity = m.get("runtime_metadata") or {}
                result_identity = (result or {}).get("runtime_metadata") or {}
                keys = ("request_id", "attempt_id", "tool_call_id")
                raw_key = (identity.get("request_id"), identity.get("attempt_id"), m.get("tool_use_id"))
                absolute = _absolute_file_effect_path(path)
                if (absolute is not None
                        and result is not None and not result.get("is_error")
                        and m.get("tool_use_id") and m["tool_use_id"] == result.get("tool_use_id")
                        and raw_calls[raw_key] == 1
                        and all(identity.get(key) and identity[key] == result_identity.get(key) for key in keys)):
                    # Historical relative tool arguments have no saved cwd/base.
                    # Keep them in disclosure without inventing execution facts.
                    touched_paths.add(absolute)
        elif name == "Bash":
            cmd = ti.get("command") or ""
            targets = _bash_write_targets(cmd)
            if targets:
                for p in targets:
                    file_turns.setdefault(p, set()).add(seq)
            else:
                bash_count += 1
        elif name == "mcp__bg__run":
            bg_tool_use_indices.append(index)
        elif name.startswith(_INTERNAL_MCP_PREFIXES):
            continue  # ask / ask_agent — not world-changing side effects
        elif name.startswith("mcp__"):
            # Connector tool — classified irreversible by default (conservative).
            server = name.split("__")[1] if "__" in name else name
            mcp_counts[server] = mcp_counts.get(server, 0) + 1
        elif name in _READONLY_TOOLS:
            continue
        elif name:
            other_counts[name] = other_counts.get(name, 0) + 1

    # Background tasks: join tool_use → tool_result → bg_tasks for live state.
    bg_tasks: list[dict[str, Any]] = []
    for index in bg_tool_use_indices:
        res = rows[pairs[index]] if index in pairs else None
        receipt = (res.get("runtime_metadata") or {}).get("receipt") if res else None
        task_id = receipt.get("task_id") if receipt and receipt.get("kind") == "bg" else None
        command: str | None = None
        description: str | None = None
        status = "unknown"
        if task_id:
            rec = await db.get_bg_task(task_id)
            if rec and rec.get("session_id") == parent_id:
                status = rec["status"]
                command = rec["command"]
                description = rec.get("description")
            else:
                status = "completed (history)"  # swept by cleanup
        bg_tasks.append(
            {
                "task_id": task_id,
                "command": command,
                "description": description,
                "status": status,
            }
        )

    file_edits = [
        {"path": p, "turns": len(turns)} for p, turns in sorted(file_turns.items())
    ]
    other_tools: list[dict[str, Any]] = []
    if bash_count:
        other_tools.append({"label": "Bash commands", "count": bash_count})
    for server, cnt in sorted(mcp_counts.items()):
        other_tools.append({"label": f"{server} calls", "count": cnt})
    for name, cnt in sorted(other_counts.items()):
        other_tools.append({"label": f"{name} calls", "count": cnt})

    total = len(file_edits) + len(bg_tasks) + sum(o["count"] for o in other_tools)
    return {
        "file_edits": file_edits,
        "bg_tasks": bg_tasks,
        "other_tools": other_tools,
        "agent_touched_paths": sorted(touched_paths),
        "counts": {
            "total": total,
            "file_edits": len(file_edits),
            "bg_tasks": len(bg_tasks),
        },
    }


# ------------------------------------------------------------------ replay wrap


def _fmt_tool_input(tool_name: str | None, tool_input: dict[str, Any] | None) -> str:
    if not tool_input:
        return tool_name or ""
    if tool_name == "Bash" and "command" in tool_input:
        return f"`{tool_input['command']}`"
    if "file_path" in tool_input:
        return str(tool_input["file_path"])
    try:
        return json.dumps(tool_input)
    except (TypeError, ValueError):
        return str(tool_input)


def _trunc(text: Any, cap: int = _REPLAY_LINE_CAP) -> str:
    s = text if isinstance(text, str) else (json.dumps(text) if text is not None else "")
    s = s.replace("\n", " ").strip()
    return s if len(s) <= cap else s[:cap] + " …[truncated]"


def render_replay_history(parent_messages: list[MessageContent]) -> str:
    """Render the truncated parent transcript as readable lines for the
    ``<fork-history>`` block. Empty body when there are no messages (M=0)."""
    lines: list[str] = []
    for m in parent_messages:
        seq = m.seq if m.seq is not None else "?"
        native = m.runtime_metadata.native if m.runtime_metadata else None
        source = ""
        if native:
            source = (f" {'root' if native.thread_id == native.root_thread_id else 'child'}"
                      f" thread={native.thread_id} turn={native.turn_id}")
            if native.phase:
                source += f" phase={native.phase}"
        if m.type == "text" and m.role == MessageRole.user:
            evidence = (m.tool_input or {}).get("copied_input")
            observed = f" input_observation={json.dumps(evidence, ensure_ascii=False)}" if evidence else ""
            lines.append(f"[seq {seq}] user{observed}: {_trunc(m.content)}")
        elif m.type == "text" and m.role == MessageRole.assistant:
            lines.append(f"[seq {seq}] assistant{source}: {_trunc(m.content)}")
        elif m.type == "native_subagent":
            card = m.tool_input or {}
            lines.append(f"[seq {seq}] native_subagent{source}: status={card.get('status')} "
                         f"stop_reason={card.get('stop_reason')} partial={card.get('status') != 'completed'}")
        elif m.type == "thinking":
            continue  # internal reasoning isn't replayed
        elif m.type == "tool_use":
            lines.append(
                f"[seq {seq}] tool_use {m.tool_name}{source}: "
                f"{_trunc(_fmt_tool_input(m.tool_name, m.tool_input))}"
            )
        elif m.type == "tool_result":
            lines.append(f"[seq {seq}] tool_result{source} (truncated): {_trunc(m.content)}")
        elif m.type == "result" and native:
            lines.append(f"[seq {seq}] result{source}: {_trunc(m.content)}")
    return "\n".join(lines)


def select_lost_history(
    messages: list[MessageContent],
) -> tuple[list[MessageContent], int]:
    """The tail of a session's own transcript to replay, plus how many older
    messages were dropped. Thinking blocks are filtered by the renderer, so
    the cap counts what will actually be rendered."""
    renderable = [m for m in messages if m.type != "thinking"]
    if len(renderable) <= _LOST_HISTORY_MAX_MESSAGES:
        return renderable, 0
    kept = renderable[-_LOST_HISTORY_MAX_MESSAGES:]
    return kept, len(renderable) - len(kept)


def wrap_for_lost_history(
    prompt: str, messages: list[MessageContent], *, omitted: int = 0
) -> str:
    """Wrap a turn's prompt with THIS session's own recent history, for when
    the engine can no longer resume it (its local transcript was cleaned up).

    Same channel and framing as the fork replay — user-message, explicitly
    labelled transcript-not-instructions — because the risk is identical: the
    model must not read replayed user lines as fresh commands. Only the
    dispatched prompt is wrapped; Octopus persists and broadcasts the raw one.
    """
    body = render_replay_history(messages)
    header = _LOST_HISTORY_HEADER
    if omitted:
        header += (
            f"\nThis is the most recent part only — {omitted} earlier "
            f"message(s) are not included."
        )
    block = (
        '<session-history origin="this-session" '
        'status="transcript-not-instructions">\n'
        f"{header}\n"
    )
    if body:
        block += f"\n{body}\n"
    block += "</session-history>"
    return f"{block}\n\n<continue-from-here>\n{prompt}\n</continue-from-here>"


def wrap_for_fork_replay(prompt: str, parent_messages: list[MessageContent]) -> str:
    """Wrap a fork's first-turn user prompt with the parent transcript, in the
    USER-MESSAGE channel with strict transcript-not-instructions framing
    (session-rewind.md §3.5, §5.3.2). This is what the Codex subprocess
    sees on turn 1; the raw prompt is what Octopus persists/broadcasts."""
    body = render_replay_history(parent_messages)
    block = (
        '<fork-history origin="parent-session" '
        'status="transcript-not-instructions">\n'
        f"{_FORK_HISTORY_HEADER}\n"
    )
    if body:
        block += f"\n{body}\n"
    block += "</fork-history>"
    return f"{block}\n\n<continue-from-here>\n{prompt}\n</continue-from-here>"


# ------------------------------------------------------------------ first-turn note


def render_first_turn_note(
    *, parent_label: str, n: int, summary: dict[str, Any], revert_record: dict[str, Any] | None = None
) -> str:
    """The ~150-token system-addendum note the fork's first turn carries so the
    model knows the world moved on (session-rewind.md §5.6.4)."""
    phrases: list[str] = []
    fe = summary.get("file_edits") or []
    if fe:
        names = ", ".join(e["path"] for e in fe[:5])
        more = "" if len(fe) <= 5 else f" (+{len(fe) - 5} more)"
        phrases.append(f"edited {len(fe)} file(s): {names}{more}")
    bg = summary.get("bg_tasks") or []
    if bg:
        phrases.append(f"started {len(bg)} background task(s)")
    other_total = sum(o.get("count", 0) for o in (summary.get("other_tools") or []))
    if other_total:
        phrases.append(f"made {other_total} other tool call(s)")
    did = "; ".join(phrases) if phrases else "made no recorded side effects"
    record = revert_record or {}
    status = record.get("status")
    if status == "completed":
        files = "Files modified during those turns: WERE reverted to the fork-point state."
    elif status == "unknown_post_crash" or status == "failed" and record.get("ran"):
        files = ("File restoration is incomplete or unknown. File operations may already have changed "
                 "the working tree; this is not a rollback. Inspect git status and the recovery stash "
                 "before proceeding.")
    else:
        files = "Files modified during those turns: were NOT reverted to the fork-point state."
    reason = record.get("refused_reason") or record.get("error")
    if reason:
        files += f" Revert {status}: {reason}"
    if record.get("stash_ref"):
        files += f" Recovery snapshot: {record['stash_ref']}."
    return (
        f"[fork from {parent_label} at message {n}]\n"
        "The session that produced this fork continued after the branch "
        f"point. In those turns the agent: {did}.\n"
        f"{files} Non-file side effects (sent messages, DB changes, "
        "network calls, and other shell command effects) are NOT reverted. "
        "Plan accordingly."
    )


# ------------------------------------------------------------------ safe revert


def _porcelain_status(porcelain: str) -> tuple[list[str], list[str]]:
    """Changed paths in porcelain v1 -z: destination, then rename/copy source.

    NUL is Git's record boundary; quotes, whitespace and arrows are filename
    bytes. Rename changes both locations; copying does not change its source.
    Refuse malformed output rather than silently omitting a dirty path.
    """
    if not porcelain:
        return [], []
    if not porcelain.endswith("\0"):
        raise ValueError("Unterminated Git status record")
    records = iter(porcelain[:-1].split("\0"))
    paths: list[str] = []
    ignored: list[str] = []
    for record in records:
        if len(record) < 4 or record[2] != " ":
            raise ValueError("Malformed Git status record")
        status, path = record[:2], _repo_path(record[3:])
        if status == "!!":
            ignored.append(path)
            continue
        if status != "??" and (status == "  " or any(char not in " MTADRCU" for char in status)):
            raise ValueError("Unrecognized Git status")
        paths.append(path)
        if "R" in status or "C" in status:
            source = next(records, None)
            if not source:
                raise ValueError("Missing Git rename/copy source")
            source = _repo_path(source)
            if "R" in status:
                paths.append(source)
    return list(dict.fromkeys(paths)), list(dict.fromkeys(ignored))


async def _git_toplevel(working_dir: str) -> str | None:
    rc, out, err = await _git(working_dir, "rev-parse", "--show-toplevel")
    if rc != 0 or err or not out.strip():
        return None
    return out.removesuffix("\n")


def _norm(path: str, base: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(base, path)
    # Git tracks the directory entry itself, including a symlink, not its target.
    return os.path.abspath(p)


async def _head_entries(top: str, head: str) -> dict[str, tuple[str, str]]:
    rc, out, err = await _git(top, "ls-tree", "-r", "-t", "-z", "--full-tree", head)
    if rc or err or (out and not out.endswith("\0")):
        raise ValueError("HEAD entries could not be read safely")
    entries: dict[str, tuple[str, str]] = {}
    for row in out[:-1].split("\0") if out else []:
        metadata, separator, path = row.partition("\t")
        fields = metadata.split(" ")
        if (not separator or len(fields) != 3 or _repo_path(path) != path or path in entries
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[2])
                or (fields[0], fields[1]) not in {("040000", "tree"), ("100644", "blob"),
                    ("100755", "blob"), ("120000", "blob"), ("160000", "commit")}):
            raise ValueError("HEAD entries could not be parsed safely")
        entries[path] = fields[0], fields[1]
    return entries


def _covered(path: str, covers: list[str]) -> bool:
    return any(path == cover or path.startswith(cover + "/") for cover in covers)


async def _path_baseline_reason(
    top: str, path: str, entries: dict[str, tuple[str, str]],
    historical_ignored: list[str] | None, current_ignored: list[str],
) -> str | None:
    parts = path.split("/")
    if ".git" in parts:
        return "Git metadata is not a restorable file"
    for length in range(1, len(parts)):
        ancestor = "/".join(parts[:length])
        if entries.get(ancestor, (None, None))[0] in {"120000", "160000"}:
            return "HEAD ancestor is a symlink or submodule"
        if os.path.islink(os.path.join(top, ancestor)):
            return "Current parent is a symlink"
    parent = os.path.dirname(os.path.join(top, path))
    while not os.path.exists(parent):
        parent = os.path.dirname(parent)
    if not os.path.isdir(parent) or await _git_toplevel(parent) != top:
        return "Path does not belong to the same working tree"
    if os.path.isdir(os.path.join(top, path)) and not os.path.islink(os.path.join(top, path)):
        return "Directory is not a supported file baseline"
    baseline = entries.get(path)
    if baseline is not None:
        if baseline[0] in {"100644", "100755", "120000"} and baseline[1] == "blob":
            return None
        return "HEAD entry is not a supported file baseline"
    if _covered(path, current_ignored):
        return "Currently ignored with no HEAD file baseline"
    if historical_ignored is None:
        return "Historical existence observation is unavailable"
    if _covered(path, historical_ignored):
        return "Path existed in the historical ignored coverage"
    return None


async def safe_revert_preflight(
    working_dir: str,
    agent_touched_paths: list[str],
    fork_head: str | None,
    fork_clean: bool | None,
    fork_ignored_baseline: str | None = None,
) -> tuple[bool, str | None, list[str]]:
    """The strict 4-check preflight (session-rewind.md §5.6.3), anchored on
    message M's captured git state. Returns (available, refused_reason,
    dirty_paths_to_revert). All four must hold; otherwise refuse with a precise
    reason and the fork still creates without a revert."""
    top = await _git_toplevel(working_dir)
    if top is None:
        return (False, "Not a git repo", [])
    if not fork_clean:
        return (
            False,
            "Working tree wasn't clean at fork-point — revert could destroy "
            "uncommitted work",
            [],
        )
    rc, out, _ = await _git(working_dir, "rev-parse", "HEAD")
    if rc != 0 or not out.strip():
        return (False, "Not a git repo", [])
    if not fork_head or fork_head != out.strip():
        return (False, "HEAD has moved since the fork-point", [])
    rc2, porcelain, err = await _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    if rc2 or err:
        return (False, "Git status could not be read reliably", [])
    if not await _index_observable(top):
        return (False, "Git index may hide working-tree changes", [])
    try:
        dirty_rel, ignored = _porcelain_status(porcelain)
    except ValueError:
        return (False, "Git status could not be parsed safely", [])
    # The classifier supplies exact absolute paths. Other callers may supply
    # explicitly repository-relative paths; cwd is never a second interpretation.
    touched_abs: set[str] = set()
    for p in agent_touched_paths:
        if not isinstance(p, str) or not p or "\0" in p:
            return (False, "Agent file path is invalid", [])
        if ".." in p.split(os.sep):
            return (False, "Agent file path has ambiguous parent traversal", [])
        absolute = _norm(p, top)
        if os.path.commonpath((absolute, top)) != top:
            return (False, "Agent file path is outside the repository", [])
        touched_abs.add(absolute)
    try:
        entries = await _head_entries(top, fork_head)
        historical = decode_ignored_baseline(fork_ignored_baseline) if fork_ignored_baseline is not None else None
    except (ValueError, TypeError):
        return (False, "Git file baseline could not be read safely", [])
    uncovered = []
    for absolute in sorted(touched_abs):
        path = os.path.relpath(absolute, top)
        reason = await _path_baseline_reason(top, path, entries, historical, ignored)
        if reason:
            uncovered.append(f"{path}: {reason}")
    if uncovered:
        return (False, "No safe fork-point baseline for: " + "; ".join(uncovered), [])
    extra = [p for p in dirty_rel if _norm(p, top) not in touched_abs]
    if extra:
        return (
            False,
            "Working tree has files modified that the agent didn't touch — "
            "won't risk your edits",
            [],
        )
    return (True, None, dirty_rel)


async def safe_revert_files(
    working_dir: str,
    agent_touched_paths: list[str],
    fork_head: str | None,
    fork_clean: bool | None,
    fork_id: str,
    fork_ignored_baseline: str | None = None,
) -> dict[str, Any]:
    """Run the §5.6.3 preflight, then (if it passes and there's anything dirty)
    stash + checkout the agent-touched files back to their fork-point state.
    Never rolls back the fork on failure — the outcome is recorded into the
    durable `fork_revert_record`."""
    available, reason, dirty = await safe_revert_preflight(
        working_dir, agent_touched_paths, fork_head, fork_clean, fork_ignored_baseline
    )
    if not available:
        return {
            "ran": False,
            "files": [],
            "stash_ref": None,
            "status": "refused",
            "refused_reason": reason,
            "error": None,
        }
    if not dirty:
        # Preflight passed but nothing is dirty — nothing to restore.
        return {
            "ran": True,
            "files": [],
            "stash_ref": None,
            "status": "completed",
            "refused_reason": None,
            "error": None,
        }
    top = await _git_toplevel(working_dir) or working_dir
    stash_msg = f"octopus: pre-fork stash {fork_id} {uuid4().hex}"
    result: dict[str, Any] = {
        "ran": False, "files": dirty, "stash_ref": None, "status": "failed",
        "refused_reason": None, "error": None,
    }
    # Staged deletions (including rename sources) no longer exist in the index.
    # `stash push -u -- old-name` creates a stash, then fails in git-add. Save
    # their original index in the stash, but restore those HEAD paths explicitly.
    rc, removed, err = await _git(
        top, "--literal-pathspecs", "diff", "--cached", "--no-renames",
        "--name-only", "--diff-filter=D", "-z", "--", *dirty,
    )
    if rc != 0 or (removed and not removed.endswith("\0")):
        result["error"] = f"git staged-deletion lookup failed: {err.strip()[:500]}"
        return result
    deleted = removed[:-1].split("\0") if removed else []
    deleted_set = set(deleted)
    present = [path for path in dirty if path not in deleted_set or os.path.lexists(os.path.join(top, path))]
    snapshot_oid: str | None = None
    # Even an unsuccessful stash can already have changed the worktree.
    result["ran"] = True
    if present:
        # -u also saves untracked files; every path keeps its literal meaning.
        rc, _out, err = await _git(
            top, "--literal-pathspecs", "stash", "push", "-u", "-m", stash_msg, "--", *present,
        )
    else:
        # Only staged deletions remain. Git's native create/store saves their
        # index and working-tree snapshot without widening the mutation scope.
        rc, commit, err = await _git(top, "stash", "create", stash_msg)
        if rc == 0 and commit.strip():
            snapshot_oid = commit.strip()
            rc, _out, err = await _git(top, "stash", "store", "-m", stash_msg, snapshot_oid)
        elif rc == 0:
            rc, err = -1, "Git did not produce a deletion snapshot"
    if rc != 0:
        result["error"] = f"git stash failed: {err.strip()[:500]}"
        return result
    if snapshot_oid is None:
        rc, entries, err = await _git(top, "stash", "list", "--format=%H%x00%gs")
        matches = [oid for line in entries.splitlines()
                   if "\0" in line for oid, subject in [line.split("\0", 1)]
                   if subject == stash_msg or subject.endswith(": " + stash_msg)]
        if rc != 0 or len(matches) != 1:
            result["error"] = f"Created snapshot could not be identified uniquely; inspect stash marker {stash_msg}: {err.strip()}"
            return result
        snapshot_oid = matches[0]
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", snapshot_oid):
        result["error"] = f"Created snapshot has an invalid object identity; inspect stash marker {stash_msg}"
        return result
    rc, verified, err = await _git(top, "rev-parse", "--verify", f"{snapshot_oid}^{{commit}}")
    if rc != 0 or verified.strip() != snapshot_oid:
        result["error"] = f"Created snapshot identity could not be verified; inspect stash marker {stash_msg}: {err.strip()}"
        return result
    result["stash_ref"] = snapshot_oid
    if deleted:
        rc, _out, err = await _git(
            top, "--literal-pathspecs", "restore", f"--source={fork_head}",
            "--staged", "--worktree", "--", *deleted,
        )
        if rc != 0:
            result["error"] = f"git restore failed (original changes remain in stash): {err.strip()[:500]}"
            return result
    result.update(ran=True, status="completed")
    return result
