"""lme-price-mcp: parser tests over recorded fixtures + in-memory MCP contract tests."""

from collections.abc import AsyncIterator
from datetime import date

import pytest
from mcp import Client

from mining_brief.common.config import Settings
from mining_brief.common.http import DiskStore
from mining_brief.servers.price.server import build_server
from mining_brief.servers.price.sources import (
    Commodity,
    ParseError,
    parse,
    parse_sina,
    parse_westmetall,
    resolve_commodity,
    source_for,
)
from tests.conftest import REPO_FIXTURES

pytestmark = pytest.mark.anyio


def _fixture(commodity: Commodity) -> bytes:
    entry = DiskStore(REPO_FIXTURES / "http").read(source_for(commodity).url)
    assert entry is not None, f"missing fixture for {commodity}; run scripts/record_fixtures.py"
    return entry[1]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("copper", Commodity.COPPER),
        ("CU", Commodity.COPPER),
        ("锂", Commodity.LITHIUM_CARBONATE),
        ("lithium carbonate", Commodity.LITHIUM_CARBONATE),
        ("aluminum", Commodity.ALUMINIUM),
    ],
)
def test_resolve_commodity(name: str, expected: Commodity) -> None:
    assert resolve_commodity(name) is expected


def test_resolve_unknown_commodity_lists_supported() -> None:
    with pytest.raises(KeyError, match="supported: copper"):
        resolve_commodity("unobtanium")


@pytest.mark.parametrize(
    "commodity", [c for c in Commodity if c is not Commodity.LITHIUM_CARBONATE]
)
def test_westmetall_fixtures_parse(commodity: Commodity) -> None:
    rows = parse(source_for(commodity), _fixture(commodity))

    assert len(rows) > 100
    assert rows == sorted(rows, key=lambda r: r.day)
    assert len({r.day for r in rows}) == len(rows)
    assert all(r.price > 0 for r in rows)
    assert "three_month" in rows[-1].extras


def test_westmetall_metals_are_distinct_series() -> None:
    latest = {
        c: parse_westmetall(_fixture(c))[-1].price
        for c in Commodity
        if c.value != "lithium_carbonate"
    }
    assert len(set(latest.values())) == len(latest)


def test_sina_fixture_parses_settlement() -> None:
    rows = parse_sina(_fixture(Commodity.LITHIUM_CARBONATE))

    assert rows[0].day < date(2024, 1, 1) < rows[-1].day
    assert {"open", "high", "low", "close"} <= rows[-1].extras.keys()


def test_parsers_fail_loudly_on_unexpected_structure() -> None:
    with pytest.raises(ParseError):
        parse_westmetall(b"<html><table><tr><td>nothing</td></tr></table></html>")
    with pytest.raises(ParseError):
        parse_sina(b"var x=null;")


@pytest.fixture
async def client(offline_settings: Settings) -> AsyncIterator[Client]:
    async with Client(build_server(offline_settings)) as c:
        yield c


async def test_lists_required_tools(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}

    assert {"get_price", "get_trend"} <= tools.keys()
    assert tools["get_price"].output_schema is not None


async def test_get_price_latest(client: Client) -> None:
    result = await client.call_tool("get_price", {"commodity": "copper"})

    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["unit"] == "USD/t"
    assert data["price"] > 0
    assert data["provenance"]["data_mode"] == "fixture"


async def test_get_price_snaps_weekend_to_previous_trading_day(client: Client) -> None:
    # 2026-10-03 is a Saturday.
    result = await client.call_tool("get_price", {"commodity": "copper", "date": "2026-10-03"})

    data = result.structured_content
    assert data is not None
    assert data["trading_date"] == "2026-10-02"
    assert "not a trading day" in data["notes"][0]


async def test_get_trend_lithium(client: Client) -> None:
    result = await client.call_tool("get_trend", {"commodity": "碳酸锂", "days": 30})

    data = result.structured_content
    assert data is not None
    assert data["unit"] == "CNY/t"
    assert data["direction"] in {"up", "down", "flat"}
    assert data["low"] <= data["mean"] <= data["high"]
    assert len(data["points"]) >= 10


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("get_price", {"commodity": "unobtanium"}, "unknown commodity"),
        ("get_price", {"commodity": "copper", "date": "10/03/2026"}, "YYYY-MM-DD"),
        ("get_price", {"commodity": "copper", "date": "1990-01-01"}, "no copper price"),
        ("get_trend", {"commodity": "zinc", "days": 9999}, "between 2 and"),
    ],
)
async def test_bad_requests_are_tool_errors(
    client: Client, tool: str, args: dict[str, object], message: str
) -> None:
    result = await client.call_tool(tool, args)

    assert result.is_error
    assert result.structured_content is None
    assert message in str(result.content[0])
