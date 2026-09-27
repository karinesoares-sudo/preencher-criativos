"""Leitura do layout da aba (blocos TESTE / PRÉ-ESCALA) e gravação em lote."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .metrics import METRIC_FORMAT, header_metric, normalize


@dataclass
class Block:
    title: str
    creative_col: int                     # índice 0-based
    metric_cols: dict[str, int] = field(default_factory=dict)
    status_col: int | None = None
    fim_col: int | None = None


@dataclass
class Layout:
    header_row: int                       # índice 0-based
    blocks: list[Block]


@dataclass
class CellWrite:
    row: int                              # 0-based
    col: int                              # 0-based
    value: float | str
    metric: str
    old: str
    block: str
    creative: str


def col_letter(idx: int) -> str:
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(row: int, col: int) -> str:
    return f"{col_letter(col)}{row + 1}"


def detect_layout(values: list[list[str]], max_scan: int = 15) -> Layout:
    """Acha a linha de cabeçalho (tem CRIATIVO e GASTO) e separa um bloco por coluna CRIATIVO."""
    for r, row in enumerate(values[:max_scan]):
        norm = [normalize(c) for c in row]
        if "CRIATIVO" in norm and any(n.startswith("GASTO") for n in norm):
            starts = [i for i, n in enumerate(norm) if n == "CRIATIVO"]
            blocks = []
            for bi, start in enumerate(starts):
                end = starts[bi + 1] if bi + 1 < len(starts) else len(norm)
                title = ""
                for up in range(r - 1, -1, -1):
                    for c in range(start, min(end, len(values[up]))):
                        if str(values[up][c]).strip():
                            title = str(values[up][c]).strip()
                            break
                    if title:
                        break
                b = Block(title=title or f"Bloco {bi + 1}", creative_col=start)
                for c in range(start + 1, end):
                    m = header_metric(row[c])
                    if m and m not in b.metric_cols:
                        b.metric_cols[m] = c
                    if norm[c] == "STATUS" and b.status_col is None:
                        b.status_col = c
                    if norm[c] == "FIM" and b.fim_col is None:
                        b.fim_col = c
                blocks.append(b)
            return Layout(header_row=r, blocks=blocks)
    raise ValueError("Não achei a linha de cabeçalho (precisa ter 'CRIATIVO' e 'GASTO') nas primeiras linhas da aba.")


def cell(values, r, c) -> str:
    return str(values[r][c]) if r < len(values) and c < len(values[r]) else ""


def plan_writes(
    values: list[list[str]],
    layout: Layout,
    blocks_to_fill: list[str],
    lookup,                                # callable(codes) -> dict de métricas | None
    extract,                               # callable(texto) -> lista de códigos
    *,
    only_empty: bool = False,
    skip_status: set[str] | None = None,
    only_status: set[str] | None = None,
    status_rule: dict | None = None,
) -> tuple[list[CellWrite], list[dict]]:
    """Monta a lista de células a gravar + um resumo por linha (para a prévia).
    only_status: se informado, só preenche linhas cujo STATUS está nesse conjunto (ex.: {"TESTE"})."""
    skip_status = {normalize(s) for s in (skip_status or set())}
    only_status = {normalize(s) for s in (only_status or set())}
    writes: list[CellWrite] = []
    summary: list[dict] = []
    for b in layout.blocks:
        if b.title not in blocks_to_fill or not b.metric_cols:
            continue
        for r in range(layout.header_row + 1, len(values)):
            creative = cell(values, r, b.creative_col).strip()
            codes = extract(creative)
            if not codes:
                continue
            status = normalize(cell(values, r, b.status_col)) if b.status_col is not None else ""
            if status in skip_status and status:
                continue
            if only_status and status not in only_status:
                continue
            metrics = lookup(codes)
            summary.append({"bloco": b.title, "linha": r + 1, "criativo": creative,
                            "encontrado": metrics is not None, **({k: metrics[k] for k in b.metric_cols} if metrics else {})})
            if metrics is None:
                continue
            for m, c in b.metric_cols.items():
                old = cell(values, r, c)
                if only_empty and old.strip():
                    continue
                writes.append(CellWrite(r, c, round(metrics[m], 6), m, old, b.title, creative))
            # Regra de fim de teste (só no bloco de TESTE): gastou acima do limite → decide o status
            rule = status_rule or {}
            if rule and "TESTE" in normalize(b.title) and b.status_col is not None:
                new_status = None
                if metrics["gasto"] > rule.get("spend", 1000):
                    new_status = "VALIDADO" if metrics["vendas"] >= rule.get("min_sales", 2) else "DESCARTADO"
                if new_status:
                    summary[-1]["novo status"] = new_status
                    writes.append(CellWrite(r, b.status_col, new_status, "status",
                                            cell(values, r, b.status_col), b.title, creative))
                    if b.fim_col is not None and rule.get("end_date"):
                        writes.append(CellWrite(r, b.fim_col, rule["end_date"], "fim",
                                                cell(values, r, b.fim_col), b.title, creative))
    return writes, summary


NUMBER_FORMATS = {
    "pct": {"type": "PERCENT", "pattern": "0.00%"},
    "brl": {"type": "CURRENCY", "pattern": '"R$" #,##0.00'},
    "int": {"type": "NUMBER", "pattern": "#,##0"},
    "dec": {"type": "NUMBER", "pattern": "0.00"},
    "date": {"type": "DATE", "pattern": "dd/mm/yyyy"},
}


def write_batch(ws, writes: list[CellWrite], *, apply_format: bool = True, chunk: int = 1500, log=None) -> int:
    """Grava tudo em poucas chamadas (lotes), com nova tentativa se o Google limitar."""
    if not writes:
        return 0
    if hasattr(ws, "write_runs"):  # planilha via Apps Script: manda blocos de linhas seguidas
        runs, cur = [], None
        for w in sorted(writes, key=lambda w: (w.col, w.row)):
            kind = METRIC_FORMAT.get(w.metric, "dec")
            fmt = NUMBER_FORMATS[kind]["pattern"] if (apply_format and kind in NUMBER_FORMATS) else None
            if cur and cur["col"] == w.col + 1 and cur["row"] + len(cur["values"]) == w.row + 1 and cur["format"] == fmt:
                cur["values"].append(w.value)
            else:
                cur = {"row": w.row + 1, "col": w.col + 1, "values": [w.value], "format": fmt}
                runs.append(cur)
        n = ws.write_runs(runs, log=log)
        if log:
            log(f"📝 gravadas {n} células ({len(runs)} blocos)")
        return n
    data = [{"range": a1(w.row, w.col), "values": [[w.value]]} for w in writes]
    for i in range(0, len(data), chunk):
        _retry(lambda: ws.batch_update(data[i:i + chunk], value_input_option="USER_ENTERED"), log)
        if log:
            log(f"📝 gravadas {min(i + chunk, len(data))}/{len(data)} células")

    if apply_format:
        # Formata só as células gravadas, agrupando linhas seguidas da mesma coluna
        by_col: dict[tuple[int, str], list[int]] = {}
        for w in writes:
            kind = METRIC_FORMAT.get(w.metric, "dec")
            if kind in NUMBER_FORMATS:
                by_col.setdefault((w.col, kind), []).append(w.row)
        fmts = []
        for (c, kind), rows in by_col.items():
            rows = sorted(set(rows))
            start = prev = rows[0]
            for r in rows[1:] + [None]:
                if r is not None and r == prev + 1:
                    prev = r
                    continue
                fmts.append({"range": f"{col_letter(c)}{start + 1}:{col_letter(c)}{prev + 1}",
                             "format": {"numberFormat": NUMBER_FORMATS[kind]}})
                if r is not None:
                    start = prev = r
        for i in range(0, len(fmts), 500):
            _retry(lambda: ws.batch_format(fmts[i:i + 500]), log)
    return len(writes)


def _retry(fn, log=None, attempts: int = 6):
    for a in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # gspread.exceptions.APIError e erros de rede
            msg = str(e)
            transient = any(s in msg for s in ("429", "500", "502", "503", "RATE_LIMIT", "Quota", "timed out", "Connection"))
            if not transient or a == attempts:
                raise
            delay = min(60, 2 ** a)
            if log:
                log(f"⏳ Google Sheets limitou/oscilou ({msg[:80]}); tentando de novo em {delay}s")
            time.sleep(delay)


def spreadsheet_id(url_or_id: str) -> str:
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()
