"""Planilha via Google Apps Script (sem chave de conta de serviço).

O script roda na conta Google da própria usuária; o app só manda pedidos para a URL do Web App.
Imita a interface do gspread usada pelo app: open_by_key → worksheets/worksheet → get_all_values/write_runs.
"""
from __future__ import annotations

from .http import ApiError, get_json


def _call(url: str, payload: dict, log=None) -> dict:
    res = get_json(url, method="POST", body=payload, timeout=300, log=log)
    if not isinstance(res, dict):
        raise ApiError(f"Resposta inesperada do script da planilha: {str(res)[:200]}")
    if res.get("error"):
        raise ApiError(f"Planilha: {res['error']}")
    return res


class GasWorksheet:
    def __init__(self, client: "GasClient", sid: str, title: str):
        self.client, self.sid, self.title = client, sid, title

    def get_all_values(self):
        return _call(self.client.url, {"action": "read", "spreadsheetId": self.sid, "sheet": self.title})["values"]

    def write_runs(self, runs: list[dict], log=None) -> int:
        total = 0
        for i in range(0, len(runs), 300):
            res = _call(self.client.url, {"action": "write", "spreadsheetId": self.sid, "sheet": self.title,
                                          "runs": runs[i:i + 300]}, log)
            total += int(res.get("written", 0))
        return total


class GasSpreadsheet:
    def __init__(self, client: "GasClient", sid: str):
        self.client, self.sid = client, sid
        self._tabs = _call(client.url, {"action": "tabs", "spreadsheetId": sid})["tabs"]

    def worksheets(self):
        return [GasWorksheet(self.client, self.sid, t) for t in self._tabs]

    def worksheet(self, title: str):
        return GasWorksheet(self.client, self.sid, title)


class GasClient:
    def __init__(self, url: str):
        self.url = url.strip()

    def open_by_key(self, sid: str) -> GasSpreadsheet:
        return GasSpreadsheet(self, sid)
