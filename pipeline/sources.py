from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil.parser import isoparse, parse as parse_datetime

from pipeline.models import Document, clean, tags

USER_AGENT = "MiningInterviewResearch/1.0 (+local educational research; low request rate)"


class Fetcher:
    def __init__(self, cache="data/cache/http", delay=0.6, timeout=15, retries=2):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.delay, self.retries = delay, retries
        self.robots = {}
        self.last = {}

    def close(self):
        self.client.close()

    def get(self, url, *, public_api=False):
        import hashlib
        host = urlsplit(url).netloc
        if not public_api:
            if host not in self.robots:
                robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
                response = self.client.get(robots_url)
                if response.status_code in (404, 410):
                    self.robots[host] = None
                else:
                    response.raise_for_status()
                    parser = RobotFileParser(robots_url)
                    parser.parse(response.text.splitlines())
                    self.robots[host] = parser
            robot = self.robots[host]
            if robot and not robot.can_fetch(USER_AGENT, url):
                raise PermissionError(f"robots.txt disallows {url}")
        delay = max(0, self.delay - (time.monotonic() - self.last.get(host, 0)))
        time.sleep(delay)
        response = None
        for attempt in range(self.retries + 1):
            try:
                self.last[host] = time.monotonic()
                response = self.client.get(url)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    retry = response.headers.get("Retry-After", "")
                    pause = min(float(retry), 10) if retry.isdigit() else 2 ** attempt
                    time.sleep(pause)
                    continue
                response.raise_for_status()
                if len(response.content) > 20_000_000:
                    raise ValueError("Response too large")
                stem = hashlib.sha256(url.encode()).hexdigest()
                (self.cache / (stem + ".body")).write_bytes(response.content)
                (self.cache / (stem + ".json")).write_text(json.dumps({"url": str(response.url), "requested_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "status": response.status_code, "sha256": hashlib.sha256(response.content).hexdigest()}, indent=2), encoding="utf-8")
                return response
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self.retries:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("Request failed")


def parse_date(value):
    if not value:
        return None
    try:
        parsed = isoparse(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def article(html, url, source, kind, *, fallback_title=None, fallback_date=None, country=None):
    soup = BeautifulSoup(html, "html.parser")
    published = None
    # DISR shows publication dates in a Drupal field; updated_time is not publication time.
    date_node = soup.select_one(".field--name-field-news-date")
    if date_node:
        try:
            published = parse_datetime(date_node.get_text(strip=True)).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for selector in ('meta[property="article:published_time"]', 'meta[name="date"]', 'meta[name="publishdate"]', 'meta[name="pubdate"]', 'meta[name="DCTERMS.issued"]', 'time[datetime]'):
        node = soup.select_one(selector)
        if node and published is None:
            published = parse_date(node.get("content") or node.get("datetime"))
            if published:
                break
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
            items = payload if isinstance(payload, list) else [payload]
            items += [item for obj in list(items) if isinstance(obj, dict) for item in obj.get("@graph", [])]
            for item in items:
                if isinstance(item, dict) and item.get("datePublished"):
                    published = published or parse_date(item["datePublished"])
        except (ValueError, TypeError):
            pass
    published = published or fallback_date
    if published is None:
        raise ValueError("No verifiable publication date (not using crawl date)")
    heading = soup.select_one(".post-main h1, .article-header h1") or soup.find("h1")
    og_title = soup.select_one('meta[property="og:title"]')
    title = clean(og_title.get("content", "")) if og_title else clean(heading.get_text(" ")) if heading else fallback_title
    if not title:
        raise ValueError("Missing article title")
    for node in soup.select("script,style,nav,footer,header,aside,form,.gdm-widget,.recommended-whitepapers,.newsletter-signup"):
        node.decompose()
    root = next((node for selector in (".article-content .main-content", ".post-content", ".entry-content", ".field--name-body", ".article-body", "article", "main") if (node := soup.select_one(selector)) is not None), None)
    if root is None:
        raise ValueError("No identifiable article body; selector needs source-specific update")
    body = clean(root.get_text(" ", strip=True))
    if len(body) < 120:
        raise ValueError("Article body too short; paywall or incomplete extraction")
    lower = body.lower()
    if any(phrase in lower for phrase in ("subscribe to continue reading", "sign in to read the full", "access denied", "unlock free access to premium content")):
        raise PermissionError("Article requires authorization")
    found = tags(title + " " + body)
    country = country or next((x for x in ("australia", "china") if x in found), None)
    commodity = next((x for x in ("copper", "zinc", "nickel", "lithium", "iron ore", "rare earth") if x in found), None)
    return Document(kind=kind, source=source, url=url, title=title, body=body, published_at=published, country=country, commodity=commodity)


def relevant_policy(doc, config):
    text = (doc.title + " " + doc.body).lower()
    topical = any(word in text for word in config["keywords"])
    regulatory = any(word in text for word in ("policy", "strategy", "regulation", "legislation", "consultation", "permit", "subsid", "export control", "funding", "grant", "政策", "条例", "办法", "出口管制", "战略", "征求意见"))
    return topical and regulatory


def rss_documents(fetcher, config, start, end, errors):
    visited = set()
    for page in range(1, config.get("max_pages", 5) + 1):
        url = config["url"].format(page=page)
        feed = feedparser.parse(fetcher.get(url).content)
        if not feed.entries:
            if page == 1:
                errors.append({"url": url, "error": "Feed has no entries or is not RSS/Atom"})
            break
        dates = [parse_date(entry.get("published")) for entry in feed.entries]
        if dates and all(dt is not None and dt < start for dt in dates):
            break
        new = False
        for entry in feed.entries:
            link = entry.get("link")
            published = parse_date(entry.get("published"))
            if not link or link in visited:
                continue
            visited.add(link)
            new = True
            if published and not start <= published < end:
                continue
            try:
                response = fetcher.get(link)
                doc = article(response.text, str(response.url), config["name"], config["kind"], fallback_title=entry.get("title"), fallback_date=published)
                if start <= doc.published_at < end:
                    yield doc
            except (httpx.HTTPError, ValueError, PermissionError) as exc:
                errors.append({"url": link, "error": str(exc)[:300]})
        if not new or "{page}" not in config["url"]:
            break


def policy_documents(fetcher, config, start, end, errors):
    seeds = config["urls"]
    visited, candidates = set(), list(seeds)
    seed_hosts = {urlsplit(url).netloc for url in seeds}
    for url in candidates:
        if url in visited or len(visited) >= config.get("max_pages", 80):
            continue
        visited.add(url)
        try:
            response = fetcher.get(url)
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.select("main a[href], article a[href]"):
                link = urljoin(str(response.url), anchor["href"]).split("#")[0]
                if any(key != "page" for key in parse_qs(urlsplit(link).query)):
                    continue
                if urlsplit(link).netloc in seed_hosts and any(word in link.lower() for word in config.get("path_terms", ["news", "critical-mineral", "rare-earth"])):
                    if link not in visited and link not in candidates and len(candidates) < config.get("max_pages", 80) * 3:
                        candidates.append(link)
            try:
                doc = article(response.text, str(response.url), config["name"], "policy", country=config.get("country"))
            except ValueError as exc:
                errors.append({"url": url, "error": str(exc)})
                continue
            if start <= doc.published_at < end and relevant_policy(doc, config):
                yield doc
        except (httpx.HTTPError, ValueError, PermissionError) as exc:
            errors.append({"url": url, "error": str(exc)[:300]})


def shfe_documents(payload, day, url):
    reported = str(payload.get("report_date", "")).replace("-", "")[:8]
    if reported and reported != day.strftime("%Y%m%d"):
        raise ValueError(f"Price report date mismatch: {reported}")
    for row in payload.get("o_curinstrument", []):
        product = str(row.get("PRODUCTID", "")).strip().split("_")[0]
        commodity = {"cu": "copper", "zn": "zinc", "ni": "nickel"}.get(product)
        contract_month = str(row.get("DELIVERYMONTH", "")).strip()
        if not commodity or not contract_month.isdigit():
            continue
        value = row.get("SETTLEMENTPRICE")
        try:
            value = float(str(value).replace(",", ""))
        except ValueError:
            continue
        if not 0 < value < float("inf"):
            continue
        contract = product + contract_month
        yield Document(kind="price", source="SHFE-public-supplement", url=url, title=f"SHFE {commodity} {contract} settlement {day}", body=f"On {day}, Shanghai Futures Exchange {commodity} contract {contract} daily settlement price was {value:g} CNY per tonne. This is a futures settlement, not an LME spot quote.", published_at=datetime(day.year, day.month, day.day, 7, tzinfo=timezone.utc), country="china", commodity=commodity, price=value, currency="CNY", unit="tonne", contract=contract, price_type="futures_settlement")
