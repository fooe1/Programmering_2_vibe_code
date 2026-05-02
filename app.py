#!/usr/bin/env python3
"""
Manga Downloader – Web GUI
--------------------------
Run:  python app.py
Then open: http://localhost:7337
"""

import re
import json
import time
import uuid
import threading
import requests as req
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify, Response, send_file

# Import all core logic from the existing downloader
from downloader import (
    get_chapter_list, collect_image_urls, download_images,
    sanitize, detect_url_type, make_browser_context, HEADERS
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory job store  {job_id: {status, progress, total, message, done}}
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

def job_update(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ---------------------------------------------------------------------------
# Cover image fetching
# ---------------------------------------------------------------------------

def get_manga_cover(series_url: str) -> str | None:
    """
    Return the cover image URL for a manga series page.
    Uses Playwright since atsu.moe is JS-rendered.
    Falls back to None if nothing found.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    try:
        with sync_playwright() as pw:
            browser, context = make_browser_context(pw)
            page = context.new_page()
            try:
                page.goto(series_url, wait_until="domcontentloaded", timeout=20_000)
            except PWTimeout:
                pass

            # Try to grab the largest image on the page that looks like a cover
            cover_url: str = page.evaluate("""
            () => {
                // Look for og:image meta tag first – most reliable
                const og = document.querySelector('meta[property="og:image"]');
                if (og && og.content) return og.content;

                // Fall back to the first large <img> on the page
                const imgs = [...document.querySelectorAll('img[src]')];
                for (const img of imgs) {
                    const src = img.src || '';
                    if (!src || src.startsWith('data:')) continue;
                    const lower = src.toLowerCase();
                    // Skip tiny UI images
                    if (lower.includes('icon') || lower.includes('logo') ||
                        lower.includes('avatar') || lower.includes('favicon')) continue;
                    if (img.naturalWidth > 100 || img.width > 100) return src;
                }
                return '';
            }
            """)
            browser.close()
            return cover_url or None
    except Exception as e:
        return None


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    """
    POST { url: "https://atsu.moe/manga/..." }
    Returns { title, cover, chapters: [{id, title, url}] }
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().strip("'\"")
    if not url:
        return jsonify(error="No URL provided"), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.split("#")[0]

    if detect_url_type(url) != "series":
        return jsonify(error="Please paste a series URL (atsu.moe/manga/...)"), 400

    try:
        # Fetch cover and chapter list concurrently
        cover_result = [None]
        def fetch_cover():
            cover_result[0] = get_manga_cover(url)

        cover_thread = threading.Thread(target=fetch_cover, daemon=True)
        cover_thread.start()

        manga_title, chapters = get_chapter_list(url)
        cover_thread.join(timeout=15)

        if not chapters:
            return jsonify(error="No chapters found. Check the URL."), 404

        return jsonify(
            title=manga_title,
            cover=cover_result[0] or "",
            chapters=[{"id": ch["id"], "title": ch["title"], "url": ch["url"]}
                      for ch in chapters]
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    """
    POST { chapter_url, chapter_title, manga_title, chapter_index }
    Starts a background download job.
    Returns { job_id }
    """
    data = request.get_json(silent=True) or {}
    chapter_url   = data.get("chapter_url", "").strip()
    chapter_title = data.get("chapter_title", "Chapter")
    manga_title   = data.get("manga_title", "manga")
    chapter_index = int(data.get("chapter_index", 1))

    if not chapter_url:
        return jsonify(error="No chapter URL"), 400

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status":   "starting",
            "progress": 0,
            "total":    0,
            "message":  "Starting…",
            "done":     False,
            "error":    None,
        }

    def run_download():
        try:
            job_update(job_id, status="scraping", message="Opening chapter page…")

            image_urls = collect_image_urls(chapter_url)
            if not image_urls:
                job_update(job_id, status="error", message="No images found.",
                           error="No images found.", done=True)
                return

            total = len(image_urls)
            job_update(job_id, status="downloading", total=total,
                       message=f"Downloading {total} images…")

            folder_name = f"{chapter_index:03d} - {sanitize(chapter_title)}"
            out_dir = Path("downloads") / sanitize(manga_title) / folder_name
            out_dir.mkdir(parents=True, exist_ok=True)

            # Wrap download_images to track progress
            import random
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from downloader import _download_one, CONCURRENT_DOWNLOADS, guess_extension

            pad = len(str(total))
            completed = [0]

            def tracked_download(idx, url):
                result = _download_one(idx, total, url, out_dir, chapter_url, pad)
                completed[0] += 1
                job_update(job_id,
                           progress=completed[0],
                           message=f"Downloaded {completed[0]}/{total} images")
                return result

            success = 0
            with ThreadPoolExecutor(max_workers=CONCURRENT_DOWNLOADS) as pool:
                futures = {
                    pool.submit(tracked_download, idx, url): idx
                    for idx, url in enumerate(image_urls, start=1)
                }
                for future in as_completed(futures):
                    if future.result():
                        success += 1

            job_update(job_id,
                       status="done",
                       progress=total,
                       message=f"Saved {success}/{total} images to '{out_dir}'",
                       done=True)

        except Exception as e:
            job_update(job_id, status="error", message=str(e),
                       error=str(e), done=True)

    t = threading.Thread(target=run_download, daemon=True)
    t.start()

    return jsonify(job_id=job_id)


@app.route("/api/progress/<job_id>")
def api_progress(job_id: str):
    """
    Server-Sent Events stream for download progress.
    The browser listens to this and updates the UI in real time.
    """
    def stream():
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break
            yield f"data: {json.dumps(job)}\n\n"
            if job.get("done"):
                break
            time.sleep(0.4)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/proxy-cover")
def proxy_cover():
    """Proxy the cover image to avoid CORS issues in the browser."""
    cover_url = request.args.get("url", "")
    if not cover_url:
        return "", 404
    try:
        r = req.get(cover_url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return Response(r.content,
                        content_type=r.headers.get("content-type", "image/jpeg"))
    except Exception:
        return "", 404


# ---------------------------------------------------------------------------
# Frontend – served as a single HTML page
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Manga Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #0d0d0f;
    --surface:   #141418;
    --border:    #252530;
    --accent:    #e8ff57;
    --accent2:   #57c8ff;
    --text:      #e8e8f0;
    --muted:     #6b6b80;
    --danger:    #ff5757;
    --success:   #57ffb0;
    --radius:    10px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Syne', sans-serif;
    min-height: 100vh;
    padding: 40px 24px 80px;
  }

  /* Subtle grid background */
  body::before {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(var(--border) 1px, transparent 1px),
      linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 48px 48px;
    opacity: 0.35;
    pointer-events: none;
    z-index: 0;
  }

  .wrap {
    position: relative; z-index: 1;
    max-width: 860px;
    margin: 0 auto;
  }

  /* Header */
  header {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 48px;
  }
  header h1 {
    font-size: 2rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--accent);
    text-transform: uppercase;
  }
  header span {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  /* Search bar */
  .search-bar {
    display: flex;
    gap: 10px;
    margin-bottom: 40px;
  }
  .search-bar input {
    flex: 1;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 0.9rem;
    padding: 14px 18px;
    outline: none;
    transition: border-color 0.2s;
  }
  .search-bar input::placeholder { color: var(--muted); }
  .search-bar input:focus { border-color: var(--accent); }
  .search-bar button {
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    color: #0d0d0f;
    font-family: 'Syne', sans-serif;
    font-size: 0.9rem;
    font-weight: 700;
    padding: 14px 28px;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.1s;
    white-space: nowrap;
  }
  .search-bar button:hover { opacity: 0.88; }
  .search-bar button:active { transform: scale(0.97); }
  .search-bar button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Status / error banner */
  .banner {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 18px;
    font-family: 'DM Mono', monospace;
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 32px;
    display: none;
  }
  .banner.error { border-color: var(--danger); color: var(--danger); }
  .banner.visible { display: block; }

  /* Manga info card */
  .manga-card {
    display: none;
    gap: 28px;
    margin-bottom: 36px;
    padding: 24px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
  }
  .manga-card.visible { display: flex; }
  .manga-card img {
    width: 120px;
    height: 170px;
    object-fit: cover;
    border-radius: 8px;
    flex-shrink: 0;
    background: var(--border);
  }
  .manga-card .info { flex: 1; display: flex; flex-direction: column; gap: 10px; }
  .manga-card .title {
    font-size: 1.5rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }
  .manga-card .meta {
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .manga-card .actions {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: auto;
  }
  .btn-all {
    background: var(--accent);
    border: none;
    border-radius: 8px;
    color: #0d0d0f;
    font-family: 'Syne', sans-serif;
    font-size: 0.82rem;
    font-weight: 700;
    padding: 9px 20px;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn-all:hover { opacity: 0.85; }
  .btn-all:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Chapter list */
  .chapters-header {
    display: none;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .chapters-header.visible { display: flex; }
  .chapters-header h2 {
    font-size: 1rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  .chapters-header .count {
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 3px 12px;
  }

  .chapter-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .chapter-row {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: border-color 0.15s;
  }
  .chapter-row:hover { border-color: #383845; }
  .chapter-row.done { border-color: #1a3328; }
  .chapter-row.error-row { border-color: #3d1a1a; }

  .chapter-num {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--muted);
    width: 32px;
    flex-shrink: 0;
    text-align: right;
  }
  .chapter-title {
    flex: 1;
    font-size: 0.92rem;
    font-weight: 600;
  }
  .chapter-status {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--muted);
    min-width: 120px;
    text-align: right;
  }
  .chapter-status.downloading { color: var(--accent2); }
  .chapter-status.done { color: var(--success); }
  .chapter-status.error { color: var(--danger); }

  /* Progress bar inside chapter row */
  .chapter-progress {
    width: 80px;
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
    flex-shrink: 0;
    display: none;
  }
  .chapter-progress.visible { display: block; }
  .chapter-progress-fill {
    height: 100%;
    background: var(--accent2);
    border-radius: 2px;
    transition: width 0.3s ease;
    width: 0%;
  }
  .chapter-row.done .chapter-progress-fill { background: var(--success); }

  /* Download button */
  .btn-dl {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 7px;
    color: var(--text);
    font-family: 'Syne', sans-serif;
    font-size: 0.78rem;
    font-weight: 600;
    padding: 6px 14px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    white-space: nowrap;
  }
  .btn-dl:hover { border-color: var(--accent); color: var(--accent); }
  .btn-dl:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn-dl.done-btn {
    border-color: var(--success);
    color: var(--success);
    opacity: 0.6;
  }

  /* Spinner */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    display: inline-block;
    vertical-align: middle;
  }

  /* Fade-in animation */
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .fade-up { animation: fadeUp 0.3s ease forwards; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>Manga DL</h1>
    <span>atsu.moe downloader</span>
  </header>

  <div class="search-bar">
    <input type="text" id="urlInput"
           placeholder="https://atsu.moe/manga/..."
           autocomplete="off" spellcheck="false">
    <button id="fetchBtn" onclick="fetchManga()">Fetch</button>
  </div>

  <div class="banner" id="banner"></div>

  <!-- Manga card -->
  <div class="manga-card" id="mangaCard">
    <img id="coverImg" src="" alt="Cover">
    <div class="info">
      <div class="title" id="mangaTitle"></div>
      <div class="meta" id="mangaMeta"></div>
      <div class="actions">
        <button class="btn-all" id="downloadAllBtn" onclick="downloadAll()">
          ↓ Download All
        </button>
      </div>
    </div>
  </div>

  <!-- Chapter list -->
  <div class="chapters-header" id="chaptersHeader">
    <h2>Chapters</h2>
    <span class="count" id="chapterCount"></span>
  </div>

  <div class="chapter-list" id="chapterList"></div>

</div>

<script>
let _chapters = [];
let _mangaTitle = '';
let _activeDownloads = 0;

// ── Fetch manga info ─────────────────────────────────────────────────────────
async function fetchManga() {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) return;

  setFetching(true);
  showBanner('Fetching chapters… this takes 10–20 seconds.', false);
  clearResults();

  try {
    const res = await fetch('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    if (!res.ok || data.error) {
      showBanner(data.error || 'Something went wrong.', true);
      return;
    }

    hideBanner();
    _chapters = data.chapters;
    _mangaTitle = data.title;
    renderMangaCard(data);
    renderChapters(data.chapters);

  } catch (e) {
    showBanner('Network error: ' + e.message, true);
  } finally {
    setFetching(false);
  }
}

// Allow pressing Enter in the input
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') fetchManga();
});

// ── Render manga card ────────────────────────────────────────────────────────
function renderMangaCard(data) {
  document.getElementById('mangaTitle').textContent = data.title;
  document.getElementById('mangaMeta').textContent =
    data.chapters.length + ' chapters available';

  const img = document.getElementById('coverImg');
  if (data.cover) {
    img.src = '/api/proxy-cover?url=' + encodeURIComponent(data.cover);
    img.onerror = () => { img.style.display = 'none'; };
  } else {
    img.style.display = 'none';
  }

  document.getElementById('mangaCard').classList.add('visible', 'fade-up');
  document.getElementById('chaptersHeader').classList.add('visible');
  document.getElementById('chapterCount').textContent = data.chapters.length;
}

// ── Render chapter rows ──────────────────────────────────────────────────────
function renderChapters(chapters) {
  const list = document.getElementById('chapterList');
  list.innerHTML = '';

  chapters.forEach((ch, i) => {
    const idx = i + 1;
    const row = document.createElement('div');
    row.className = 'chapter-row fade-up';
    row.id = 'row-' + i;
    row.style.animationDelay = Math.min(i * 18, 400) + 'ms';

    row.innerHTML = `
      <span class="chapter-num">${String(idx).padStart(3, '0')}</span>
      <span class="chapter-title">${escHtml(ch.title)}</span>
      <div class="chapter-progress" id="prog-${i}">
        <div class="chapter-progress-fill" id="progfill-${i}"></div>
      </div>
      <span class="chapter-status" id="status-${i}"></span>
      <button class="btn-dl" id="btn-${i}"
              onclick="downloadChapter(${i})">↓ Download</button>
    `;
    list.appendChild(row);
  });
}

// ── Download a single chapter ────────────────────────────────────────────────
async function downloadChapter(i) {
  const ch = _chapters[i];
  const btn = document.getElementById('btn-' + i);
  const statusEl = document.getElementById('status-' + i);
  const progEl = document.getElementById('prog-' + i);
  const progFill = document.getElementById('progfill-' + i);
  const row = document.getElementById('row-' + i);

  btn.disabled = true;
  btn.textContent = '…';
  statusEl.textContent = 'Starting…';
  statusEl.className = 'chapter-status downloading';
  progEl.classList.add('visible');
  _activeDownloads++;
  updateDownloadAllBtn();

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chapter_url:   ch.url,
        chapter_title: ch.title,
        manga_title:   _mangaTitle,
        chapter_index: i + 1
      })
    });
    const { job_id, error } = await res.json();

    if (error) throw new Error(error);

    // Listen to SSE progress stream
    const evtSrc = new EventSource('/api/progress/' + job_id);
    evtSrc.onmessage = (e) => {
      const job = JSON.parse(e.data);

      if (job.total > 0) {
        const pct = Math.round((job.progress / job.total) * 100);
        progFill.style.width = pct + '%';
        statusEl.textContent = job.progress + '/' + job.total;
      } else {
        statusEl.textContent = job.message || job.status;
      }

      if (job.done) {
        evtSrc.close();
        _activeDownloads--;
        updateDownloadAllBtn();

        if (job.status === 'done') {
          row.classList.add('done');
          statusEl.textContent = '✓ Done';
          statusEl.className = 'chapter-status done';
          progFill.style.width = '100%';
          btn.textContent = '✓';
          btn.className = 'btn-dl done-btn';
          btn.disabled = true;
        } else {
          statusEl.textContent = '✕ ' + (job.error || 'Failed');
          statusEl.className = 'chapter-status error';
          row.classList.add('error-row');
          btn.textContent = '↓ Retry';
          btn.disabled = false;
        }
      }
    };

    evtSrc.onerror = () => {
      evtSrc.close();
      statusEl.textContent = 'Connection lost';
      statusEl.className = 'chapter-status error';
      btn.textContent = '↓ Retry';
      btn.disabled = false;
      _activeDownloads--;
      updateDownloadAllBtn();
    };

  } catch (err) {
    statusEl.textContent = '✕ ' + err.message;
    statusEl.className = 'chapter-status error';
    btn.textContent = '↓ Retry';
    btn.disabled = false;
    _activeDownloads--;
    updateDownloadAllBtn();
  }
}

// ── Download all chapters sequentially ───────────────────────────────────────
async function downloadAll() {
  const btn = document.getElementById('downloadAllBtn');
  btn.disabled = true;
  btn.textContent = 'Downloading…';

  for (let i = 0; i < _chapters.length; i++) {
    const rowBtn = document.getElementById('btn-' + i);
    // Skip already-done chapters
    if (rowBtn && rowBtn.classList.contains('done-btn')) continue;
    await downloadChapter(i);
    // Wait for this download to finish before starting the next
    await waitForDownloadDone(i);
  }

  btn.disabled = false;
  btn.textContent = '✓ All Done';
}

function waitForDownloadDone(i) {
  return new Promise(resolve => {
    const check = () => {
      const row = document.getElementById('row-' + i);
      if (row && (row.classList.contains('done') || row.classList.contains('error-row'))) {
        resolve();
      } else {
        setTimeout(check, 500);
      }
    };
    setTimeout(check, 500);
  });
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function updateDownloadAllBtn() {
  const btn = document.getElementById('downloadAllBtn');
  if (!btn) return;
  if (_activeDownloads > 0) {
    btn.disabled = true;
  } else {
    btn.disabled = false;
  }
}

function setFetching(active) {
  const btn = document.getElementById('fetchBtn');
  const input = document.getElementById('urlInput');
  btn.disabled = active;
  input.disabled = active;
  btn.innerHTML = active
    ? '<span class="spinner"></span>'
    : 'Fetch';
}

function showBanner(msg, isError) {
  const b = document.getElementById('banner');
  b.textContent = msg;
  b.className = 'banner visible' + (isError ? ' error' : '');
}

function hideBanner() {
  document.getElementById('banner').className = 'banner';
}

function clearResults() {
  _chapters = [];
  document.getElementById('mangaCard').className = 'manga-card';
  document.getElementById('chaptersHeader').className = 'chapters-header';
  document.getElementById('chapterList').innerHTML = '';
  document.getElementById('coverImg').style.display = '';
  document.getElementById('coverImg').src = '';
}

function escHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return HTML


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │   Manga Downloader GUI                  │")
    print("  │   Open: http://localhost:7337            │")
    print("  │   Press Ctrl+C to stop                  │")
    print("  └─────────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=7337, debug=False, threaded=True)
