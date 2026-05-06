# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Alpaca Paper Trading Account

Credentials are stored in `.env`. Always load from there for API calls:

- `ALPACA_BASE_URL` — `https://paper-api.alpaca.markets/v2`
- `ALPACA_API_KEY` — API key ID (header: `APCA-API-KEY-ID`)
- `ALPACA_API_SECRET` — API secret key (header: `APCA-API-SECRET-KEY`)

This is a **paper trading** account (simulated, no real money).

## Making API Calls

Use `curl` with these headers:

```bash
curl -s -X <METHOD> "$ALPACA_BASE_URL/<endpoint>" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_API_SECRET" \
  -H "Content-Type: application/json"
```

Key endpoints:
- `POST /orders` — place an order
- `DELETE /orders/{order_id}` — cancel an order
- `GET /orders` — list open orders
- `GET /positions` — list current positions
- `GET /account` — account details
