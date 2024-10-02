"""
Bing Search
"""

from pathlib import Path
from typing import List, Optional, Text, Union

from bs4 import BeautifulSoup
from diskcache import Cache
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from yarl import URL

from languru.config import console
from languru.types.web.search import SearchResult
from languru.utils._playwright import (
    handle_captcha_page,
    simulate_human_behavior,
    try_close_page,
)
from languru.utils.bs import drop_no_used_attrs
from languru.utils.crawler import escape_query

HOME_URL = "https://www.bing.com/"

cache = Cache(Path.home().joinpath(".languru/data/cache/web_cache"))


async def search_with_page(
    query: Text,
    page: "Page",
    *,
    num_results: int = 10,
    timeout_ms: int = 60000,
    home_url: Text | URL = URL(HOME_URL),
    screenshot_filepath: Optional[Union[Path, Text]] = None,
    cache_result: Cache = cache,
    close_page: bool = True,
    raise_captcha: bool = False,
    skip_captcha: bool = False,
    manual_solve_captcha: bool = False,  # Default behavior.
    debug: bool = False,
) -> List["SearchResult"]:
    """
    Search for a query on Bing and return the search results.
    """

    query = escape_query(query)

    search_results: List["SearchResult"] = []

    try:
        await page.goto(
            str(home_url),
            timeout=timeout_ms,
            wait_until="domcontentloaded",
        )
        await simulate_human_behavior(page, timeout_ms=timeout_ms)

        # Input text into the search box
        search_box = page.locator("#sb_form_q")
        await search_box.fill(query)

        # Press 'Enter' instead of clicking the search button
        await search_box.press("Enter", timeout=1000)

        # Wait for the results page to load
        await page.wait_for_load_state("domcontentloaded")
        # page.wait_for_selector("#search")

        # Check for CAPTCHA
        if not await handle_captcha_page(
            page,
            raise_captcha=raise_captcha,
            skip_captcha=skip_captcha,
            captcha_manual_solve=manual_solve_captcha,
        ):
            return []

        # Wait for the search results to load
        try:
            await page.wait_for_selector("#b_results", state="visible", timeout=10000)
        except PlaywrightTimeoutError:
            console.print("Could not find search results, skip it.")
            return []
        await simulate_human_behavior(page, timeout_ms=timeout_ms)

        # Get the page content
        content = await page.content()
        content = drop_no_used_attrs(content)

        # Parse the search results
        search_results = parse_search_results(content, debug=debug)

        if screenshot_filepath:
            await page.screenshot(type="jpeg", path=screenshot_filepath)
        cache_result[query] = search_results

    except PlaywrightTimeoutError:
        console.print_exception()
        if screenshot_filepath:
            await page.screenshot(type="jpeg", path=screenshot_filepath)

    if close_page:
        await try_close_page(page)
    return search_results[:num_results]


def parse_search_results(
    html_content: Text | BeautifulSoup, *, debug: bool = False
) -> List["SearchResult"]:
    soup = (
        BeautifulSoup(html_content, "html.parser")
        if isinstance(html_content, Text)
        else html_content
    )
    search_results = []

    # Find all search result divs
    result_divs = soup.find_all("li", class_="b_algo")

    for div in result_divs:
        # Extract URL
        a_tag = div.find("a")
        if not a_tag or "href" not in a_tag.attrs:  # No URL found
            continue
        url: Optional[Text] = a_tag["href"]
        # Extract title
        title_tag = div.find("h2")
        title: Optional[Text] = title_tag.get_text() if title_tag else None

        # More reliable way to extract Description
        # Strategy: find the closest following sibling or div/span
        # after the title that contains text
        description: Optional[Text] = None
        description_tag = div.find("p")
        description = description_tag.get_text(strip=True) if description_tag else None

        if url and title and description:
            result = SearchResult.model_validate(
                {"url": url, "title": title, "description": description}
            )
            search_results.append(result)
        else:
            if debug:
                console.print("")
                console.print(f"Could not parse search result:\n{div=}")
                console.print("")

    return search_results
