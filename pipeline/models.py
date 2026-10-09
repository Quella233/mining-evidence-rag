from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

ALIASES = {
    "australia": ("澳洲", "澳大利亚", "australia", "australian"),
    "china": ("中国", "china", "chinese"),
    "copper": ("铜", "copper"),
    "zinc": ("锌", "zinc"),
    "nickel": ("镍", "nickel"),
    "lithium": ("锂", "lithium"),
    "iron ore": ("铁矿", "iron ore"),
    "rare earth": ("稀土", "rare earth"),
    "export": ("出口", "export"),
    "policy": ("政策", "policy", "strategy", "regulation"),
    "price": ("价格", "报价", "price", "settlement"),
}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_") and k not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def tags(text: str) -> list[str]:
    text = text.lower()
    return [key for key, words in ALIASES.items() if any(word in text for word in words)]


def tokens(text: str) -> list[str]:
    text = text.lower()
    output = re.findall(r"[a-z0-9]+", text)
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        output.extend(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    # Domain aliases make Chinese questions match English source documents.
    output.extend(tag.replace(" ", "_") for tag in tags(text))
    return output or ["_empty_"]


class Document(BaseModel):
    id: str = ""
    kind: str
    source: str = Field(min_length=1)
    url: HttpUrl
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    published_at: datetime
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    country: str | None = None
    commodity: str | None = None
    price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    currency: str | None = None
    unit: str | None = None
    contract: str | None = None
    price_type: str | None = None
    effective_date: date | None = None
    is_demo: bool = False

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value):
        if value not in {"news", "policy", "price"}:
            raise ValueError("kind must be news, policy or price")
        return value

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None:
            raise ValueError("timestamps must include timezone")
        return value.astimezone(timezone.utc)

    @field_validator("title", "body")
    @classmethod
    def normalized(cls, value):
        value = clean(value)
        if not value:
            raise ValueError("empty text")
        return value

    @model_validator(mode="after")
    def identity(self):
        if self.kind == "price" and (self.price is None or not all((self.currency, self.unit, self.commodity, self.price_type))):
            raise ValueError("price requires value, currency, unit, commodity and price_type")
        identity = canonical_url(str(self.url))
        if self.kind == "price":
            identity += "|" + "|".join(str(x) for x in (self.source, self.published_at.date(), self.commodity, self.contract, self.price_type, self.currency, self.unit))
        self.id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return self


def chunks(text: str, size=1000, overlap=150) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size - overlap)]
