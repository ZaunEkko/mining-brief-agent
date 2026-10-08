"""Markdown rendering. Every number shown here comes straight from tool output."""

from datetime import datetime
from typing import Any

from mining_brief.agent.compose import BriefContent
from mining_brief.agent.intent import COMMODITY_LABELS, BriefPlan

MODE_LABELS = {
    "live": "实时抓取",
    "cache": "缓存（TTL 内）",
    "stale_cache": "过期缓存（上游不可达）",
    "fixture": "合成示例数据（离线）",
}
_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 24) -> str:
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width - 1)] + [values[-1]]
    lo, hi = min(values), max(values)
    if hi == lo:
        return _BARS[3] * len(values)
    return "".join(_BARS[round((v - lo) / (hi - lo) * (len(_BARS) - 1))] for v in values)


def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"


def _signed(pct: float) -> str:
    arrow = "▲" if pct > 0 else "▼" if pct < 0 else "■"
    return f"{arrow} {pct:+.2f}%"


def _resources(content: BriefContent) -> list[str]:
    out = ["## 二、储量数据", ""]
    if content.resources_failure:
        return [*out, f"> 数据暂不可用：{content.resources_failure}", ""]
    data = content.resources
    if data is None:
        return [*out, "> 未匹配到该项目的资源量报告。", ""]
    table = data["table"]
    cite = f"[{content.resources_cite}]" if content.resources_cite else ""
    checks = table["checks"]
    passed = sum(c["passed"] for c in checks)
    out.append(
        f"来源：{table['caption'] or '资源量表'}（{data['reporting_code']}，第 {table['page']} 页）{cite}"
    )
    out.append("")
    if data["abstain"]:
        failed = [c for c in checks if not c["passed"]]
        out += [
            f"> ⚠ 抽取结果未通过一致性校验（{passed}/{len(checks)}），按规则**不展示数字**，请人工核对原报告。",
            *[f"> - {c['name']}：{c['detail']}" for c in failed[:5]],
            "",
        ]
        return out
    rows = table["headline"]["rows"]
    unit = rows[0]["grade_unit"] if rows else ""
    commodity = table["commodity"]
    contained_unit = (rows[0]["contained_unit"] if rows else None) or "报告未披露"
    out += [
        f"**{table['headline']['label']}**",
        "",
        f"| 类别 | 矿石量 (Mt) | 品位 ({unit} {commodity}) | 金属量 ({commodity}, {contained_unit}) |",
        "|---|---:|---:|---:|",
    ]
    names = {
        "Measured": "探明 Measured",
        "Indicated": "控制 Indicated",
        "Inferred": "推断 Inferred",
        "Total": "合计 Total",
        "Subtotal": "小计 Subtotal",
        "Measured & Indicated": "探明+控制",
    }
    for r in rows:
        out.append(
            f"| {names.get(r['category'], r['category'])} | {_num(r['tonnage_mt'], 2)} | "
            f"{_num(r['grade'], 2)} | {_num(r['contained'], 2)} |"
        )
    out += [
        "",
        f"自校验：{passed}/{len(checks)} 项通过（吨位×品位≈金属量、分类合计≈总量），置信度 **{table['confidence']}**。",
    ]
    out += [f"- 注：{n}" for n in data.get("notes", [])]
    out.append("")
    return out


def _prices(content: BriefContent) -> list[str]:
    out = ["## 三、价格走势", ""]
    if content.prices_failure and not content.prices:
        return [*out, f"> 数据暂不可用：{content.prices_failure}", ""]
    out += [
        "| 品种 | 最新价 | 交易日 | 区间涨跌 | 区间低 / 高 | 走势 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for trend, cite in content.prices:
        label = COMMODITY_LABELS.get(trend["commodity"], trend["commodity"])
        points = [p["price"] for p in trend["points"]]
        out.append(
            f"| {label} [{cite}] | {_num(trend['last'], 0)} {trend['unit']} | {trend['end_date']} | "
            f"{_signed(trend['change_pct'])}（{trend['window_days']} 天） | "
            f"{_num(trend['low'], 0)} / {_num(trend['high'], 0)} | `{sparkline(points)}` |"
        )
    for trend, _ in content.prices:
        out += [f"- 注：{n}" for n in trend.get("notes", [])]
    if content.prices_failure:
        out.append(f"- 部分价格不可用：{content.prices_failure}")
    out.append("")
    return out


def render(plan: BriefPlan, content: BriefContent, generated_at: datetime) -> str:
    project = plan.project
    title = f"{plan.subject} 矿业日报 · {generated_at:%Y-%m-%d}"
    meta = [f"请求：{plan.query}"]
    if project:
        meta += [f"项目：{project.name}（{project.owner}）", f"地区：{project.region}"]
    out = [f"# {title}", "", "> " + " ｜ ".join(meta), ""]

    if content.overview:
        out += ["**今日要点**：" + content.overview, ""]
    for w in content.warnings:
        out.append(f"> ⚠ {w}")
    if content.warnings:
        out.append("")

    out += [f"## 一、新闻摘要（近 {plan.news_days} 天，检索词：{plan.news_query}）", ""]
    if content.news_failure:
        out.append(f"> 数据暂不可用：{content.news_failure}")
    elif not content.news:
        out.append("> 检索窗口内没有匹配的新闻。")
    groups = [(g, [p for p in content.news if p.group == g]) for g in ("project", "market")]
    titled = plan.project is not None
    for group, points in groups:
        if not points:
            continue
        if titled:
            heading = "项目动态" if group == "project" else "行业动态"
            out += [f"**{heading}**", ""]
        for p in points:
            out.append(f"- **{p.title}**（{p.publisher}，{p.date}）[{p.cite}]  ")
            out.append(f"  {p.summary}")
        if titled:
            out.append("")
    if titled and not groups[0][1] and content.news:
        out.append("> 检索窗口内没有直接提及该项目的新闻，以下为行业动态。")
    out += [f"- 注：{n}" for n in content.news_notes]
    out.append("")

    out += _resources(content)
    out += _prices(content)

    out += ["## 四、风险提示", ""]
    if not content.risks:
        out.append("- 未从本次数据中识别到需要特别提示的风险信号。")
    for r in content.risks:
        cites = "".join(f"[{n}]" for n in r.cites)
        out.append(f"- {r.text}{(' ' + cites) if cites else ''}")
    out.append("")

    out += ["## 引用来源", ""]
    for s in content.sources.items:
        extra = "，".join(x for x in (s.publisher, s.date) if x)
        out.append(f"{s.n}. [{s.title}]({s.url}){'（' + extra + '）' if extra else ''}")
    out.append("")

    out += ["---", "", "**数据说明**", ""]
    for section, modes in content.data_modes.items():
        if modes:
            out.append(f"- {section}：" + "、".join(MODE_LABELS.get(m, m) for m in sorted(modes)))
    out += [
        f"- 生成方式：{content.generator}",
        f"- 生成时间：{generated_at:%Y-%m-%d %H:%M} (UTC{generated_at:%z})",
        "- 价格来自公开转载源（westmetall / 新浪财经），非交易所官方授权数据；本简报仅供研究参考，不构成投资建议。",
        "",
    ]
    return "\n".join(out)


def facts_for_llm(content: BriefContent) -> dict[str, Any]:
    """Compact numeric context the LLM may reason about (but not restate)."""
    facts: dict[str, Any] = {}
    if content.resources and not content.resources["abstain"]:
        rows = content.resources["table"]["headline"]["rows"]
        facts["resources"] = [
            {k: r[k] for k in ("category", "tonnage_mt", "grade", "grade_unit")} for r in rows
        ]
    facts["prices"] = [
        {k: t[k] for k in ("commodity", "change_pct", "window_days", "direction")} | {"source": c}
        for t, c in content.prices
    ]
    return facts
