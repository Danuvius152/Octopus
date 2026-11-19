#!/usr/bin/env python3
"""Copy the OpenAPI generator's source from a Git tree or the working tree."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def copy_commit_tree(destination: Path, tree: str) -> None:
    entries = subprocess.check_output(["git", "ls-tree", "-rz", tree]).split(b"\0")
    # Git blobs provide the committed bytes independently of checkout and archive filters.
    with subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE) as reader:
        assert reader.stdin is not None and reader.stdout is not None
        for entry in filter(None, entries):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.split()
            target = destination / os.fsdecode(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == b"commit":  # A Git submodule entry has no checked-in source here.
                target.mkdir()
                continue

            reader.stdin.write(object_id + b"\n")
            reader.stdin.flush()
            _, _, size = reader.stdout.readline().split()
            content = reader.stdout.read(int(size))
            if reader.stdout.read(1) != b"\n":
                raise RuntimeError(f"Incomplete Git blob for {os.fsdecode(name)}")
            if mode == b"120000":
                target.symlink_to(os.fsdecode(content))
            else:
                target.write_bytes(content)
                if mode == b"100755":
                    target.chmod(0o755)
        reader.stdin.close()
        if reader.wait() != 0:
            raise SystemExit(reader.returncode)


def copy_working_tree(destination: Path) -> None:
    paths = subprocess.check_output([
        "git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--deduplicate",
        "--", ".", ":(exclude).venv", ":(exclude)web/node_modules",
    ]).split(b"\0")
    for name in filter(None, paths):
        source = Path(os.fsdecode(name))
        if source.is_file() or source.is_symlink():
            target = destination / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)


if __name__ == "__main__":
    output, commit_tree = Path(sys.argv[1]), sys.argv[2]
    output.mkdir()
    if commit_tree:
        copy_commit_tree(output, commit_tree)
    else:
        copy_working_tree(output)
