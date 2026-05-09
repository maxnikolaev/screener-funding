"""
MEXC Funding Screener microservice.

Features:
- Aggregates futures funding data from MEXC public contract API.
- Exposes filtered/sorted JSON endpoints for TradingOS and standalone clients.
- Serves a lightweight standalone frontend at '/'.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


MEXC_BASE_URL = os.getenv("MEXC_CONTRACT_BASE_URL", "https://contract.mexc.com")
HTTP_TIMEOUT_SEC = float(os.getenv("MEXC_HTTP_TIMEOUT_SEC", "8"))
TICKER_CACHE_TTL_SEC = float(os.getenv("MEXC_TICKER_CACHE_TTL_SEC", "8"))
DETAIL_CACHE_TTL_SEC = float(os.getenv("MEXC_DETAIL_CACHE_TTL_SEC", "300"))
SETTLE_CACHE_TTL_SEC = float(os.getenv("MEXC_SETTLE_CACHE_TTL_SEC", "45"))
DEFAULT_PORT = int(os.getenv("MEXC_FUNDING_SCREENER_PORT", "8790"))


@dataclass
class SnapshotRow:
    symbol: str
    funding_rate_pct: float
    abs_funding_rate_pct: float
    hold_vol: float
    volume24_contracts: float
    volume24_usd: float
    last_price: float | None
    next_settle_time: int | None
    time_to_funding_sec: int | None
    timestamp: int | None
    quote_coin: str | None
    settle_coin: str | None
    api_allowed: bool | None


class FundingState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ticker_rows: list[dict[str, Any]] = []
        self._details_map: dict[str, dict[str, Any]] = {}
        self._ticker_updated_at = 0.0
        self._details_updated_at = 0.0
        self._last_error: str | None = None
        self._next_settle_cache: dict[str, tuple[int | None, float]] = {}

    @staticmethod
    def _ensure_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _fetch_json(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=MEXC_BASE_URL, timeout=HTTP_TIMEOUT_SEC) as client:
            response = await client.get(path)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise RuntimeError(f"Unexpected MEXC payload for {path}")
            return payload

    async def _refresh_ticker(self) -> None:
        payload = await self._fetch_json("/api/v1/contract/ticker")
        rows = self._ensure_list(payload.get("data"))
        if not rows:
            raise RuntimeError("MEXC ticker returned no rows")
        self._ticker_rows = rows
        self._ticker_updated_at = time.time()

    async def _refresh_details(self) -> None:
        payload = await self._fetch_json("/api/v1/contract/detail")
        rows = self._ensure_list(payload.get("data"))
        details: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if symbol:
                details[symbol] = row
        if details:
            self._details_map = details
            self._details_updated_at = time.time()

    async def _resolve_next_settle_for_symbol(self, symbol: str) -> int | None:
        now = time.time()
        cached = self._next_settle_cache.get(symbol)
        if cached and (now - cached[1]) <= SETTLE_CACHE_TTL_SEC:
            return cached[0]

        path = f"/api/v1/contract/funding_rate/{symbol}"
        payload = await self._fetch_json(path)
        data = payload.get("data")
        if not isinstance(data, dict):
            self._next_settle_cache[symbol] = (None, now)
            return None
        next_settle = self._to_int(data.get("nextSettleTime"))
        self._next_settle_cache[symbol] = (next_settle, now)
        return next_settle

    async def _fill_next_settle_bulk(self, rows: list[SnapshotRow]) -> None:
        sem = asyncio.Semaphore(10)
        now_sec = int(time.time())

        async def worker(row: SnapshotRow) -> None:
            if row.next_settle_time is not None:
                row.time_to_funding_sec = max(0, int(row.next_settle_time / 1000) - now_sec)
                return
            async with sem:
                try:
                    next_settle = await self._resolve_next_settle_for_symbol(row.symbol)
                except Exception:  # noqa: BLE001
                    next_settle = None
            row.next_settle_time = next_settle
            if next_settle is not None:
                row.time_to_funding_sec = max(0, int(next_settle / 1000) - now_sec)
            else:
                row.time_to_funding_sec = None

        await asyncio.gather(*(worker(row) for row in rows))

    async def ensure_fresh(self, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            need_ticker = force or (now - self._ticker_updated_at >= TICKER_CACHE_TTL_SEC)
            need_detail = force or (now - self._details_updated_at >= DETAIL_CACHE_TTL_SEC)

            try:
                if need_ticker:
                    await self._refresh_ticker()
                if need_detail:
                    await self._refresh_details()
                self._last_error = None
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                # If we have cache, keep serving stale data.
                if not self._ticker_rows:
                    raise

    async def get_rows(
        self,
        *,
        limit: int,
        sort: str,
        min_hold_vol: float,
        min_volume24_usd: float,
        min_abs_funding_pct: float,
        quote_coin: str | None,
        symbol_query: str | None,
        only_enabled: bool,
        include_api_blocked: bool,
    ) -> dict[str, Any]:
        await self.ensure_fresh()

        requested_quote = (quote_coin or "").strip().upper() or None
        query = (symbol_query or "").strip().upper()

        rows: list[SnapshotRow] = []
        for ticker in self._ticker_rows:
            if not isinstance(ticker, dict):
                continue

            symbol = str(ticker.get("symbol") or "").strip()
            if not symbol:
                continue

            details = self._details_map.get(symbol, {})
            state = self._to_int(details.get("state"))
            api_allowed_raw = details.get("apiAllowed")
            api_allowed = bool(api_allowed_raw) if isinstance(api_allowed_raw, bool) else None
            settle_coin = (details.get("settleCoin") or ticker.get("settleCoin") or None)
            quote = (details.get("quoteCoin") or ticker.get("quoteCoin") or None)
            if isinstance(settle_coin, str):
                settle_coin = settle_coin.upper()
            if isinstance(quote, str):
                quote = quote.upper()

            if only_enabled and state is not None and state != 0:
                continue
            if not include_api_blocked and api_allowed is False:
                continue
            if requested_quote and quote != requested_quote:
                continue
            if query and query not in symbol.upper():
                continue

            funding_rate = self._to_float(ticker.get("fundingRate"))
            hold_vol = self._to_float(ticker.get("holdVol"))
            volume24 = self._to_float(ticker.get("volume24"))
            amount24 = self._to_float(ticker.get("amount24"))
            if funding_rate is None or hold_vol is None or volume24 is None:
                continue

            funding_rate_pct = funding_rate * 100.0
            abs_funding_rate_pct = abs(funding_rate_pct)
            last_price = self._to_float(ticker.get("lastPrice"))
            volume24_usd = amount24
            if volume24_usd is None and last_price is not None:
                volume24_usd = volume24 * last_price
            if volume24_usd is None:
                volume24_usd = 0.0

            if (
                hold_vol < min_hold_vol
                or volume24_usd < min_volume24_usd
                or abs_funding_rate_pct < min_abs_funding_pct
            ):
                continue

            rows.append(
                SnapshotRow(
                    symbol=symbol,
                    funding_rate_pct=funding_rate_pct,
                    abs_funding_rate_pct=abs_funding_rate_pct,
                    hold_vol=hold_vol,
                    volume24_contracts=volume24,
                    volume24_usd=volume24_usd,
                    last_price=last_price,
                    next_settle_time=self._to_int(ticker.get("nextSettleTime")),
                    time_to_funding_sec=None,
                    timestamp=self._to_int(ticker.get("timestamp")),
                    quote_coin=quote,
                    settle_coin=settle_coin,
                    api_allowed=api_allowed,
                )
            )

        key_map: dict[str, tuple[str, bool]] = {
            "abs_desc": ("abs_funding_rate_pct", True),
            "abs_asc": ("abs_funding_rate_pct", False),
            "rate_desc": ("funding_rate_pct", True),
            "rate_asc": ("funding_rate_pct", False),
            "hold_desc": ("hold_vol", True),
            "volume_desc": ("volume24_usd", True),
            "settle_asc": ("time_to_funding_sec", False),
        }
        sort_key, reverse = key_map.get(sort, ("abs_funding_rate_pct", True))
        if sort_key == "time_to_funding_sec":
            rows.sort(
                key=lambda row: row.time_to_funding_sec if row.time_to_funding_sec is not None else 10**12,
                reverse=reverse,
            )
        else:
            rows.sort(key=lambda row: getattr(row, sort_key), reverse=reverse)

        selected = rows[:limit]
        await self._fill_next_settle_bulk(selected)
        return {
            "success": True,
            "asOf": int(time.time() * 1000),
            "source": {
                "mexcBaseUrl": MEXC_BASE_URL,
                "tickerUpdatedAtMs": int(self._ticker_updated_at * 1000),
                "detailsUpdatedAtMs": int(self._details_updated_at * 1000),
                "tickerCacheTtlSec": TICKER_CACHE_TTL_SEC,
                "detailsCacheTtlSec": DETAIL_CACHE_TTL_SEC,
                "settleCacheTtlSec": SETTLE_CACHE_TTL_SEC,
                "lastError": self._last_error,
            },
            "filters": {
                "limit": limit,
                "sort": sort,
                "minHoldVol": min_hold_vol,
                "minVolume24Usd": min_volume24_usd,
                "minAbsFundingPct": min_abs_funding_pct,
                "quoteCoin": requested_quote,
                "symbolQuery": query or None,
                "onlyEnabled": only_enabled,
                "includeApiBlocked": include_api_blocked,
            },
            "stats": {
                "totalAfterFilter": len(rows),
                "returned": len(selected),
            },
            "rows": [row.__dict__ for row in selected],
        }


state = FundingState()

app = FastAPI(title="MEXC Funding Screener")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).resolve().parent / "web"
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        await state.ensure_fresh()
        return {
            "status": "ok",
            "tickerRows": len(state._ticker_rows),
            "detailsRows": len(state._details_map),
            "lastError": state._last_error,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/funding/top")
async def funding_top(
    limit: int = Query(default=50, ge=1, le=300),
    sort: str = Query(default="abs_desc"),
    min_hold_vol: float = Query(default=0.0, ge=0.0),
    min_volume24: float = Query(default=0.0, ge=0.0, description="Backward-compat alias for USD volume filter"),
    min_volume24_usd: float | None = Query(default=None, ge=0.0),
    min_abs_funding_pct: float = Query(default=0.0, ge=0.0),
    quote_coin: str | None = Query(default="USDT"),
    symbol_query: str | None = Query(default=None),
    only_enabled: bool = Query(default=True),
    include_api_blocked: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return await state.get_rows(
            limit=limit,
            sort=sort,
            min_hold_vol=min_hold_vol,
            min_volume24_usd=min_volume24 if min_volume24_usd is None else min_volume24_usd,
            min_abs_funding_pct=min_abs_funding_pct,
            quote_coin=quote_coin,
            symbol_query=symbol_query,
            only_enabled=only_enabled,
            include_api_blocked=include_api_blocked,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/funding/history/{symbol}")
async def funding_history(
    symbol: str,
    page_num: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    path = f"/api/v1/contract/funding_rate/history?symbol={symbol.upper()}&page_num={page_num}&page_size={page_size}"
    try:
        payload = await state._fetch_json(path)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected history payload")
        return {
            "success": True,
            "symbol": symbol.upper(),
            "page": {
                "pageNum": page_num,
                "pageSize": page_size,
                "totalCount": data.get("totalCount"),
                "totalPage": data.get("totalPage"),
            },
            "rows": data.get("resultList") or [],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/refresh")
async def refresh_now() -> dict[str, Any]:
    try:
        await state.ensure_fresh(force=True)
        return {
            "success": True,
            "tickerRows": len(state._ticker_rows),
            "detailsRows": len(state._details_map),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
async def index() -> FileResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Standalone UI is missing")
    return FileResponse(index_file)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT)
