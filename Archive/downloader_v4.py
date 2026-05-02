#!/usr/bin/env python3
"""
atsu.moe Manga Downloader
--------------------------
Two modes:

  Single chapter:
      python downloader.py "https://atsu.moe/read/GTyxf/PBvnfXlp"

  Full series (all chapters):
      python downloader.py "https://atsu.moe/manga/GTyxf"

Output structure:
  downloads/
    Manga Title/
      001 - Chapter 1/
        001.webp
      002 - Chapter 2/
        ...

Requirements:
    pip install playwright requests
    playwright install chromium
"""

import re
import sys
import time
import logging
import requests
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("atsu_dl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def guess_extension(url: str, content_type: str = "") -> str:
    ext_map = {
        "image/jpeg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif", "image/avif": "avif",
    }
    for mime, ext in ext_map.items():
        if mime in content_type:
            return ext
    suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
    return suffix if suffix in ext_map.values() else "jpg"


def make_browser_context(pw):
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        ignore_https_errors=True,
    )
    return browser, context


def _looks_like_manga_image(url: str) -> bool:
    if not url or url.startswith("data:"):
        return False
    lower = url.lower()
    skip = ("icon", "logo", "favicon", "avatar", "ad_", "/ads/",
            "banner_small", "pixel.gif", "spacer", "spinner")
    if any(kw in lower for kw in skip):
        return False
    has_ext = any(e in lower for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))
    has_cdn = any(c in lower for c in ("cdn", "img", "image", "media", "static",
                                        "upload", "storage", "manga"))
    return has_ext or has_cdn


# ---------------------------------------------------------------------------
# URL type detection
# ---------------------------------------------------------------------------

def detect_url_type(url: str) -> str:
    path = urlparse(url).path
    if path.startswith("/manga/"):
        return "series"
    if path.startswith("/read/"):
        return "chapter"
    parts = [p for p in path.split("/") if p]
    return "chapter" if len(parts) >= 3 else "series"


# ---------------------------------------------------------------------------
# Series mode – get chapter list
# ---------------------------------------------------------------------------

def get_chapter_list(series_url: str) -> tuple[str, list[dict]]:
    """
    Two-strategy approach:

    Strategy A – API interception (fast, works for large manga like Blue Lock):
      Some manga trigger a fetch/XHR request to a chapter-list API endpoint.
      We intercept the request URL, then re-fetch it with requests.
      The response must be a JSON array whose items have both 'id' and 'title'.

    Strategy B – Virtual DOM scrolling (works for manga like GTyxf):
      Some manga render chapters directly in a virtualised DOM list.
      Only visible chapters exist in the DOM at any time.
      We scroll through the entire chapter list section slowly, collecting
      every /read/ link that appears. We stop when no new links appear
      after a full scroll pass.

    We try A first. If it yields no results, we fall back to B.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    manga_id = [p for p in urlparse(series_url).path.split("/") if p][-1]
    log.info(f"Manga ID: {manga_id}")
    log.info(f"Fetching chapter list from: {series_url}")

    # --- shared state ---
    api_urls_seen: list[str] = []
    captured_cookies: list[dict] = []
    manga_title = "manga"

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)
        page = context.new_page()

        # Intercept fetch/XHR request URLs that contain the manga_id
        def on_request(request):
            if request.resource_type not in ("fetch", "xhr"):
                return
            url = request.url
            if manga_id in url and url not in api_urls_seen:
                api_urls_seen.append(url)
                log.info(f"  Spotted API request: {url}")

        page.on("request", on_request)

        try:
            page.goto(series_url, wait_until="networkidle", timeout=30_000)
        except PWTimeout:
            log.warning("Page load timed out – continuing.")

        # Get title
        try:
            raw = page.title()
            if raw:
                manga_title = re.split(r"\s*[|–—]\s*", raw)[0].strip()
        except Exception:
            pass

        # Click 'Show all chapters' if present
        btn = page.query_selector("button:has-text('Show all chapters')")
        if btn:
            log.info("Clicking 'Show all chapters' …")
            prev = len(api_urls_seen)
            btn.click()
            for _ in range(40):
                time.sleep(0.2)
                if len(api_urls_seen) > prev:
                    break

        log.info(f"API URLs spotted: {api_urls_seen}")

        # ── Strategy A: try API URLs ────────────────────────────────────────
        captured_cookies = context.cookies()

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, */*",
            "Referer": series_url,
        })
        for c in captured_cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

        api_chapters: list[dict] = []
        for api_url in api_urls_seen:
            log.info(f"  Trying API: {api_url}")
            try:
                resp = session.get(api_url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if (isinstance(data, list) and len(data) > 0
                        and "id" in data[0] and "title" in data[0]
                        and len(data) > len(api_chapters)):
                    api_chapters = data
                    log.info(f"  Got {len(data)} chapters from API.")
            except Exception as e:
                log.warning(f"  API fetch failed: {e}")

        if api_chapters:
            browser.close()
            return _build_chapter_list(api_chapters, manga_id, manga_title)

        # ── Strategy B: virtualised DOM scrolling ──────────────────────────
        log.info("API strategy yielded nothing – scrolling chapter list in DOM …")

        # First we need to find the chapter list container and scroll IT,
        # not the whole page. The chapter list is a scrollable element
        # separate from the main page scroll.
        #
        # We identify it by finding the element that contains the most
        # /read/ links. We then scroll that element step by step,
        # collecting links as they appear, until two full passes produce
        # no new links.

        collected: dict[str, str] = {}  # chapter_id -> title

        def scrape_visible_links():
            """Grab all /read/<manga_id>/ links currently in the DOM."""
            links = page.evaluate(f"""
            () => {{
                const anchors = document.querySelectorAll('a[href*="/read/{manga_id}/"]');
                const results = [];
                anchors.forEach(a => {{
                    const href = a.href.split('#')[0];  // strip fragment
                    const text = a.textContent.trim();
                    results.push({{href, text}});
                }});
                return results;
            }}
            """)
            new_found = 0
            for item in links:
                href = item["href"]
                parts = [p for p in urlparse(href).path.split("/") if p]
                if len(parts) < 2:
                    continue
                chap_id = parts[-1]
                if chap_id == manga_id:
                    continue  # skip links that are just the manga page itself
                if chap_id not in collected:
                    # Extract chapter title: look for "Chapter X" in text
                    text = item["text"]
                    m = re.search(r"(Chapter\s+[\d.]+)", text, re.I)
                    title = m.group(1) if m else text.split("\n")[0].strip()
                    collected[chap_id] = title or f"Chapter {chap_id}"
                    new_found += 1
            return new_found

        # Find the scrollable chapter list container.
        # It's the element with the most /read/ links inside it.
        container_selector = _find_chapter_container(page, manga_id)
        log.info(f"  Chapter list container: {container_selector}")

        # Scroll through the container collecting links
        max_passes_without_new = 3
        passes_without_new = 0
        scroll_amount = 400  # px per step inside the container

        scrape_visible_links()
        log.info(f"  After initial scrape: {len(collected)} chapters")

        while passes_without_new < max_passes_without_new:
            # Scroll the container down
            if container_selector:
                page.evaluate(f"""
                () => {{
                    const el = document.querySelector('{container_selector}');
                    if (el) el.scrollTop += {scroll_amount};
                }}
                """)
            else:
                page.evaluate(f"window.scrollBy(0, {scroll_amount})")

            time.sleep(0.3)
            new = scrape_visible_links()

            if new == 0:
                passes_without_new += 1
            else:
                passes_without_new = 0
                log.info(f"  Found {new} new chapters (total: {len(collected)})")

        browser.close()

    if not collected:
        log.error("Could not find any chapters.")
        return manga_title, []

    log.info(f"DOM scrolling found {len(collected)} chapters total.")

    # Convert to list format
    raw_chapters = [
        {"id": chap_id, "title": title}
        for chap_id, title in collected.items()
    ]
    return _build_chapter_list(raw_chapters, manga_id, manga_title)


def _find_chapter_container(page, manga_id: str) -> str:
    """
    Find a CSS selector for the scrollable element that holds the chapter list.
    Returns a selector string, or empty string to fall back to window scroll.
    """
    # Ask JS to find which element contains the most /read/ links
    result = page.evaluate(f"""
    () => {{
        const anchors = [...document.querySelectorAll('a[href*="/read/{manga_id}/"]')];
        if (!anchors.length) return '';

        // Walk up from anchors to find the common scrollable container
        let best = null;
        let bestCount = 0;

        // Check ancestors of the first anchor for overflow:auto/scroll
        let el = anchors[0];
        while (el && el !== document.body) {{
            el = el.parentElement;
            if (!el) break;
            const style = window.getComputedStyle(el);
            const overflow = style.overflowY;
            if (overflow === 'auto' || overflow === 'scroll') {{
                // Count how many anchors are inside this element
                const count = el.querySelectorAll('a[href*="/read/{manga_id}/"]').length;
                if (count > bestCount) {{
                    bestCount = count;
                    best = el;
                }}
            }}
        }}

        if (!best) return '';

        // Build a simple selector from id or class
        if (best.id) return '#' + best.id;
        if (best.className) {{
            const first = best.className.trim().split(' ')[0];
            if (first) return '.' + first;
        }}
        return '';
    }}
    """)
    return result or ""


def _build_chapter_list(raw: list[dict], manga_id: str,
                         manga_title: str) -> tuple[str, list[dict]]:
    """Sort chapters by number and build full URLs."""
    base = "https://atsu.moe"
    chapters = []
    for entry in raw:
        chap_id = entry.get("id", "")
        title   = entry.get("title", f"Chapter {chap_id}")
        url     = f"{base}/read/{manga_id}/{chap_id}"
        chapters.append({"id": chap_id, "title": title, "url": url})

    def _num(ch):
        m = re.search(r"(\d+\.?\d*)", ch["title"])
        return float(m.group(1)) if m else 0

    chapters.sort(key=_num)
    log.info(f"Total: {len(chapters)} chapters for '{manga_title}'")
    return manga_title, chapters


# ---------------------------------------------------------------------------
# Chapter mode – collect image URLs from a single chapter page
# ---------------------------------------------------------------------------

def collect_image_urls(chapter_url: str) -> list[str]:
    """
    Open the chapter page, scroll until page height stops growing,
    collect all manga image URLs.

    Stops when height stabilises rather than scrolling to the absolute
    bottom — this prevents atsu.moe from appending the next chapter.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    intercepted: list[str] = []

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)
        page = context.new_page()

        def on_request(request):
            if request.resource_type == "image":
                if _looks_like_manga_image(request.url):
                    intercepted.append(request.url)

        page.on("request", on_request)

        log.info(f"  Opening: {chapter_url}")
        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("  Page load timed out – continuing.")

        try:
            page.wait_for_selector("img[src]", timeout=15_000)
        except PWTimeout:
            log.warning("  No images appeared within 15 s.")

        _scroll_until_stable(page)

        time.sleep(1.5)  # let trailing image requests finish

        dom_urls: list[str] = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            const urls = [];
            imgs.forEach(img => {
                const src = img.src
                    || img.getAttribute('data-src')
                    || img.getAttribute('data-lazy')
                    || img.getAttribute('data-original')
                    || img.getAttribute('data-url')
                    || '';
                if (src) urls.push(src);
            });
            return urls;
        }
        """)

        browser.close()

    all_urls = list(dict.fromkeys(intercepted + dom_urls))
    manga_urls = [u for u in all_urls if _looks_like_manga_image(u)]
    log.info(f"  Found {len(manga_urls)} images "
             f"({len(intercepted)} intercepted, {len(dom_urls)} from DOM)")
    return manga_urls


def _scroll_until_stable(page, step: int = 600, pause: float = 0.6,
                          stable_rounds: int = 4):
    """
    Scroll down until page height stops growing for stable_rounds checks.
    Prevents triggering next-chapter auto-load at the bottom.
    """
    stable = 0
    prev_height: int = page.evaluate("document.body.scrollHeight")
    while True:
        page.evaluate(f"window.scrollBy(0, {step})")
        time.sleep(pause)
        new_height: int = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            stable += 1
            if stable >= stable_rounds:
                log.info("  Page height stable – done scrolling.")
                break
        else:
            stable = 0
            prev_height = new_height


# ---------------------------------------------------------------------------
# Image downloader
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def download_images(image_urls: list[str], out_dir: Path, referer: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = referer

    success = 0
    pad = len(str(len(image_urls)))

    for idx, url in enumerate(image_urls, start=1):
        filepath = None
        try:
            resp = session.get(url, timeout=20, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            ext = guess_extension(url, content_type)
            filename = f"{idx:0{pad}d}.{ext}"
            filepath = out_dir / filename

            if filepath.exists() and filepath.stat().st_size > 0:
                log.info(f"    [{idx}/{len(image_urls)}] skip: {filename}")
                success += 1
                continue

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    f.write(chunk)

            size_kb = filepath.stat().st_size // 1024
            log.info(f"    [{idx}/{len(image_urls)}] {filename}  ({size_kb} KB)")
            success += 1
            time.sleep(0.5)

        except requests.HTTPError as e:
            log.warning(f"    [{idx}/{len(image_urls)}] HTTP {e.response.status_code}: {url}")
        except Exception as e:
            log.warning(f"    [{idx}/{len(image_urls)}] failed: {e}")
            if filepath and filepath.exists() and filepath.stat().st_size == 0:
                filepath.unlink(missing_ok=True)

    return success


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        url = sys.argv[1].strip().strip("'\"")
    else:
        url = input("Paste series or chapter URL: ").strip().strip("'\"")

    if not url:
        log.error("No URL provided.")
        sys.exit(1)

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.split("#")[0]

    mode = detect_url_type(url)
    log.info(f"Mode: {mode}")

    if mode == "series":
        manga_title, chapters = get_chapter_list(url)

        if not chapters:
            log.error("No chapters found. Exiting.")
            sys.exit(1)

        manga_dir = Path("downloads") / sanitize(manga_title)
        log.info(f"'{manga_title}'  —  {len(chapters)} chapters  —  {manga_dir}")

        for i, chapter in enumerate(chapters, 1):
            chap_title  = sanitize(chapter["title"])
            folder_name = f"{i:03d} - {chap_title}"
            out_dir     = manga_dir / folder_name

            log.info(f"[{i}/{len(chapters)}] {chapter['title']}")

            if out_dir.exists() and any(out_dir.iterdir()):
                log.info("  Already downloaded – skipping.")
                continue

            image_urls = collect_image_urls(chapter["url"])

            if not image_urls:
                log.warning("  No images found – skipping.")
                continue

            saved = download_images(image_urls, out_dir, referer=chapter["url"])
            log.info(f"  Saved {saved}/{len(image_urls)} images → '{out_dir}'")
            time.sleep(1.0)

        log.info(f"All done. Files saved under '{manga_dir}'")

    else:
        parts = [p for p in urlparse(url).path.split("/") if p]
        folder_name = sanitize(parts[-1]) if parts else "chapter"
        out_dir = Path("downloads") / folder_name

        log.info(f"Output directory: {out_dir}")
        image_urls = collect_image_urls(url)

        if not image_urls:
            log.error("No images found. Check the URL and try again.")
            sys.exit(1)

        log.info(f"Downloading {len(image_urls)} images …")
        saved = download_images(image_urls, out_dir, referer=url)
        log.info(f"Done. {saved}/{len(image_urls)} images saved to '{out_dir}'")


if __name__ == "__main__":
    main()
