"""CNKI DOM helpers adapted from the installed cnki-mcp 0.1.0 copy.

Upstream: https://github.com/wuruiqi/cnki-mcp
Source commit: 419aa08142259e7492f9925adf2e9543a07ecedc
Copyright (c) 2026 wuruiqi; MIT, see third_party/CNKI-MCP-LICENSE.
The installed source includes subsequent local CNKI compatibility fixes.
"""

import os
import re
from typing import Dict, List, Optional, Tuple
from .webvpn import resolve_cnki_url


_INPUT_SELECTORS = ["input#txt_search", "input.search-input", "input[name='kw']"]


_BUTTON_SELECTORS = ["input.search-btn", ".search-btn", "button.search-btn"]


_RESULT_MARKERS = [
    ".result-table-list",
    "#gridTable",
    ".brief-list",
]


_ROW_SELECTORS = [
    ".result-table-list tbody tr",
    "#gridTable tbody tr",
    "table.result-table-list tr",
]


_FIELD_SELECTORS: Dict[str, List[str]] = {
    "title":     ["td.name a", "a.fz14", ".name a"],
    "year":      ["td.date", ".date"],
    "journal":   ["td.source", ".source"],
    "authors":   ["td.author", ".author"],
    "citations": ["td.quote", ".quote", "td.cited", ".cited"],
}


_DB_TAB_TEXT = {
    "CJFD": "学术期刊",
    "CDFD": "博士",
    "CMFD": "硕士",
}


_DEFAULT_JOURNAL_WHITELIST = {
    "新闻与传播研究", "国际新闻界", "新闻大学", "现代传播(中国传媒大学学报)", "现代传播",
    "新闻界", "新闻与传播评论", "编辑之友", "新闻记者", "当代传播", "中国编辑",
    "全球传媒学刊", "新闻与写作", "传媒观察", "现代出版", "出版发行研究", "出版科学",
    "编辑学报", "科技与出版", "中国出版", "中国科技期刊研究", "青年记者", "新闻春秋",
    "对外传播", "未来传播", "新闻传播学刊", "新闻爱好者", "出版广角", "出版与印刷",
    "传媒", "符号与传媒", "新媒体与社会", "中国网络传播研究", "传媒经济与管理研究",
}


def _normalize_journal(value: str) -> str:
    value = (value or "").strip().replace("《", "").replace("》", "")
    value = value.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", value)


def _load_journal_whitelist() -> set[str]:
    configured = os.getenv("CNKI_JOURNAL_WHITELIST", "").strip()
    if configured:
        names = re.split(r"[;；|,，\n]+", configured)
        return {_normalize_journal(name) for name in names if name.strip()}
    return {_normalize_journal(name) for name in _DEFAULT_JOURNAL_WHITELIST}


_JOURNAL_WHITELIST = _load_journal_whitelist()


def _filter_by_journal(papers: List[Dict], max_n: Optional[int] = None) -> List[Dict]:
    """按白名单过滤期刊；白名单启用时绝不回退到目录外期刊。"""
    if not _JOURNAL_WHITELIST:
        return papers[:max_n] if max_n is not None else papers
    filtered = [
        paper for paper in papers
        if _normalize_journal(paper.get("journal", "")) in _JOURNAL_WHITELIST
    ]
    return filtered[:max_n] if max_n is not None else filtered


async def _fill_and_search(page, query: str) -> bool:
    """在检索框输入关键词并提交，返回是否到达结果页。"""
    # 填检索框
    filled = False
    for sel in _INPUT_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                await page.fill(sel, query)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        return False

    # 点检索按钮（失败则回车兜底）
    clicked = False
    for sel in _BUTTON_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                await page.locator(sel).first.click()
                clicked = True
                break
        except Exception:
            pass
    if not clicked:
        try:
            await page.press(_INPUT_SELECTORS[0], "Enter")
        except Exception:
            return False

    # 等结果容器出现
    await page.wait_for_timeout(4500)
    for marker in _RESULT_MARKERS:
        try:
            if await page.locator(marker).count() > 0:
                return True
        except Exception:
            pass
    return False


async def _restrict_to_db(page, db: str) -> None:
    """点击对应文献类型标签页（如"学术期刊"），缩小结果范围。失败则忽略。"""
    label = _DB_TAB_TEXT.get(db)
    if not label:
        return
    try:
        tab = page.locator("a:has-text('{}')".format(label)).first
        if await tab.count() > 0 and await tab.is_visible():
            await tab.click()
            await page.wait_for_timeout(3000)
    except Exception:
        pass


async def _extract_rows(page, max_count: int) -> List[Dict]:
    """从当前结果页提取论文列表（含引用量）。"""

    async def first_text(row, selectors: List[str]) -> str:
        for sel in selectors:
            try:
                el = await row.query_selector(sel)
                if el:
                    text = (await el.text_content() or "").strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""

    papers = []

    for row_sel in _ROW_SELECTORS:
        rows = await page.query_selector_all(row_sel)
        if not rows:
            continue

        for row in rows:
            if len(papers) >= max_count:
                break
            try:
                # 标题（必须有）
                title_el = None
                for sel in _FIELD_SELECTORS["title"]:
                    title_el = await row.query_selector(sel)
                    if title_el:
                        break
                if not title_el:
                    continue

                title = (await title_el.text_content() or "").strip()
                if not title or len(title) < 4:
                    continue

                href  = (await title_el.get_attribute("href") or "").strip()
                year      = await first_text(row, _FIELD_SELECTORS["year"])
                journal   = await first_text(row, _FIELD_SELECTORS["journal"])
                authors   = await first_text(row, _FIELD_SELECTORS["authors"])
                cite_text = await first_text(row, _FIELD_SELECTORS["citations"])

                # 年份：只保留 4 位数字
                m = re.search(r"\d{4}", year)
                year = m.group() if m else ""

                # 引用量：提取数字
                cm = re.search(r"\d+", cite_text)
                citations = int(cm.group()) if cm else 0

                papers.append({
                    "title":     title,
                    "href":      href,
                    "year":      year,
                    "journal":   journal,
                    "authors":   authors,
                    "citations": citations,
                    "url":       resolve_cnki_url(href, getattr(page, "url", "https://kns.cnki.net/")),
                })
            except Exception:
                pass

        if papers:
            break  # 第一个有效选择器命中后停止

    return papers


def _filter_by_year(papers: List[Dict], year_start: int, year_end: int, max_n: int) -> List[Dict]:
    """严格过滤年份范围；没有命中时返回空列表，不回退到范围外结果。"""
    filtered = []
    for p in papers:
        y = p.get("year", "")
        if not y.isdigit() or not (year_start <= int(y) <= year_end):
            continue
        filtered.append(p)
        if len(filtered) >= max_n:
            break
    return filtered
