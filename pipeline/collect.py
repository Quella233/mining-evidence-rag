from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from pipeline.models import Document
from pipeline.sources import Fetcher, policy_documents, rss_documents, shfe_documents
from pipeline.store import Embedder, Store


def window(as_of: date, days: int):
    end = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), timezone.utc)
    return end - timedelta(days=days), end


def import_file(store, path):
    path = Path(path)
    records = csv.DictReader(path.open(encoding="utf-8-sig", newline="")) if path.suffix == ".csv" else (json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())
    counts = Counter()
    for line, record in enumerate(records, 1):
        try:
            record = {k: v for k, v in record.items() if v != ""}
            counts[store.upsert(Document.model_validate(record))] += 1
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Import line {line}: {exc}") from exc
    return dict(counts)


def collect(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    start, end = window(args.as_of, args.days)
    store = Store(args.db, Embedder(args.embedding))
    fetcher = Fetcher(timeout=args.timeout, retries=args.retries)
    results = []
    try:
        if args.import_file:
            results.append({"name": "import", "counts": import_file(store, args.import_file), "errors": []})
        else:
            for source in config["sources"]:
                if args.only and source["adapter"] not in args.only:
                    continue
                errors, counts = [], Counter()
                print(f"Collecting {source['name']} ...", flush=True)
                try:
                    if source["adapter"] in {"rss", "policy"}:
                        collector = rss_documents if source["adapter"] == "rss" else policy_documents
                        for doc in collector(fetcher, source, start, end, errors):
                            counts[store.upsert(doc)] += 1
                            if sum(counts.values()) % 25 == 0:
                                print(f"  processed={sum(counts.values())}", flush=True)
                    elif source["adapter"] == "shfe":
                        for offset in range(args.days):
                            day = args.as_of - timedelta(days=offset)
                            if day.weekday() >= 5:
                                continue
                            url = source["url"].format(date=day.strftime("%Y%m%d"))
                            try:
                                payload = fetcher.get(url, public_api=True).json()
                                for doc in shfe_documents(payload, day, url):
                                    if start <= doc.published_at < end:
                                        counts[store.upsert(doc)] += 1
                            except (httpx.HTTPError, ValueError, PermissionError) as exc:
                                errors.append({"url": url, "error": str(exc)[:300]})
                except (httpx.HTTPError, ValueError, PermissionError) as exc:
                    errors.append({"error": str(exc)[:300]})
                results.append({"name": source["name"], "counts": dict(counts), "errors": errors})
                print(f"  {dict(counts)}; errors={len(errors)}", flush=True)
        counts = store.counts(start, end)
        totals = {kind: sum(row["n"] for row in counts if row["kind"] == kind) for kind in ("news", "policy", "price")}
        from pipeline.coverage import audit
        coverage = audit(args.db, args.as_of, args.days)
        report = {"as_of": str(args.as_of), "days": args.days, "window_start_inclusive": start.isoformat(), "window_end_exclusive": end.isoformat(), "embedding": args.embedding, "counts_by_source": counts, "counts_by_kind": totals, "category_quantity_target_met": coverage["category_quantity_target_met"], "original_source_quantity_target_met": coverage["original_source_quantity_target_met"], "unresolved_requirements": config.get("authorization_required", []), "runs": results}
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"counts": totals, "report": args.report}, ensure_ascii=False), flush=True)
        target = "original_source_quantity_target_met" if args.strict else "category_quantity_target_met"
        return 0 if report[target] else 2
    finally:
        store.close()
        fetcher.close()


def main():
    parser = argparse.ArgumentParser(description="Collect auditable public mining records; exit 2 means coverage incomplete")
    parser.add_argument("--db", default="data/mining.db")
    parser.add_argument("--embedding", choices=["hash", "fastembed"], default="hash")
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--only", nargs="+", choices=["rss", "policy", "shfe"])
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--report", default="data/collection-report.json")
    parser.add_argument("--import-file")
    parser.add_argument("--strict", action="store_true", help="Require the exact original source requirements, not merely category counts")
    args = parser.parse_args()
    if not 1 <= args.days <= 366:
        parser.error("days must be between 1 and 366")
    raise SystemExit(collect(args))


if __name__ == "__main__":
    main()
