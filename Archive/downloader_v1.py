#!/usr/bin/env python3
"""
WeebCentral Chapter Downloader
--------------------------------
Downloads all images from a single WeebCentral chapter URL,
including images injected by JavaScript (lazy-loaded).

Usage:
    python downloader.py <chapter_url>

Example:
    python downloader.py "https://weebcentral.com/chapters/01J.../images?reading_style=long_strip"

Or just run it and paste the URL when prompted:
    python downloader.py

Requirements (install via pip):
    playwright, requests, Pillow
    playwright install chromium   (run once after pip install)
"""

import os
import re
import sys
import time
import logging
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wc_dl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    """Remove characters that are illegal in directory / file names."""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def natural_sort_key(text: str):
    """Sort '2.jpg' before '10.jpg'."""
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", text)]


def guess_extension(url: str, content_type: str = "") -> str:
    """Return a sensible image extension."""
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
    # Fall back to URL path
    path = urlparse(url).path
    suffix = path.rsplit(".", 1)[-1].lower()
    if suffix in ext_map.values():
        return suffix
    return "jpg"


# ---------------------------------------------------------------------------
# Step 1 – Use Playwright to render the page and collect image URLs
# ---------------------------------------------------------------------------

def collect_image_urls(chapter_url: str) -> list[str]:
    """
    Open the chapter page in a headless Chromium browser, scroll to the
    bottom (triggering all lazy-load events), then collect every image src
    that looks like a real manga page.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log.info("Launching headless browser …")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,  # allow self-signed / untrusted certs
        )
        page = context.new_page()

        # ── Intercept network requests so we can also capture image URLs
        #    that are fetched dynamically (XHR / fetch / img src swap).
        intercepted: list[str] = []

        def on_request(request):
            if request.resource_type == "image":
                url = request.url
                if _looks_like_manga_image(url):
                    intercepted.append(url)

        page.on("request", on_request)

        log.info(f"Navigating to: {chapter_url}")
        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            log.warning("Page load timed out – continuing with what loaded so far.")

        # ── Wait for the first image to appear ──────────────────────────────
        try:
            page.wait_for_selector("img[src]", timeout=15_000)
        except PWTimeout:
            log.warning("No <img src> appeared within 15 s – page may be empty.")

        # ── Slow-scroll to trigger lazy loaders ────────────────────────────
        log.info("Scrolling page to trigger lazy-load …")
        _scroll_to_bottom(page)

        # ── Extra wait for any deferred requests ───────────────────────────
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass  # fine – we already have images

        # ── Collect URLs from the DOM ───────────────────────────────────────
        dom_urls: list[str] = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            const urls = [];
            imgs.forEach(img => {
                // real src first, then common lazy-load attributes
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

    # ── Merge intercepted + DOM lists, deduplicate, filter ─────────────────
    all_urls = list(dict.fromkeys(intercepted + dom_urls))  # preserves order
    manga_urls = [u for u in all_urls if _looks_like_manga_image(u)]

    log.info(f"Found {len(manga_urls)} candidate image URLs "
             f"({len(intercepted)} intercepted, {len(dom_urls)} from DOM)")
    return manga_urls


def _scroll_to_bottom(page, step: int = 800, pause: float = 0.4):
    """Scroll the page in increments so lazy loaders fire."""
    total_height: int = page.evaluate("document.body.scrollHeight")
    scrolled = 0
    while scrolled < total_height:
        page.evaluate(f"window.scrollBy(0, {step})")
        time.sleep(pause)
        scrolled += step
        # Page may grow as images load
        total_height = page.evaluate("document.body.scrollHeight")


def _looks_like_manga_image(url: str) -> bool:
    """Heuristic filter – skip icons, avatars, logos, data-URIs, etc."""
    if not url or url.startswith("data:"):
        return False
    lower = url.lower()
    # Must look like an image
    if not any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        # Some CDNs serve images without extension – allow those too
        # but only if they're not obviously UI assets
        if any(skip in lower for skip in ("icon", "logo", "avatar", "favicon",
                                           "banner", "badge", "button", "sprite")):
            return False
        # If no extension AND no obvious image CDN pattern, skip
        if not any(cdn in lower for cdn in ("cdn", "img", "image", "media", "static",
                                             "upload", "storage", "manga")):
            return False
    # Skip known UI / ad assets
    skip_keywords = ("icon", "logo", "favicon", "avatar", "ad_", "/ads/",
                     "banner_small", "pixel.gif", "spacer")
    return not any(kw in lower for kw in skip_keywords)


# ---------------------------------------------------------------------------
# Step 2 – Derive a folder name from the page title / URL
# ---------------------------------------------------------------------------

def get_folder_name(chapter_url: str) -> str:
    """
    Try to build a descriptive folder name from the URL path.
    WeebCentral chapter URLs look like:
      https://weebcentral.com/chapters/<id>/images  or
      https://weebcentral.com/chapters/<id>
    We'll use the chapter ID portion as a fallback.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=20_000)
            title = page.title()
            browser.close()
        if title:
            # Strip common suffixes like " | WeebCentral"
            title = re.split(r"\s*[|–—]\s*", title)[0].strip()
            return sanitize(title) or "chapter"
    except Exception:
        pass

    # Fallback: use last non-empty URL path segment
    parts = [p for p in urlparse(chapter_url).path.split("/") if p]
    # Drop 'images' suffix if present
    if parts and parts[-1].lower() == "images":
        parts = parts[:-1]
    return sanitize(parts[-1]) if parts else "chapter"


# ---------------------------------------------------------------------------
# Step 3 – Download images
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
    """Download each URL sequentially into out_dir. Returns count of successes."""
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = referer

    success = 0
    pad = len(str(len(image_urls)))  # for zero-padded filenames

    for idx, url in enumerate(image_urls, start=1):
        filepath = None  # determined after HEAD / first chunk
        try:
            resp = session.get(url, timeout=20, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            ext = guess_extension(url, content_type)
            filename = f"{idx:0{pad}d}.{ext}"
            filepath = out_dir / filename

            if filepath.exists() and filepath.stat().st_size > 0:
                log.info(f"  [{idx}/{len(image_urls)}] skip (exists): {filename}")
                success += 1
                continue

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65_536):
                    f.write(chunk)

            size_kb = filepath.stat().st_size // 1024
            log.info(f"  [{idx}/{len(image_urls)}] saved {filename}  ({size_kb} KB)")
            success += 1

            # Polite delay between requests
            time.sleep(0.5)

        except requests.HTTPError as e:
            log.warning(f"  [{idx}/{len(image_urls)}] HTTP error {e.response.status_code}: {url}")
        except Exception as e:
            log.warning(f"  [{idx}/{len(image_urls)}] failed: {e}")
            if filepath and filepath.exists() and filepath.stat().st_size == 0:
                filepath.unlink(missing_ok=True)

    return success


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        chapter_url = sys.argv[1].strip()
    else:
        chapter_url = input("Paste chapter URL: ").strip()

    if not chapter_url:
        log.error("No URL provided. Exiting.")
        sys.exit(1)

    if not chapter_url.startswith(("http://", "https://")):
        chapter_url = "https://" + chapter_url

    # ── Derive output folder ────────────────────────────────────────────────
    log.info("Detecting chapter title …")
    folder_name = get_folder_name(chapter_url)
    out_dir = Path("downloads") / folder_name
    log.info(f"Output directory: {out_dir}")

    # ── Collect image URLs ──────────────────────────────────────────────────
    image_urls = collect_image_urls(chapter_url)

    if not image_urls:
        log.error("No manga images found. The URL may be wrong, or the site "
                  "requires login / has changed its structure.")
        sys.exit(1)

    log.info(f"Preparing to download {len(image_urls)} images into '{out_dir}' …")

    # ── Download ────────────────────────────────────────────────────────────
    saved = download_images(image_urls, out_dir, referer=chapter_url)

    log.info(f"Done. {saved}/{len(image_urls)} images saved to '{out_dir}'")


if __name__ == "__main__":
    main()
