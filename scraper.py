"""
TurnBackHoax.ID Article Scraper — v4 FINAL
============================================
Target  : https://turnbackhoax.id/articles
Total   : ~15,291 articles
Output  : turnbackhoax_articles.csv

Fields:
  1. classification  - Label inside [BRACKETS] in title
  2. category        - e.g. Politik, Lowongan, Bantuan
  3. date            - Publication date (DD/MM/YYYY)
  4. narasi          - Hoax narrative/claim
  5. penjelasan      - Fact-check explanation
  6. kesimpulan      - Conclusion
  7. hasil_periksa   - Verdict (Salah / Benar)
  8. link_source     - Source link of the hoax (from "Sumber:")
  9. referensi       - Reference URLs for validity check

Fixes in v4:
  - kesimpulan: now uses full-text regex between section markers
  - link_source: split from hasil_periksa via "Sumber:" pattern
  - narasi/penjelasan: full-text regex (more reliable than tag-walking)
  - referensi: regex findall for all URLs after "Referensi" header
  - classification: handles Unicode/smart bracket variants
"""

import re
import time
import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
BASE_URL        = "https://turnbackhoax.id"
ARTICLES_URL    = "https://turnbackhoax.id/articles"
OUTPUT_CSV      = "turnbackhoax_articles.csv"
DELAY_SECONDS   = 1.5       # polite delay between requests
REQUEST_TIMEOUT = 20        # seconds
MAX_RETRIES     = 3
START_PAGE      = 1
MAX_PAGES       = None     # Set to e.g. 2 for testing, None for full run

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def get_page(url: str) -> BeautifulSoup | None:
    """Fetch URL with retries, return BeautifulSoup or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {url}: {e}")
            time.sleep(2 ** attempt)
    log.error(f"Giving up on: {url}")
    return None


def clean(text: str) -> str:
    """Normalize whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def extract_classification(title: str) -> str:
    """
    Extract [LABEL] from title.
    Handles normal brackets and Unicode/smart bracket variants.
    """
    title = title.replace("\uff3b", "[").replace("\uff3d", "]")
    title = title.replace("\u3010", "[").replace("\u3011", "]")
    match = re.search(r"\[([^\]]+)\]", title)
    return match.group(1).strip() if match else ""


def extract_between(text: str, start_kw: list, end_kw: list) -> str:
    """
    Extract text between two section markers using regex.
    More reliable than tag-walking since the site uses plain text labels.
    """
    start_pat = "|".join(re.escape(k) for k in start_kw)
    end_pat   = "|".join(re.escape(k) for k in end_kw)
    pattern   = rf"(?:{start_pat})\s*\n(.*?)(?=\n\s*(?:{end_pat}))"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if m:
        return clean(m.group(1))
    return ""


# ──────────────────────────────────────────────
# STEP 1: COLLECT ALL ARTICLE LINKS
# ──────────────────────────────────────────────
def get_all_article_links() -> list[dict]:
    """
    Paginate through https://turnbackhoax.id/articles?page=N
    Collect each article's URL and date.
    """
    all_articles = []
    page = START_PAGE

    while True:
        if MAX_PAGES and (page - START_PAGE) >= MAX_PAGES:
            log.info(f"Reached MAX_PAGES={MAX_PAGES} limit. Stopping.")
            break

        url = f"{ARTICLES_URL}?page={page}"
        log.info(f"Fetching list page {page}: {url}")
        soup = get_page(url)

        if soup is None:
            log.error("Failed to fetch list page. Stopping.")
            break

        # Find all links matching article URL pattern
        article_links = soup.find_all(
            "a", href=re.compile(r"/articles/\d+-.+")
        )

        # Deduplicate — same article can appear multiple times in a card
        seen = set()
        unique_links = []
        for a in article_links:
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            if href not in seen:
                seen.add(href)
                unique_links.append((href, a))

        if not unique_links:
            log.info(f"No articles found on page {page}. Pagination complete.")
            break

        for href, a in unique_links:
            # Try to get date from card text
            parent   = a.find_parent()
            card_text = parent.get_text(" ", strip=True) if parent else ""
            date_match = re.search(r"\d{2}/\d{2}/\d{4}", card_text)
            date = date_match.group(0) if date_match else ""

            all_articles.append({
                "url":      href,
                "date":     date,
                "category": "",  # scraped from article page
            })

        log.info(
            f"  → {len(unique_links)} articles on page {page}. "
            f"Total so far: {len(all_articles)}"
        )

        page += 1
        time.sleep(DELAY_SECONDS)

    log.info(f"✅ Total article URLs collected: {len(all_articles)}")
    return all_articles


# ──────────────────────────────────────────────
# STEP 2: SCRAPE INDIVIDUAL ARTICLE
# ──────────────────────────────────────────────
def scrape_article(meta: dict) -> dict:
    """
    Scrape one article page and return all 9 data fields.

    Key approach: full-text regex between section markers.
    The site uses plain-text bold labels (Narasi, Penjelasan, etc.)
    not semantic HTML — so regex on page text is more reliable
    than walking <p>/<strong> tags.
    """
    url = meta["url"]

    result = {
        "url":            url,
        "classification": "",
        "category":       meta.get("category", ""),
        "date":           meta.get("date", ""),
        "narasi":         "",
        "penjelasan":     "",
        "kesimpulan":     "",
        "hasil_periksa":  "",
        "link_source":    "",
        "referensi":      "",
    }

    soup = get_page(url)
    if soup is None:
        return result

    # ── 1. CLASSIFICATION ─────────────────────────────────────────────
    for tag in soup.find_all(["h1", "h2"]):
        cls = extract_classification(tag.get_text())
        if cls:
            result["classification"] = cls
            break

    # ── 2. CATEGORY ───────────────────────────────────────────────────
    cat_link = soup.find("a", href=re.compile(r"\?category="))
    if cat_link:
        result["category"] = clean(cat_link.get_text())

    # ── 3. DATE ───────────────────────────────────────────────────────
    if not result["date"]:
        m = re.search(r"\d{2}/\d{2}/\d{4}", soup.get_text())
        if m:
            result["date"] = m.group(0)

    # ── 4. FULL TEXT (basis for all section extraction) ───────────────
    full_text = soup.get_text(separator="\n")

    # ── 5. NARASI ─────────────────────────────────────────────────────
    result["narasi"] = extract_between(
        full_text,
        start_kw=["Narasi"],
        end_kw=["Penjelasan", "Kesimpulan", "Hasil Periksa"]
    )

    # ── 6. PENJELASAN ─────────────────────────────────────────────────
    result["penjelasan"] = extract_between(
        full_text,
        start_kw=["Penjelasan"],
        end_kw=["Kesimpulan", "Hasil Periksa"]
    )

    # ── 7. KESIMPULAN ─────────────────────────────────────────────────
    result["kesimpulan"] = extract_between(
        full_text,
        start_kw=["Kesimpulan"],
        end_kw=["Hasil Periksa fakta", "Hasil Periksa Fakta", "Hasil Periksa", "Referensi"]
    )

    # ── 8. HASIL PERIKSA FAKTA + LINK SOURCE ─────────────────────────
    # The verdict (Salah/Benar) and hoax source URL appear together:
    # e.g. "Salah Sumber: https://..."
    # We split them: verdict → hasil_periksa, URL → link_source
    hasil_raw = extract_between(
        full_text,
        start_kw=["Hasil Periksa fakta", "Hasil Periksa Fakta", "Hasil Periksa"],
        end_kw=["Referensi", "Artikel terbaru", "Affiliation"]
    )

    sumber_match = re.search(r"(?i)sumber\s*[:\-]?\s*(https?://\S+)", hasil_raw)
    if sumber_match:
        result["link_source"]   = sumber_match.group(1).strip()
        result["hasil_periksa"] = clean(hasil_raw[:sumber_match.start()])
    else:
        result["hasil_periksa"] = hasil_raw

    # ── 9. REFERENSI ──────────────────────────────────────────────────
    # Extract all URLs that appear after the "Referensi" section header
    ref_match = re.search(
        r"Referensi\s*\n(.*?)(?=\nArtikel terbaru|\nAffiliat|\nTautan Cepat|\Z)",
        full_text, re.DOTALL | re.IGNORECASE
    )
    if ref_match:
        ref_block = ref_match.group(1)
        urls = re.findall(r"https?://\S+", ref_block)
        result["referensi"] = " | ".join(urls)

    return result


# ──────────────────────────────────────────────
# SAVE HELPER
# ──────────────────────────────────────────────
def save_csv(records: list[dict], path: str = OUTPUT_CSV):
    """Save records to CSV with proper UTF-8 encoding for Indonesian text."""
    df = pd.DataFrame(records, columns=[
        "url", "classification", "category", "date",
        "narasi", "penjelasan", "kesimpulan",
        "hasil_periksa", "link_source", "referensi",
    ])
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("TurnBackHoax Scraper v4 — FINAL")
    log.info(f"Target : {ARTICLES_URL}")
    log.info(f"Output : {OUTPUT_CSV}")
    log.info("=" * 60)

    # ── Phase 1: collect all article URLs from list pages ─────────────
    articles_meta = get_all_article_links()
    if not articles_meta:
        log.error("No articles collected. Check network or selectors.")
        return

    # ── Phase 2: scrape each article page ─────────────────────────────
    records = []
    for meta in tqdm(articles_meta, desc="Scraping articles", unit="art"):
        record = scrape_article(meta)
        records.append(record)
        time.sleep(DELAY_SECONDS)

        # Auto-save every 100 articles as crash protection
        if len(records) % 100 == 0:
            save_csv(records)
            log.info(f"  💾 Auto-saved checkpoint: {len(records)} articles")

    # ── Phase 3: final save ───────────────────────────────────────────
    save_csv(records)

    # ── Summary ───────────────────────────────────────────────────────
    df = pd.read_csv(OUTPUT_CSV)
    log.info("=" * 60)
    log.info(f"✅ DONE! {len(df)} articles saved to '{OUTPUT_CSV}'")
    log.info(f"   Classifications : {df['classification'].value_counts().to_dict()}")
    log.info(f"   Categories      : {df['category'].value_counts().to_dict()}")
    log.info(f"   Date range      : {df['date'].min()} → {df['date'].max()}")
    log.info(f"   Empty narasi    : {df['narasi'].isna().sum()}")
    log.info(f"   Empty kesimpulan: {df['kesimpulan'].isna().sum()}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()