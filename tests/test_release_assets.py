"""The candidate must contain frontend assets before real app construction."""

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from importlib import metadata
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_assets", ROOT / "scripts/validate-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("reference", [
    pytest.param('<link rel="modulepreload" href="/assets/main.js">', id="modulepreload"),
    pytest.param('<script type="application/json" src="/assets/main.js"></script>', id="data-script"),
    pytest.param('<link rel="stylesheet" href="/assets/main.js">', id="stylesheet"),
    pytest.param('<script nomodule src="/assets/main.js"></script>', id="legacy-fallback"),
    pytest.param('<script type="module" src="https://example.invalid/main.js"></script>', id="remote-module"),
    pytest.param(
        '<template><template></template><script type="module" src="/assets/main.js"></script></template>',
        id="nested-template",
    ),
    pytest.param('<noscript><script type="module" src="/assets/main.js"></script></noscript>', id="noscript"),
    pytest.param(
        '<base href="//example.invalid/"><script type="module" src="/assets/main.js"></script>',
        id="external-document-base",
    ),
    pytest.param(
        '<base href="///example.invalid/"><script type="module" src="/assets/main.js"></script>',
        id="multiple-slash-document-base",
    ),
    pytest.param(
        r'<base href="\\example.invalid/"><script type="module" src="/assets/main.js"></script>',
        id="backslash-document-base",
    ),
    pytest.param(
        '<template/><script type="module" src="/assets/main.js"></script>',
        id="template-with-trailing-slash",
    ),
])
def test_candidate_requires_a_local_executable_script(tmp_path, reference):
    dist = tmp_path / "web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(reference)
    (dist / "assets/main.js").write_text("window.candidate = true;")

    with pytest.raises(ValueError, match="no local executable JavaScript entry"):
        release.validate_dist(tmp_path)


@pytest.mark.parametrize(("base", "asset"), [
    ("/assets/", "assets/main.js"),
    ("/%2e%2e/", "main.js"),
])
def test_candidate_resolves_the_entry_against_its_local_document_base(tmp_path, base, asset):
    dist = tmp_path / "web/dist"
    script = dist / asset
    script.parent.mkdir(parents=True)
    (dist / "index.html").write_text(
        f'<base href="{base}">'
        '<script type="module" src="main.js"></script>'
    )
    script.write_text("window.candidate = true;")

    assert release.validate_dist(tmp_path)["assets"].keys() == {"/", f"/{asset}"}


def test_candidate_rejects_a_document_base_after_an_asset_reference(tmp_path):
    dist = tmp_path / "web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="main.js"></script>'
        '<base href="/assets/">'
    )
    (dist / "assets/main.js").write_text("window.candidate = true;")

    with pytest.raises(ValueError, match="<base href> must precede the candidate asset references"):
        release.validate_dist(tmp_path)


def test_candidate_rejects_a_directory_asset_alias_before_and_after_retargeting(tmp_path):
    dist = tmp_path / "web/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text('<script type="module" src="/main.js"></script>')
    (dist / "main.js").write_text("window.candidate = true;")
    alias = dist / "alias"
    for version in ("v1", "v2"):
        target = dist / version
        target.mkdir()
        (target / "lazy.js").write_text(f"export const version = '{version}';")
        alias.symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError, match="directory symlink: /alias"):
            release.validate_dist(tmp_path)
        alias.unlink()


def test_clean_candidate_is_checked_before_switch_and_serves_its_own_assets(tmp_path):
    with pytest.raises(ValueError, match="no usable"):
        release.validate_dist(tmp_path)
    dist = tmp_path / "web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="/assets/main.js"></script>'
        '<link rel="stylesheet" href="/assets/main.css">'
        '<link rel="modulepreload" href="/assets/shared.js">'
    )
    required = {
        "assets/main.js": "window.candidate = true;",
        "assets/main.css": "body { color: black; }",
        "assets/shared.js": "export const shared = true;",
    }
    for path, content in required.items():
        with pytest.raises(ValueError, match=f"missing or unsafe: /{path}"):
            release.validate_dist(tmp_path)
        (dist / path).write_text(content)
    (dist / "assets/lazy.js").write_text("export const lazy = true;")
    manifest = release.validate_dist(tmp_path)
    shutil.copytree(ROOT / "server", tmp_path / "server", ignore=shutil.ignore_patterns("__pycache__"))
    code = '''import asyncio,hashlib,json,pathlib,httpx
from server.main import app
import server.main
async def probe():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://candidate") as client:
        assert (await client.get("/health")).status_code == 200
        assets = {}
        for path in ["/","/assets/main.js","/assets/main.css","/assets/shared.js","/assets/lazy.js"]:
            response = await client.get(path)
            assert response.status_code == 200, (path,response.status_code)
            assets[path] = hashlib.sha256(response.content).hexdigest()
        print(json.dumps({"module":server.main.__file__,"assets":assets}))
asyncio.run(probe())'''
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["module"]).is_relative_to(tmp_path)
    assert observed["assets"] == manifest["assets"]


def test_served_verifier_matches_escaped_asset_paths_and_rejects_changes(tmp_path, monkeypatch):
    dist = tmp_path / "web/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text('<script type="module" src="/main.js"></script>')
    (dist / "main.js").write_text("window.candidate = true;")
    name = "延迟 chunk#?%.js"
    path = f"/{name}"
    (dist / name).write_text("export const lazy = true;")
    manifest = release.validate_dist(tmp_path)
    assert path in manifest["assets"]

    served = tmp_path / "served"
    shutil.copytree(dist, served)
    (served / "health").write_text('{"status":"ok"}')
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    handler = partial(SimpleHTTPRequestHandler, directory=served)
    asset = served / name
    with HTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            release.validate_served(manifest, base_url)
            asset.write_text("export const lazy = false;")
            with pytest.raises(ValueError) as changed:
                release.validate_served(manifest, base_url)
            assert str(changed.value) == f"Served asset differs from the candidate: {path}"

            asset.unlink()
            with pytest.raises(ValueError) as missing:
                release.validate_served(manifest, base_url)
            assert str(missing.value) == f"Candidate asset could not be fetched (HTTP 404): {path}"
        finally:
            server.shutdown()
            thread.join()


def test_candidate_preparation_rejects_the_actual_interpreters_dependency_drift(tmp_path):
    dist = tmp_path / "web/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/main.js"></script>')
    (dist / "main.js").write_text("window.candidate = true;")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="octopus"\nrequires-python=">=3.12"\ndependencies=["packaging>=24"]\n')
    environment = {"implementation_name": sys.implementation.name, "python_full_version": platform.python_version(),
                   "python_version": ".".join(platform.python_version_tuple()[:2]), "sys_platform": sys.platform,
                   "platform_machine": platform.machine()}
    pins = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in metadata.distributions()
                  if dist.metadata["Name"].lower() not in {"octopus", "packaging"})
    (tmp_path / "constraints-python.txt").write_text("# environment: " + json.dumps(environment) + "\n" + "\n".join(pins) + "\npackaging==0\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/validate-release.py"), str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0, "assets alone must not approve an incompatible runtime"
    assert "packaging" in result.stderr
