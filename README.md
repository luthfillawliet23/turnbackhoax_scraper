# TurnBackHoax Scraper

A Python-based data collection pipeline for Indonesian hoax detection research. This repository scrapes and structures fact-checking articles from [TurnBackHoax.ID](https://turnbackhoax.id) (MAFINDO), then retrieves the full page content of both the original hoax sources and their evidence references — producing a paired dataset suitable for training NLP and deep learning models.

This project is part of ongoing research submitted to **SOFTT 2026** (IEEE Symposium on Future Telecommunication Technologies, organized by Telkom University / AICOMS, Yogyakarta), targeting automated hoax detection with Retrieval-Augmented Generation (RAG), sarcasm/non-literal language detection, and knowledge distillation.

---

## Repository Structure

```
turnbackhoax_scraper/
│
├── scraper.py                  # Phase 1 — scrapes article metadata from TurnBackHoax.ID
├── content_scraper.py          # Phase 2 — scrapes full page content from source URLs
├── inspect_site.py             # Diagnostic tool — inspect HTML structure before scraping
├── test_single.py              # Test harness — validate scraper on individual articles
│
├── turnbackhoax_articles.csv   # Output of Phase 1 (960 articles, 10 fields)
├── scraped_content.csv         # Output of Phase 2 (long-format, one row per URL)
│
├── scraper.log                 # Run log for Phase 1
├── content_scraper.log         # Run log for Phase 2
│
├── venv/                       # Python virtual environment (not tracked)
└── __pycache__/                # Python cache (not tracked)
```

---

## Pipeline Overview

```
TurnBackHoax.ID
      │
      ▼
 scraper.py  ──────────────────────────►  turnbackhoax_articles.csv
 (Phase 1: article metadata)              960 rows × 10 fields
      │
      │  reads link_source + referensi columns
      ▼
 content_scraper.py  ──────────────────► scraped_content.csv
 (Phase 2: full page content)             long format, one row per URL
```

---

## Phase 1 — Article Metadata Scraper (`scraper.py`)

Crawls all paginated article listing pages at `turnbackhoax.id/articles?page=N` and scrapes 9 structured fields from each individual article page.

### Output: `turnbackhoax_articles.csv`

| Field | Description |
|---|---|
| `url` | Article URL on TurnBackHoax.ID |
| `classification` | Label in `[BRACKETS]` from title e.g. `SALAH`, `PENIPUAN`, `PARODI` |
| `category` | Topic category e.g. `Politik`, `Lowongan`, `Bantuan`, `Kesehatan` |
| `date` | Publication date `DD/MM/YYYY` |
| `narasi` | The hoax claim / circulating narrative |
| `penjelasan` | MAFINDO's fact-check explanation |
| `kesimpulan` | Conclusion |
| `hasil_periksa` | Verdict — `Salah` (false) or `Benar` (true) |
| `link_source` | URL of the original hoax post |
| `referensi` | Pipe-separated `\|` reference/evidence URLs |

### Key design decisions

- Uses full-text regex extraction between section markers (`Narasi`, `Penjelasan`, `Kesimpulan`, etc.) rather than tag-walking, because TurnBackHoax.ID uses plain-text bold labels inside `<p>` tags rather than semantic HTML.
- Unicode/smart bracket variants (`\uff3b`, `\u3010`, etc.) are normalised before classification label extraction.
- Auto-saves a checkpoint every 100 articles as crash protection.

### How to run

```bash
# Test mode — first 2 pages only
# Set MAX_PAGES = 2 in scraper.py, then:
python scraper.py

# Full run — all ~15,000+ articles
# Set MAX_PAGES = None, then:
python scraper.py
```

---

## Phase 2 — Content Scraper (`content_scraper.py`)

Reads `turnbackhoax_articles.csv` and visits every URL in `link_source` (the hoax source) and `referensi` (the evidence references), scraping the full text content of each page.

### Output: `scraped_content.csv`

Long format — one row per URL. Multiple rows share the same `article_id`, identified by `source_type` and `source_index`.

| Field | Description |
|---|---|
| `article_id` | Row index from `turnbackhoax_articles.csv` |
| `source_type` | `"false"` (hoax source) or `"true"` (evidence reference) |
| `source_index` | `0` for false; `0, 1, 2 …` for each referensi URL |
| `url` | The original URL |
| `content_type` | `"news_site"` \| `"archive"` \| `"social_media"` \| `"skip"` \| `"empty"` |
| `title` | Page title (populated for `news_site` and `archive` only) |
| `text` | Cleaned body text extracted from the page |
| `scraped_date` | ISO-8601 UTC datetime of scrape |
| `scrape_status` | `"ok"` \| `"blocked"` \| `"timeout"` \| `"404"` \| `"error"` \| `"skipped"` \| `"empty_url"` \| `"archive_blocked"` \| `"js_redirect"` |

### URL routing logic

Each URL is classified before fetching based on its domain:

| `content_type` | Examples | Behaviour |
|---|---|---|
| `news_site` | Kompas, Detik, Liputan6, Tempo | Fetch → extract `title` + `text` from article container |
| `archive` | archive.ph, archive.today, arsip.cekfakta.com | Fetch with Referer header → extract from archive wrapper |
| `social_media` | Facebook, TikTok, Instagram, X, YouTube | Fetch attempted → text grabbed where accessible |
| `skip` | magma.esdm.go.id, turnbackhoax.id | Not fetched — logged as `skipped` |
| `empty` | Missing URL in source CSV | Not fetched — logged as `empty_url` |

### Fixes applied (v2)

| Fix | Issue | Solution |
|---|---|---|
| Fix 1 | `source_type` saved as Python bool `True`/`False` | Force `.astype(str).str.lower()` on write |
| Fix 2 | `archive.ph` / `archive.today` blocked by bot detection | Use dedicated `ARCHIVE_HEADERS` with `Referer: google.com`; log as `archive_blocked` |
| Fix 3 | TikTok short URLs return `"Please wait..."` JS redirect | Detect short pages with redirect markers → `js_redirect` status |
| Fix 4 | Kompas.com / Kompas.tv paywall boilerplate in text | Strip known login-wall prefixes from start of extracted text |

### How to run

```bash
# Test mode — first 5 articles only (recommended before full run)
# Set MAX_ROWS = 5 in content_scraper.py, then:
python content_scraper.py

# Full run — all 960 articles
# Set MAX_ROWS = None, then:
python content_scraper.py
```

Resume support is built in — if interrupted, re-running will skip already-processed `article_id`s automatically.

---

## Dataset Statistics (current)

| Dataset | Rows | Notes |
|---|---|---|
| `turnbackhoax_articles.csv` | 960 | Scraped June 2026 |
| `scraped_content.csv` | TBD (full run pending) | Test run: 31 rows (5 articles) |

**`turnbackhoax_articles.csv` label distribution (from scraper.log):**

| Classification | Count |
|---|---|
| SALAH | majority |
| PENIPUAN | significant |
| PARODI | minority |

**`referensi` URL statistics:**
- Average: ~3.8 reference URLs per article
- Maximum: 13 reference URLs in a single article
- ~90% of articles have 2 or more reference URLs

---

## Installation

```bash
# Clone the repository
git clone https://github.com/luthfillawliet23/turnbackhoax_scraper.git
cd turnbackhoax_scraper

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install requests beautifulsoup4 lxml tqdm pandas
```

---

## Research Context

This pipeline forms the data collection layer for a deep learning-based Indonesian hoax detection model, developed as part of a research paper targeting **SOFTT 2026** (IEEE, Telkom University).

The assembled dataset enables the following research directions:

**Retrieval-Augmented Generation (RAG)**
Each article pairs a hoax claim (`narasi`) with structured evidence retrieved from `referensi` URLs. This supports building a RAG pipeline where a retriever fetches relevant evidence before a classifier delivers a verdict.

**Non-Literal Language Detection**
The `classification` field includes `PARODI` (parody/satire) labels alongside `SALAH` (false) and `PENIPUAN` (fraud/scam). The dataset can be used to train models that distinguish irony and sarcasm from straightforward misinformation.

**Knowledge Distillation**
The structured paired format (claim + false source + true evidence) supports training large teacher models whose knowledge can then be distilled into lightweight student models suitable for real-time deployment.

---

## Data Source

All article content is sourced from **[TurnBackHoax.ID](https://turnbackhoax.id)**, the fact-checking platform of **MAFINDO** (Masyarakat Anti Fitnah Indonesia). MAFINDO is a member of the International Fact-Checking Network (IFCN).

This data is collected for academic research purposes only.

---

## License

This repository is for academic research use. Please refer to MAFINDO's terms of use regarding TurnBackHoax.ID content.
