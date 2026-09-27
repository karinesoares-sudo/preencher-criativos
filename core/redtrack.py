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
