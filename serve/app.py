from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from pipeline.collect import window
from pipeline.models import tags, tokens
from pipeline.store import Embedder, Store


class Query(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    as_of: date | None = None
    days: int | None = Field(default=None, ge=1, le=366)
    start_date: date | None = None
    end_date: date | None = None
    kind: Literal["news", "policy", "price"] | None = None
    country: Literal["china", "australia"] | None = None
    commodity: Literal["copper", "zinc", "nickel", "lithium", "iron ore", "rare earth"] | None = None

    @model_validator(mode="after")
    def dates(self):
        if not self.question.strip():
            raise ValueError("question must not be blank")
        if bool(self.start_date) != bool(self.end_date):
            raise ValueError("start_date and end_date must be supplied together")
        if self.start_date and self.start_date > self.end_date:
            raise ValueError("start_date must be <= end_date")
        if self.start_date and (self.end_date - self.start_date).days > 365:
            raise ValueError("date range cannot exceed 366 days")
        return self


def filters(query: Query):
    found = tags(query.question)
    end_day = query.end_date or query.as_of or date.today()
    days = query.days
    match = re.search(r"(?:近|最近|过去|last|past)\s*(\d+)\s*(?:天|日|days?)", query.question, re.I)
    if days is None:
        days = int(match[1]) if match else 7 if any(x in query.question for x in ("近一周", "最近一周", "过去一周")) else 30
    if not 1 <= days <= 366:
        raise ValueError("Question date range must be between 1 and 366 days")
    if query.start_date:
        days = (end_day - query.start_date).days + 1
    start, end = window(end_day, days)
    kind = query.kind or next((x for x in ("policy", "price") if x in found), None)
    country = query.country or next((x for x in ("australia", "china") if x in found), None)
    commodity = query.commodity or next((x for x in ("copper", "zinc", "nickel", "lithium", "iron ore", "rare earth") if x in found), None)
    return dict(start=start, end=end, kind=kind, country=country, commodity=commodity)


def query_store(store, query: Query, allow_demo=False):
    applied = filters(query)
    hits = store.search(query.question, limit=query.top_k, allow_demo=allow_demo, **applied)
    evidence, claims = [], []
    query_terms = set(tokens(query.question))
    for i, hit in enumerate(hits, 1):
        doc = hit["document"]
        # Extract verbatim source sentences; no unsupported paraphrase or causal inference.
        body = hit["text"].split("\n", 1)[-1]
        spans = list(re.finditer(r"[^。！？.!?]+[。！？.!?]*", body))
        if spans:
            best = max(range(len(spans)), key=lambda j: len(query_terms.intersection(tokens(spans[j].group()))))
            first, last = max(0, best - 1), min(len(spans) - 1, best + 2)
            quote = body[spans[first].start():spans[last].end()].strip()[:1200]
        else:
            quote = body[:1200]
        evidence.append({"citation": i, "document_id": doc["id"], "title": doc["title"], "source": doc["source"], "url": doc["url"], "published_at": doc["published_at"], "quote": quote, "score": round(hit["score"], 6), "is_demo": doc["is_demo"], "price": doc.get("price"), "currency": doc.get("currency"), "unit": doc.get("unit"), "contract": doc.get("contract"), "price_type": doc.get("price_type")})
        claims.append({"text": quote, "citation": i})
    warnings = ["检索结果来自当前已入库数据，并不代表完整市场覆盖。无结果不能证明没有政策变化。", "当前为可核验的原文摘录回答，不自动推断政策因果，也不混算不同合约、币种或价格口径。"]
    if store.embedding.backend == "hash":
        warnings.append("当前使用离线哈希向量基线，语义理解有限；生产使用请构建 fastembed 多语言向量库。")
    if any(item["is_demo"] for item in evidence):
        warnings.append("当前结果包含明确标注的合成测试数据，不是真实新闻、政策或报价。")
    if not evidence:
        answer = "现有数据库在指定日期和筛选条件下没有足够证据回答。请补充对应来源数据，不能据此断言没有变化。"
    else:
        answer = "检索到以下原文证据：\n" + "\n".join(f"[{item['citation']}] {item['quote']}" for item in evidence)
    return {"question": query.question, "answer": answer, "answer_mode": "extractive", "insufficient_evidence": not bool(evidence), "claims": claims, "citations": evidence, "filters": {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in applied.items()}, "embedding": store.embedding.backend, "warnings": warnings}


def create_app(db_path=None, embedding_backend=None, allow_demo=None):
    path = db_path or os.environ.get("MINING_DB", "data/mining.db")
    backend = embedding_backend or os.environ.get("EMBEDDING_BACKEND", "hash")
    demo = allow_demo if allow_demo is not None else os.environ.get("ALLOW_DEMO", "false").lower() == "true"

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(path, Embedder(backend))
        yield
        app.state.store.close()

    app = FastAPI(title="矿业三源检索 API", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok", "embedding": backend, "allow_demo": demo, "counts": request.app.state.store.counts()}

    @app.post("/query")
    def query(payload: Query, request: Request):
        try:
            return query_store(request.app.state.store, payload, demo)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
