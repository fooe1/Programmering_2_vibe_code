#!/usr/bin/env python3
"""
atsu.moe Manga Downloader
--------------------------
Two modes:

  Single chapter:
      python downloader.py "https://atsu.moe/read/0OElQ/TBulw77i"

  Full series (all chapters):
      python downloader.py "https://atsu.moe/title/0OElQ/some-manga-name"

Output structure:
  downloads/
    Manga Title/
      001 - Chapter 1/
        001.jpg
        002.jpg
      002 - Chapter 2/
        001.jpg
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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
    """Remove characters that are illegal in directory/file names."""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def guess_extension(url: str, content_type: str = "") -> str:
    """Return a sensible image file extension."""
    ext_map = {
        "image/jpeg": "jpg",
        "image/png":  "png",
        "image/webp": "webp",
        "image/gif":  "gif",
        "image/avif": "avif",
    }
    for mime, ext in ext_map.items():
        if mime in content_type:
            return ext
    suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
    if suffix in ext_map.values():
        return suffix
    return "jpg"


def make_browser_context(pw):
    """Create a standard Playwright browser context used throughout."""
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


def _scroll_to_bottom(page, step: int = 800, pause: float = 0.4):
    """Scroll the page slowly so lazy loaders fire."""
    total_height: int = page.evaluate("document.body.scrollHeight")
    scrolled = 0
    while scrolled < total_height:
        page.evaluate(f"window.scrollBy(0, {step})")
        time.sleep(pause)
        scrolled += step
        total_height = page.evaluate("document.body.scrollHeight")


def _looks_like_manga_image(url: str) -> bool:
    """Filter out icons, logos, UI elements – keep only real page images."""
    if not url or url.startswith("data:"):
        return False
    lower = url.lower()
    skip_keywords = ("icon", "logo", "favicon", "avatar", "ad_", "/ads/",
                     "banner_small", "pixel.gif", "spacer", "spinner")
    if any(kw in lower for kw in skip_keywords):
        return False
    has_image_ext = any(ext in lower for ext in
                        (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))
    has_cdn_pattern = any(cdn in lower for cdn in
                          ("cdn", "img", "image", "media", "static",
                           "upload", "storage", "manga"))
    return has_image_ext or has_cdn_pattern


# ---------------------------------------------------------------------------
# URL type detection
# ---------------------------------------------------------------------------

def detect_url_type(url: str) -> str:
    """
    Returns 'series' or 'chapter' based on the URL path.

    atsu.moe patterns:
      Series : https://atsu.moe/manga/<id>
      Chapter: https://atsu.moe/read/<manga_id>/<chapter_id>
    """
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
    Navigate to the series page, click 'Show all chapters', intercept the
    JSON response, and return (manga_title, list_of_chapters).

    Each chapter dict has: { 'id', 'title', 'url' }

    URL pattern: https://atsu.moe/read/<manga_id>/<chapter_id>
    The manga_id is the path segment from the series URL (e.g. GTyxf).
    """
    # Extract manga_id from series URL: /manga/GTyxf -> GTyxf
    manga_id = [p for p in urlparse(series_url).path.split("/") if p][-1]
    log.info(f"Manga ID: {manga_id}")
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log.info(f"Fetching chapter list from: {series_url}")

    chapters_json: list[dict] = []
    scanlation_id: str = ""

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)
        page = context.new_page()

        # Intercept the JSON fetch that fires when "Show all chapters" is clicked
        def on_response(response):
            nonlocal chapters_json, scanlation_id
            if response.status != 200:
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            try:
                data = response.json()
                if isinstance(data, list) and len(data) > 0 and "id" in data[0]:
                    chapters_json = data
                    scanlation_id = data[0].get("scanlationMangaId", "")
                    log.info(f"Intercepted chapter list JSON ({len(data)} chapters)")
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(series_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("Series page load timed out – continuing anyway.")

        # Get manga title from page
        manga_title = "manga"
        try:
            raw_title = page.title()
            if raw_title:
                manga_title = re.split(r"\s*[|–—]\s*", raw_title)[0].strip()
        except Exception:
            pass

        # Find and click "Show all chapters"
        try:
            log.info("Looking for 'Show all chapters' button …")
            btn = page.get_by_role("button", name=re.compile(r"show all chapters", re.I))
            btn.wait_for(timeout=10_000)
            btn.click()
            log.info("Clicked 'Show all chapters'")
        except Exception:
            try:
                page.click("text=Show all chapters", timeout=8_000)
                log.info("Clicked 'Show all chapters' (fallback)")
            except Exception:
                log.warning("Could not find 'Show all chapters' button – "
                            "will try collecting links from DOM.")

        # Wait up to 10 s for the JSON to arrive
        deadline = time.time() + 10
        while time.time() < deadline and not chapters_json:
            time.sleep(0.3)

        # If JSON interception failed, fall back to DOM link collection
        if not chapters_json:
            log.warning("JSON interception failed – falling back to DOM links.")
            links: list[str] = page.evaluate("""
            () => [...document.querySelectorAll('a[href*="/read/"]')]
                    .map(a => a.href)
            """)
            for i, link in enumerate(links, 1):
                parts = [p for p in urlparse(link).path.split("/") if p]
                chap_id = parts[-1] if parts else str(i)
                sid = parts[-2] if len(parts) >= 2 else ""
                chapters_json.append({
                    "id": chap_id,
                    "title": f"Chapter {i:03d}",
                    "scanlationMangaId": sid,
                })
            if not scanlation_id and chapters_json:
                scanlation_id = chapters_json[0].get("scanlationMangaId", "")

        browser.close()

    if not chapters_json:
        log.error("Could not retrieve chapter list.")
        return manga_title, []

    # Build full URLs using the manga_id from the series URL
    # Pattern: https://atsu.moe/read/<manga_id>/<chapter_id>
    base = "https://atsu.moe"
    chapters = []
    for entry in chapters_json:
        chap_id = entry.get("id", "")
        title   = entry.get("title", f"Chapter {chap_id}")
        url     = f"{base}/read/{manga_id}/{chap_id}"
        chapters.append({"id": chap_id, "title": title, "url": url})

    def _chap_num(ch):
        m = re.search(r"(\d+\.?\d*)", ch["title"])
        return float(m.group(1)) if m else 0

    chapters.sort(key=_chap_num)
    log.info(f"Found {len(chapters)} chapters for '{manga_title}'")
    return manga_title, chapters


# ---------------------------------------------------------------------------
# Chapter mode – collect image URLs from one chapter page
# ---------------------------------------------------------------------------

def collect_image_urls(chapter_url: str) -> list[str]:
    """
    Open the chapter page in headless Chromium, scroll to trigger lazy loaders,
    and return all manga image URLs (intercepted network requests + DOM scan).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    intercepted: list[str] = []

    with sync_playwright() as pw:
        browser, context = make_browser_context(pw)
        page = context.new_page()

        def on_request(request):
            if request.resource_type == "image":
                url = request.url
                if _looks_like_manga_image(url):
                    intercepted.append(url)

        page.on("request", on_request)

        log.info(f"  Opening chapter: {chapter_url}")
        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("  Chapter page load timed out – continuing.")

        try:
            page.wait_for_selector("img[src]", timeout=15_000)
        except PWTimeout:
            log.warning("  No images appeared within 15 s.")

        _scroll_to_bottom(page)

        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

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
    """Download each image sequentially into out_dir. Returns success count."""
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
                log.info(f"    [{idx}/{len(image_urls)}] skip (exists): {filename}")
                success += 1
                continue

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    f.write(chunk)

            size_kb = filepath.stat().st_size // 1024
            log.info(f"    [{idx}/{len(image_urls)}] saved {filename}  ({size_kb} KB)")
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

    # Strip URL fragment (#page:14 etc.) – not needed by the scraper
    url = url.split("#")[0]

    mode = detect_url_type(url)
    log.info(f"Mode: {mode}")

    if mode == "series":
        # ── Download all chapters ───────────────────────────────────────────
        manga_title, chapters = get_chapter_list(url)

        if not chapters:
            log.error("No chapters found. Exiting.")
            sys.exit(1)

        manga_dir = Path("downloads") / sanitize(manga_title)
        log.info(f"'{manga_title}'  —  {len(chapters)} chapters  —  {manga_dir}")

        for i, chapter in enumerate(chapters, 1):
            chap_title   = sanitize(chapter["title"])
            folder_name  = f"{i:03d} - {chap_title}"
            out_dir      = manga_dir / folder_name

            log.info(f"[{i}/{len(chapters)}] {chapter['title']}")

            if out_dir.exists() and any(out_dir.iterdir()):
                log.info("  Already downloaded – skipping.")
                continue

            image_urls = collect_image_urls(chapter["url"])

            if not image_urls:
                log.warning(f"  No images found – skipping.")
                continue

            saved = download_images(image_urls, out_dir, referer=chapter["url"])
            log.info(f"  Saved {saved}/{len(image_urls)} images → '{out_dir}'")
            time.sleep(1.0)

        log.info(f"All done. Files saved under '{manga_dir}'")

    else:
        # ── Single chapter ──────────────────────────────────────────────────
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
