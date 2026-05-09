# MEXC Funding Screener

Standalone Python microservice for MEXC futures funding-rate screening.

## Run standalone

```bash
cd services/mexc-funding-screener
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Open:
- UI: `http://127.0.0.1:8790/`
- Health: `http://127.0.0.1:8790/health`
- API: `http://127.0.0.1:8790/api/v1/funding/top`

## Main endpoints

- `GET /api/v1/funding/top`
- `GET /api/v1/funding/history/{symbol}`
- `POST /api/v1/refresh`

## Environment

- `MEXC_FUNDING_SCREENER_PORT` (default `8790`)
- `MEXC_CONTRACT_BASE_URL` (default `https://contract.mexc.com`)
- `MEXC_HTTP_TIMEOUT_SEC` (default `8`)
- `MEXC_TICKER_CACHE_TTL_SEC` (default `8`)
- `MEXC_DETAIL_CACHE_TTL_SEC` (default `300`)
