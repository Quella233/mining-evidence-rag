from datetime import date, datetime, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eval.fixtures import build
from eval.run import evaluate
from pipeline.collect import import_file, window
from pipeline.models import Document, canonical_url
from pipeline.sources import Fetcher, article, shfe_documents
from pipeline.store import Store
from serve.app import Query, create_app, filters, query_store


def make_doc(**changes):
    values = dict(kind="policy", source="test", url="https://example.invalid/article", title="澳洲锂出口政策", body="澳洲锂出口政策要求提供原产地证书。", published_at="2026-10-05T08:00:00Z", country="australia", commodity="lithium")
    values.update(changes)
    return Document(**values)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "test.db")
    yield instance
    instance.close()


def test_dedup_tracking_and_reingest(store):
    first = make_doc(url="https://example.invalid/article?utm_source=rss#top")
    second = make_doc()
    assert first.id == second.id
    assert store.upsert(first) == "inserted"
    store.upsert(second)
    assert store.upsert(second) == "unchanged"
    assert store.db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1


def test_duplicate_content_different_url(store):
    store.upsert(make_doc())
    assert store.upsert(make_doc(url="https://example.invalid/syndicated")) == "duplicate"


def test_update_replaces_old_chunks(store):
    store.upsert(make_doc(body="旧政策内容。" * 700))
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 1
    assert store.upsert(make_doc(body="澳洲锂出口政策要求提供新的批次信息。")) == "updated"
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1


def test_chunk_hits_are_distinct_documents(store):
    store.upsert(make_doc(body="澳洲锂出口政策要求申报。" * 500))
    assert len(store.search("锂出口政策", limit=5)) == 1


def test_date_window_and_metadata_filter(store):
    store.upsert(make_doc())
    store.upsert(make_doc(url="https://example.invalid/old", body="澳洲锂旧政策。", published_at="2026-09-01T00:00:00Z"))
    store.upsert(make_doc(url="https://example.invalid/china", body="中国锂出口政策。", country="china"))
    response = query_store(store, Query(question="近7天澳洲锂出口政策有何变化？", as_of=date(2026, 10, 8)))
    assert len(response["citations"]) == 1
    assert response["filters"]["start"] == "2026-10-02T00:00:00+00:00"
    assert response["citations"][0]["document_id"] == make_doc().id


def test_end_date_excludes_next_day(store):
    store.upsert(make_doc(published_at="2026-10-09T00:00:00Z"))
    start, end = window(date(2026, 10, 8), 7)
    assert not store.search("锂", start=start, end=end)


def test_demo_isolation(store):
    store.upsert(make_doc(is_demo=True))
    assert store.counts() == []
    assert store.search("锂") == []
    assert len(store.search("锂", allow_demo=True)) == 1


def test_no_evidence_abstains(store):
    store.upsert(make_doc())
    result = query_store(store, Query(question="澳洲铁矿价格多少？", as_of=date(2026, 10, 8)))
    assert result["insufficient_evidence"]
    assert not result["claims"]
    assert "不能据此断言没有变化" in result["answer"]


def test_explicit_filters_override_inference():
    applied = filters(Query(question="近7天澳洲锂政策", country="china", days=14, as_of=date(2026, 10, 8)))
    assert applied["country"] == "china"
    assert (applied["end"] - applied["start"]).days == 14


@pytest.mark.parametrize("values", [dict(question=" "), dict(question="测试", top_k=100), dict(question="测试", start_date="2026-10-01"), dict(question="测试", start_date="2026-10-08", end_date="2026-10-01")])
def test_invalid_queries(values):
    with pytest.raises(ValidationError):
        Query(**values)


def test_article_jsonld_and_missing_dates():
    text = "Australia published a critical minerals strategy supporting lithium processing and transparent export reporting. " * 3
    html = '<script type="application/ld+json">{"@graph":[{"datePublished":"2026-10-04T10:00:00Z"}]}</script><article><h1>Lithium strategy</h1><p>' + text + '</p></article>'
    doc = article(html, "https://example.invalid/p", "test", "policy")
    assert doc.published_at.day == 4
    assert doc.country == "australia"
    assert doc.commodity == "lithium"
    with pytest.raises(ValueError, match="publication date"):
        article("<article><h1>Missing date</h1>" + text + "</article>", "https://example.invalid/p", "test", "policy")


def test_paywall_rejected():
    html = '<meta name="date" content="2026-10-04"><article><h1>Title</h1><p>' + "subscribe to continue reading " * 10 + '</p></article>'
    with pytest.raises(PermissionError):
        article(html, "https://example.invalid/p", "test", "news")


def test_real_source_layout_regressions():
    html = '<meta property="og:title" content="Actual mining title"><meta property="article:published_time" content="2026-10-08T09:00:00Z"><h1>News</h1><article><div class="gdm-widget">Unwanted promotion</div><div class="post-content">' + "A mining company announced a new nickel processing plant in Australia. " * 4 + '</div></article>'
    doc = article(html, "https://example.invalid/p", "test", "news")
    assert doc.title == "Actual mining title"
    assert "Unwanted promotion" not in doc.body
    html = '<div class="field--name-field-news-date">8 October 2026</div><h1>Policy</h1><main>' + "A new critical minerals export policy was announced by the government. " * 4 + '</main>'
    assert article(html, "https://example.invalid/p", "test", "policy").published_at.date() == date(2026, 10, 8)


def test_forecast_is_not_automatically_a_policy():
    from pipeline.sources import relevant_policy
    doc = make_doc(title="Copper export earnings forecast", body="Australia copper export earnings are forecast to rise as commodity prices recover.")
    assert not relevant_policy(doc, {"keywords": ["copper", "export"]})
    doc.body = "Australia published a copper export control policy with new reporting obligations."
    assert relevant_policy(doc, {"keywords": ["copper", "export"]})


def test_delete_removes_vectors(store):
    doc = make_doc()
    store.upsert(doc)
    store.delete(doc.id)
    assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    assert not store.search("锂")


def test_shfe_contracts_not_summed_or_mislabeled():
    payload = {"report_date": "20261008", "o_curinstrument": [{"PRODUCTID": "cu_f", "DELIVERYMONTH": "2610", "SETTLEMENTPRICE": 70000}, {"PRODUCTID": "cu_f", "DELIVERYMONTH": "2611", "SETTLEMENTPRICE": 71000}, {"PRODUCTID": "cu_f", "DELIVERYMONTH": "小计", "SETTLEMENTPRICE": 0}, {"PRODUCTID": "cu_f", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": "NaN"}]}
    docs = list(shfe_documents(payload, date(2026, 10, 8), "https://example.invalid/prices"))
    assert len(docs) == 2 and docs[0].id != docs[1].id
    assert docs[0].currency == "CNY" and docs[0].source == "SHFE-public-supplement"
    with pytest.raises(ValueError, match="date mismatch"):
        list(shfe_documents(payload, date(2026, 10, 7), "https://example.invalid/prices"))


def test_price_requires_units():
    with pytest.raises(ValidationError):
        make_doc(kind="price", price=12)


def test_robots_and_retry(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(200, text="ok")
    fetcher = Fetcher(cache=tmp_path / "cache", delay=0, retries=0)
    fetcher.client.close()
    fetcher.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PermissionError):
            fetcher.get("https://example.invalid/private")
        assert "/private" not in calls
        assert fetcher.get("https://example.invalid/public").text == "ok"
    finally:
        fetcher.close()


def test_import_and_validation(store, tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_text(make_doc().model_dump_json() + "\n", encoding="utf-8")
    assert import_file(store, path) == {"inserted": 1}
    assert import_file(store, path) == {"unchanged": 1}
    path.write_text('{"kind":"price"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Import line 1"):
        import_file(store, path)


def test_api_lifespan_and_validation(tmp_path):
    db = tmp_path / "api.db"
    store = Store(db)
    store.upsert(make_doc())
    store.close()
    with TestClient(create_app(db_path=str(db), embedding_backend="hash")) as client:
        assert client.get("/health").json()["status"] == "ok"
        result = client.post("/query", json={"question": "近7天澳洲锂出口政策", "as_of": "2026-10-08"})
        assert result.status_code == 200
        assert len(result.json()["citations"]) == 1
        assert client.post("/query", json={"question": "a"}).status_code == 422
        assert client.post("/query", json={"question": "近999天政策"}).status_code == 422


def test_manual_twenty_question_benchmark(tmp_path):
    db, gold = tmp_path / "demo.db", tmp_path / "gold.jsonl"
    build(db, gold)
    store = Store(db)
    try:
        cases = [json.loads(line) for line in gold.read_text(encoding="utf-8").splitlines()]
        report = evaluate(store, cases, allow_demo=True)
        assert report["n"] == 20
        assert report["recall_at_5"] >= 0.85
        assert report["answer_faithfulness"] == 1.0
        assert report["expected_answer_accuracy"] >= 0.85
    finally:
        store.close()
