"""Shared temporary installation preparation; no resources at import time."""

import json


def installation_settings(tmp_path):
    from server.config import Settings

    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = {
        "db_path": str(tmp_path / "main.db"),
        "metrics_db_path": str(tmp_path / "metrics.db"),
        "master_key_file": str(tmp_path / "master.key"),
        "users_root": str(tmp_path / "users"),
        "attachments_dir": str(tmp_path / "attachments"),
        "large_prompts_dir": str(tmp_path / "prompts"),
        "codex_home_dir": str(tmp_path / "codex"),
        "research_dir": str(tmp_path / "research"),
        "wiki_dir": str(tmp_path / "wiki"),
        "agents_dir": str(tmp_path / "agents"),
        "applications_dir": str(tmp_path / "apps"),
        "default_working_dir": str(tmp_path / "workspace"),
        "host": "127.0.0.1", "port": 43211, "enable_tunnel": False, "debug": False,
    }
    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps(paths))
    return Settings(_env_file=None, **paths), config_file
