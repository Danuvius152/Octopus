"""The candidate must contain frontend assets before real app construction."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_assets", ROOT / "scripts/validate-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_clean_candidate_is_checked_before_switch_and_serves_its_own_assets(tmp_path):
    with pytest.raises(ValueError, match="no usable"):
        release.validate_dist(tmp_path)
    dist = tmp_path / "web/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<html><script type="module" src="/assets/main.js"></script><link rel="stylesheet" href="/assets/main.css"></html>')
    with pytest.raises(ValueError, match="missing"):
        release.validate_dist(tmp_path)
    (dist / "assets/main.js").write_text("window.candidate = true;")
    (dist / "assets/main.css").write_text("body { color: black; }")
    manifest = release.validate_dist(tmp_path)
    shutil.copytree(ROOT / "server", tmp_path / "server", ignore=shutil.ignore_patterns("__pycache__"))
    code = '''import asyncio,hashlib,json,pathlib,httpx
from server.main import app
import server.main
async def probe():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://candidate") as client:
        assert (await client.get("/health")).status_code == 200
        assets = {}
        for path in ["/","/assets/main.js","/assets/main.css"]:
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
