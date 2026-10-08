"""Upstream price sources and their parsers.

Parsers are pure functions over raw bytes so they can be tested against recorded
fixtures without any MCP or network machinery.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from lxml import html


class Commodity(StrEnum):
    COPPER = "copper"
    ZINC = "zinc"
    NICKEL = "nickel"
    ALUMINIUM = "aluminium"
    LEAD = "lead"
    TIN = "tin"
    LITHIUM_CARBONATE = "lithium_carbonate"


ALIASES: dict[str, Commodity] = {
    "cu": Commodity.COPPER,
    "铜": Commodity.COPPER,
    "zn": Commodity.ZINC,
    "锌": Commodity.ZINC,
    "ni": Commodity.NICKEL,
    "镍": Commodity.NICKEL,
    "al": Commodity.ALUMINIUM,
    "aluminum": Commodity.ALUMINIUM,
    "铝": Commodity.ALUMINIUM,
    "pb": Commodity.LEAD,
    "铅": Commodity.LEAD,
    "sn": Commodity.TIN,
    "锡": Commodity.TIN,
    "lithium": Commodity.LITHIUM_CARBONATE,
    "li": Commodity.LITHIUM_CARBONATE,
    "lc": Commodity.LITHIUM_CARBONATE,
    "锂": Commodity.LITHIUM_CARBONATE,
    "碳酸锂": Commodity.LITHIUM_CARBONATE,
}


class ParseError(ValueError):
    """Upstream page no longer has the structure the parser expects."""


@dataclass(frozen=True, slots=True)
class DailyPrice:
    day: date
    price: float
    extras: dict[str, float]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    url: str
    unit: str
    price_type: str
    parser: str


_LME_SYMBOL = {
    Commodity.COPPER: "Cu",
    Commodity.ZINC: "Zn",
    Commodity.NICKEL: "Ni",
    Commodity.ALUMINIUM: "Al",
    Commodity.LEAD: "Pb",
    Commodity.TIN: "Sn",
}

WESTMETALL_URL = "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_{symbol}_cash"
SINA_LC_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_LC0="
    "/InnerFuturesNewService.getDailyKLine?symbol=LC0"
)


def resolve_commodity(name: str) -> Commodity:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    if key in {c.value for c in Commodity}:
        return Commodity(key)
    if key in ALIASES:
        return ALIASES[key]
    supported = ", ".join(c.value for c in Commodity)
    raise KeyError(f"unknown commodity {name!r}; supported: {supported}")


def source_for(commodity: Commodity) -> SourceSpec:
    if commodity is Commodity.LITHIUM_CARBONATE:
        return SourceSpec(
            name="Sina Finance · GFEX lithium carbonate (LC0 continuous)",
            url=SINA_LC_URL,
            unit="CNY/t",
            price_type="GFEX daily settlement",
            parser="sina",
        )
    return SourceSpec(
        name="westmetall.com · LME official prices",
        url=WESTMETALL_URL.format(symbol=_LME_SYMBOL[commodity]),
        unit="USD/t",
        price_type="LME cash settlement",
        parser="westmetall",
    )


def parse(spec: SourceSpec, content: bytes) -> list[DailyPrice]:
    rows = parse_westmetall(content) if spec.parser == "westmetall" else parse_sina(content)
    return sorted(rows, key=lambda r: r.day)


def _number(text: str) -> float | None:
    cleaned = text.strip().replace(",", "")
    if not cleaned or cleaned in {"-", "n/a"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_westmetall(content: bytes) -> list[DailyPrice]:
    """Rows look like ``07. October 2026 | 14,510.00 | 14,422.00 | 239,875``."""
    tree = html.fromstring(content)
    rows: list[DailyPrice] = []
    for tr in tree.xpath("//table//tr[td]"):
        cells = [td.text_content().strip() for td in tr.xpath("./td")]
        if len(cells) < 2:
            continue
        try:
            day = datetime.strptime(cells[0], "%d. %B %Y").date()
        except ValueError:
            continue
        cash = _number(cells[1])
        if cash is None:
            continue
        extras: dict[str, float] = {}
        if len(cells) > 2 and (three_month := _number(cells[2])) is not None:
            extras["three_month"] = three_month
        if len(cells) > 3 and (stock := _number(cells[3])) is not None:
            extras["lme_stock_t"] = stock
        rows.append(DailyPrice(day=day, price=cash, extras=extras))
    if not rows:
        raise ParseError("westmetall: no dated price rows found in table")
    return rows


def parse_sina(content: bytes) -> list[DailyPrice]:
    """JSONP: ``var _LC0=([{"d":"2026-09-30","o":..,"h":..,"l":..,"c":..,"s":..}, ...]);``."""
    text = content.decode("utf-8", errors="replace")
    start, end = text.find("(["), text.rfind("])")
    if start < 0 or end < 0:
        raise ParseError("sina: JSONP array not found")
    try:
        records = json.loads(text[start + 1 : end + 1])
    except json.JSONDecodeError as exc:
        raise ParseError(f"sina: invalid JSON payload: {exc}") from exc
    rows: list[DailyPrice] = []
    for rec in records:
        settle = _number(str(rec.get("s", "")))
        close = _number(str(rec.get("c", "")))
        price = settle if settle else close
        if price is None:
            continue
        extras = {
            k: v
            for k, v in (
                ("open", _number(str(rec.get("o", "")))),
                ("high", _number(str(rec.get("h", "")))),
                ("low", _number(str(rec.get("l", "")))),
                ("close", close),
                ("volume", _number(str(rec.get("v", "")))),
                ("open_interest", _number(str(rec.get("p", "")))),
            )
            if v is not None
        }
        rows.append(DailyPrice(day=date.fromisoformat(rec["d"]), price=price, extras=extras))
    if not rows:
        raise ParseError("sina: payload contained no usable rows")
    return rows
