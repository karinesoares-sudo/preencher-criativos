"""Facebook Ads (Graph API): contas e insights por anúncio, com paginação completa."""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Callable

from .http import ApiError, get_json

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v23.0"

INSIGHT_FIELDS = [
    "ad_id",
    "ad_name",
    "spend",
    "impressions",
    "clicks",
    "inline_link_clicks",
    "actions",
    "video_p75_watched_actions",
]


def _paginate(url: str, params: dict | None, log=None, attempts: int = 6) -> list[dict]:
    """Segue paging.next até o fim — nada fica de fora."""
    out: list[dict] = []
    payload = get_json(url, params, log=log, max_attempts=attempts)
    while True:
        out.extend(payload.get("data", []))
        nxt = (payload.get("paging") or {}).get("next")
        if not nxt:
            return out
        payload = get_json(nxt, None, log=log, max_attempts=attempts)  # a URL "next" já traz token e cursor


def list_ad_accounts(token: str, version: str = DEFAULT_VERSION, log=None) -> list[dict]:
    """Contas do usuário + contas próprias e de clientes de cada BM (sem duplicar)."""
    base = f"{GRAPH}/{version}"
    fields = "name,account_id,account_status,currency"
    accounts: dict[str, dict] = {}

    def add(rows, bm_name=""):
        for r in rows:
            aid = r.get("account_id") or r.get("id", "").replace("act_", "")
            if aid and aid not in accounts:
                accounts[aid] = {
                    "id": f"act_{aid}",
                    "name": r.get("name", aid),
                    "status": r.get("account_status"),
                    "currency": r.get("currency", ""),
                    "bm": bm_name,
                }

    add(_paginate(f"{base}/me/adaccounts", {"access_token": token, "fields": fields, "limit": 500}, log))
    try:
        bms = _paginate(f"{base}/me/businesses", {"access_token": token, "fields": "id,name", "limit": 200}, log)
    except ApiError:
        bms = []
    for bm in bms:
        for edge in ("owned_ad_accounts", "client_ad_accounts"):
            try:
                rows = _paginate(f"{base}/{bm['id']}/{edge}", {"access_token": token, "fields": fields, "limit": 500}, log)
                add(rows, bm.get("name", ""))
            except ApiError as e:
                if log:
                    log(f"⚠️ BM {bm.get('name')}: {edge} indisponível ({e})")
    return sorted(accounts.values(), key=lambda a: a["name"].lower())


def fetch_ad_insights(
    account_id: str,
    since: date,
    until: date,
    token: str,
    *,
    name_contains: str = "",
    version: str = DEFAULT_VERSION,
    log: Callable[[str], None] | None = None,
    limit: int = 500,
    deadline: float | None = None,
) -> list[dict]:
    """Insights no nível de anúncio (período somado). Se o Facebook reclamar de volume, divide o período
    e, se mesmo assim não der, pede páginas menores."""
    params = {
        "access_token": token,
        "level": "ad",
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "limit": limit,
        "action_breakdowns": "action_type",
    }
    filtering = [{"field": "ad.impressions", "operator": "GREATER_THAN", "value": 0}]
    if name_contains.strip():
        filtering.append({"field": "ad.name", "operator": "CONTAIN", "value": name_contains.strip()})
    params["filtering"] = json.dumps(filtering)

    if deadline is None:
        deadline = time.time() + 300  # no máximo 5 min por conta; depois vai para "tentar de novo"
    if time.time() > deadline:
        raise ApiError(f"{account_id}: demorou demais (Facebook instável); use o botão de tentar de novo")
    try:
        return _paginate(f"{GRAPH}/{version}/{account_id}/insights", params, log, attempts=3)
    except ApiError as e:
        heavy = e.too_much_data or (e.status or 0) >= 500 or "Falhou após" in str(e)
        if not heavy:
            raise
        if since < until:
            mid = since + timedelta(days=(until - since).days // 2)
            if log:
                log(f"✂️ {account_id}: muito dado, dividindo {since}→{mid} e {mid + timedelta(days=1)}→{until}")
            kw = dict(name_contains=name_contains, version=version, log=log, limit=limit, deadline=deadline)
            return (fetch_ad_insights(account_id, since, mid, token, **kw)
                    + fetch_ad_insights(account_id, mid + timedelta(days=1), until, token, **kw))
        if limit > 25:
            if log:
                log(f"🔽 {account_id} {since}: pedindo páginas menores ({limit // 4})")
            return fetch_ad_insights(account_id, since, until, token, name_contains=name_contains,
                                     version=version, log=log, limit=max(25, limit // 4), deadline=deadline)
        raise


def _action_value(items, action_type: str) -> float:
    for it in items or []:
        if it.get("action_type") == action_type:
            try:
                return float(it.get("value", 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def row_to_base(row: dict) -> dict:
    """Converte uma linha do insights em números somáveis."""
    def f(k):
        try:
            return float(row.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "spend": f("spend"),
        "impressions": f("impressions"),
        "clicks": f("clicks"),
        "link_clicks": f("inline_link_clicks"),
        "video_3s": _action_value(row.get("actions"), "video_view"),
        "video_p75": _action_value(row.get("video_p75_watched_actions"), "video_view"),
    }
