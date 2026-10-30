"""Live-test homes keep selected login ownership and the same model catalog."""

import json

from server.harness import get_harness
from server.harness.events import HarnessCredential
from tests import conftest


def test_isolated_home_keeps_model_capabilities_without_sharing_catalog_writes(tmp_path):
    source, destination = tmp_path / 'selected', tmp_path / 'isolated'
    source.mkdir()
    destination.mkdir()
    (source / 'auth.json').write_text('synthetic-login-owner')
    (source / 'config.toml').write_text('model = "gpt-6.1-sol"\n')
    catalog = json.dumps({'models': [{'slug': 'gpt-6.1-sol', 'visibility': 'list',
                                    'supported_reasoning_levels': [{'effort': 'max'}]}]})
    (source / 'models_cache.json').write_text(catalog)
    conftest._seed_cli_home(source, destination, ('auth.json', 'config.toml', 'models_cache.json'))
    settings = get_harness('codex').model_settings(HarnessCredential(
        backend='codex', auth_type='oauth', home_dir=str(destination)))
    assert settings.efforts_for('gpt-6.1-sol') == ['max']
    assert (destination / 'auth.json').is_symlink()
    assert (destination / 'auth.json').resolve() == source / 'auth.json'
    assert (destination / 'config.toml').is_symlink()
    assert not (destination / 'models_cache.json').is_symlink()
    (destination / 'models_cache.json').write_text('{}')
    assert (source / 'models_cache.json').read_text() == catalog
