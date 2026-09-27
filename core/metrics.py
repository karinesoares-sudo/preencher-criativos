"""Identificação do criativo pelo nome e cálculo das métricas."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Iterable

BASE_KEYS = ("spend", "impressions", "clicks", "link_clicks", "video_3s", "video_p75", "sales", "revenue")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[\"'´`’”]+", "", t)
    return re.sub(r"\s+", " ", t).strip().upper()


def build_code_regex(prefixes: Iterable[str]) -> re.Pattern:
    """Ex.: prefixos {'LT'} → casa LT1900, LT1711.49.32; não casa CA1, V2 etc."""
    pref = sorted({p.upper() for p in prefixes if p}, key=len, reverse=True)
    if not pref:
        pref = ["[A-Z]{1,4}"]
    alt = "|".join(re.escape(p) if p.isalpha() else p for p in pref)
    return re.compile(rf"(?<![A-Z0-9])(?:{alt})\d+(?:\.\d+)*(?![0-9])", re.IGNORECASE)


def extract_codes(text: str, rx: re.Pattern) -> list[str]:
    seen, out = set(), []
    for m in rx.finditer(str(text or "")):
        c = m.group(0).upper()
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def prefixes_from_codes(codes: Iterable[str]) -> set[str]:
    out = set()
    for c in codes:
        m = re.match(r"([A-Za-z]+)\d", c)
        if m:
            out.add(m.group(1).upper())
    return out


class Aggregator:
    """Soma números base por código de criativo."""

    def __init__(self, rx: re.Pattern):
        self.rx = rx
        self.data: dict[str, dict[str, float]] = defaultdict(lambda: {k: 0.0 for k in BASE_KEYS})
        self.unmatched: dict[str, float] = defaultdict(float)  # nome → gasto/vendas sem código

    def add(self, name: str, values: dict[str, float]) -> None:
        codes = extract_codes(name, self.rx)
        if not codes:
            self.unmatched[name] += values.get("spend", 0) or values.get("sales", 0)
            return
        code = codes[0]  # o primeiro código do nome do anúncio é o criativo
        for k, v in values.items():
            self.data[code][k] += v

    def merge(self, other: "Aggregator") -> None:
        for code, vals in other.data.items():
            for k, v in vals.items():
                self.data[code][k] += v
        for n, v in other.unmatched.items():
            self.unmatched[n] += v

    def sum_for(self, codes: Iterable[str], include_variations: bool = False) -> dict[str, float] | None:
        total = {k: 0.0 for k in BASE_KEYS}
        found = False
        wanted = [c.upper() for c in codes]
        for code, vals in self.data.items():
            hit = code in wanted or (include_variations and any(code.startswith(w + ".") for w in wanted))
            if hit:
                found = True
                for k in BASE_KEYS:
                    total[k] += vals[k]
        return total if found else None


def _div(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute(base: dict[str, float]) -> dict[str, float]:
    imp, spend, clicks, sales = base["impressions"], base["spend"], base["clicks"], base["sales"]
    return {
        "hook": _div(base["video_3s"], imp),
        "body": _div(base["video_p75"], imp),
        "cpm": _div(spend, imp) * 1000,
        "ctr": _div(clicks, imp),
        "cpc": _div(spend, clicks),
        "gasto": spend,
        "vendas": sales,
        "cpa": _div(spend, sales),
        "roas": _div(base["revenue"], spend),
        "faturamento": base["revenue"],
        "impressoes": imp,
        "cliques": clicks,
    }


# Cabeçalho da planilha (normalizado) → métrica
HEADER_TO_METRIC = [
    ("HOOK", "hook"),
    ("BODY", "body"),
    ("CPM", "cpm"),
    ("CTR", "ctr"),
    ("CPC", "cpc"),
    ("GASTO", "gasto"),
    ("INVESTIMENTO", "gasto"),
    ("VENDAS", "vendas"),
    ("CPA", "cpa"),
    ("ROAS", "roas"),
    ("FATURAMENTO", "faturamento"),
    ("RECEITA", "faturamento"),
    ("IMPRESS", "impressoes"),
    ("CLIQUES", "cliques"),
]

METRIC_FORMAT = {
    "hook": "pct", "body": "pct", "ctr": "pct",
    "cpm": "brl", "cpc": "brl", "gasto": "brl", "cpa": "brl", "faturamento": "brl",
    "vendas": "int", "impressoes": "int", "cliques": "int",
    "roas": "dec",
}


def header_metric(header: str) -> str | None:
    h = normalize(header)
    if not h:
        return None
    for key, metric in HEADER_TO_METRIC:
        if h == key or h.startswith(key + " ") or h.startswith(key):
            return metric
    return None
