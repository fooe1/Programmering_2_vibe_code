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
    Two strategies tried in order:

    A) API interception: some manga fire a fetch/XHR to a chapter-list
       endpoint. We intercept the request URL, re-fetch it with requests,
       and parse the JSON array.

    B) DOM scrolling: for manga where chapters are rendered in a
       virtualised list (only visible items exist in DOM), we scroll
       the chapter list element slowly and collect every link that appears.
       We keep scrolling until 5 consecutive passes find nothing new.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    manga_id = [p for p in urlparse(series_url).path.split("/") if p][-1]
    log.info(f"Manga ID: {manga_id}")
    log.info(f"Fetching chapter list from: {series_url}")

    api_urls_seen: list[str] = []
    captured_cookies: list[dict] = []
    manga_title = "manga"

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)
        page = context.new_page()

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

        captured_cookies = context.cookies()

        # ── Strategy A: try API URLs ────────────────────────────────────────
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

        # ── Strategy B: scroll the chapter list in the DOM ─────────────────
        log.info("No API data – using DOM scroll strategy …")

        collected: dict[str, str] = {}  # chapter_id -> title

        def clean_title(raw_text: str) -> str:
            """
            Extract chapter title from raw link text.
            Input looks like: "Chapter 20\n10 months ago"
            We want only: "Chapter 20"
            """
            lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
            for line in lines:
                m = re.match(r"(Chapter\s+[\d]+(?:\.[\d]+)?)", line, re.I)
                if m:
                    return m.group(1)
            return lines[0] if lines else raw_text.strip()

        def scrape_visible_links() -> int:
            """Collect all chapter links currently in the DOM."""
            try:
                links = page.evaluate(f"""
                () => {{
                    const anchors = document.querySelectorAll('a[href*="/read/{manga_id}/"]');
                    const out = [];
                    anchors.forEach(a => {{
                        out.push({{
                            href: a.href.split('#')[0],
                            text: a.innerText || a.textContent || ''
                        }});
                    }});
                    return out;
                }}
                """)
            except Exception:
                return 0

            new_found = 0
            for item in links:
                href = item.get("href", "")
                parts = [p for p in urlparse(href).path.split("/") if p]
                if len(parts) < 2:
                    continue
                chap_id = parts[-1]
                if chap_id == manga_id:
                    continue
                if chap_id not in collected:
                    title = clean_title(item.get("text", ""))
                    collected[chap_id] = title or f"Chapter {chap_id}"
                    new_found += 1
            return new_found

        # ── Scroll the window from top to bottom collecting chapter links ────
        # The chapter list on atsu.moe uses a virtualised renderer that swaps
        # DOM rows as the WINDOW scrolls. There is no inner scrollable container
        # (all ancestors have overflow:visible). We scroll window.scrollY from
        # 0 to document.body.scrollHeight in small steps, scraping links after
        # each step. We stop when we reach the bottom of the document.

        # First scroll back to top to make sure we start from the beginning
        try:
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.3)
        except Exception:
            pass

        scrape_visible_links()
        log.info(f"  Initial scrape: {len(collected)} chapters")

        SCROLL_STEP = 120   # small steps so virtualised rows don't get skipped
        PAUSE       = 0.15  # seconds between steps

        while True:
            try:
                scroll_y      = page.evaluate("window.scrollY")
                total_height  = page.evaluate("document.body.scrollHeight")
                client_height = page.evaluate("window.innerHeight")
            except Exception:
                break

            at_bottom = scroll_y + client_height >= total_height - 5

            try:
                page.evaluate(f"window.scrollBy(0, {SCROLL_STEP})")
            except Exception:
                break

            time.sleep(PAUSE)

            new = scrape_visible_links()
            if new > 0:
                log.info(f"  +{new} chapters (total: {len(collected)}, y: {scroll_y}/{total_height})")

            if at_bottom:
                # Reached the true bottom – do one final scrape and stop
                scrape_visible_links()
                break

        log.info(f"  Scroll complete. Total: {len(collected)} chapters")
        browser.close()

    if not collected:
        log.error("Could not find any chapters.")
        return manga_title, []

    log.info(f"DOM scrolling found {len(collected)} chapters total.")
    raw = [{"id": cid, "title": title} for cid, title in collected.items()]
    return _build_chapter_list(raw, manga_id, manga_title)


def _build_chapter_list(raw: list[dict], manga_id: str,
                         manga_title: str) -> tuple[str, list[dict]]:
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
    Open the chapter in a fresh browser, intercept image requests fired
    during the INITIAL page load only (before any scrolling), then scroll
    carefully until the page height stabilises.

    Key fix for duplicate images: we block the page from navigating away
    by aborting navigation requests after the initial load. This prevents
    atsu.moe from appending the next chapter and keeps our image list clean.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    # Only collect images whose URL contains the chapter ID
    # This is the most reliable way to avoid collecting images from
    # adjacent chapters that get preloaded.
    chapter_id = [p for p in urlparse(chapter_url).path.split("/") if p][-1]

    intercepted: list[str] = []
    initial_load_done = False

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)

        # Block image requests for other chapters to prevent cross-contamination
        def handle_route(route):
            url = route.request.url
            req_type = route.request.resource_type
            # After initial load, block navigations away from this chapter
            if initial_load_done and req_type == "document":
                try:
                    route.abort()
                except Exception:
                    pass
                return
            try:
                route.continue_()
            except Exception:
                pass

        context.route("**/*", handle_route)

        page = context.new_page()

        def on_request(request):
            if request.resource_type == "image":
                url = request.url
                if _looks_like_manga_image(url) and url not in intercepted:
                    intercepted.append(url)

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

        initial_load_done = True

        # Scroll carefully – stop when height stabilises
        _scroll_until_stable(page, chapter_url)

        # Short extra wait for trailing requests
        time.sleep(1.0)

        # Also read src attributes from DOM as backup
        try:
            dom_urls: list[str] = page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img');
                const urls = [];
                imgs.forEach(img => {
                    const src = img.src
                        || img.getAttribute('data-src')
                        || img.getAttribute('data-lazy')
                        || img.getAttribute('data-original')
                        || '';
                    if (src && !src.startsWith('data:')) urls.push(src);
                });
                return urls;
            }
            """)
        except Exception:
            dom_urls = []

        browser.close()

    # Merge and deduplicate – intercepted list is authoritative for order
    all_urls = list(dict.fromkeys(intercepted + dom_urls))
    manga_urls = [u for u in all_urls if _looks_like_manga_image(u)]
    log.info(f"  Found {len(manga_urls)} images "
             f"({len(intercepted)} intercepted, {len(dom_urls)} from DOM)")
    return manga_urls


def _scroll_until_stable(page, chapter_url: str,
                          step: int = 600, pause: float = 0.6,
                          stable_rounds: int = 4):
    """
    Scroll down until the page height stops growing.
    Every JS call is wrapped in try/except so a page navigation
    (atsu.moe loading the next chapter) doesn't crash the program.
    """
    try:
        prev_height: int = page.evaluate("document.body.scrollHeight")
    except Exception:
        return

    stable = 0
    while True:
        try:
            page.evaluate(f"window.scrollBy(0, {step})")
        except Exception:
            break

        time.sleep(pause)

        try:
            new_height: int = page.evaluate("document.body.scrollHeight")
        except Exception:
            break  # page navigated away – stop scrolling

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
