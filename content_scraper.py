"""
content_scraper.py
==================
Reads turnbackhoax_articles.csv and scrapes the actual page content
from two URL columns:

  • link_source  → the false/hoax post (1 URL per article)
  • referensi    → the true/evidence sources (1–13 URLs per article,
                   pipe-separated)

Output schema — scraped_content.csv (long format, one row per URL):
  article_id    — row index from the source CSV
  source_type   — "false" | "true"   (always lowercase strings)
  source_index  — 0 for false; 0, 1, 2 … for each referensi URL
  url           — the original URL
  content_type  — "news_site" | "archive" | "social_media" | "skip" | "empty"
  title         — page <title> tag (only meaningful for news_site rows)
  text          — cleaned body text extracted from the page
  scraped_date  — ISO-8601 datetime when this row was fetched
  scrape_status — "ok" | "blocked" | "timeout" | "404" | "error" |
                  "skipped" | "empty_url" | "archive_blocked" | "js_redirect"

── FIXES vs v1 ───────────────────────────────────────────────────────────────
  Fix 1 — source_type forced to lowercase str ("true"/"false", never booleans)
  Fix 2 — archive.ph / archive.today get dedicated "archive_blocked" status
           and use a Referer header trick before giving up
  Fix 3 — JS-redirect pages (TikTok "Please wait...") detected → "js_redirect"
  Fix 4 — Paywall / login-wall boilerplate stripped from Kompas, Kompas.tv,
           and other Indonesian news sites before storing text

── HOW TO RUN ────────────────────────────────────────────────────────────────

  Test mode  (first 5 articles only — run this first):
      Set  MAX_ROWS = 5  in the CONFIGURATION section below, then:
      python content_scraper.py

  Full run   (all 960 rows):
      Set  MAX_ROWS = None,  then:
      python content_scraper.py

── RESUME SUPPORT ────────────────────────────────────────────────────────────
  If interrupted, the scraper resumes automatically from the last saved
  article_id — no rows are re-fetched.

── DEPENDENCIES ──────────────────────────────────────────────────────────────
  pip install requests beautifulsoup4 lxml tqdm pandas
"""

import re
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  ← edit here
# ══════════════════════════════════════════════════════════════════════════════

INPUT_CSV       = "turnbackhoax_articles.csv"
OUTPUT_CSV      = "scraped_content.csv"

MAX_ROWS        = 5        # ← set to None for the full run
DELAY_SECONDS   = 1.5      # polite pause between requests
REQUEST_TIMEOUT = 20       # seconds per request
MAX_RETRIES     = 3        # attempts before giving up on a URL
AUTOSAVE_EVERY  = 50       # write checkpoint every N articles processed

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("content_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS — domain routing
# ══════════════════════════════════════════════════════════════════════════════

# Social media: we attempt the request but can't extract clean article text
SOCIAL_DOMAINS = {
    "web.facebook.com", "facebook.com",
    "tiktok.com", "vt.tiktok.com",
    "instagram.com",
    "x.com", "twitter.com",
    "threads.com", "www.threads.com",
    "youtube.com", "youtu.be",
    "linkedin.com",
}

# Archive/mirror sites: often have the hoax content preserved as plain HTML
ARCHIVE_DOMAINS = {
    "archive.ph", "archive.today", "archive.md",
    "archive.org", "web.archive.org",
    "arsip.cekfakta.com", "webarchive.io",
    "ibb.co", "ibb.co.com",   # image hosts — text will be empty but that's fine
}

# Skip entirely: tool / internal / non-article pages
SKIP_DOMAINS = {
    "hivemoderation.com",       # AI detection tool, not a news page
    "magma.esdm.go.id",         # government monitoring dashboard
    "geologi.esdm.go.id",
    "turnbackhoax.id",          # self-referential link back to MAFINDO
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Fix 2 — archive sites need a Referer header to avoid bot-detection blocks
ARCHIVE_HEADERS = {
    **HEADERS,
    "Referer": "https://www.google.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

# Fix 3 — phrases that indicate a JS-redirect waiting page (no real content)
JS_REDIRECT_MARKERS = [
    "please wait",
    "redirecting",
    "just a moment",
    "enable javascript",
    "checking your browser",
    "loading",
]

# Fix 4 — paywall / login-wall boilerplate phrases found at the START of text
# from Indonesian news sites. If the extracted text begins with any of these
# we strip everything up to the first real sentence.
PAYWALL_PREFIXES = [
    # Kompas.com / Kompas.tv
    "peringatan! materi khusus dewasa",
    "konten ini merupakan konten dewasa",
    "login gabung kompas.com+",
    "konten yang disimpan",
    # Generic login walls
    "masuk untuk melanjutkan",
    "silakan login",
    "daftar sekarang",
    "langganan untuk membaca",
    "subscribe to read",
    "please log in",
    "please sign in",
]

# Column order for the output CSV
OUTPUT_COLUMNS = [
    "article_id",
    "source_type",
    "source_index",
    "url",
    "content_type",
    "title",
    "text",
    "scraped_date",
    "scrape_status",
]

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_domain(url: str) -> str:
    """Extract the bare domain from a URL (no www. prefix)."""
    if not isinstance(url, str):
        return ""
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def classify_url(url: str) -> str:
    """
    Assign a content_type label to a URL before fetching.

    Returns one of:
      "empty"        — no URL stored in the source CSV
      "skip"         — domain is a tool / dashboard / self-link (don't fetch)
      "social_media" — social platform (attempt fetch; extraction may fail)
      "archive"      — wayback / mirror page (attempt fetch; usually works)
      "news_site"    — regular news article (attempt fetch; extract title + body)
    """
    if not isinstance(url, str) or not url.strip().startswith("http"):
        return "empty"
    domain = get_domain(url)
    if domain in SKIP_DOMAINS:
        return "skip"
    if domain in SOCIAL_DOMAINS:
        return "social_media"
    if domain in ARCHIVE_DOMAINS:
        return "archive"
    return "news_site"


def clean_text(text: str) -> str:
    """Collapse whitespace and strip leading/trailing spaces."""
    return re.sub(r"\s+", " ", text).strip()


def is_js_redirect(text: str) -> bool:
    """
    Fix 3 — detect JS-redirect waiting pages (e.g. TikTok "Please wait...").
    Returns True if the page text is essentially just a redirect placeholder.
    """
    if not text:
        return False
    sample = text.lower().strip()[:300]
    # Short page that contains a known redirect phrase → waiting page
    if len(text) < 500:
        for marker in JS_REDIRECT_MARKERS:
            if marker in sample:
                return True
    return False


def strip_paywall_boilerplate(text: str) -> str:
    """
    Fix 4 — remove paywall / login-wall copy that Indonesian news sites
    inject at the top of their pages (Kompas.com, Kompas.tv, etc.).

    Strategy: if the text starts with a known boilerplate phrase, find the
    first sentence that looks like real article content (starts with a capital
    letter after the boilerplate block) and return from there.
    """
    lower = text.lower()
    for prefix in PAYWALL_PREFIXES:
        if lower.startswith(prefix):
            # Find the first occurrence of a capital letter after a period/newline
            # that comes after at least 100 chars (past the boilerplate)
            match = re.search(r"(?<=[.!?])\s+([A-Z\u00C0-\u024F])", text[100:])
            if match:
                cut = 100 + match.start()
                cleaned = text[cut:].strip()
                log.info(f"    ✂ Stripped paywall boilerplate ({cut} chars)")
                return cleaned
            break
    return text


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════════
# FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_page(url: str, use_archive_headers: bool = False) -> tuple[BeautifulSoup | None, str]:
    """
    Fetch a URL with retries.

    Returns (soup, status) where status is one of:
      "ok" | "blocked" | "timeout" | "404" | "error" | "archive_blocked"

    use_archive_headers — pass True for archive.ph / archive.today to send
    a Referer: google.com header that bypasses some bot-detection checks.
    """
    headers = ARCHIVE_HEADERS if use_archive_headers else HEADERS

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            # Treat 403 / 401 as "blocked" — common for social media
            if resp.status_code in (401, 403):
                log.warning(f"  Blocked ({resp.status_code}): {url}")
                # Fix 2 — archive sites get a specific status so we can track them
                status = "archive_blocked" if use_archive_headers else "blocked"
                return None, status

            if resp.status_code == 404:
                log.warning(f"  404 Not Found: {url}")
                return None, "404"

            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            return soup, "ok"

        except requests.exceptions.Timeout:
            log.warning(f"  Timeout (attempt {attempt}/{MAX_RETRIES}): {url}")
            if attempt == MAX_RETRIES:
                return None, "timeout"

        except requests.exceptions.TooManyRedirects:
            log.warning(f"  Too many redirects: {url}")
            return None, "blocked"

        except requests.exceptions.ConnectionError:
            log.warning(f"  Connection error (attempt {attempt}/{MAX_RETRIES}): {url}")
            if attempt == MAX_RETRIES:
                status = "archive_blocked" if use_archive_headers else "error"
                return None, status

        except requests.exceptions.HTTPError as e:
            log.warning(f"  HTTP error: {e} — {url}")
            return None, "error"

        except Exception as e:
            log.warning(f"  Unexpected error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt == MAX_RETRIES:
                return None, "error"

        time.sleep(2 ** attempt)   # exponential back-off between retries

    return None, "error"


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

# Tags whose text is almost always boilerplate noise
_NOISE_TAGS = [
    "script", "style", "noscript", "header", "footer",
    "nav", "aside", "form", "button", "meta", "link",
    "iframe", "figure", "figcaption",
]

# CSS selectors that typically hold the main article body
_ARTICLE_SELECTORS = [
    "article",
    '[class*="article-body"]',
    '[class*="post-content"]',
    '[class*="entry-content"]',
    '[class*="content-body"]',
    '[class*="story-body"]',
    "main",
    ".content",
    "#content",
]


def extract_title(soup: BeautifulSoup) -> str:
    """Extract page title — prefer <h1> inside article, fallback to <title>."""
    # Try article-level h1 first
    for sel in _ARTICLE_SELECTORS:
        container = soup.select_one(sel)
        if container:
            h1 = container.find("h1")
            if h1:
                return clean_text(h1.get_text())

    # Fallback: any h1 on the page
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text())

    # Last resort: <title> tag
    title_tag = soup.find("title")
    if title_tag:
        return clean_text(title_tag.get_text())

    return ""


def extract_body_text(soup: BeautifulSoup, content_type: str) -> str:
    """
    Extract the main readable text from a page.

    Strategy differs by content_type:
      news_site    — look for known article containers; fallback to <body>
      archive      — strip nav/boilerplate from archive wrapper, grab content area
      social_media — grab all visible text (caption / post text is mixed in)

    Fix 4 is applied after extraction: paywall boilerplate at the start of
    the text is stripped before returning.
    """
    # Remove noise tags globally first
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    text = ""

    if content_type == "news_site":
        # Try known article containers in priority order
        for sel in _ARTICLE_SELECTORS:
            container = soup.select_one(sel)
            if container and len(container.get_text(strip=True)) > 200:
                text = clean_text(container.get_text(separator=" "))
                break

    if content_type == "archive":
        # archive.ph / archive.today wrap the original page inside a div
        for sel in ["#CONTENT", "#ORIGINAL", ".TEXT-BLOCK", "article", "main"]:
            container = soup.select_one(sel)
            if container and len(container.get_text(strip=True)) > 100:
                text = clean_text(container.get_text(separator=" "))
                break

    # Fallback for all types: entire <body> text, denoised
    if not text:
        body = soup.find("body")
        if body:
            text = clean_text(body.get_text(separator=" "))
        else:
            text = clean_text(soup.get_text(separator=" "))

    # Fix 4 — strip paywall / login-wall boilerplate from the top
    text = strip_paywall_boilerplate(text)

    return text


# ══════════════════════════════════════════════════════════════════════════════
# CORE: SCRAPE ONE URL → ONE OUTPUT ROW
# ══════════════════════════════════════════════════════════════════════════════

def scrape_url(
    article_id: int,
    source_type: str,
    source_index: int,
    url: str,
) -> dict:
    """
    Fetch one URL and return a single output row dict.

    All logic about whether to fetch, how to extract, and what to
    record in scrape_status lives here.
    """
    content_type = classify_url(url)

    row = {
        "article_id":   article_id,
        "source_type":  source_type,
        "source_index": source_index,
        "url":          url,
        "content_type": content_type,
        "title":        "",
        "text":         "",
        "scraped_date": now_iso(),
        "scrape_status": "",
    }

    # ── Empty URL ────────────────────────────────────────────────────────────
    if content_type == "empty":
        row["scrape_status"] = "empty_url"
        log.info(f"    [{source_type}#{source_index}] EMPTY URL — skipping")
        return row

    # ── Skip domains ─────────────────────────────────────────────────────────
    if content_type == "skip":
        row["scrape_status"] = "skipped"
        log.info(f"    [{source_type}#{source_index}] SKIP  {url}")
        return row

    # ── Fetch the page ───────────────────────────────────────────────────────
    log.info(f"    [{source_type}#{source_index}] {content_type.upper()}  {url}")

    # Fix 2 — pass archive flag so fetch_page uses the Referer header trick
    use_archive = (content_type == "archive")
    soup, status = fetch_page(url, use_archive_headers=use_archive)
    row["scrape_status"] = status

    if soup is None:
        # Fetch failed — row still records url, content_type, status, date
        return row

    # ── Extract content ──────────────────────────────────────────────────────
    # Title: only populate for news_site and archive (not social_media captions)
    if content_type in ("news_site", "archive"):
        row["title"] = extract_title(soup)

    row["text"] = extract_body_text(soup, content_type)

    # Fix 3 — detect JS-redirect placeholder pages (e.g. TikTok "Please wait...")
    if is_js_redirect(row["text"]):
        log.warning(f"    ⚠ JS redirect detected — marking as js_redirect: {url}")
        row["scrape_status"] = "js_redirect"
        row["text"] = ""
        return row

    # Warn if we got nothing useful after all extraction attempts
    if not row["text"]:
        log.warning(f"    ⚠ No text extracted from {url}")
        row["scrape_status"] = "error"

    return row


# ══════════════════════════════════════════════════════════════════════════════
# SAVE
# ══════════════════════════════════════════════════════════════════════════════

def save_csv(records: list[dict], path: str = OUTPUT_CSV) -> None:
    """Write the current records list to CSV (UTF-8 with BOM for Excel compat)."""
    df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    # Fix 1 — ensure source_type is always a lowercase string, never a Python bool.
    # pandas can coerce "true"/"false" strings to booleans on read, which then
    # capitalise to "True"/"False" on write. Explicit cast prevents this.
    df["source_type"] = df["source_type"].astype(str).str.lower()
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ══════════════════════════════════════════════════════════════════════════════
# RESUME SUPPORT
# ══════════════════════════════════════════════════════════════════════════════

def load_existing_records(path: str) -> tuple[list[dict], set[int]]:
    """
    If OUTPUT_CSV already exists (from a previous interrupted run), load it
    and return (records_list, set_of_completed_article_ids).
    Returns ([], set()) if no file exists yet.
    """
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        records = df.to_dict("records")
        # An article is "complete" only if its last URL was processed
        # We track by article_id presence — any partial article will be re-done
        # Use the max source_index approach: article is done if false row exists
        done_ids = set(df["article_id"].unique())
        log.info(f"  ↩  Resuming: {len(done_ids)} articles already in {path}")
        return records, done_ids
    except FileNotFoundError:
        return [], set()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    log.info("=" * 65)
    log.info("Content Scraper — TurnBackHoax")
    log.info(f"  Input  : {INPUT_CSV}")
    log.info(f"  Output : {OUTPUT_CSV}")
    log.info(f"  MAX_ROWS: {MAX_ROWS if MAX_ROWS else 'ALL'}")
    log.info("=" * 65)

    # ── Load source data ─────────────────────────────────────────────────────
    source_df = pd.read_csv(INPUT_CSV)
    if MAX_ROWS is not None:
        source_df = source_df.head(MAX_ROWS)
        log.info(f"  TEST MODE: processing first {MAX_ROWS} rows only.")

    total_articles = len(source_df)
    log.info(f"  Articles to process: {total_articles}")

    # ── Resume support ────────────────────────────────────────────────────────
    records, done_ids = load_existing_records(OUTPUT_CSV)

    # ── Main loop ─────────────────────────────────────────────────────────────
    articles_done_this_run = 0

    for article_id, row in tqdm(
        source_df.iterrows(),
        total=total_articles,
        desc="Articles",
        unit="art",
    ):
        if article_id in done_ids:
            log.info(f"[{article_id}] Already done — skipping")
            continue

        log.info(f"\n[{article_id}] Processing article {article_id + 1}/{total_articles}")

        # ── FALSE NEWS — link_source (always index 0) ─────────────────────
        false_url = str(row.get("link_source", "")).strip()
        records.append(
            scrape_url(article_id, "false", 0, false_url)
        )
        time.sleep(DELAY_SECONDS)

        # ── TRUE NEWS — referensi (0 … N) ────────────────────────────────
        raw_refs = str(row.get("referensi", ""))
        ref_urls = [u.strip() for u in raw_refs.split("|") if u.strip()]

        for ref_index, ref_url in enumerate(ref_urls):
            records.append(
                scrape_url(article_id, "true", ref_index, ref_url)
            )
            time.sleep(DELAY_SECONDS)

        articles_done_this_run += 1
        done_ids.add(article_id)

        # ── Checkpoint save ────────────────────────────────────────────────
        if articles_done_this_run % AUTOSAVE_EVERY == 0:
            save_csv(records)
            log.info(f"  💾 Checkpoint: {len(records)} rows saved to {OUTPUT_CSV}")

    # ── Final save ────────────────────────────────────────────────────────────
    save_csv(records)

    # ── Summary ───────────────────────────────────────────────────────────────
    df_out = pd.read_csv(OUTPUT_CSV, encoding="utf-8-sig")
    log.info("\n" + "=" * 65)
    log.info(f"✅ DONE — {len(df_out)} rows written to {OUTPUT_CSV}")
    log.info(f"   Articles processed   : {articles_done_this_run}")
    log.info(f"   source_type counts   : {df_out['source_type'].value_counts().to_dict()}")
    log.info(f"   content_type counts  : {df_out['content_type'].value_counts().to_dict()}")
    log.info(f"   scrape_status counts : {df_out['scrape_status'].value_counts().to_dict()}")
    log.info("=" * 65)


if __name__ == "__main__":
    main()