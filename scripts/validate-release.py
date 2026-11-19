#!/usr/bin/env python3
"""Validate candidate assets and its actual Python runtime before switching."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin
from urllib.request import urlopen


def local_entry_assets(entry_content: bytes) -> list:
    parser = Path(__file__).resolve().parents[1] / "web/release-entry-assets.mjs"
    try:
        result = subprocess.run(
            ["node", str(parser)], input=entry_content, capture_output=True, timeout=10,
        )
    except FileNotFoundError as error:
        raise ValueError("Candidate asset validation requires Node.js from the web build environment") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"Candidate entry parser exited with status {result.returncode}")
    return json.loads(result.stdout)


def raise_scan_error(error: OSError) -> None:
    raise error


def validate_dist(root: Path) -> dict:
    root = root.resolve()
    dist = (root / "web/dist").resolve()
    entry = dist / "index.html"
    if not entry.resolve().is_relative_to(dist) or not entry.is_file() or not entry.stat().st_size:
        raise ValueError("Candidate has no usable web/dist/index.html")
    entry_content = entry.read_bytes()
    files: dict[str, Path] = {}
    has_entry_script = False
    for reference, url_path, executable in local_entry_assets(entry_content):
        relative = unquote(url_path).lstrip("/")
        asset = (dist / relative).resolve()
        if not asset.is_relative_to(dist) or not asset.is_file() or not asset.stat().st_size:
            raise ValueError(f"Candidate entry asset is missing or unsafe: {reference}")
        files["/" + relative] = asset
        if executable and asset.suffix in {".js", ".mjs"}:
            has_entry_script = True
    if not has_entry_script:
        raise ValueError("Candidate has no local executable JavaScript entry asset")

    for directory, _, names in dist.walk(on_error=raise_scan_error):
        for name in names:
            asset = directory / name
            if asset == entry:
                continue
            relative = asset.relative_to(dist).as_posix()
            resolved = asset.resolve()
            if not resolved.is_relative_to(dist):
                raise ValueError(f"Candidate asset is unsafe: /{relative}")
            if resolved.is_dir():
                raise ValueError(f"Candidate has a directory symlink: /{relative}")
            if not resolved.is_file():
                raise ValueError(f"Candidate asset is missing or is not a file: /{relative}")
            files.setdefault("/" + relative, resolved)

    assets = {"/": hashlib.sha256(entry_content).hexdigest()}
    for path, asset in files.items():
        with asset.open("rb") as file:
            assets[path] = hashlib.file_digest(file, "sha256").hexdigest()
    return {"root": str(root), "assets": assets}


def validate_served(manifest: dict, base_url: str) -> None:
    with urlopen(urljoin(base_url, "/health"), timeout=5) as response:
        if response.status != 200:
            raise ValueError("Candidate health check failed")
    for path, expected in manifest["assets"].items():
        try:
            with urlopen(urljoin(base_url, quote(path)), timeout=5) as response:
                actual = hashlib.file_digest(response, "sha256").hexdigest()
        except HTTPError as error:
            error.close()
            raise ValueError(f"Candidate asset could not be fetched (HTTP {error.code}): {path}") from error
        if actual != expected:
            raise ValueError(f"Served asset differs from the candidate: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--python", default=sys.executable, help="Actual candidate service interpreter (may be shared)")
    args = parser.parse_args()
    manifest = validate_dist(args.root)
    manifest["python_environment"] = json.loads(subprocess.check_output([
        sys.executable, str(Path(__file__).with_name("python_environment.py")), "check",
        "--project", str(args.root), "--python", args.python,
    ], text=True))
    if args.base_url:
        validate_served(manifest, args.base_url)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
