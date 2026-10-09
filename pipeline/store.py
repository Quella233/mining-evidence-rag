from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import numpy as np
import sqlite_vec
from rank_bm25 import BM25Okapi

from pipeline.models import Document, canonical_url, chunks, tokens


class Embedder:
    def __init__(self, backend="hash", cache_dir="data/cache"):
        self.backend = backend
        self.model = None
        if backend == "fastembed":
            from fastembed import TextEmbedding
            self.model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", cache_dir=cache_dir, threads=2)
            self.dim = 384
        elif backend == "hash":
            self.dim = 384
        else:
            raise ValueError("Unknown embedding backend")

    def encode(self, texts: list[str], query=False) -> list[np.ndarray]:
        if self.model:
            return [np.asarray(v, dtype=np.float32) for v in self.model.embed(texts)]
        result = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            for term in tokens(text):
                digest = hashlib.blake2b(term.encode(), digest_size=8).digest()
                vector[int.from_bytes(digest[:4], "little") % self.dim] += 1 if digest[4] % 2 else -1
            vector /= max(np.linalg.norm(vector), 1e-12)
            result.append(vector)
        return result


class Store:
    def __init__(self, path, embedder=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.embedding = embedder or Embedder()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
                source TEXT NOT NULL, published_at TEXT NOT NULL, is_demo INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS doc_filter ON documents(kind,published_at,is_demo);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                text TEXT NOT NULL, lexical TEXT NOT NULL
            );
        """)
        current = self.db.execute("SELECT value FROM meta WHERE key='embedding'").fetchone()
        if current and current[0] != self.embedding.backend:
            self.db.close()
            raise ValueError("Database embedding differs; build a separate database with the chosen backend")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('embedding',?)", (self.embedding.backend,))
        self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{self.embedding.dim}])")
        self.db.commit()

    def close(self):
        self.db.close()

    def delete(self, document_id):
        with self.lock, self.db:
            ids = self.db.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,)).fetchall()
            self.db.executemany("DELETE FROM vectors WHERE rowid=?", [(row[0],) for row in ids])
            self.db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            self.db.execute("DELETE FROM documents WHERE id=?", (document_id,))

    def upsert(self, document: Document) -> str:
        fingerprint = hashlib.sha256((document.kind + "|" + document.body).encode()).hexdigest()
        with self.lock:
            duplicate = self.db.execute("SELECT id FROM documents WHERE fingerprint=? AND is_demo=? AND id<>?", (fingerprint, document.is_demo, document.id)).fetchone()
            if duplicate and document.kind != "price":
                return "duplicate"
            existing = self.db.execute("SELECT data FROM documents WHERE id=?", (document.id,)).fetchone()
            if existing:
                previous = Document.model_validate_json(existing[0])
                if previous.model_dump(exclude={"retrieved_at"}) == document.model_dump(exclude={"retrieved_at"}):
                    return "unchanged"
            texts = [document.title + "\n" + text for text in chunks(document.body)]
            embeddings = self.embedding.encode(texts)
            with self.db:
                ids = self.db.execute("SELECT id FROM chunks WHERE document_id=?", (document.id,)).fetchall()
                self.db.executemany("DELETE FROM vectors WHERE rowid=?", [(row[0],) for row in ids])
                self.db.execute("DELETE FROM chunks WHERE document_id=?", (document.id,))
                self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?)", (document.id, fingerprint, document.kind, document.source, document.published_at.isoformat(), document.is_demo, document.model_dump_json()))
                for text, vector in zip(texts, embeddings):
                    cursor = self.db.execute("INSERT INTO chunks(document_id,text,lexical) VALUES(?,?,?)", (document.id, text, json.dumps(tokens(text), ensure_ascii=False)))
                    self.db.execute("INSERT INTO vectors(rowid,embedding) VALUES(?,?)", (cursor.lastrowid, vector.astype(np.float32).tobytes()))
            return "updated" if existing else "inserted"

    def search(self, question, *, limit=5, start=None, end=None, kind=None, country=None, commodity=None, allow_demo=False):
        sql = "SELECT c.id,c.document_id,c.text,c.lexical,d.data FROM chunks c JOIN documents d ON d.id=c.document_id WHERE 1=1"
        params = []
        for column, operator, value in (("published_at", ">=", start), ("published_at", "<", end), ("kind", "=", kind)):
            if value is not None:
                sql += f" AND d.{column}{operator}?"
                params.append(value.isoformat() if hasattr(value, "isoformat") else value)
        if not allow_demo:
            sql += " AND d.is_demo=0"
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
            candidates = []
            for row in rows:
                doc = json.loads(row["data"])
                if country and doc["country"] != country:
                    continue
                if commodity and doc["commodity"] != commodity:
                    continue
                candidates.append((row, doc))
            if not candidates:
                return []
            corpus = [json.loads(row["lexical"]) for row, _ in candidates]
            lexical = BM25Okapi(corpus).get_scores(tokens(question))
            query_vec = self.embedding.encode([question], query=True)[0].tobytes()
            # ponytail: exact scan suits this 600-document task; use partitioned ANN beyond ~100k chunks.
            distances = dict(self.db.execute("SELECT rowid,vec_distance_cosine(embedding,?) FROM vectors", (query_vec,)).fetchall())
        dense_order = sorted(range(len(candidates)), key=lambda i: distances[candidates[i][0]["id"]])
        lexical_order = sorted(range(len(candidates)), key=lambda i: lexical[i], reverse=True)
        scores = {i: 0.0 for i in range(len(candidates))}
        for ordering in (dense_order, lexical_order):
            for rank, i in enumerate(ordering, 1):
                scores[i] += 1 / (60 + rank)
        result, used = [], set()
        query_terms = set(tokens(question))
        for i in sorted(scores, key=scores.get, reverse=True):
            row, doc = candidates[i]
            if row["document_id"] in used:
                continue
            # Hash mode has no semantic confidence; reject candidates with no lexical evidence.
            overlap = query_terms.intersection(corpus[i]) - {"_empty_"}
            if self.embedding.backend == "hash" and not overlap:
                continue
            if self.embedding.backend == "fastembed" and distances[row["id"]] > 0.35 and not overlap:
                continue
            used.add(row["document_id"])
            result.append({"document": doc, "text": row["text"], "score": scores[i], "distance": distances[row["id"]]})
            if len(result) == limit:
                break
        return result

    def counts(self, start=None, end=None):
        clauses = ["is_demo=0"]
        params = []
        if start:
            clauses.append("published_at>=?")
            params.append(start.isoformat())
        if end:
            clauses.append("published_at<?")
            params.append(end.isoformat())
        with self.lock:
            rows = self.db.execute("SELECT kind,source,count(*) n FROM documents WHERE " + " AND ".join(clauses) + " GROUP BY kind,source", params).fetchall()
        return [dict(row) for row in rows]
