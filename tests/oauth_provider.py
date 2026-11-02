"""The two provider results used by offline credential route tests."""

from server.oauth_errors import ScopeMissingError
from server.oauth_providers import OAuthTokenSet, get_provider


def patch_provider(monkeypatch, *, api_key="sk-ant-fresh-token-1234567890", tokens=None):
    async def exchange(**kwargs):
        return tokens or OAuthTokenSet("new-access", "refresh", 2000000000, ["user:inference"])

    async def mint(access_token):
        if api_key is None:
            raise ScopeMissingError("No organization", missing_scope="org:create_api_key")
        return api_key

    provider = get_provider("claude-code")
    monkeypatch.setattr(provider, "exchange_code", exchange)
    monkeypatch.setattr(provider, "mint_api_key", mint)
    return provider
