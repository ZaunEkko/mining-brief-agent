"""Price lookups over the parsed daily series."""

from datetime import date, timedelta
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, Field

from mining_brief.common.http import CachedHttp
from mining_brief.common.models import Provenance
from mining_brief.servers.price.sources import (
    Commodity,
    DailyPrice,
    SourceSpec,
    parse,
    resolve_commodity,
    source_for,
)

PRICE_TTL = timedelta(hours=6)
MAX_TREND_DAYS = 365
FLAT_THRESHOLD_PCT = 1.0


class PriceLookupError(Exception):
    """A request that cannot be answered from the available series."""


class PriceQuote(BaseModel):
    commodity: Commodity
    requested_date: date | None
    trading_date: date = Field(description="Latest trading day on or before the requested date")
    price: float
    unit: str
    price_type: str
    extras: dict[str, float] = Field(default_factory=dict)
    provenance: Provenance
    notes: list[str] = Field(default_factory=list)


class PricePoint(BaseModel):
    trading_date: date
    price: float


class PriceTrend(BaseModel):
    commodity: Commodity
    unit: str
    price_type: str
    window_days: int
    start_date: date
    end_date: date
    first: float
    last: float
    change_abs: float
    change_pct: float
    high: float
    low: float
    mean: float
    direction: Literal["up", "down", "flat"]
    points: list[PricePoint]
    provenance: Provenance
    notes: list[str] = Field(default_factory=list)


class PriceService:
    def __init__(self, http: CachedHttp) -> None:
        self._http = http

    async def _series(
        self, commodity: str
    ) -> tuple[Commodity, SourceSpec, list[DailyPrice], Provenance]:
        try:
            resolved = resolve_commodity(commodity)
        except KeyError as exc:
            raise PriceLookupError(str(exc.args[0])) from exc
        spec = source_for(resolved)
        result = await self._http.get(spec.url, ttl=PRICE_TTL)
        provenance = Provenance(
            source=spec.name,
            source_url=spec.url,
            retrieved_at=result.retrieved_at,
            data_mode=result.data_mode,
        )
        return resolved, spec, parse(spec, result.content), provenance

    async def get_price(self, commodity: str, on: date | None = None) -> PriceQuote:
        resolved, spec, rows, provenance = await self._series(commodity)
        eligible = [r for r in rows if on is None or r.day <= on]
        if not eligible:
            raise PriceLookupError(
                f"no {resolved.value} price on or before {on}; "
                f"series starts {rows[0].day.isoformat()}"
            )
        row = eligible[-1]
        notes: list[str] = []
        if on is not None and row.day != on:
            notes.append(f"{on.isoformat()} is not a trading day; using {row.day.isoformat()}")
        return PriceQuote(
            commodity=resolved,
            requested_date=on,
            trading_date=row.day,
            price=row.price,
            unit=spec.unit,
            price_type=spec.price_type,
            extras=row.extras,
            provenance=provenance,
            notes=notes,
        )

    async def get_trend(self, commodity: str, days: int = 30) -> PriceTrend:
        if not 2 <= days <= MAX_TREND_DAYS:
            raise PriceLookupError(f"days must be between 2 and {MAX_TREND_DAYS}, got {days}")
        resolved, spec, rows, provenance = await self._series(commodity)
        end = rows[-1].day
        window = [r for r in rows if r.day > end - timedelta(days=days)]
        if len(window) < 2:
            raise PriceLookupError(f"fewer than 2 trading days in the last {days} days")
        notes: list[str] = []
        if window[0].day == rows[0].day and end - rows[0].day < timedelta(days=days - 1):
            notes.append(f"series only covers data since {rows[0].day.isoformat()}")
        first, last = window[0].price, window[-1].price
        change_pct = (last - first) / first * 100
        direction: Literal["up", "down", "flat"] = (
            "flat" if abs(change_pct) < FLAT_THRESHOLD_PCT else "up" if change_pct > 0 else "down"
        )
        prices = [r.price for r in window]
        return PriceTrend(
            commodity=resolved,
            unit=spec.unit,
            price_type=spec.price_type,
            window_days=days,
            start_date=window[0].day,
            end_date=end,
            first=first,
            last=last,
            change_abs=round(last - first, 4),
            change_pct=round(change_pct, 2),
            high=max(prices),
            low=min(prices),
            mean=round(fmean(prices), 2),
            direction=direction,
            points=[PricePoint(trading_date=r.day, price=r.price) for r in window],
            provenance=provenance,
            notes=notes,
        )
