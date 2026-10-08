"""Mineral Resource table extraction from page text (NI 43-101 / JORC).

The extractor is deliberately deterministic: it locates a table by its category rows
(Measured / Indicated / Inferred / Total), reads column meaning from the units header
(``(Mt) (%) (ppm) (Mt)``), and then *checks itself*:

* tonnage x grade must reproduce the reported contained metal (within rounding);
* Measured + Indicated + Inferred must add up to the reported total;
* Measured + Indicated must add up to a reported M&I row.

The check results drive ``confidence`` so callers can abstain instead of publishing
numbers that do not reconcile. Tables without a contained-metal column are still
extracted, but can reach at most what their remaining checks support.
"""

import contextlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["Measured", "Indicated", "Inferred", "Measured & Indicated", "Subtotal", "Total"]
GradeUnit = Literal["%", "g/t", "ppm"]
Confidence = Literal["high", "medium", "low"]
UnitKind = Literal["tonnage", "grade", "mass", "oz", "other"]

_CATEGORY = re.compile(
    r"\b(?P<cat>Measured\s*(?:&|and|\+)\s*Indicated|M\s*\+\s*I|M&I|Measured|Indicated|Inferred|"
    r"Sub[-\s]?Total|Total|Combined)\b",
    re.IGNORECASE,
)
_PARTS = {"Measured", "Indicated", "Inferred"}
_VALUE = r"-?\d[\d,]*(?:\.\d+)?|[-–—]"
_ROW = re.compile(rf"^(?P<label>.*?)\s+(?P<values>(?:(?:{_VALUE})\s+)*(?:{_VALUE}))\s*$")
_UNIT = re.compile(r"\(([^()]{1,12})\)")
# Words that mark a line as part of a table header rather than a section label.
_HEADER_WORDS = re.compile(
    r"\b(tonnage|tonnes|quantity|grade|contained|category|classification|class|metal)\b", re.I
)

_TONNAGE_TO_MT = {"mt": 1.0, "kt": 1e-3, "t": 1e-6}
_MASS_TO_T = {"mt": 1e6, "kt": 1e3, "t": 1.0, "mlb": 453.59237}
_OZ = {"moz": 1e6, "koz": 1e3, "oz": 1.0}
_TROY_OZ_G = 31.1034768
# Commodity formulas we recognise in headers, in order of preference.
# OCR'd reports often read the subscript 2 as part of the symbol: "Li20".
_OCR_ALIASES = {"Li2O": ["Li20"]}
_ELEMENTS = [
    "Li2O",
    "Au",
    "Cu",
    "Ni",
    "Zn",
    "Ag",
    "Co",
    "U3O8",
    "Fe",
    "Pb",
    "Mo",
    "Sn",
    "Ta2O5",
    "TREO",
]


class ExtractionError(ValueError):
    """No usable resource table was found."""


class ResourceRow(BaseModel):
    category: Category
    label: str = Field(description="Row label as printed, e.g. 'In-situ Indicated'")
    tonnage_mt: float | None
    tonnage_reported: float | None = Field(description="Tonnage as printed, in tonnage_unit")
    tonnage_unit: str
    grade: float | None
    grade_unit: GradeUnit
    contained: float | None = Field(description="Contained metal in the reported unit")
    contained_unit: str | None = Field(description="None when the table has no contained column")
    contained_t: float | None = Field(default=None, description="Contained metal in tonnes")
    contained_oz: float | None = Field(default=None, description="Contained metal in troy oz")
    raw: str


class Check(BaseModel):
    name: str
    section: str
    passed: bool
    detail: str


class ResourceSection(BaseModel):
    label: str
    rows: list[ResourceRow]


class ResourceTable(BaseModel):
    page: int = Field(description="1-based page number")
    caption: str | None
    commodity: str = Field(description="Grade element/compound, e.g. Li2O or Au")
    columns: list[str]
    sections: list[ResourceSection]
    headline: ResourceSection = Field(description="Section that best represents the deposit total")
    checks: list[Check]
    confidence: Confidence
    notes: list[str] = Field(default_factory=list)


def unit_base(unit: str) -> tuple[UnitKind, str]:
    """Classify a header unit, tolerating element suffixes and OCR noise.

    ``"kt Li O"`` -> ("tonnage", "kt"); ``"% Li O"`` / ``"Lî20%"`` -> ("grade", "%");
    ``"M"`` (million tonnes) -> ("tonnage", "mt"); ``"M lb"`` -> ("mass", "mlb").
    """
    lowered = unit.lower().strip()
    compact = re.sub(r"[\s,.']", "", lowered)
    if "%" in compact:
        return "grade", "%"
    if "g/t" in compact or compact.startswith("gpt"):
        return "grade", "g/t"
    if compact.startswith("ppm"):
        return "grade", "ppm"
    if compact.startswith("mlb"):
        return "mass", "mlb"
    for oz in ("moz", "koz", "oz"):
        if compact.startswith(oz):
            return "oz", oz
    if compact.startswith(("000t", "kt")):
        return "tonnage", "kt"
    first = lowered.split()[0] if lowered.split() else ""
    if first in {"m", "mt", "mtonnes", "million"}:
        return "tonnage", "mt"
    if first in {"t", "tonnes"}:
        return "tonnage", "t"
    return "other", compact


@dataclass(slots=True)
class _Column:
    name: str | None
    unit: str

    @property
    def kind(self) -> UnitKind:
        return unit_base(self.unit)[0]

    @property
    def base(self) -> str:
        return unit_base(self.unit)[1]


@dataclass(slots=True)
class _Header:
    columns: list[_Column]
    caption: str | None
    caption_line: int | None
    units_line: int


@dataclass(slots=True)
class _Row:
    category: Category
    label: str
    values: list[float | None]
    raw: str
    line_no: int


@dataclass(slots=True)
class _Block:
    rows: list[_Row] = field(default_factory=list)
    label_lines: list[str] = field(default_factory=list)


def _category(word: str) -> Category:
    w = re.sub(r"\s+", " ", word.lower())
    if "indicated" in w and ("measured" in w or "&" in w or "+" in w):
        return "Measured & Indicated"
    if w in {"m+i", "m + i", "m&i"}:
        return "Measured & Indicated"
    if w.startswith("sub"):
        return "Subtotal"
    if w in {"total", "combined"}:
        return "Total"
    return word.strip().capitalize()  # type: ignore[return-value]


def _number(token: str) -> float | None:
    if token in {"-", "–", "—"}:
        return None
    return float(token.replace(",", ""))


def _parse_row(line: str, line_no: int) -> _Row | None:
    match = _ROW.match(line.strip())
    if not match:
        return None
    label = match["label"].strip()
    cats = list(_CATEGORY.finditer(label))
    if not cats:
        return None
    values = [_number(t) for t in match["values"].split()]
    # The category is the last keyword in the label ("Sub-Total Indicated" -> Indicated).
    cat = _category(cats[-1]["cat"])
    return _Row(category=cat, label=label, values=values, raw=line.strip(), line_no=line_no)


def _parse_header(lines: list[str], first_row: int, ncols: int) -> _Header:
    """Find the units line above the first data row; names come from the line above it."""
    for i in range(first_row - 1, max(-1, first_row - 8), -1):
        units = _UNIT.findall(lines[i])
        if len(units) != ncols:
            continue
        inline = re.findall(r"([A-Za-z][\w]*)\s*\(([^()]{1,12})\)", lines[i])
        if len(inline) == ncols:
            names: list[str | None] = [n for n, _ in inline]
        else:
            above = lines[i - 1].split() if i > 0 else []
            above = [w for w in above if w.lower() not in {"category", "classification", "class"}]
            # Reject stray tokens such as split subscripts ("Resource Category 2").
            usable = len(above) == ncols and not any(w.isdigit() for w in above)
            names = list(above) if usable else [None] * ncols
        caption = caption_line = None
        for j in range(i - 1, max(-1, i - 6), -1):
            if re.match(r"^Table\s+\d", lines[j]):
                caption, caption_line = lines[j].strip(), j
                if caption.count("(") > caption.count(")") and j + 1 < i:
                    caption = f"{caption} {lines[j + 1].strip()}"
                break
        columns = [_Column(name=n, unit=u.strip()) for n, u in zip(names, units, strict=True)]
        return _Header(columns, caption, caption_line, i)
    raise ExtractionError("units header not found above resource rows")


def _guess_element(columns: list[_Column], page_text: str) -> str:
    grade = next((c for c in columns if c.kind == "grade"), None)
    if grade is not None:
        # Element named in the unit itself: "% Cu", "g/t Au", "% Li O" (split subscript).
        tail = re.sub(r"[^a-z0-9]", "", grade.unit.lower().replace(grade.base, "", 1))
        for el in _ELEMENTS:
            if (grade.name or "").replace(" ", "") == el or tail in {
                el.lower(),
                re.sub(r"\d", "", el.lower()),
            }:
                return el

    # Subscripts are often split off by text extraction ("Li O" + "2"), so match both forms.
    def hits(el: str) -> int:
        split_form = r"\s+".join(re.escape(p) for p in re.split(r"\d+", el) if p)
        forms = [re.escape(el), split_form, *map(re.escape, _OCR_ALIASES.get(el, []))]
        boundary = r"\b"
        pattern = rf"{boundary}(?:{'|'.join(forms)}){boundary}"
        return len(re.findall(pattern, page_text))

    counts = Counter({el: hits(el) for el in _ELEMENTS})
    element, n = counts.most_common(1)[0]
    return element if n else "unknown"


def _select_columns(columns: list[_Column], element: str) -> tuple[int, int, int | None]:
    """Indices of (tonnage, grade, contained) for the primary commodity."""
    tonnage = next((i for i, c in enumerate(columns) if c.kind == "tonnage"), None)
    grade = next((i for i, c in enumerate(columns) if c.kind == "grade"), None)
    if tonnage is None or grade is None:
        raise ExtractionError(
            f"need tonnage and grade columns, got units {[c.unit for c in columns]}"
        )
    later = [i for i, c in enumerate(columns) if i > grade and c.kind in {"tonnage", "mass", "oz"}]
    named = [i for i in later if (columns[i].name or "").replace(" ", "") == element]
    if named:
        return tonnage, grade, named[0]
    return tonnage, grade, later[0] if later else None


def _decimals(value: float, raw: str) -> int:
    for token in raw.split():
        cleaned = token.replace(",", "")
        try:
            if float(cleaned) == value and "." in cleaned:
                return len(cleaned.split(".")[1])
        except ValueError:
            continue
    return 0


def _to_resource_row(
    row: _Row, cols: list[_Column], idx: tuple[int, int, int | None]
) -> ResourceRow:
    t_i, g_i, c_i = idx
    tonnage_raw, grade = row.values[t_i], row.values[g_i]
    tonnage_mt = None if tonnage_raw is None else tonnage_raw * _TONNAGE_TO_MT[cols[t_i].base]
    contained = contained_t = contained_oz = None
    contained_unit = None
    if c_i is not None:
        contained, contained_unit = row.values[c_i], cols[c_i].unit
        base = cols[c_i].base
        if contained is not None:
            if base in _OZ:
                contained_oz = contained * _OZ[base]
            else:
                contained_t = contained * _MASS_TO_T[base]
    return ResourceRow(
        category=row.category,
        label=row.label,
        tonnage_mt=tonnage_mt,
        tonnage_reported=tonnage_raw,
        tonnage_unit=cols[t_i].unit,
        grade=grade,
        grade_unit=unit_base(cols[g_i].unit)[1],
        contained=contained,
        contained_unit=contained_unit,
        contained_t=contained_t,
        contained_oz=contained_oz,
        raw=row.raw,
    )


def _expected_contained(row: ResourceRow) -> float | None:
    """Contained metal implied by tonnage x grade, in the row's reported unit."""
    if row.tonnage_mt is None or row.grade is None or row.contained is None:
        return None
    if row.contained_unit is None:
        return None
    kind, base = unit_base(row.contained_unit)
    tonnes_ore = row.tonnage_mt * 1e6
    if row.grade_unit == "g/t":
        oz = tonnes_ore * row.grade / _TROY_OZ_G
        return oz / _OZ[base] if kind == "oz" else None
    fraction = row.grade / 100 if row.grade_unit == "%" else row.grade / 1e6
    return tonnes_ore * fraction / _MASS_TO_T[base] if base in _MASS_TO_T else None


def _rounding_step_mt(row: ResourceRow) -> float:
    """Last printed digit of the row's tonnage, expressed in Mt."""
    if row.tonnage_reported is None:
        return 0.0
    factor = _TONNAGE_TO_MT[unit_base(row.tonnage_unit)[1]]
    return float(10 ** -_decimals(row.tonnage_reported, row.raw)) * factor


def _sum_check(
    name: str, section: str, parts: list[ResourceRow], total: ResourceRow
) -> Check | None:
    if total.tonnage_mt is None or len(parts) < 2:
        return None
    summed = sum(r.tonnage_mt or 0 for r in parts)
    tolerance = max(_rounding_step_mt(total) * len(parts), total.tonnage_mt * 0.01)
    return Check(
        name=name,
        section=section,
        passed=abs(summed - total.tonnage_mt) <= tolerance,
        detail=f"sum {summed:g} Mt vs reported {total.tonnage_mt:g} Mt",
    )


def _check_section(section: ResourceSection) -> list[Check]:
    checks: list[Check] = []
    for row in section.rows:
        expected = _expected_contained(row)
        if expected is None or row.contained is None:
            continue
        step = 10 ** -_decimals(row.contained, row.raw)
        tolerance = max(step / 2, abs(expected) * 0.1)
        checks.append(
            Check(
                name=f"{section.label}/{row.category}: tonnage x grade = contained",
                section=section.label,
                passed=abs(row.contained - expected) <= tolerance,
                detail=f"reported {row.contained:g} {row.contained_unit}, implied {expected:.4g}",
            )
        )
    by_cat = {r.category: r for r in section.rows}
    parts = [r for r in section.rows if r.category in _PARTS]
    total = next((r for r in section.rows if r.category in {"Total", "Subtotal"}), None)
    if total:
        check = _sum_check(
            f"{section.label}: categories sum to total tonnage", section.label, parts, total
        )
        checks += [check] if check else []
    if "Measured & Indicated" in by_cat:
        mi = [r for r in parts if r.category in {"Measured", "Indicated"}]
        check = _sum_check(
            f"{section.label}: Measured + Indicated = M&I",
            section.label,
            mi,
            by_cat["Measured & Indicated"],
        )
        checks += [check] if check else []
    return checks


def _blocks(rows: list[_Row], lines: list[str]) -> list[_Block]:
    """Split rows into sections: a Subtotal/Total row closes one, and a category that
    repeats within a section starts a new one (e.g. inclusive / exclusive of reserves)."""
    blocks: list[_Block] = [_Block()]
    previous_line = rows[0].line_no - 1
    for row in rows:
        seen = {r.category for r in blocks[-1].rows}
        if row.category in seen and row.category not in {"Subtotal", "Total"}:
            blocks.append(_Block())
        between = [
            ln.strip()
            for ln in lines[previous_line + 1 : row.line_no]
            if ln.strip() and not re.match(r"^\(.*\)$", ln.strip())
        ]
        blocks[-1].label_lines += between
        blocks[-1].rows.append(row)
        previous_line = row.line_no
        if row.category in {"Subtotal", "Total"}:
            blocks.append(_Block())
    return [b for b in blocks if b.rows]


def _section_label(block: _Block, index: int) -> str:
    prefixes = [_CATEGORY.split(r.label)[0].strip() for r in block.rows]
    prefixes += block.label_lines
    words = [
        p for p in prefixes if p and not re.search(r"\d|%|≥|\?", p) and not _HEADER_WORDS.search(p)
    ]
    return words[0] if words else f"Section {index + 1}"


def _size(section: ResourceSection) -> float:
    totals = [r.tonnage_mt or 0 for r in section.rows if r.category in {"Total", "Subtotal"}]
    return (
        max(totals)
        if totals
        else sum(r.tonnage_mt or 0 for r in section.rows if r.category in _PARTS)
    )


def _choose_headline(sections: list[ResourceSection]) -> tuple[ResourceSection, list[str]]:
    def categories(s: ResourceSection) -> set[str]:
        return {str(r.category) for r in s.rows} & _PARTS

    with_total = [
        s for s in sections if any(r.category == "Total" for r in s.rows) and categories(s)
    ]
    if with_total:
        return with_total[-1], []
    candidates = [s for s in sections if categories(s)]
    if not candidates:
        raise ExtractionError("table has totals but no Measured/Indicated/Inferred rows")
    best = max(candidates, key=_size)
    notes = (
        []
        if len(candidates) == 1
        else [
            f"table has {len(candidates)} sections without a categorised grand total; "
            f"headline is the largest ({best.label}), see sections for the rest"
        ]
    )
    return best, notes


def _confidence(checks: list[Check], headline: ResourceSection) -> Confidence:
    cats = {r.category for r in headline.rows}
    failed = [c for c in checks if not c.passed]
    if not checks:
        # Nothing to reconcile against (no contained column, no totals): usable, unverified.
        return "medium"
    if any(c.section == headline.label for c in failed):
        # The headline section is what gets published; any contradiction there abstains.
        return "low"
    if not failed and {"Indicated", "Inferred"} <= cats:
        return "high"
    if len(failed) <= max(1, len(checks) // 4):
        return "medium"
    return "low"


def _first_section_lines(lines: list[str], header: _Header, floor: int) -> list[str]:
    """Lines between the caption (or the previous table) and the units header,
    e.g. 'Inclusive of Mineral Reserves'."""
    if header.caption_line is not None:
        start = header.caption_line + 1
    else:
        start = max(floor, header.units_line - 4)
    return [ln.strip() for ln in lines[start : header.units_line] if ln.strip()]


def _is_units_header(line: str) -> bool:
    kinds = {unit_base(u)[0] for u in _UNIT.findall(line)}
    return {"tonnage", "grade"} <= kinds


def _segments(lines: list[str], rows: list[_Row]) -> list[tuple[int, list[_Row]]]:
    """Group rows under the closest units header above them: one group per table."""
    headers = [i for i, ln in enumerate(lines) if _is_units_header(ln)]
    groups: list[tuple[int, list[_Row]]] = []
    for k, h in enumerate(headers):
        end = headers[k + 1] if k + 1 < len(headers) else len(lines)
        members = [r for r in rows if h < r.line_no < end]
        if {r.category for r in members} & _PARTS:
            groups.append((h, members))
    return groups


def _parse_table(
    page_no: int, lines: list[str], text: str, rows: list[_Row], floor: int
) -> ResourceTable:
    ncols = Counter(len(r.values) for r in rows if r.category in _PARTS).most_common(1)[0][0]
    rows = [r for r in rows if len(r.values) == ncols]
    header = _parse_header(lines, rows[0].line_no, ncols)
    element = _guess_element(header.columns, text)
    idx = _select_columns(header.columns, element)
    blocks = _blocks(rows, lines)
    blocks[0].label_lines = _first_section_lines(lines, header, floor) + blocks[0].label_lines
    sections = [
        ResourceSection(
            label=_section_label(block, i),
            rows=[_to_resource_row(r, header.columns, idx) for r in block.rows],
        )
        for i, block in enumerate(blocks)
    ]
    headline, notes = _choose_headline(sections)
    checks = [c for s in sections for c in _check_section(s)]
    if idx[2] is None:
        notes.append("table reports no contained metal; tonnage x grade cannot be reconciled")
    if not checks:
        notes.append("no totals or contained metal to cross-check; numbers are unverified")
    return ResourceTable(
        page=page_no,
        caption=header.caption,
        commodity=element,
        columns=[f"{c.name or '?'} ({c.unit})" for c in header.columns],
        sections=sections,
        headline=headline,
        checks=checks,
        confidence=_confidence(checks, headline),
        notes=notes,
    )


def extract_tables(pages: list[str]) -> list[ResourceTable]:
    """All resource tables found, in page order (several per page are possible)."""
    tables: list[ResourceTable] = []
    for page_no, text in enumerate(pages, start=1):
        lines = text.splitlines()
        rows = [r for i, ln in enumerate(lines) if (r := _parse_row(ln, i))]
        floor = 0
        for _, members in _segments(lines, rows):
            with contextlib.suppress(ExtractionError):  # not a resource table after all
                tables.append(_parse_table(page_no, lines, text, members, floor))
            floor = members[-1].line_no + 1
    return tables


def detect_reporting_code(text: str) -> Literal["NI 43-101", "JORC", "unknown"]:
    ni = len(re.findall(r"NI\s*43[-‐]101|National Instrument 43[-‐]101", text))
    jorc = len(re.findall(r"\bJORC\b", text))
    if ni == jorc == 0:
        return "unknown"
    return "NI 43-101" if ni > jorc else "JORC"
