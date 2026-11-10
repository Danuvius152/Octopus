"""Application resource addresses, content trees and required access links."""

import os
from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Literal

ResourceRole = Literal["code", "data", "runtime"]


def data_dir_for(app_dir: str) -> str:
    """Durable state is a sibling of the stored code address, never its target."""
    return app_dir.rstrip("/") + ".data"


def runtime_dir_for(app_dir: str) -> str:
    """Rebuildable dependencies are separate from code and durable state."""
    return app_dir.rstrip("/") + ".runtime"


@dataclass(frozen=True)
class ApplicationResource:
    role: ResourceRole
    stored: Path
    entry: Path
    content: Path
    present: bool
    links: frozenset[Path]


def _required_links(stored: Path) -> frozenset[Path]:
    links: set[Path] = set()
    pending, visited = [stored], set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        for part in (*path.parents, path):
            if part.is_symlink():
                links.add(part.parent.resolve() / part.name)
                target = Path(os.readlink(part))
                pending.append(target if target.is_absolute() else part.parent / target)
    return frozenset(links)


def application_resources(app_dir: str) -> tuple[ApplicationResource, ...]:
    """Observe one resource family; callers reobserve before coordinated effects.

    Links are entries at physical parents, including chains. An ancestor link
    is required access, not an exclusive claim to its entire target subtree.
    Only missing paths permit non-strict resolution; other I/O errors propagate.
    """
    resources = []
    family: tuple[tuple[ResourceRole, str], ...] = (
        ("code", app_dir), ("data", data_dir_for(app_dir)), ("runtime", runtime_dir_for(app_dir)),
    )
    for role, address in family:
        # Keep '..' until symlinks have been resolved. Lexical abspath/normpath
        # would change the target of an existing 'link/../resource' address.
        stored = Path(address).expanduser()
        if not stored.is_absolute():
            stored = Path.cwd() / stored
        try:
            content = Path(os.path.realpath(stored, strict=True))
            present = True
        except FileNotFoundError:
            content = Path(os.path.realpath(stored))
            present = False
        resources.append(ApplicationResource(role, stored, stored.parent.resolve() / stored.name, content, present, _required_links(stored)))
    return tuple(resources)


def removal_order(resources: tuple[ApplicationResource, ...], links: set[Path]) -> list[tuple[Path, bool]]:
    """Keep every unfinished resource discoverable through its stored address.

    A tree may contain a sibling's access link; that sibling must finish first.
    Inner links precede outer links so a failed unlink remains discoverable on
    retry. Missing contents need no effect, even if a removed inner link now
    makes a remaining alias resolve to a different, also missing, address.
    Cyclic cleanup dependencies are refused before any filesystem effect.
    """
    trees = tuple(resource.content for resource in resources if resource.present)

    def effect(link: Path) -> Path | None:
        return next((tree for tree in trees if link.is_relative_to(tree)), link if link in links else None)

    dependencies: dict[Path, set[Path]] = {tree: set() for tree in trees}
    for resource in resources:
        for link in resource.links:
            node = effect(link)
            if node is not None:
                dependencies.setdefault(node, set())
                if resource.present:
                    dependencies[node].add(resource.content)
    for link in sorted(links):
        node = effect(link)
        assert node is not None
        target = Path(os.readlink(link))
        target = target if target.is_absolute() else link.parent / target
        for required in _required_links(target):
            dependency = effect(required)
            if dependency is not None and dependency != node:
                dependencies[node].add(dependency)
    ordering = TopologicalSorter(dependencies)
    ordering.prepare()
    ready: list[Path] = []
    result = []
    while ordering.is_active():
        ready.extend(ordering.get_ready())
        # Finish a completed tree's access chain before starting an unrelated
        # tree. That also keeps interrupted deletion archives free of dangling
        # aliases whenever the next content removal fails.
        path = next((path for path in ready if path not in trees), ready[0])
        ready.remove(path)
        result.append((path, path not in trees))
        ordering.done(path)
    return result
