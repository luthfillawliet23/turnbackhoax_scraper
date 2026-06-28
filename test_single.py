# test_single.py — verify all fixes before full run
from scraper import scrape_article

test_urls = [
    # Row 6 — had missing kesimpulan + blockquote in narasi
    {
        "url": "https://turnbackhoax.id/articles/35281-salah-dokumentasi-presiden-erdogan-tidak-mau-bersalaman-dengan-prabowo",
        "date": "",
        "category": ""
    },
    # Row 7 — had empty classification
    {
        "url": "https://turnbackhoax.id/articles/35285-salah-mahasiswa-unhas-diancam-drop-out-usai-demo-tolak-mbg",
        "date": "",
        "category": ""
    },
]

print("=" * 60)
print("TurnBackHoax Scraper — Single Article Test")
print("=" * 60)

for i, meta in enumerate(test_urls, 1):
    print(f"\n{'='*60}")
    print(f"TEST {i}: {meta['url']}")
    print("=" * 60)

    r = scrape_article(meta)

    fields = [
        ("classification", r["classification"]),
        ("category",       r["category"]),
        ("date",           r["date"]),
        ("narasi",         r["narasi"][:150]),
        ("penjelasan",     r["penjelasan"][:150]),
        ("kesimpulan",     r["kesimpulan"][:150]),
        ("hasil_periksa",  r["hasil_periksa"]),
        ("link_source",    r["link_source"]),
        ("referensi",      r["referensi"][:150]),
    ]

    for field, value in fields:
        status = "✅" if value and value != "nan" else "❌ EMPTY"
        print(f"  {status}  {field:15}: {value}")