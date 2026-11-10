import os

import pytest


def test_settings_defaults():
    """The declared defaults are what we think they are.

    Instantiated in isolation — `_env_file=None` *and* a cleared environment —
    because `Settings` otherwise reads the developer's own `.env`. A test of
    **defaults** that reads local configuration asserts whatever that machine
    happens to be set to, and quietly means nothing on a machine that sets
    those keys. It passed for years only because no `.env` here set them; a
    worktree that redirects its port and paths to keep away from a deployed
    install makes it fail with nothing actually wrong.
    """
    from server.config import Settings

    cleared = {
        key: os.environ.pop(key)
        for key in list(os.environ)
        if key.startswith("OCTOPUS_")
    }
    try:
        s = Settings(_env_file=None, auth_token="test-token")
        assert s.auth_token == "test-token"
        assert s.host == "0.0.0.0"
        assert s.port == 8000
        assert s.default_working_dir == "."
        assert s.default_backend == "claude-code"
        assert "http://localhost:5173" in s.cors_origins
        # Paths too: these are what the deployed install runs on when its own
        # `.env` says nothing, so a change to them is a change to production.
        assert s.db_path == "octopus.db"
        assert s.agents_dir == "~/.octopus/agents"
        assert s.turn_max_seconds == 0
        assert s.turn_idle_max_retries == 2
    finally:
        os.environ.update(cleared)


def test_settings_from_env(monkeypatch):
    """Config reads from environment variables."""
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "my-secret")
    monkeypatch.setenv("OCTOPUS_PORT", "9000")
    monkeypatch.setenv("OCTOPUS_DEFAULT_BACKEND", "codex")
    monkeypatch.setenv("OCTOPUS_TURN_IDLE_MAX_RETRIES", "0")

    from server.config import Settings

    s = Settings()
    assert s.auth_token == "my-secret"
    assert s.port == 9000
    assert s.default_backend == "codex"
    assert s.turn_idle_max_retries == 0


def test_default_backend_rejects_an_unknown_harness():
    from pydantic import ValidationError

    from server.config import Settings

    with pytest.raises(ValidationError, match="default_backend"):
        Settings(_env_file=None, default_backend="unavailable")


@pytest.mark.parametrize(("configured", "expected"), [
    (None, None),
    ("", None),
    (" \t\n ", None),
    ("https://public.test/", "https://public.test"),
    ("  HTTPS://PUBLIC.test:443///  ", "https://public.test"),
    ("http://public.test:80", "http://public.test"),
    ("https://public.test:8443/octopus///", "https://public.test:8443/octopus"),
    ("http://[::1]:9000/octopus/", "http://[::1]:9000/octopus"),
])
def test_public_base_configuration_is_normalized_independently_of_client_credentials(configured, expected):
    from server.config import Settings

    settings = Settings(_env_file=None, public_base_url=configured,
                        github_oauth_client_id=" cid ", github_oauth_client_secret=" secret ")
    assert settings.public_base_url == expected
    assert settings.github_oauth_client_id == " cid "
    assert settings.github_oauth_client_secret == " secret "


@pytest.mark.parametrize("configured", [
    "/", "///", "not-a-url", "//public.test", "ftp://public.test", "https://",
    "https://user:password@public.test", "https://user@public.test",
    "https://public.test?next=elsewhere", "https://public.test?",
    "https://public.test/#part", "https://public.test/#", "https://public.test:99999",
])
def test_public_base_refuses_nonempty_configuration_that_cannot_form_a_callback(configured):
    from pydantic import ValidationError

    from server.config import Settings

    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(_env_file=None, public_base_url=configured)


def test_public_base_environment_uses_the_same_url_normalization(monkeypatch):
    from server.config import Settings

    monkeypatch.setenv("OCTOPUS_PUBLIC_BASE_URL", " HTTPS://PUBLIC.test:443/octopus/// ")
    assert Settings(_env_file=None).public_base_url == "https://public.test/octopus"
