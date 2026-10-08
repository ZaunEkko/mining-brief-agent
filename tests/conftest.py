from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mining_brief.common.config import Settings
from mining_brief.common.http import CachedHttp

REPO_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "demo"
TEST_DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: empty cache, the synthetic demo fixtures, LLM disabled."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        cache_dir=tmp_path / "cache",
        fixtures_dir=REPO_FIXTURES,
        llm_provider="none",
    )


@pytest.fixture
def offline_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"offline": True})


@pytest.fixture
async def http(settings: Settings) -> AsyncIterator[CachedHttp]:
    client = CachedHttp(settings)
    yield client
    await client.aclose()
