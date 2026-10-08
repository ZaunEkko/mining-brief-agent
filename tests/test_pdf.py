"""mineral-pdf-mcp: extractor unit tests, ground-truth accuracy and MCP contract tests."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import Client

from mining_brief.common.config import Settings
from mining_brief.servers.pdf.extract import detect_reporting_code, extract_tables, unit_base
from mining_brief.servers.pdf.server import build_server
from tests.conftest import TEST_DATA

pytestmark = pytest.mark.anyio

GROUND_TRUTH: list[dict[str, Any]] = json.loads(
    (TEST_DATA / "resource_ground_truth.json").read_text("utf-8")
)["reports"]
TOLERANCE = 0.05

# Excerpt typed from the 2025 Pilgangoora release, Table 1 (line structure kept verbatim).
TABLE_EXCERPT = """\
Table 1 - Pilgangoora Operation updated JORC Mineral Resource as at 31 March 2025
Category
Tonnes Li2O Ta2O5 Fe2O3 Li2O Ta2O5
(Mt) (%) (ppm) (%) (Mt) (M lb)
Measured 19 1.34 113 0.41 0.3 5
In-situ Indicated 349 1.29 121 0.54 4.5 93
(>=0.2% Li2O)
Inferred 70 1.25 134 0.58 0.9 21
Sub-Total 438 1.29 122 0.54 5.6 118
Measured 20 1.32 113 0.43 0.3 5
Indicated 356 1.29 120 0.60 4.6 94
Pilgangoora
Inferred 70 1.25 134 0.58 0.9 21
Total 446 1.28 122 0.59 5.7 120
"""


def _assert_close(actual: float | None, expected: float, field: str) -> None:
    assert actual is not None, f"{field} missing"
    assert abs(actual - expected) <= abs(expected) * TOLERANCE, f"{field}: {actual} vs {expected}"


def test_excerpt_headline_is_grand_total_section() -> None:
    (table,) = extract_tables([TABLE_EXCERPT])

    assert table.commodity == "Li2O"
    assert [s.label for s in table.sections] == ["In-situ", "Pilgangoora"]
    assert table.headline.label == "Pilgangoora"
    assert table.confidence == "high"
    indicated = next(r for r in table.headline.rows if r.category == "Indicated")
    assert (indicated.tonnage_mt, indicated.grade, indicated.contained_t) == (356, 1.29, 4_600_000)


def test_inconsistent_units_lower_confidence() -> None:
    # Contained column labelled Mt but holding tonnes: reconciliation must fail.
    broken = TABLE_EXCERPT.replace("4.6 94", "4600000 94")

    (table,) = extract_tables([broken])

    assert any(not c.passed for c in table.checks)
    assert table.confidence != "high"


def test_gold_table_in_ounces() -> None:
    text = (
        "Category Tonnes (Mt) Grade (g/t Au) Contained (Moz)\n"
        "Indicated 50.0 1.20 1.93\n"
        "Inferred 10.0 0.90 0.29\n"
        "Total 60.0 1.15 2.22\n"
    )
    (table,) = extract_tables([text])

    assert table.commodity == "Au"
    row = table.headline.rows[0]
    assert row.grade_unit == "g/t"
    assert row.contained_oz == pytest.approx(1.93e6)
    assert table.confidence == "high"


def test_page_without_table_yields_nothing() -> None:
    assert extract_tables(["Indicated and Inferred resources are discussed below."]) == []


def test_reporting_code_detection() -> None:
    assert detect_reporting_code("prepared under NI 43-101 by ...") == "NI 43-101"
    assert detect_reporting_code("classified per the JORC Code 2012") == "JORC"
    assert detect_reporting_code("no code") == "unknown"


@pytest.fixture
async def client(offline_settings: Settings) -> AsyncIterator[Client]:
    async with Client(build_server(offline_settings)) as c:
        yield c


async def _extract(client: Client, url: str) -> dict[str, Any]:
    result = await client.call_tool("extract_resources", {"pdf_url": url})
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def _check_against_truth(data: dict[str, Any], truth: dict[str, Any]) -> None:
    assert data["reporting_code"] == truth["reporting_code"]
    table = data["table"]
    assert table["page"] == truth["page"]
    assert table["commodity"] == truth["commodity"]
    assert table["confidence"] == truth.get("confidence", "high")
    assert data["abstain"] is False
    if truth["headline_section"]:
        assert table["headline"]["label"] == truth["headline_section"]
    rows = {r["category"]: r for r in table["headline"]["rows"]}
    for category, expected in truth["rows"].items():
        for field, value in expected.items():
            _assert_close(rows[category][field], value, f"{truth['id']}/{category}/{field}")


@pytest.mark.parametrize("truth", [t for t in GROUND_TRUTH if t["fixture"]], ids=lambda t: t["id"])
async def test_ground_truth_offline(client: Client, truth: dict[str, Any]) -> None:
    data = await _extract(client, truth["url"])

    _check_against_truth(data, truth)
    assert data["indicated"]["tonnage_mt"] == truth["rows"]["Indicated"]["tonnage_mt"]
    assert data["provenance"]["data_mode"] == "fixture"


@pytest.mark.live
@pytest.mark.parametrize("truth", GROUND_TRUTH, ids=lambda t: t["id"])
async def test_ground_truth_live(settings: Settings, truth: dict[str, Any]) -> None:
    async with Client(build_server(settings), read_timeout_seconds=600) as client:
        result = await client.call_tool(
            "extract_resources", {"pdf_url": truth["url"], "max_pages": 60}
        )
    assert not result.is_error, result.content
    assert result.structured_content is not None
    _check_against_truth(result.structured_content, truth)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://192.168.1.10/report.pdf", "non-public"),
        (
            "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash",
            "did not return a PDF",
        ),
        ("https://example.com/missing.pdf", "offline mode"),
    ],
)
async def test_errors_are_tool_errors(client: Client, url: str, message: str) -> None:
    result = await client.call_tool("extract_resources", {"pdf_url": url})

    assert result.is_error
    assert message in str(result.content[0])


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("kt Li O", ("tonnage", "kt")),
        ("% Li O", ("grade", "%")),
        ("Lî20%", ("grade", "%")),
        ("M", ("tonnage", "mt")),
        ("M lb", ("mass", "mlb")),
        ("g/t Au", ("grade", "g/t")),
        ("Moz", ("oz", "moz")),
        ("t", ("tonnage", "t")),
    ],
)
def test_unit_normalisation(unit: str, expected: tuple[str, str]) -> None:
    assert unit_base(unit) == expected


def test_repeated_categories_start_new_section_and_mi_is_checked() -> None:
    # Layout of the Allkem James Bay NI 43-101 summary table (two sections, no totals).
    text = """\
Table 1-1 - Summary of Mineral Resources
Inclusive of Mineral Reserves
Tonnage Grade Contained Metal
Category
(Mt) (% Li O) (kt Li O)
Measured - - -
Indicated 54.3 1.30 706
Total Measured + Indicated 54.3 1.30 706
Inferred 55.9 1.29 724
Exclusive of Mineral Reserves
Measured - - -
Indicated 18.1 1.12 204
Total Measured + Indicated 18.1 1.12 204
Inferred 55.9 1.29 724
"""

    (table,) = extract_tables([text])

    assert [s.label for s in table.sections] == [
        "Inclusive of Mineral Reserves",
        "Exclusive of Mineral Reserves",
    ]
    assert table.headline.label == "Inclusive of Mineral Reserves"
    assert table.commodity == "Li2O"
    assert any("M&I" in c.name and c.passed for c in table.checks)
    assert table.confidence == "high"


def test_table_without_contained_column_is_medium_not_abstained() -> None:
    # Layout of the Rio Tinto James Bay statement: Indicated only, tonnes, no contained.
    text = """\
Resource Category 2
(t) (%)
Indicated 40,330,000 1.40
"""

    (table,) = extract_tables([text])

    row = table.headline.rows[0]
    assert row.tonnage_mt == pytest.approx(40.33)
    assert row.contained is None
    assert row.contained_unit is None
    assert table.confidence == "medium"
    assert any("no contained metal" in n for n in table.notes)
