"""Re-extract saved successful responses after a source HTML selector is improved."""
import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from datetime import date

from pipeline.collect import window
from pipeline.sources import article, relevant_policy
from pipeline.models import canonical_url
from pipeline.store import Store, Embedder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mining.db")
    parser.add_argument("--embedding", default="hash", choices=["hash", "fastembed"])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--prune-rejected", action="store_true", help="Remove cached articles now rejected by extraction/validation")
    args = parser.parse_args()
    config = json.loads(Path("sources.json").read_text(encoding="utf-8"))
    start, end = window(args.as_of, args.days)
    store = Store(args.db, Embedder(args.embedding))
    counts, errors = Counter(), []
    existing = {canonical_url(d["url"]): d["id"] for row in store.db.execute("SELECT data FROM documents WHERE kind<>'price'") if (d := json.loads(row[0]))}
    try:
        for meta in Path("data/cache/http").glob("*.json"):
            saved = json.loads(meta.read_text(encoding="utf-8"))
            url = saved["url"]
            for source in config["sources"]:
                source_urls = source.get("urls", [source.get("url", "")])
                if source["adapter"] not in {"policy", "rss"} or urlsplit(url).netloc not in {urlsplit(u).netloc for u in source_urls} or "/feed/" in url:
                    continue
                try:
                    doc = article(meta.with_suffix(".body").read_text(encoding="utf-8"), url, source["name"], source["kind"], country=source.get("country"))
                    relevant = source["kind"] == "news" or relevant_policy(doc, source)
                    if start <= doc.published_at < end and relevant:
                        counts[store.upsert(doc)] += 1
                    elif args.prune_rejected and canonical_url(url) in existing:
                        store.delete(existing[canonical_url(url)])
                        counts["removed"] += 1
                except (ValueError, PermissionError) as exc:
                    errors.append({"url": url, "error": str(exc)})
                    if args.prune_rejected and canonical_url(url) in existing:
                        store.delete(existing[canonical_url(url)])
                        counts["removed"] += 1
        report = {"counts": dict(counts), "errors": errors, "counts_by_source": store.counts(start, end)}
        Path("data/reprocess-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"counts": dict(counts), "sources": report["counts_by_source"]}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
