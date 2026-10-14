#!/usr/bin/env python3
"""Validate a candidate's frontend before switching, and its served assets afterwards."""

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import urlopen


class EntryAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        reference = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" and attrs.get("rel") in {"stylesheet", "modulepreload"} else None
        if reference:
            self.references.append(reference)


def validate_dist(root: Path) -> dict:
    root = root.resolve()
    dist = root / "web/dist"
    entry = dist / "index.html"
    if not entry.is_file() or not entry.stat().st_size:
        raise ValueError("Candidate has no usable web/dist/index.html")
    parser = EntryAssets()
    parser.feed(entry.read_text(encoding="utf-8"))
    assets = {"/": hashlib.sha256(entry.read_bytes()).hexdigest()}
    scripts = 0
    for reference in parser.references:
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            continue
        relative = unquote(url.path).lstrip("/")
        asset = (dist / relative).resolve()
        if not asset.is_relative_to(dist.resolve()) or not asset.is_file() or not asset.stat().st_size:
            raise ValueError(f"Candidate entry asset is missing or unsafe: {reference}")
        assets["/" + relative] = hashlib.sha256(asset.read_bytes()).hexdigest()
        if asset.suffix in {".js", ".mjs"}:
            scripts += 1
    if not scripts:
        raise ValueError("Candidate has no local JavaScript entry asset")
    return {"root": str(root), "assets": assets}


def validate_served(manifest: dict, base_url: str) -> None:
    with urlopen(urljoin(base_url, "/health"), timeout=5) as response:
        if response.status != 200:
            raise ValueError("Candidate health check failed")
    for path, expected in manifest["assets"].items():
        with urlopen(urljoin(base_url, path), timeout=5) as response:
            actual = hashlib.sha256(response.read()).hexdigest()
        if actual != expected:
            raise ValueError(f"Served asset differs from the candidate: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--base-url")
    args = parser.parse_args()
    manifest = validate_dist(args.root)
    if args.base_url:
        validate_served(manifest, args.base_url)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
