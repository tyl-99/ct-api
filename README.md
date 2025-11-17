# cTrader Trendbar API

FastAPI server for fetching forex trendbar (candlestick) data from cTrader OpenAPI.

## Features

- Fetch trendbar data for supported forex pairs
- Async API endpoints
- Railway-ready deployment
- Supports multiple timeframes

## Setup

### Environment Variables

Required:
- `CTRADER_CLIENT_ID` - cTrader OpenAPI client ID
- `CTRADER_CLIENT_SECRET` - cTrader OpenAPI client secret
- `CTRADER_ACCOUNT_ID` - cTrader account ID

Optional:
- `CTRADER_ACCESS_TOKEN` - Access token if required
- `PORT` - Server port (default: 8000)

### Installation

```bash
pip install -r requirements.txt
```

### Run Locally

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or:

```bash
python main.py
```

## API Endpoints

### GET `/`
Health check endpoint

**Response:**
```json
{
  "status": "ok",
  "service": "cTrader Trendbar API",
  "available_pairs": ["EUR/USD", "GBP/USD", ...]
}
```

### GET `/health`
Health check with trader connection status

### POST `/trendbar`
Fetch trendbar data

**Request Body:**
```json
{
  "pair": "EUR/USD",
  "timeframe": "M30",  // Optional, defaults to pair's default
  "weeks": 6           // Optional, defaults to 6
}
```

**Response:**
```json
{
  "pair": "EUR/USD",
  "timeframe": "M30",
  "weeks": 6,
  "count": 2016,
  "data": [
    {
      "timestamp": "2024-01-01 00:00:00 UTC",
      "open": 1.08000,
      "high": 1.08100,
      "low": 1.07950,
      "close": 1.08050,
      "volume": 12345,
      "candle_range": 0.00150
    },
    ...
  ]
}
```

### GET `/trendbar/{pair}`
Alternative GET endpoint

**Query Parameters:**
- `timeframe` (optional): M1, M5, M15, M30, H1, H4, D1, etc.
- `weeks` (optional): Number of weeks of data (default: 6)

**Example:**
```
GET /trendbar/EUR%2FUSD?timeframe=M30&weeks=4
```

### POST `/news`
Get forex news events from ForexFactory

**Request Body:**
```json
{
  "pair": "EUR/USD",     // Optional: filter by pair
  "impact": "high",      // Optional: "high", "medium", "low", or null for all
  "refresh": false        // Optional: force refresh cache
}
```

### GET `/news`
Get all news events (with optional query params)

**Query Parameters:**
- `pair` (optional): Filter by forex pair
- `impact` (optional): Filter by impact level
- `refresh` (optional): Force refresh cache

### GET `/news/{pair}`
Get news for a specific pair

**Query Parameters:**
- `impact` (optional): Filter by impact level

### POST `/news/refresh`
Force refresh the news cache by scraping ForexFactory

### POST `/order`
Execute a LIMIT or STOP order

**Request Body:**
```json
{
  "pair": "EUR/USD",
  "order_type": "LIMIT",        // Optional: "LIMIT" or "STOP" (default: "LIMIT")
  "side": "BUY",                // "BUY" or "SELL"
  "volume": 0.01,               // Volume in lots (min: 0.01, max: 100)
  "entry_price": 1.08000,      // Limit price for LIMIT, stop price for STOP
  "stop_loss": 1.07900,        // Optional stop loss price
  "take_profit": 1.08200,      // Optional take profit price
  "expiration_minutes": 15     // Optional expiration in minutes (default: 15)
}
```

**Note:** If `order_type` is not provided, it defaults to `"LIMIT"`. You can omit it for LIMIT orders:
```json
{
  "pair": "EUR/USD",
  "side": "BUY",
  "volume": 0.01,
  "entry_price": 1.08000
}
```

**Response:**
```json
{
  "status": "success",
  "pair": "EUR/USD",
  "order_type": "LIMIT",
  "side": "BUY",
  "volume": 0.01,
  "entry_price": 1.08000,
  "position_id": 12345,
  "order_id": 67890,
  "price": 1.08000,
  "stop_loss": 1.07900,
  "take_profit": 1.08200,
  "message": "Order executed successfully"
}
```

**Note:** 
- For LIMIT orders: `entry_price` is the limit price at which the order will be executed
- For STOP orders: `entry_price` is the stop price that triggers the order
- Volume is automatically rounded to the nearest valid increment (multiples of 0.01 lots)
- Prices are automatically rounded to appropriate precision (5 decimals for most pairs, 3 for JPY pairs)

### POST `/order/cancel`
Cancel an existing pending order

**Request Body:**
```json
{
  "order_id": 276165059
}
```

**Response:**
```json
{
  "status": "success",
  "order_id": 276165059,
  "message": "Order cancelled successfully"
}
```

### DELETE `/order/{order_id}`
Alternative DELETE endpoint for cancelling an order

**Example:**
```
DELETE /order/276165059
```

## Supported Pairs

- EUR/USD
- GBP/USD
- EUR/JPY
- USD/JPY
- GBP/JPY
- EUR/GBP

## Deployment to Railway

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment instructions.

Quick steps:
1. Push code to GitHub
2. Connect Railway to your GitHub repo
3. Set environment variables in Railway dashboard
4. Deploy!

All necessary files are prepared:
- `Procfile` - Start command
- `requirements.txt` - Dependencies
- `railway.json` - Railway config
- `nixpacks.toml` - Chrome/Selenium setup
- `runtime.txt` - Python version

## Example Usage

### Using curl

```bash
# POST request
curl -X POST "http://localhost:8000/trendbar" \
  -H "Content-Type: application/json" \
  -d '{"pair": "EUR/USD", "weeks": 4}'

# GET request
curl "http://localhost:8000/trendbar/EUR%2FUSD?weeks=4"
```

### Using Python

```python
import requests

response = requests.post("http://localhost:8000/trendbar", json={
    "pair": "EUR/USD",
    "timeframe": "M30",
    "weeks": 6
})

data = response.json()
print(f"Got {data['count']} candles")
```

## Notes

- The Twisted reactor runs in a background thread to handle cTrader connections
- First request may take a few seconds while authentication completes
- Timeout is 30 seconds per request

