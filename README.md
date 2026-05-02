# Manga Downloader

A manga downloader for [atsu.moe](https://atsu.moe) built as a school assignment in vibe coding. Downloads chapters with full JavaScript rendering support, a web-based GUI, and parallel image downloading.

---

## Features

- **Web GUI** — paste a URL, see the cover and chapter list, download with one click
- **Full chapter detection** — handles virtualised chapter lists and the `?filter=all` endpoint
- **Parallel downloads** — 4 images at a time for faster chapter downloads
- **Chapter selection** — download all or pick specific chapters via ranges (`1-10`, `5-`, `1,3,5`)
- **JS rendering** — uses a real headless Chromium browser to handle lazy-loaded images
- **Smart filtering** — ignores comment attachments, profile pictures, GIFs, and UI elements
- **Duplicate handling** — skips multiple scanlation groups for the same chapter number
- **Resume support** — skips already-downloaded chapters automatically

---

## Setup

**Requirements:** Python 3.11+, Arch Linux (or any Linux distro)

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/manga-downloader
cd manga-downloader

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install the Chromium browser Playwright needs (one-time)
playwright install chromium
```

---

## Usage

### Web GUI (recommended)

```bash
python app.py
```

Then open **http://localhost:7337** in your browser.

1. Paste a series URL (`https://atsu.moe/manga/...`)
2. Hit **Fetch** — the cover, title, and chapter list load automatically
3. Click **↓ Download** next to any chapter, or **↓ Download All** for everything

### Command line

```bash
# Download all chapters
python downloader.py "https://atsu.moe/manga/GTyxf"

# Choose which chapters interactively
python downloader.py "https://atsu.moe/manga/GTyxf" --select

# Download a single chapter
python downloader.py "https://atsu.moe/read/GTyxf/PBvnfXlp"
```

**Chapter selection syntax** (shown when using `--select`):

| Input | Result |
|---|---|
| `all` | Every chapter |
| `1-10` | Chapters 1 through 10 |
| `5-` | Chapter 5 to the end |
| `1,3,5` | Chapters 1, 3, and 5 |
| `1-5,10,15-20` | Mix of ranges and singles |

---

## Output structure

```
downloads/
  Manga Title/
    001 - Chapter 1/
      001.webp
      002.webp
      ...
    002 - Chapter 2/
      ...
```

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask web server and GUI |
| `downloader.py` | All scraping and download logic |
| `requirements.txt` | Python dependencies |

---

## How it works

1. The series page is opened in a headless Chromium browser with `?filter=all` appended — this is what the "All chapters" button on the site does
2. Chapter links are collected by slowly scrolling the page while scraping the DOM
3. For each chapter, Playwright opens the reader page and intercepts image requests as the page scrolls
4. Images are filtered to exclude UI elements, comment attachments, profile pictures, and GIFs from external hosts
5. Filtered images are downloaded in parallel using a thread pool with random jitter to avoid rate limiting

---

## Notes

- This project was built for educational purposes as a school assignment in vibe coding
- Only works with atsu.moe — other sites have different structures
- Be respectful of the site: don't run multiple instances at once
- Downloaded files are saved locally and never uploaded anywhere
