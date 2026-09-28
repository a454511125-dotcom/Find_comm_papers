"""CNKI DOM helpers adapted from the installed cnki-mcp 0.1.0 copy.

Upstream: https://github.com/wuruiqi/cnki-mcp
Source commit: 419aa08142259e7492f9925adf2e9543a07ecedc
Copyright (c) 2026 wuruiqi; MIT, see third_party/CNKI-MCP-LICENSE.
The installed source includes subsequent local CNKI compatibility fixes.
"""

import os
import re
from typing import Dict, List, Optional, Tuple


_DL_BUTTON_SELECTORS = [
    "a#pdfDown",            # PDF下载（首选）
    "li.btn-dlpdf a",
    "a#cajDown",            # CAJ下载（兜底）
    "li.btn-dlcaj a",
    "a:has-text('PDF下载')",
    "a:has-text('CAJ下载')",
    "a[href*='bar.cnki.net/bar/download']",
]


_CAPTCHA_SELECTORS = [
    "#nc_1_wrapper",          # 阿里云盾 NVC 滑块
    ".nc-container",
    ".verify-wrap",
    ".verify-bar-area",
    ".slidercaptcha",
    "#captchaBox",
    ".captcha-box",
    "iframe[src*='captcha']",
]


async def _find_download_btn(page) -> Tuple[Optional[object], str]:
    """
    在页面上查找下载按钮，返回 (element, format_str)。
    format_str 为 'pdf' 或 'caj'。
    """
    for sel in _DL_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                text = await el.text_content() or ""
                fmt = "caj" if ("caj" in sel.lower() or "CAJ" in text) else "pdf"
                return el, fmt
        except Exception:
            pass
    return None, "pdf"
