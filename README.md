# WeebCentral Chapter Downloader

A minimal, single-chapter manga downloader that handles JavaScript-injected images
by running a real headless browser (Playwright + Chromium).

---

## Setup (Arch Linux)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install the Chromium browser Playwright needs (one-time)
playwright install chromium
```

---

## Usage

```bash
# Option A – pass the URL directly
python downloader.py "https://weebcentral.com/chapters/<id>/images?reading_style=long_strip"

# Option B – run and paste when prompted
python downloader.py
```

Images are saved to:
```
downloads/<chapter-title>/001.jpg
downloads/<chapter-title>/002.jpg
...
```

---

## How it works

1. **Playwright** opens a real headless Chromium browser and navigates to the chapter URL.
2. It **intercepts every image network request** as the page loads.
3. It **slow-scrolls** the page to trigger lazy-loaders and JS image injections.
4. It also reads `src`, `data-src`, `data-lazy`, and other common lazy-load attributes
   directly from the DOM after JS has run.
5. All collected URLs are deduplicated and filtered (removes icons, logos, etc.).
6. Images are downloaded sequentially with `requests`, zero-padded and saved in order.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `playwright install` fails | Make sure you're inside the venv first |
| 0 images found | Try the URL with `?reading_style=long_strip` appended |
| Images are 403 | The site may have added Cloudflare – open an issue |
| Wrong images included | Adjust `_looks_like_manga_image()` in downloader.py |
