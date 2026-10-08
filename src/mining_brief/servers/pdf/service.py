"""Download a technical report PDF and extract its Mineral Resource table."""

import asyncio
import io
from datetime import timedelta
from typing import Literal

import pdfplumber
from pydantic import BaseModel, Field

from mining_brief.common.http import CachedHttp, FetchError
from mining_brief.common.models import Provenance
from mining_brief.common.urls import UnsafeUrlError
from mining_brief.servers.pdf.extract import (
    ResourceRow,
    ResourceTable,
    detect_reporting_code,
    extract_tables,
)

PDF_TTL = timedelta(days=30)
MAX_PDF_BYTES = 60 * 1024 * 1024
DEFAULT_SCAN_PAGES = 40
SCAN_CHUNK = 8
_RANK = {"high": 0, "medium": 1, "low": 2}


class PdfError(Exception):
    """The PDF cannot be fetched or contains no usable resource table."""


class TableRef(BaseModel):
    page: int
    caption: str | None
    confidence: Literal["high", "medium", "low"]


class ResourceExtraction(BaseModel):
    pdf_url: str
    reporting_code: Literal["NI 43-101", "JORC", "unknown"]
    page_count: int
    scanned_pages: int
    table: ResourceTable = Field(description="Primary table: highest confidence, earliest page")
    indicated: ResourceRow | None = Field(description="Indicated row of the headline section")
    inferred: ResourceRow | None = Field(description="Inferred row of the headline section")
    abstain: bool = Field(
        description="True when the primary table failed its consistency checks; "
        "do not present its numbers as fact"
    )
    other_tables: list[TableRef]
    provenance: Provenance
    notes: list[str] = Field(default_factory=list)


def _scan(content: bytes, max_pages: int) -> tuple[list[str], list[ResourceTable], int]:
    """Read pages in chunks and stop at the first chunk that yields a high-confidence
    table: resource summaries sit near the front, and pdfplumber is slow per page."""
    pages: list[str] = []
    tables: list[ResourceTable] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        total = len(pdf.pages)
        for start in range(0, min(max_pages, total), SCAN_CHUNK):
            chunk = pdf.pages[start : min(start + SCAN_CHUNK, max_pages)]
            texts = [p.extract_text() or "" for p in chunk]
            found = extract_tables(texts)
            for t in found:
                t.page += start
            pages += texts
            tables += found
            if any(t.confidence == "high" for t in found):
                break
    return pages, tables, total


def _row(table: ResourceTable, category: str) -> ResourceRow | None:
    return next((r for r in table.headline.rows if r.category == category), None)


class PdfService:
    def __init__(self, http: CachedHttp) -> None:
        self._http = http

    async def extract_resources(
        self, pdf_url: str, max_pages: int = DEFAULT_SCAN_PAGES
    ) -> ResourceExtraction:
        try:
            result = await self._http.get(
                pdf_url, ttl=PDF_TTL, public_only=True, max_bytes=MAX_PDF_BYTES
            )
        except (UnsafeUrlError, FetchError) as exc:
            raise PdfError(str(exc)) from exc
        if not result.content.startswith(b"%PDF"):
            raise PdfError(f"{pdf_url} did not return a PDF (content-type {result.content_type})")

        pages, tables, page_count = await asyncio.to_thread(_scan, result.content, max_pages)
        if not any(p.strip() for p in pages):
            raise PdfError("PDF has no extractable text (scanned image?); OCR is not supported")
        if not tables:
            raise PdfError(
                f"no Mineral Resource table with Measured/Indicated/Inferred rows and a units "
                f"header found in the first {len(pages)} pages"
            )
        primary = min(tables, key=lambda t: (_RANK[t.confidence], t.page))
        notes = list(primary.notes)
        abstain = primary.confidence == "low"
        if abstain:
            notes.append("primary table failed consistency checks; numbers withheld from summary")
        indicated, inferred = _row(primary, "Indicated"), _row(primary, "Inferred")
        for name, row in (("Indicated", indicated), ("Inferred", inferred)):
            if row is None:
                notes.append(f"no {name} row in the headline section")
        return ResourceExtraction(
            pdf_url=pdf_url,
            reporting_code=detect_reporting_code("\n".join(pages)),
            page_count=page_count,
            scanned_pages=len(pages),
            table=primary,
            indicated=indicated,
            inferred=inferred,
            abstain=abstain,
            other_tables=[
                TableRef(page=t.page, caption=t.caption, confidence=t.confidence)
                for t in tables
                if t is not primary
            ],
            provenance=Provenance(
                source="Technical report PDF",
                source_url=pdf_url,
                retrieved_at=result.retrieved_at,
                data_mode=result.data_mode,
            ),
            notes=notes,
        )
