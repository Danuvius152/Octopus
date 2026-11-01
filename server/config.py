import os
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def startup_env_file() -> str:
    """The dotenv source selected when Settings loads in a new process."""
    return os.environ.get("OCTOPUS_ENV_FILE") or ".env"


class Settings(BaseSettings):
    # Setup proof only. An unconfigured installation keeps setup closed.
    auth_token: str = ""
    # Retain the saved display setting when strictly parsing existing dotenv
    # files. Business identity never reads it or treats it as an account.
    user_label: str = "octopus"
    # New agents use the install's preferred harness; saved agents and explicit
    # per-session choices retain their own backend.
    default_backend: Literal["claude-code", "codex"] = "claude-code"
    # The key that wraps every user's data key (multi-tenancy.md §4). Set
    # `OCTOPUS_MASTER_KEY` to hand it in from a secret manager; leave it empty
    # and the server creates one at `master_key_file` on first boot, which is
    # what a single box wants — a key nobody has to remember cannot be lost by
    # being forgotten.
    master_key: str = ""
    master_key_file: str = "~/.octopus/master.key"
    # One directory per account, holding that account's workspace, agent
    # memory, applications and uploads (multi-tenancy.md §6). The legacy
    # single-user directories above stay exactly as they are and keep serving
    # an install that has no accounts yet.
    users_root: str = "~/.octopus/users"
    # How many turns one account may have running at once (multi-tenancy.md
    # §8). A held `claude` is ~250 MB, and inline-steering.md §7 records the
    # OOM that a *single* user managed; with several, an unbounded box is a
    # question of when. 0 disables the cap, which is what a single-user
    # install has always effectively had.
    max_concurrent_turns_per_user: int = 4
    host: str = "0.0.0.0"
    port: int = 8000
    default_working_dir: str = "."
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8000"]
    db_path: str = "octopus.db"
    # Metrics live in their OWN file (polish-2026-09.md §9 G2). octopus.db is
    # already ~83 MB of product data with no retention policy; keeping metrics
    # apart means they prune on their own schedule, can be deleted outright
    # without touching session history, and a collection bug cannot corrupt
    # anything a user would miss.
    metrics_db_path: str = "octopus-metrics.db"
    # How long an event or gauge reading is kept.
    metrics_retention_days: int = 30

    # User-uploaded attachments live here, one subdir per session_id.
    # `~` is expanded at use time (not config load time) so tests that
    # override $HOME via monkeypatch see the override.
    attachments_dir: str = "~/.octopus/attachments"
    # Per-session spill directory for prompts too large to deliver as
    # positional argv (the kernel's MAX_ARG_STRLEN ceiling is ~128 KB).
    # When a prompt exceeds the threshold, Octopus writes it to a file
    # under this root and sends the backend a small pointer message
    # instructing the model to Read the file. See server/large_prompts.py.
    large_prompts_dir: str = "~/.octopus/large-prompts"
    # Per-credential CODEX_HOME root (codex-backend.md §7, option B). Each
    # in-app Codex login gets `<codex_home_dir>/<credential_id>/`, which holds
    # the `auth.json` Codex writes + manages. `~` expanded at use time.
    codex_home_dir: str = "~/.octopus/codex"
    # Ordinary requests use the verified App Server; Research owns its exec leaf.
    codex_app_server_binary: str = "codex"
    # A deep-research job's scratch cwd and its report. It was hardcoded to
    # `~/.octopus/research`, which put every test run's output into whichever
    # install happened to be deployed on the box, and — once accounts exist —
    # would have pooled every account's research in one directory no
    # confinement check covers. A setting for the pre-accounts install, and
    # `<users_root>/<id>/research` after (multi-tenancy.md §6).
    research_dir: str = "~/.octopus/research"
    # Rebuildable Wiki indexes, revisions and configuration. Vault originals
    # stay in the registered existing location, separate from this state.
    wiki_dir: str = "~/.octopus/wiki"
    wiki_reconcile_seconds: float = Field(default=30, ge=1)
    wiki_selection_page_size: int = Field(default=50, ge=1, le=200)
    wiki_selection_max_page_size: int = Field(default=200, ge=1, le=200)
    wiki_selection_max_candidates: int = Field(default=10000, ge=1)
    wiki_selection_ttl_seconds: int = Field(default=86400, ge=1)

    @model_validator(mode="after")
    def validate_wiki_selection_limits(self):
        if self.wiki_selection_page_size > self.wiki_selection_max_page_size:
            raise ValueError("Wiki selection default page size exceeds its maximum")
        return self
    # Per-agent durable state root (docs/implemented/memory.md §2). Each agent gets
    # `<agents_dir>/<agent_id>/memory/` — the canonical native, agent-written
    # markdown memory both harnesses point at (Claude via
    # CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its instructions blurb).
    # `~` expanded at use time.
    agents_dir: str = "~/.octopus/agents"
    # Agent-built web applications (docs/implemented/applications.md §2). Each app
    # gets `<applications_dir>/<slug>/` — a plain directory of static files
    # that the server streams back under /apps/{id}/. `~` expanded at use
    # time so tests (and the e2e suite) can point it at a temp root.
    applications_dir: str = "~/.octopus/applications"

    # Dev mode (enables uvicorn reload)
    debug: bool = False

    # Cloudflare Tunnel (opt-in)
    enable_tunnel: bool = False


    # If an AskUserQuestion goes unanswered for this long, the server
    # synthesizes an "act autonomously" reply so the session doesn't
    # wedge forever (matters most for scheduled-task sessions
    # where no human will ever see the prompt). 0 disables auto-answer.
    ask_user_question_timeout_seconds: int = 1800

    # Per-turn safety watchdog (turn-safety.md §3). A turn that emits no harness
    # event for `turn_idle_timeout_seconds`, or that runs longer than
    # `turn_max_seconds` overall, is stopped instead of hanging forever. Idle
    # stops can resume automatically within a bounded per-turn retry budget.
    # An optional overall cap includes all attempts; it is disabled by default
    # so long tasks with continuing progress aren't stopped just for their age.
    # 0 disables each timeout check.
    turn_idle_timeout_seconds: int = 300
    turn_max_seconds: int = 0
    turn_idle_max_retries: int = 2  # 0 disables automatic idle recovery

    # Native deep research (native-deep-research.md §6/§8). Max jobs running
    # concurrently across all sessions (per-job leaf fan-out is bounded inside
    # the pipeline); and a hard per-job wall-clock cap.
    research_max_concurrent_jobs: int = 2
    research_job_timeout_seconds: int = 1200

    # Connectors (connectors.md §7). The public base URL is what connector
    # OAuth redirect URIs are built against; behind a tunnel it must be set
    # to the stable public host. Unset → computed as http://127.0.0.1:{port}.
    # Per-kind client credentials gate availability in the catalog: a kind is
    # only installable once both its id and secret are present.
    public_base_url: str | None = None
    gmail_oauth_client_id: str | None = None
    gmail_oauth_client_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None

    @field_validator("public_base_url", "gmail_oauth_client_id", "gmail_oauth_client_secret",
                     "github_oauth_client_id", "github_oauth_client_secret", mode="before")
    @classmethod
    def empty_optional_configuration(cls, value: object) -> object:
        return None if value == "" else value

    model_config = {"env_prefix": "OCTOPUS_", "env_file": startup_env_file()}

    @property
    def resolved_public_base_url(self) -> str:
        """Base URL for OAuth redirect URIs — explicit config or localhost."""
        return self.public_base_url or f"http://127.0.0.1:{self.port}"


settings = Settings()
