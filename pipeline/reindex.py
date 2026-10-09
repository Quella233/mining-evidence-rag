"""Build a separate semantic database; never alter an existing embedding space."""
import argparse
import sqlite3
from pathlib import Path

from pipeline.models import Document
from pipeline.store import Embedder, Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="data/mining.db")
    parser.add_argument("--target", default="data/mining-semantic.db")
    args = parser.parse_args()
    if Path(args.source).resolve() == Path(args.target).resolve():
        parser.error("source and target must be different")
    if not Path(args.source).is_file():
        parser.error("source database does not exist")
    db = sqlite3.connect(args.source)
    try:
        docs = [Document.model_validate_json(row[0]) for row in db.execute("SELECT data FROM documents")]
    finally:
        db.close()
    store = Store(args.target, Embedder("fastembed"))
    try:
        source_ids = {doc.id for doc in docs}
        for (doc_id,) in store.db.execute("SELECT id FROM documents").fetchall():
            if doc_id not in source_ids:
                store.delete(doc_id)
        for i, doc in enumerate(docs, 1):
            store.upsert(doc)
            if i % 100 == 0:
                print(f"Indexed {i}/{len(docs)} documents", flush=True)
        print(f"Ready: {args.target}; documents={len(docs)}", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
