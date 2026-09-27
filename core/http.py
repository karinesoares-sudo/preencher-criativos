"""Chamadas HTTP com nova tentativa automática (backoff exponencial + jitter)."""
from __future__ import annotations

import random
import time
from typing import Any, Callable

import requests

# Códigos de erro do Facebook que são temporários (limite de uso / instabilidade)
FB_TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613, 80000, 80003, 80004, 80014}
RETRY_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None, code: int | None = None, too_much_data: bool = False):
        super().__init__(msg)
        self.status = status
        self.code = code
        self.too_much_data = too_much_data


_session = requests.Session()
_session.headers.update({"User-Agent": "preenchedor-criativos/1.0"})


def _fb_error(payload: Any) -> tuple[int | None, str, bool]:
    if isinstance(payload, dict) and "error" in payload and isinstance(payload["error"], dict):
        err = payload["error"]
        code = err.get("code")
        msg = err.get("error_user_msg") or err.get("message") or str(err)
        too_much = code == 1 and "reduce the amount of data" in str(err.get("message", "")).lower()
        return code, msg, too_much
    return None, "", False


def get_json(
    url: str,
    params: dict | None = None,
    *,
    method: str = "GET",
    body: Any = None,
    max_attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 90.0,
    timeout: float = 120.0,
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """GET que tenta de novo sozinho em erro de rede, 429, 5xx e erros temporários do Facebook."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = (_session.post(url, params=params, json=body, timeout=timeout) if method == "POST"
                    else _session.get(url, params=params, timeout=timeout))
            try:
                payload = resp.json()
            except ValueError:
                payload = None

            code, msg, too_much = _fb_error(payload)
            if resp.ok and code is None:
                return payload

            # "reduza a quantidade de dados": não adianta repetir igual, quem chama divide o período
            if too_much:
                raise ApiError(msg, resp.status_code, code, too_much_data=True)

            transient = resp.status_code in RETRY_HTTP_STATUS or (code in FB_TRANSIENT_CODES)
            err = ApiError(msg or f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code, code)
            if not transient:
                raise err
            last_exc = err
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else None
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            delay = None
        except ApiError:
            raise

        if attempt == max_attempts:
            break
        if delay is None:
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.7 + random.random() * 0.6)
        if log:
            log(f"⏳ tentativa {attempt}/{max_attempts} falhou ({last_exc}); nova tentativa em {delay:.0f}s")
        sleep(delay)

    raise ApiError(f"Falhou após {max_attempts} tentativas: {last_exc}")
