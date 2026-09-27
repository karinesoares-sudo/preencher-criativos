"""RedTrack: relatório agrupado pelo sub que carrega o nome do anúncio."""
from __future__ import annotations

from datetime import date
from typing import Callable

from .http import get_json

API = "https://api.redtrack.io"
PER_PAGE = 1000  # máximo aceito pela API


def fetch_report_by_sub(
    api_key: str,
    since: date,
    until: date,
    *,
    group: str = "sub5",
    campaign_ids: list[str] | None = None,
    timezone: str = "",
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Todas as páginas do /report agrupado por `group` (ex.: sub5, rt_ad)."""
    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "api_key": api_key,
            "group": group,
            "date_from": since.isoformat(),
            "date_to": until.isoformat(),
            "per": PER_PAGE,
            "page": page,
        }
        if campaign_ids:
            params["campaign_id"] = ",".join(campaign_ids)
        if timezone:
            params["timezone"] = timezone
        payload = get_json(f"{API}/report", params, log=log)
        items = payload.get("items", payload.get("data", [])) if isinstance(payload, dict) else (payload or [])
        rows.extend(items)
        if log:
            log(f"RedTrack página {page}: {len(items)} linhas")
        if len(items) < PER_PAGE:
            return rows
        page += 1


def fetch_campaigns(api_key: str, log=None) -> list[dict]:
    """Lista de campanhas (id + título) para filtrar, se quiser."""
    out: list[dict] = []
    page = 1
    while True:
        payload = get_json(f"{API}/campaigns", {"api_key": api_key, "per": PER_PAGE, "page": page}, log=log)
        items = payload.get("items", payload.get("data", [])) if isinstance(payload, dict) else (payload or [])
        out.extend({"id": str(c.get("id")), "title": c.get("title") or c.get("name") or str(c.get("id"))} for c in items)
        if len(items) < PER_PAGE:
            return out
        page += 1


def num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


CANDIDATE_FIELDS = [f"sub{i}" for i in range(1, 21)] + ["rt_ad", "rt_adgroup", "rt_campaign", "rt_ad_id"]


def detect_sub(api_key: str, since: date, until: date, matches, *, max_pages: int = 5, log=None):
    """Olha as conversões do período e descobre qual sub traz o criativo (nome ou ID do anúncio).
    matches(valor) -> código ou None. Retorna (melhor_campo, placar, tipos_de_conversão)."""
    scores = {f: 0 for f in CANDIDATE_FIELDS}
    types: dict[str, int] = {}
    total = 0
    for page in range(1, max_pages + 1):
        payload = get_json(f"{API}/conversions", {"api_key": api_key, "date_from": since.isoformat(),
                                                  "date_to": until.isoformat(), "per": PER_PAGE, "page": page}, log=log)
        items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
        for c in items:
            total += 1
            t = str(c.get("type") or "?")
            types[t] = types.get(t, 0) + 1
            for f in CANDIDATE_FIELDS:
                if c.get(f) and matches(str(c.get(f))):
                    scores[f] += 1
        if len(items) < PER_PAGE:
            break
    best = max(scores, key=scores.get) if total else None
    if best and scores[best] == 0:
        best = None
    if log:
        top = ", ".join(f"{k}={v}" for k, v in sorted(scores.items(), key=lambda x: -x[1])[:3])
        log(f"🔍 RedTrack: {total} conversões analisadas; campos que trazem o criativo: {top}")
    return best, scores, types


def fetch_conversions(api_key: str, since: date, until: date, *, campaign_ids: list[str] | None = None,
                      max_pages: int = 50, log=None) -> list[dict]:
    """Lista de conversões (uma por venda/evento), com o nome do tipo (ex.: Purchase) e os subs."""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"api_key": api_key, "date_from": since.isoformat(), "date_to": until.isoformat(),
                  "per": PER_PAGE, "page": page}
        if campaign_ids:
            params["campaign_id"] = ",".join(campaign_ids)
        payload = get_json(f"{API}/conversions", params, log=log)
        items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
        out.extend(items)
        if log:
            log(f"RedTrack conversões página {page}: {len(items)}")
        if len(items) < PER_PAGE:
            break
    return out
