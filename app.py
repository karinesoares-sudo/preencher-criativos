"""Preenchedor de Criativos — Facebook Ads + RedTrack → Google Sheets (aba DADOS_TESTE)."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from core import facebook as fb
from core import redtrack as rt
from core.metrics import Aggregator, build_code_regex, compute, extract_codes, prefixes_from_codes
from core.sheets import detect_layout, plan_writes, spreadsheet_id, write_batch

st.set_page_config(page_title="Preenchedor de Criativos", page_icon="📊", layout="wide")

# ----------------------------------------------------------------- estado / log
ss = st.session_state
ss.setdefault("fb_cache", {})        # (conta, de, até, filtro) -> Aggregator  → permite retomar
ss.setdefault("fb_failed", {})       # conta -> erro
ss.setdefault("rt_agg", None)
ss.setdefault("rt_fields", [])
ss.setdefault("rt_allowed", None)
ss.setdefault("log", [])
_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        ss_log.append(f"{datetime.now():%H:%M:%S} {msg}")


ss_log = ss["log"]  # referência usada também pelas threads


def secret(name: str, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def usd_rate() -> float:
    """Cotação do dólar do dia (AwesomeAPI); se não conseguir, usa 5,50 e deixa editar."""
    try:
        import requests
        r = requests.get("https://economia.awesomeapi.com.br/json/last/USD-BRL", timeout=10).json()
        return round(float(r["USDBRL"]["bid"]), 2)
    except Exception:
        return 5.50


# ----------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("⚙️ Configurações")
    fb_token = st.text_input("Token do Facebook", value=secret("FB_ACCESS_TOKEN"), type="password")
    rt_key = st.text_input("Chave API RedTrack", value=secret("REDTRACK_API_KEY"), type="password")
    st.caption("Já vêm preenchidos se estiverem salvos no app.")

    with st.expander("Avançado", expanded=False):
        graph_version = st.text_input("Versão Graph API", value=secret("FB_GRAPH_VERSION", fb.DEFAULT_VERSION))
        _subs = ["automático"] + [f"sub{i}" for i in range(1, 21)] + ["rt_ad", "rt_campaign", "rt_adgroup", "rt_ad_id"]
        _def_sub = secret("REDTRACK_SUB", "sub4")
        rt_group = st.selectbox("Sub do RedTrack com o nome do anúncio", _subs,
                                index=_subs.index(_def_sub) if _def_sub in _subs else 0,
                                help="Seus links usam sub4 = nome do anúncio ({{ad.name}}). No automático, o app tenta descobrir sozinho.")
        rt_sales_field = st.text_input("Campo de VENDAS no RedTrack", value=secret("REDTRACK_SALES_FIELD", "conversions"))
        rt_revenue_field = st.text_input("Campo de FATURAMENTO no RedTrack", value=secret("REDTRACK_REVENUE_FIELD", "revenue"))
        rt_cost_field = st.text_input("Campo de GASTO no RedTrack", value=secret("REDTRACK_COST_FIELD", "cost"))
        rt_id_sub = st.selectbox("Sub com o ID do anúncio ({{ad.id}})", [f"sub{i}" for i in range(1, 21)], index=0)
        rt_tz = st.text_input("Fuso do RedTrack (vazio = padrão da conta)", value=secret("REDTRACK_TIMEZONE", ""))
        workers = st.slider("Contas do Facebook em paralelo", 1, 16, 6)
        usd_brl = st.number_input("Cotação do dólar (contas em USD viram R$)", min_value=0.0,
                                  value=float(usd_rate()), step=0.01, format="%.2f")
        include_variations = st.checkbox("Somar variações (LT1900 inclui LT1900.1, LT1900.2…)", value=False)

    if st.button("🧹 Limpar dados baixados"):
        ss.fb_cache.clear(); ss.fb_failed.clear(); ss.rt_agg = None
        st.rerun()

st.title("Preencher Planilha de Criativos 🚀")
st.caption("Puxa Gasto, CPM, CTR, CPC, Hook e Body do Facebook e Vendas do RedTrack e preenche a aba de criativos. Se der erro, ele tenta de novo sozinho.")

# ----------------------------------------------------------------- 1. período
st.subheader("1. Datas")
c1, c2, c3 = st.columns([1, 1, 2])
today = date.today()
since = c1.date_input("De", value=today - timedelta(days=7), format="DD/MM/YYYY")
until = c2.date_input("Até", value=today, format="DD/MM/YYYY")
if since > until:
    st.error("A data inicial é depois da final.")
    st.stop()

# ----------------------------------------------------------------- 2. planilha
st.subheader("2. Planilha")
st.caption("Cole o link da planilha. A aba DADOS_TESTE já vem escolhida.")


@st.cache_resource(show_spinner=False)
def gclient():
    url = secret("SHEETS_WEBAPP_URL", "")
    if url:
        from core.gas import GasClient
        return GasClient(url)
    import gspread
    import json as _json
    raw = secret("GOOGLE_JSON", "")
    info = _json.loads(raw) if raw else dict(st.secrets["gcp_service_account"])
    return gspread.service_account_from_dict(info)


sheet_url = st.text_input("Link da planilha", value=secret("DEFAULT_SHEET_URL", ""),
                          placeholder="https://docs.google.com/spreadsheets/d/...")
layout = values = ws = None
if sheet_url:
    try:
        sh = gclient().open_by_key(spreadsheet_id(sheet_url))
        tabs = [w.title for w in sh.worksheets()]
        default_tab = tabs.index("DADOS_TESTE") if "DADOS_TESTE" in tabs else 0
        tab = st.selectbox("Aba", tabs, index=default_tab)
        ws = sh.worksheet(tab)
        values = ws.get_all_values()
        layout = detect_layout(values)
    except KeyError:
        st.error("Falta configurar o acesso à planilha (SHEETS_WEBAPP_URL) nos Secrets.")
    except Exception as e:
        st.error(f"Não consegui ler a planilha: {e}")
        st.info("Confira se o link está certo e se a sua conta Google consegue editar essa planilha.")

if layout:
    fillable = [b for b in layout.blocks if b.metric_cols]
    cols = st.columns(len(fillable) or 1)
    for col, b in zip(cols, fillable):
        col.markdown(f"**{b.title}**  \n" + ", ".join(sorted(b.metric_cols)))
    blocks_to_fill = st.multiselect("Blocos a preencher", [b.title for b in fillable], default=[b.title for b in fillable])

    sheet_codes = set()
    statuses = set()
    for b in layout.blocks:
        for r in range(layout.header_row + 1, len(values)):
            v = values[r][b.creative_col] if b.creative_col < len(values[r]) else ""
            sheet_codes.update(c for c in re.findall(r"[A-Za-z]+\d+(?:\.\d+)*", v))
            if b.status_col is not None and b.status_col < len(values[r]) and values[r][b.status_col].strip():
                statuses.add(values[r][b.status_col].strip())
    prefixes = prefixes_from_codes(sheet_codes) or {"LT"}
    adv = st.expander("Opções (não precisa mexer)")
    prefix_txt = adv.text_input("Prefixo(s) do código do criativo", value=", ".join(sorted(prefixes)),
                               help="Detectado da planilha. O app procura esse código no nome do anúncio.")
    rx = build_code_regex([p.strip() for p in prefix_txt.split(",")])

    only_empty = adv.checkbox("Preencher só células vazias (não sobrescrever)", value=False)
    skip_status: list[str] = []
    _st_opts = sorted(statuses | {"TESTE"})
    only_status = st.multiselect("Preencher só criativos com STATUS", _st_opts, default=["TESTE"],
                                 help="Linhas com outro status (VALIDADO, DESCARTADO, PAUSADO…) não são mexidas.")

# ----------------------------------------------------------------- 3. campanhas RedTrack
st.subheader("3. Campanhas do RedTrack")
st.caption("Escolha as suas campanhas. O gasto, as vendas e os anúncios considerados vêm só delas — "
           "assim não entra gasto de outros gestores.")


@st.cache_data(ttl=1800, show_spinner=False)
def load_rt_campaigns(key: str):
    return rt.fetch_campaigns(key)


rt_camps: list[dict] = []
if rt_key:
    try:
        with st.spinner("Carregando campanhas do RedTrack…"):
            rt_camps = load_rt_campaigns(rt_key)
    except Exception as e:
        st.error(f"Erro ao listar campanhas do RedTrack: {e}")
camp_label = {c["id"]: c["title"] for c in rt_camps}
selected_camps = st.multiselect(
    f"Campanhas ({len(rt_camps)} disponíveis — digite para buscar, pode escolher várias)",
    list(camp_label), format_func=lambda i: camp_label.get(i, i), key="rt_camps_sel")
if rt_camps and not selected_camps:
    st.warning("Nenhuma campanha escolhida: vou usar TODAS as campanhas do RedTrack (inclui outros gestores).")

# ----------------------------------------------------------------- 4. contas
st.subheader("4. Contas de anúncio do Facebook")
st.caption("Daqui vêm Hook, Body, CPM, CTR e CPC — só dos anúncios que estão nas campanhas escolhidas acima.")


@st.cache_data(ttl=3600, show_spinner=False)
def load_accounts(token: str, version: str):
    return fb.list_ad_accounts(token, version)


accounts = []
currency_of: dict[str, str] = {}
if fb_token:
    try:
        with st.spinner("Carregando contas…"):
            accounts = load_accounts(fb_token, graph_version)
            currency_of = {a["id"]: a["currency"] for a in accounts}
    except Exception as e:
        st.error(f"Erro ao listar contas do Facebook: {e}")
else:
    st.info("Informe o token do Facebook na barra lateral.")

selected_ids: list[str] = []
name_filter = ""
if accounts:
    label = {a["id"]: f"{a['name']} ({a['id']})" + (f" · {a['bm']}" if a["bm"] else "") for a in accounts}
    active = [a["id"] for a in accounts if a["status"] == 1]
    a1c, a2c = st.columns([3, 1])
    use_all = a2c.checkbox(f"Todas as ativas ({len(active)})", value=True)
    if use_all:
        selected_ids = active
        a1c.caption(f"{len(active)} contas ativas de {len(accounts)} encontradas.")
    else:
        selected_ids = a1c.multiselect("Escolha as contas", list(label), format_func=label.get,
                                       default=secret("DEFAULT_ACCOUNTS", []) or [])
    name_filter = st.expander("Filtro por nome do anúncio (opcional)").text_input("Só anúncios cujo nome contém (opcional, acelera muito)", value=secret("AD_NAME_FILTER", ""),
                                placeholder="ex.: LT")
    currencies = {a["currency"] for a in accounts if a["id"] in selected_ids}
    other = currencies - {"BRL", "USD"}
    if other:
        st.warning(f"Há contas em {', '.join(sorted(other))}: o gasto delas entra sem conversão.")
    if "USD" in currencies:
        st.caption(f"Contas em dólar são convertidas para R$ pela cotação {usd_brl:.2f} (dá para mudar em Avançado).")

# ----------------------------------------------------------------- 4. buscar
st.subheader("5. Buscar os dados")
ready = bool(layout and fb_token and rt_key and selected_ids)
b1, b2 = st.columns(2)
go = b1.button("🚀 Buscar dados", type="primary", disabled=not ready)
retry_failed = b2.button(f"🔁 Tentar de novo só as que falharam ({len(ss.fb_failed)})", disabled=not ss.fb_failed)


def run_facebook(ids: list[str]) -> None:
    key = lambda a: (a, since.isoformat(), until.isoformat(), name_filter.strip(), rx.pattern, usd_brl)
    todo = [a for a in ids if key(a) not in ss.fb_cache]
    skipped = len(ids) - len(todo)
    if skipped:
        log(f"♻️ {skipped} contas já baixadas neste período — reaproveitando")
    if not todo:
        return
    bar = st.progress(0.0, text=f"Facebook: 0/{len(todo)} contas")
    done = 0

    def work(acc):
        rows = fb.fetch_ad_insights(acc, since, until, fb_token, name_contains=name_filter,
                                    version=graph_version, log=log)
        mult = usd_brl if currency_of.get(acc) == "USD" else 1.0
        out = []
        for r in rows:
            base = fb.row_to_base(r)
            base["spend"] *= mult
            out.append((str(r.get("ad_id", "")), r.get("ad_name", ""), base))
        return out, len(rows)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, a): a for a in todo}
        for f in as_completed(futs):
            acc = futs[f]
            done += 1
            try:
                agg, n = f.result()
                ss.fb_cache[key(acc)] = agg
                ss.fb_failed.pop(acc, None)
                if n:
                    log(f"✅ {acc}: {n} anúncios")
            except Exception as e:
                ss.fb_failed[acc] = str(e)
                log(f"❌ {acc}: {e}")
            bar.progress(done / len(todo), text=f"Facebook: {done}/{len(todo)} contas")
    bar.empty()


def fb_rows_current():
    for k in ss.fb_cache:
        if k[1] == since.isoformat() and k[2] == until.isoformat() and k[5] == usd_brl and k[0] in selected_ids:
            yield from ss.fb_cache[k]


def build_fb(allowed: set[str] | None) -> Aggregator:
    agg = Aggregator(rx)
    for ad_id, name, base in fb_rows_current():
        if allowed is None or ad_id in allowed:
            agg.add(name, base, ad_id=ad_id)
    return agg


def run_redtrack() -> None:
    id_map: dict[str, str] = {}
    for ad_id, name, _ in fb_rows_current():
        c = extract_codes(name, rx)
        if c and ad_id:
            id_map[ad_id] = c[0]
    group = rt_group
    with st.spinner("Buscando RedTrack…"):
        if group == "automático":
            def matches(v):
                return bool(extract_codes(v, rx)) or v.strip() in id_map
            group, scores, types = rt.detect_sub(rt_key, since, until, matches, log=log)
            ss.rt_types = types
            if not group:
                st.warning("Não achei o código do criativo em nenhum sub das conversões do RedTrack neste período. "
                           "Escolha o sub manualmente em Avançado.")
                log("⚠️ RedTrack: nenhum sub com o código do criativo")
                ss.rt_agg = Aggregator(rx)
                return
            st.info(f"RedTrack: o criativo está no **{group}** (achei em {scores[group]} conversões).")
        ss.rt_group_used = group
        camp = list(selected_camps)
        rows = rt.fetch_report_by_sub(rt_key, since, until, group=group, campaign_ids=camp or None,
                                      timezone=rt_tz, log=log)
        ss.rt_allowed = None
        if camp:
            id_rows = rt.fetch_report_by_sub(rt_key, since, until, group=rt_id_sub, campaign_ids=camp,
                                             timezone=rt_tz, log=log)
            ss.rt_allowed = {str(r.get(rt_id_sub, "")).strip() for r in id_rows if str(r.get(rt_id_sub, "")).strip()}
            log(f"🎯 {len(ss.rt_allowed)} anúncios (IDs) nas campanhas escolhidas")
    agg = Aggregator(rx)
    fields = set()
    for r in rows:
        fields.update(k for k, v in r.items() if isinstance(v, (int, float)))
        agg.add(str(r.get(group, "")), {"sales": rt.num(r, rt_sales_field), "revenue": rt.num(r, rt_revenue_field),
                                         "spend": rt.num(r, rt_cost_field)}, id_map=id_map)
    ss.rt_agg = agg
    ss.rt_fields = sorted(fields)
    total_sales = sum(v["sales"] for v in agg.data.values())
    total_cost = sum(v["spend"] for v in agg.data.values())
    log(f"✅ RedTrack ({group}): {len(rows)} linhas, {len(agg.data)} criativos, {total_sales:.0f} vendas, gasto {total_cost:,.2f}")


if go or retry_failed:
    t0 = time.time()
    run_facebook(list(ss.fb_failed) if retry_failed else selected_ids)
    if go:
        try:
            run_redtrack()
        except Exception as e:
            st.error(f"RedTrack falhou mesmo após novas tentativas: {e}")
            log(f"❌ RedTrack: {e}")
    st.success(f"Concluído em {time.time() - t0:.0f}s")

if ss.fb_failed:
    st.warning(f"{len(ss.fb_failed)} conta(s) falharam depois de todas as tentativas. Use o botão 🔁 para repetir só elas.")
    with st.expander("Ver contas com erro"):
        st.dataframe(pd.DataFrame([{"conta": k, "erro": v} for k, v in ss.fb_failed.items()]), hide_index=True)

# ----------------------------------------------------------------- 5. prévia e gravação
has_fb = any(True for _ in fb_rows_current()) if layout else False
if layout and (has_fb or ss.rt_agg):
    st.subheader("6. Conferir e gravar na planilha")
    allowed = ss.get("rt_allowed")
    fb_all = build_fb(allowed if allowed else None)
    rt_all = ss.rt_agg or Aggregator(rx)
    if allowed:
        st.caption(f"Facebook filtrado para os {len(allowed)} anúncios das campanhas escolhidas. Gasto, vendas e CPA vêm do RedTrack.")

    def lookup(codes):
        f = fb_all.sum_for(codes, include_variations)
        r = rt_all.sum_for(codes, include_variations)
        if f is None and r is None:
            return None
        base = f or {k: 0.0 for k in ("spend", "impressions", "clicks", "link_clicks", "video_3s", "video_p75", "sales", "revenue")}
        if r:
            base["sales"] = r["sales"]
            base["revenue"] = r["revenue"]
            base["rt_spend"] = r["spend"]
        return compute(base)

    writes, summary = plan_writes(values, layout, blocks_to_fill, lookup, lambda t: extract_codes(t, rx),
                                  only_empty=only_empty, skip_status=set(skip_status),
                                  only_status=set(only_status))
    df = pd.DataFrame(summary)
    found = int(df["encontrado"].sum()) if not df.empty else 0
    m1, m2, m3 = st.columns(3)
    m1.metric("Linhas com criativo", len(df))
    m2.metric("Com dados no período", found)
    m3.metric("Células a gravar", len(writes))
    if not df.empty:
        st.dataframe(df, hide_index=True, height=320)
    with st.expander("Criativos com dados que não estão na planilha / anúncios sem código"):
        in_sheet = {c for s in summary for c in extract_codes(s["criativo"], rx)}
        extra = sorted(set(fb_all.data) - in_sheet)
        st.write(f"{len(extra)} códigos com gasto no Facebook que não aparecem na aba:", ", ".join(extra[:300]))
        top_un = sorted(fb_all.unmatched.items(), key=lambda x: -x[1])[:30]
        if top_un:
            st.write("Anúncios sem código no nome (maior gasto):")
            st.dataframe(pd.DataFrame(top_un, columns=["nome do anúncio", "gasto"]), hide_index=True)
        if ss.rt_fields:
            st.caption("Campos numéricos disponíveis no RedTrack: " + ", ".join(ss.rt_fields))
        if ss.get("rt_types"):
            st.caption("Tipos de conversão no período: " + ", ".join(f"{k} ({v})" for k, v in ss.rt_types.items()))

    if st.button(f"💾 Gravar {len(writes)} células na planilha", type="primary", disabled=not writes):
        try:
            with st.spinner("Gravando em lote…"):
                n = write_batch(ws, writes, log=log)
            st.success(f"Pronto! {n} células gravadas na aba “{ws.title}”.")
            log(f"💾 {n} células gravadas em {ws.title}")
        except Exception as e:
            st.error(f"Erro ao gravar: {e}")

with st.expander(f"📜 Registro ({len(ss_log)})"):
    st.code("\n".join(ss_log[-400:]) or "—")
