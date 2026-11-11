# Connectors — setup

Connectors let an agent call third-party accounts (GitHub, Gmail, or a compatible
OAuth2 API you define) as tools. Connector configuration is available **from the
browser**, without server shell or filesystem access. This assumes Octopus already
has a reachable public/callback address when the provider requires one; connector
setup does not configure the host's network or tunnel.

OAuth client registration and custom connector definitions are installation-wide
administrator operations. Each account authorizes its own installations and
chooses which of its agents may use them; a regular account cannot replace the
shared client credentials.

The one unavoidable step is registering an OAuth app with the provider to get a
**client id + secret** — but that's done in your browser too. (A hosted SaaS
hides this by pre-registering one shared app centrally; self-hosted, you're the
operator, so you register your own.)

## The flow, in the UI

1. Sidebar → **Connectors → +** opens the catalog.
2. A connector with no OAuth client shows **Set up** → paste its client id and
   secret (stored **encrypted** in the DB; no env edit, no restart). It then
   shows **Connect**.
3. **Connect** opens the provider's consent screen in a new tab; approve it and
   the installation appears in the sidebar.
4. Enable it per agent in **Agent settings → Connectors**. That agent gets the
   connector's tools on its next turn.

When an installation shows **needs reconnect**, use **Reconnect** on that
installation's card. Successful authorization updates the selected connection
and keeps its Agent enablement. Other same-kind Custom installations are
unchanged; **Connect** starts a separate Custom installation. If the selected
installation or its custom definition was deleted during login, that login is
refused. Inspect the current connections before starting again.

The **Set up** dialog shows the exact **Redirect URI** to register with the
provider. A nonempty `OCTOPUS_PUBLIC_BASE_URL` takes precedence even when you
open Octopus through an internal address. Otherwise the URI follows your request,
including the deployment's existing forwarded host/proto behavior for tunnels.
Copy the displayed URI; the login retains it through authorization and token
exchange even if the callback arrives through another address.

## GitHub

Provider-console steps and token/review policies in the GitHub and Gmail
sections are inherited observations whose original date was not recorded.
They were not revalidated during the 2026-11-02 documentation reorganization.
Check the provider's current console and policy before using them; the
redirect URI selection and per-agent enablement above describe Octopus.

In the Set up dialog, follow the steps (link goes to GitHub Developer settings):

1. GitHub → Settings → Developer settings → **OAuth Apps → New OAuth App**.
2. **Authorization callback URL** = the redirect URI shown in the dialog.
3. Register, then **Generate a new client secret**.
4. Paste the Client ID + secret into Set up → **Save** → **Connect**.

Octopus requests scopes `repo` and `read:org`. In the recorded GitHub setup,
classic OAuth-App tokens did not expire and needed no periodic refresh.

## Gmail (Google Cloud)

Gmail is fussier — three things trip people up, in order:

1. **Enable the Gmail API** for your project:
   `console.cloud.google.com/apis/library/gmail.googleapis.com` → **Enable**.
   *Without this, sign-in succeeds but the profile lookup fails with `403`.*
2. **Add yourself as a Test user**: Google Auth Platform → **Audience**
   (`console.cloud.google.com/auth/audience`) → **Test users → Add users** →
   your Gmail address. *While the app is in "Testing", anyone not listed is
   blocked with `Error 403: access_denied`.*
3. **Create the OAuth client**: APIs & Services → Credentials → Create
   credentials → **OAuth client ID → Web application** → add the redirect URI
   under **Authorized redirect URIs**. Paste the Client ID + secret into Set up.

Octopus requests scope `gmail.modify` (read, label, draft, send). In the recorded
setup, Google's "Testing" status caused refresh-token expiry after about 7 days;
publishing and verification affected that policy. Treat those as historical
observations, not a current expiry or review guarantee. If Octopus shows
**needs reconnect**, reconnect to re-authorize. Transient refresh failures do
not by themselves invalidate the grant; see
[token failure classification](implemented/connectors.md#token-failures-and-delayed-rejection).

<a id="custom-connectors-any-oauth2-api"></a>
## Custom connectors (compatible OAuth2 APIs)

An administrator can use Catalog → **Add custom connector** to define a new
kind without server code. The generic adapter supports authorization-code OAuth2,
optional PKCE, form-encoded token requests and JSON token responses; providers
requiring a different protocol or token-authentication scheme may need a dedicated
adapter rather than configuration alone:

- **Slug** (e.g. `linear`) and **display name**.
- **Authorize URL**, **Token URL**, **API base URL**, **scopes**, **PKCE** on/off.
- **Client ID + secret** from the OAuth app you register with that provider
  (callback = the redirect URI shown in the form).

The agent gets one tool, `mcp__<kind>_<id>__request(method, path, query?,
body?)`, that calls `{api_base}{path}` with the stored token attached. Remove a
custom connector with the **×** in the catalog as an administrator. This deletes
the shared definition and **all accounts'** installations, stored credentials and
Agent enablement links for that kind; it is not an account-local disconnect.

## Env-var fallback (operators with server access)

If you run the server yourself, you can pre-seed client creds via env instead
of the Set up dialog (the in-app config takes precedence when both exist):

```bash
OCTOPUS_GITHUB_OAUTH_CLIENT_ID=...     OCTOPUS_GITHUB_OAUTH_CLIENT_SECRET=...
OCTOPUS_GMAIL_OAUTH_CLIENT_ID=...      OCTOPUS_GMAIL_OAUTH_CLIENT_SECRET=...
# Only needed if the request-derived redirect URI is ever wrong for your setup:
OCTOPUS_PUBLIC_BASE_URL=https://your-stable-host
```

The public base accepts an absolute HTTP(S) URL, including a port, IPv6 host or
existing proxy path prefix. Settings trims surrounding whitespace, canonicalizes
the URL and removes trailing slashes; an empty or whitespace-only value leaves
request-derived behavior active. Credentials, query, fragment and malformed
nonempty values such as `/` are configuration errors. A prefix must match your
actual proxy routing. Use the URI shown by Set up after configuring the base;
the complete selection and login-capture rules are in the
[public OAuth base contract](implemented/connectors.md#public-oauth-base).

OAuth client configuration is installation-wide: its client secret is encrypted
with the installation master key. An account's installation access/refresh
tokens are encrypted with that account's DEK, itself wrapped by the master key.
The setup proof `OCTOPUS_AUTH_TOKEN` is not a current connector-token key; the
legacy installation-token business path has retired. Tools resolve tokens under
the calling account and agent scope. The retained legacy diagnostic transport
does not grant unscoped access to stored installation secrets. See the
[connector contract](implemented/connectors.md) and
[account trust boundary](implemented/multi-tenancy.md).
