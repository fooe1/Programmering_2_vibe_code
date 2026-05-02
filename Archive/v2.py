#!/usr/bin/env python3
"""
Universal Image Downloader (Generic Websites)
--------------------------------------------
Downloads all meaningful images from almost any webpage.

Usage:
    python downloader.py <url>

Requirements:
    playwright, requests
    playwright install chromium
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
log = logging.getLogger("img_dl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def guess_extension(content_type: str, url: str) -> str:
    if "jpeg" in content_type:
        return "jpg"
    if "png" in content_type:
        return "png"
    if "webp" in content_type:
        return "webp"
    if "gif" in content_type:
        return "gif"

    # fallback from URL
    path = urlparse(url).path
    if "." in path:
        return path.rsplit(".", 1)[-1].lower()

    return "jpg"


# ---------------------------------------------------------------------------
# Collect Images (GENERIC)
# ---------------------------------------------------------------------------

def collect_image_urls(url: str) -> list[str]:
    from playwright.sync_api import sync_playwright

    log.info("Launching browser...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        intercepted = []

        def on_request(req):
            if req.resource_type == "image":
                intercepted.append(req.url)

        page.on("request", on_request)

        log.info(f"Opening {url}")
        page.goto(url, wait_until="domcontentloaded")

        time.sleep(2)

        # Scroll to trigger lazy loading
        scroll(page)

        # Collect from DOM
        dom_images = page.evaluate("""
        () => {
            const urls = new Set();

            // IMG tags
            document.querySelectorAll("img").forEach(img => {
                if (img.src) urls.add(img.src);

                ["data-src","data-lazy","data-original"].forEach(attr => {
                    if (img.getAttribute(attr))
                        urls.add(img.getAttribute(attr));
                });
            });

            // Background images
            document.querySelectorAll("*").forEach(el => {
                const style = getComputedStyle(el);
                const bg = style.backgroundImage;
                if (bg && bg.startsWith("url(")) {
                    const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
                    if (match) urls.add(match[1]);
                }
            });

            return Array.from(urls);
        }
        """)

        browser.close()

    # Merge & deduplicate
    all_urls = list(dict.fromkeys(intercepted + dom_images))

    log.info(f"Collected {len(all_urls)} raw image URLs")

    return all_urls


def scroll(page):
    for _ in range(10):
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Download Images (GENERIC)
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "image/*,*/*;q=0.8",
}


def download_images(urls: list[str], out_dir: Path, referer: str):
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = referer

    saved = 0

    for i, url in enumerate(urls, 1):
        try:
            resp = session.get(url, timeout=15, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")

            if "image" not in content_type:
                continue

            # Filter small junk images
            size = int(resp.headers.get("content-length", 0))
            if size < 20_000:
                continue

            ext = guess_extension(content_type, url)
            filename = f"{i:04d}.{ext}"
            path = out_dir / filename

            with open(path, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)

            log.info(f"[{i}] saved {filename} ({size//1024} KB)")
            saved += 1

        except Exception as e:
            log.warning(f"[{i}] failed: {e}")

    log.info(f"Downloaded {saved} images")


# ---------------------------------------------------------------------------
# Folder Name
# ---------------------------------------------------------------------------

def get_folder_name(url: str) -> str:
    parsed = urlparse(url)
    name = parsed.netloc + parsed.path
    return sanitize(name)[:100]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter URL: ").strip()

    if not url.startswith("http"):
        url = "https://" + url

    folder = get_folder_name(url)
    out_dir = Path("downloads") / folder

    log.info(f"Saving to: {out_dir}")

    urls = collect_image_urls(url)

    if not urls:
        log.error("No images found.")
        return

    download_images(urls, out_dir, url)


if __name__ == "__main__":
    main()