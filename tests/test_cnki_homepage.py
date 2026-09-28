import asyncio
import pytest
from types import SimpleNamespace
from paper_search_mcp.comm_cnki_host import CNKI_HOME, submit_homepage
from paper_search_mcp.comm_cnki_host import visible_geometry


@pytest.mark.parametrize("styles,rect,expected", [
    ([{"opacity":"0"}], [50,50,300,150], False),
    ([{"opacity":"1"}], [0,-1000000,300,150], False),
    ([{"opacity":"1"},{"display":"none"}], [50,50,300,150], False),
    ([{"visibility":"hidden"}], [50,50,300,150], False),
    ([{"opacity":"1"}], [50,50,300,150], True),
])
def test_only_visible_challenges_interrupt_search(styles, rect, expected):
    assert visible_geometry({"ancestors":styles,"rect":rect,"viewport":[1280,720]}) is expected


class Page:
    def __init__(self, context, popup=None, field_present=True):
        self.context, self.popup, self.field_present = context, popup, field_present
        self.actions = []

    def locator(self, selector):
        self.actions.append(("selector", selector))
        page = self
        class Locator:
            async def count(self):
                return int(page.field_present)
            async def fill(self, value):
                page.actions.append(("fill", value))
            @property
            def first(self):
                return self
            async def click(self, **kwargs):
                page.actions.append(("click", selector))
                if page.popup:
                    page.context.pages.append(page.popup)
        return Locator()

    async def wait_for_timeout(self, _):
        pass

    async def wait_for_load_state(self, *args, **kwargs):
        self.actions.append(("load", args[0]))


def test_homepage_submission_uses_live_textarea_and_button():
    ctx = SimpleNamespace(pages=[])
    page = Page(ctx)
    ctx.pages.append(page)
    target, submitted = asyncio.run(submit_homepage(page, ctx, "算法推荐"))
    assert CNKI_HOME == "https://www.cnki.net/"
    assert target is page and submitted
    assert ("selector", "textarea#txt_SearchText") in page.actions
    assert ("fill", "算法推荐") in page.actions
    assert ("click", ".search-form .search-btn") in page.actions


def test_homepage_submission_follows_result_popup():
    ctx = SimpleNamespace(pages=[])
    popup = Page(ctx)
    page = Page(ctx, popup=popup)
    ctx.pages.append(page)
    target, submitted = asyncio.run(submit_homepage(page, ctx, "政治极化"))
    assert target is popup and submitted
    assert ("load", "domcontentloaded") in popup.actions


def test_missing_form_does_not_submit_or_guess_an_endpoint():
    ctx = SimpleNamespace(pages=[])
    page = Page(ctx, field_present=False)
    target, submitted = asyncio.run(submit_homepage(page, ctx, "算法推荐"))
    assert target is page and not submitted
    assert not any(action[0] in {"click", "fill"} for action in page.actions)
