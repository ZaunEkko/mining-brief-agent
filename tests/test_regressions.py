"""Regression tests for previously fixed defects."""

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from mining_brief.agent.ask import unverified_urls
from mining_brief.agent.compose import merge_quote
from mining_brief.agent.events import Event
from mining_brief.agent.orchestrator import generate_brief
from mining_brief.agent.toolbox import ServerName, Target, Toolbox
from mining_brief.common.config import Settings
from mining_brief.common.http import CachedHttp, FetchError, ResponseTooLarge
from mining_brief.common.urls import UnsafeUrlError
from mining_brief.servers.news.server import build_server as news_server
from mining_brief.servers.news.sources import canonical_url
from mining_brief.servers.pdf.extract import extract_tables
from mining_brief.servers.pdf.server import build_server as pdf_server
from mining_brief.servers.price.server import build_server as price_server
from mining_brief.web.app import _stream

pytestmark = pytest.mark.anyio
TTL = timedelta(hours=1)


async def _public(host: str) -> list[str]:
    return ["93.184.216.34"]


async def _private(host: str) -> list[str]:
    return ["10.0.0.7"]


# --- SSRF: redirects and resolved addresses ------------------------------


@respx.mock
async def test_redirect_to_private_address_is_refused(settings: Settings) -> None:
    respx.get("https://public.example/a").mock(
        return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1:9/secret"})
    )
    secret = respx.get("http://127.0.0.1:9/secret").mock(return_value=httpx.Response(200, text="s"))
    http = CachedHttp(settings, resolver=_public)

    with pytest.raises(UnsafeUrlError, match="non-public"):
        await http.get("https://public.example/a", ttl=TTL, public_only=True)
    assert not secret.called
    await http.aclose()


@respx.mock
async def test_hostname_resolving_to_private_ip_is_refused(settings: Settings) -> None:
    route = respx.get("https://internal.example/x").mock(return_value=httpx.Response(200))
    http = CachedHttp(settings, resolver=_private)

    with pytest.raises(UnsafeUrlError, match="resolves to non-public"):
        await http.get("https://internal.example/x", ttl=TTL, public_only=True)
    assert not route.called
    await http.aclose()


@respx.mock
async def test_public_redirect_chain_still_works(settings: Settings) -> None:
    respx.get("https://public.example/a").mock(
        return_value=httpx.Response(301, headers={"Location": "/b"})
    )
    respx.get("https://public.example/b").mock(return_value=httpx.Response(200, text="ok"))
    http = CachedHttp(settings, resolver=_public)

    result = await http.get("https://public.example/a", ttl=TTL, public_only=True)

    assert result.text == "ok"
    await http.aclose()


async def test_proxy_fake_ip_range_is_allowed_by_default(settings: Settings) -> None:
    async def fake_ip(host: str) -> list[str]:
        return ["198.18.2.209"]  # Clash / mihomo TUN fake-ip

    http = CachedHttp(settings, resolver=fake_ip)
    await http.check_public("https://www.mining.com/x")  # must not raise
    await http.aclose()


# --- response size limit ------------------------------------------------


@respx.mock
async def test_oversized_response_is_rejected_while_streaming(settings: Settings) -> None:
    respx.get("https://public.example/big.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF" + b"0" * 5000)
    )
    http = CachedHttp(settings, resolver=_public)

    with pytest.raises(ResponseTooLarge):
        await http.get("https://public.example/big.pdf", ttl=TTL, max_bytes=1000)
    assert not list((settings.cache_dir / "http").glob("*.bin")), "nothing cached"
    await http.aclose()


def test_response_too_large_is_a_fetch_error() -> None:
    assert issubclass(ResponseTooLarge, FetchError)


# --- resource extraction -------------------------------


def test_each_table_on_a_page_uses_its_own_header() -> None:
    page = """\
Table 1 - Deposit A
Category Tonnes (Mt) Grade (%) Contained (kt)
Indicated 10.0 1.00 100
Inferred 5.0 1.00 50
Table 2 - Deposit B
Category Tonnes (kt) Grade (%) Contained (t)
Indicated 100 1.00 1000
Inferred 50 1.00 500
"""
    tables = extract_tables([page])

    assert [t.caption for t in tables] == ["Table 1 - Deposit A", "Table 2 - Deposit B"]
    b = next(r for r in tables[1].headline.rows if r.category == "Indicated")
    assert b.tonnage_mt == pytest.approx(0.1)
    assert b.contained_t == pytest.approx(1000)


def test_total_tolerance_respects_tonne_units() -> None:
    page = """\
(t) (%) (t)
Indicated 100,000 1.00 1,000
Inferred 100,000 1.00 1,000
Total 2,000,000 1.00 2,000
"""
    (table,) = extract_tables([page])

    total_check = next(c for c in table.checks if "sum to total" in c.name)
    assert not total_check.passed
    assert table.confidence != "high"


def test_failed_check_in_headline_section_forces_abstain_level() -> None:
    page = """\
Table 1
Category Tonnes (Mt) Grade (%) Contained (Mt)
Measured 20 1.32 0.3
Indicated 356 1.29 4600000
Inferred 70 1.25 0.9
Total 446 1.28 5.7
"""
    (table,) = extract_tables([page])

    assert any(not c.passed for c in table.checks)
    assert table.confidence == "low"


# --- quote vs trend --------------------------------------------------------


def test_newer_quote_does_not_rewrite_trend_statistics() -> None:
    trend = {"last": 100.0, "end_date": "2026-10-01", "change_pct": 0.0, "high": 100.0, "notes": []}
    quote = {"price": 200.0, "trading_date": "2026-10-02", "unit": "USD/t"}

    merged = merge_quote(trend, quote)

    assert merged["last"] == 100.0
    assert merged["end_date"] == "2026-10-01"
    assert any("200" in n and "2026-10-02" in n for n in merged["notes"])


def test_same_day_quote_is_a_no_op() -> None:
    trend = {"last": 100.0, "end_date": "2026-10-01", "notes": []}
    quote = {"price": 100.0, "trading_date": "2026-10-01", "unit": "USD/t"}

    assert merge_quote(trend, quote) == trend


# --- URL verification ----------------------------------------------------------


def test_url_must_match_a_successful_result_exactly() -> None:
    # Only successful results are evidence; ask() never passes error messages in.
    ok = ['{"source_url": "https://publisher.example/report-2025.pdf"}']
    answer = (
        "[a](https://publisher.example/report-2025.pdf) "
        "[b](https://publisher.example/report-2025) "
        "[c](https://echo.example/only-in-error)"
    )

    assert unverified_urls(answer, ok) == [
        "https://echo.example/only-in-error",
        "https://publisher.example/report-2025",
    ]


# --- per-server isolation in discovery ----------------------------------------


async def test_one_failing_list_tools_does_not_hide_other_servers(
    offline_settings: Settings,
) -> None:
    targets: dict[ServerName, Target] = {
        "news": news_server(offline_settings),
        "pdf": pdf_server(offline_settings),
        "price": price_server(offline_settings),
    }
    async with Toolbox(targets, 30) as tb:

        async def broken() -> Any:
            raise ConnectionError("server went away")

        tb._clients["news"].list_tools = broken  # type: ignore[method-assign]

        specs = await tb.tool_specs()
        described = await tb.describe()

    assert {s.name.split("__")[0] for s in specs} == {"pdf", "price"}
    news = next(d for d in described if d["server"] == "news")
    assert news["tools"] == []
    assert "server went away" in news["error"]


# --- wrong-shape LLM JSON ------------------------------------------------------


class _ShapeLLM:
    name = "shape:test"

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"overview": "", "news": None, "risks": []}

    def tool_session(self, system: str, tools: Any) -> Any:
        raise NotImplementedError


async def test_wrong_shape_llm_output_degrades(offline_settings: Settings) -> None:
    targets: dict[ServerName, Target] = {
        "news": news_server(offline_settings),
        "pdf": pdf_server(offline_settings),
        "price": price_server(offline_settings),
    }

    brief = await generate_brief("Pilbara 锂矿简报", targets, llm=_ShapeLLM())

    assert "LLM 调用失败，已降级" in brief.markdown
    assert "deterministic (LLM failed" in brief.markdown


# --- canonical URLs ---------------------------------------------------------


def test_meaningful_query_parameters_are_kept() -> None:
    assert canonical_url("https://p.example/?p=101") != canonical_url("https://p.example/?p=102")
    assert canonical_url("https://p.example/a/?utm_source=rss&id=7") == canonical_url(
        "https://P.example/a?id=7"
    )


# --- SSE producer cleanup ------------------------------------------------------


async def test_closing_the_stream_awaits_producer_cleanup() -> None:
    cleaned: list[bool] = []

    async def run(sink: Any) -> dict[str, Any]:
        await sink(Event("plan", {}))
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.append(True)
        return {}

    stream = _stream(run)
    first = await anext(stream)
    await stream.aclose()

    assert first.startswith("event: plan")
    assert cleaned == [True]
