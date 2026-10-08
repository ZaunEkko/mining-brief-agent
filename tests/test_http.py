import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from mining_brief.common.config import Settings
from mining_brief.common.http import CachedHttp, DiskStore, FetchError, record_fixture
from mining_brief.common.models import DataMode

URL = "https://example.test/data"
TTL = timedelta(hours=1)

pytestmark = pytest.mark.anyio


@respx.mock
async def test_live_then_fresh_cache(http: CachedHttp) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="hello"))

    first = await http.get(URL, ttl=TTL)
    second = await http.get(URL, ttl=TTL)

    assert first.data_mode is DataMode.LIVE
    assert second.data_mode is DataMode.CACHE
    assert second.text == "hello"
    assert route.call_count == 1


@respx.mock
async def test_expired_cache_is_refreshed(http: CachedHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="v1"))
    await http.get(URL, ttl=TTL)
    respx.get(URL).mock(return_value=httpx.Response(200, text="v2"))

    result = await http.get(URL, ttl=timedelta(0))

    assert result.data_mode is DataMode.LIVE
    assert result.text == "v2"


@respx.mock
async def test_upstream_failure_falls_back_to_stale_cache(http: CachedHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="old"))
    await http.get(URL, ttl=TTL)
    respx.get(URL).mock(return_value=httpx.Response(503))

    result = await http.get(URL, ttl=timedelta(0))

    assert result.data_mode is DataMode.STALE_CACHE
    assert result.text == "old"


@respx.mock
async def test_upstream_failure_falls_back_to_fixture(tmp_path: Path, settings: Settings) -> None:
    fixtures = tmp_path / "fixtures"
    DiskStore(fixtures / "http").write(URL, b"recorded", "text/plain")
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    http = CachedHttp(settings.model_copy(update={"fixtures_dir": fixtures}))

    result = await http.get(URL, ttl=TTL)

    assert result.data_mode is DataMode.FIXTURE
    assert result.text == "recorded"
    await http.aclose()


async def test_offline_never_touches_network(tmp_path: Path, offline_settings: Settings) -> None:
    http = CachedHttp(offline_settings.model_copy(update={"fixtures_dir": tmp_path}))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(URL)
        with pytest.raises(FetchError, match="offline mode"):
            await http.get(URL, ttl=TTL)
        assert not route.called
    await http.aclose()


@respx.mock
async def test_record_fixture_writes_manifest(tmp_path: Path, http: CachedHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="x"))
    result = await http.get(URL, ttl=TTL)

    record_fixture(tmp_path, result)

    manifest = json.loads((tmp_path / "MANIFEST.json").read_text("utf-8"))
    assert [entry["url"] for entry in manifest.values()] == [URL]
    assert DiskStore(tmp_path / "http").read(URL) is not None


@respx.mock
async def test_record_fixture_rejects_non_live(tmp_path: Path, http: CachedHttp) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="x"))
    await http.get(URL, ttl=TTL)
    cached = await http.get(URL, ttl=TTL)

    with pytest.raises(ValueError, match="non-live"):
        record_fixture(tmp_path, cached)


@respx.mock
async def test_transient_status_is_retried_once(
    http: CachedHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mining_brief.common.http.RETRY_BACKOFF_S", 0)
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, text="recovered")]
    )

    result = await http.get(URL, ttl=TTL)

    assert result.text == "recovered"
    assert route.call_count == 2
