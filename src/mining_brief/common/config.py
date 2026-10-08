"""Single source of configuration. Business code must not read ``os.environ`` directly."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]

LLMProvider = Literal["auto", "anthropic", "openai", "none"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MB_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    offline: bool = False
    cache_dir: Path = _REPO_ROOT / ".cache"
    # Synthetic offline dataset shipped with the repo; real recordings stay
    # in the local cache and are never committed.
    fixtures_dir: Path = _REPO_ROOT / "fixtures" / "demo"
    http_timeout_s: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 mining-brief-agent/0.1"
    )
    # Host headers accepted by HTTP-transport servers (DNS-rebinding protection).
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "news:*", "pdf:*", "price:*"]
    )

    # Agent -> server wiring: an http(s) URL uses streamable HTTP, unset spawns stdio.
    news_url: str | None = None
    pdf_url: str | None = None
    price_url: str | None = None
    tool_timeout_s: float = 180.0
    timezone: str = "Asia/Shanghai"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_dist: Path = _REPO_ROOT / "web" / "dist"

    # Non-public ranges a fetch may still resolve to. 198.18.0.0/15 is where TUN-mode
    # proxies (Clash/mihomo fake-ip) map every hostname; blocking it would block all.
    ssrf_allowed_networks: list[str] = Field(default_factory=lambda: ["198.18.0.0/15"])

    llm_provider: LLMProvider = "auto"
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "MB_ANTHROPIC_API_KEY")
    )
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Server-side refusal fallback (beta). Disable for gateways that reject unknown params.
    anthropic_server_fallback: bool = True
    openai_base_url: str | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None

    def server_env(self) -> dict[str, str]:
        """Environment for stdio server subprocesses.

        The MCP SDK only forwards a minimal allow-list of variables to subprocesses,
        so data-path settings and proxy variables are passed explicitly.
        """
        env = {
            "MB_OFFLINE": "1" if self.offline else "0",
            "MB_CACHE_DIR": str(self.cache_dir),
            "MB_FIXTURES_DIR": str(self.fixtures_dir),
        }
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if value := os.environ.get(key):
                env[key] = value
        return env


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
