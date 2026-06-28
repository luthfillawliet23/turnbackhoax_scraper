"""
inspect_site.py  –  Run this BEFORE the full scraper.
It prints the raw HTML structure so you can verify/adjust selectors.
"""
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}

def inspect(url, label):
    r = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    print(f"\n{'='*60}")
    print(f"PAGE: {label}  ({url})")
    print(f"{'='*60}")

    # Show candidate containers
    for sel in ["table", "article", ".post-list", ".entry-list", "tbody tr"]:
        found = soup.select(sel)
        print(f"  selector '{sel}': {len(found)} elements")

    # Show first article link found
    first_link = soup.select_one("a[href*='turnbackhoax']")
    if first_link:
        print(f"\n  First article link: {first_link['href']}")
        print(f"  Link text: {first_link.get_text(strip=True)[:80]}")

# Inspect the list page
inspect("https://turnbackhoax.id/daftar-artikel/", "Article List")

# Inspect one sample article (replace with a real article URL after checking above)
sample = "https://turnbackhoax.id/2024/01/15/example-article/"  # update this
inspect(sample, "Sample Article")