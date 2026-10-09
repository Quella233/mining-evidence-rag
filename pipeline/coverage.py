"""Compute counts from original records, excluding chunks and synthetic fixtures."""
import argparse
import json
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.collect import window


def audit(db_path, as_of, days=30):
    start, end = window(as_of, days)
    db = sqlite3.connect(db_path)
    try:
        docs = [json.loads(row[0]) for row in db.execute("SELECT data FROM documents WHERE is_demo=0 AND published_at>=? AND published_at<?", (start.isoformat(), end.isoformat()))]
    finally:
        db.close()
    counts, sources, exact = Counter(), Counter(), Counter()
    original_hosts = {"news": ("mining.com", "spglobal.com"), "policy": ("industry.gov.au", "chinareg.com"), "price": ("lme.com", "mysteel.com", "gfex.com.cn")}
    price_series = set()
    for doc in docs:
        counts[doc["kind"]] += 1
        sources[(doc["kind"], doc["source"])] += 1
        host = urlsplit(doc["url"]).hostname or ""
        if any(host == domain or host.endswith("." + domain) for domain in original_hosts[doc["kind"]]):
            exact[doc["kind"]] += 1
        if doc["kind"] == "price":
            price_series.add((doc["source"], doc["commodity"], doc["price_type"], doc["currency"], doc["unit"]))
    kinds = ("news", "policy", "price")
    required = {"news": 200, "policy": 200, "price": 200}
    return {"as_of": str(as_of), "days": days, "total_real_documents": len(docs), "counts_by_kind": {k: counts[k] for k in kinds}, "shortfall_by_kind": {k: max(0, required[k] - counts[k]) for k in kinds}, "counts_by_source": [{"kind": k, "source": s, "count": n} for (k, s), n in sorted(sources.items())], "original_source_category_counts": {k: exact[k] for k in kinds}, "category_quantity_target_met": all(counts[k] >= required[k] for k in kinds), "original_source_quantity_target_met": all(exact[k] >= required[k] for k in kinds), "price_series": sorted(price_series), "notes": ["Counts are original articles or day+instrument+contract+price-type observations, not chunks.", "SHFE copper/zinc/nickel and supplementary news are not substitutions silently credited to the original providers.", "Lithium venue in the prompt needs clarification: GFEX versus SHFE. Quantity alone does not prove complete instrument or licensed-source coverage."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mining.db")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", default="data/coverage-report.json")
    args = parser.parse_args()
    report = audit(args.db, args.as_of, args.days)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
