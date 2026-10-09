import argparse
import json
from pathlib import Path

from pipeline.store import Embedder, Store
from serve.app import Query, filters, query_store


def evaluate(store, cases, allow_demo=False):
    rows = []
    for case in cases:
        if case.get("is_demo") and not allow_demo:
            raise ValueError("Synthetic gold cases require --allow-demo")
        query = Query(**{k: case[k] for k in ("question", "as_of", "kind", "country", "commodity", "days", "start_date", "end_date") if k in case}, top_k=5)
        result = query_store(store, query, allow_demo)
        relevant = set(case["expected_document_ids"])
        found = {c["document_id"] for c in result["citations"]}
        recall = len(relevant & found) / len(relevant) if relevant else None
        documents = {hit["document"]["id"]: hit["document"]["body"] for hit in store.search(query.question, limit=5, allow_demo=allow_demo, **filters(query))}
        supported = 0
        for claim in result["claims"]:
            citation = next((c for c in result["citations"] if c["citation"] == claim["citation"]), None)
            if citation and claim["text"] in documents.get(citation["document_id"], ""):
                supported += 1
        faithfulness = supported / len(result["claims"]) if result["claims"] else None
        rows.append({"id": case["id"], "recall_at_5": recall, "answer_faithfulness": faithfulness, "expected_answer_present": case["expected_answer"] in result["answer"], "retrieved_ids": sorted(found), "insufficient_evidence": result["insufficient_evidence"]})
    def mean(key):
        values = [row[key] for row in rows if row[key] is not None]
        return sum(values) / len(values) if values else None
    return {"n": len(rows), "recall_at_5": mean("recall_at_5"), "answer_faithfulness": mean("answer_faithfulness"), "expected_answer_accuracy": mean("expected_answer_present"), "answered_cases": sum(not r["insufficient_evidence"] for r in rows), "evaluation_scope": "synthetic integration benchmark" if any(c.get("is_demo") for c in cases) else "provided real-world gold set", "faithfulness_definition": "Fraction of emitted extractive claims that occur verbatim in their cited original document. This is citation-span support, not an independent semantic/LLM judge or completeness score. Abstentions excluded and reported separately.", "cases": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/demo.db")
    parser.add_argument("--gold", default="eval/gold.jsonl")
    parser.add_argument("--output", default="eval/report.json")
    parser.add_argument("--embedding", choices=["hash", "fastembed"], default="hash")
    parser.add_argument("--allow-demo", action="store_true")
    args = parser.parse_args()
    cases = [json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines() if line.strip()]
    store = Store(args.db, Embedder(args.embedding))
    try:
        report = evaluate(store, cases, args.allow_demo)
    finally:
        store.close()
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))
    if report["recall_at_5"] is None or report["recall_at_5"] < 0.85 or report["answer_faithfulness"] != 1 or report["expected_answer_accuracy"] < 0.85:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
