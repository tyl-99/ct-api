# cTrader API - cURL Examples

Base URL: `https://web-production-a7d00.up.railway.app`

All endpoints return JSON responses.

---

## Health Check Endpoints

### GET `/` - Root Health Check
```bash
curl https://web-production-a7d00.up.railway.app/
```

### GET `/health` - Detailed Health Check
```bash
curl https://web-production-a7d00.up.railway.app/health
```

---

## Trendbar Endpoints

### POST `/trendbar` - Fetch Trendbar Data (POST)
```bash
curl -X POST https://web-production-a7d00.up.railway.app/trendbar \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "EUR/USD",
    "timeframe": "M30",
    "weeks": 6
  }'
```

### GET `/trendbar/{pair}` - Fetch Trendbar Data (GET)
```bash
# Basic request (uses default timeframe and 6 weeks)
curl "https://web-production-a7d00.up.railway.app/trendbar/EUR%2FUSD"

# With query parameters
curl "https://web-production-a7d00.up.railway.app/trendbar/EUR%2FUSD?timeframe=M30&weeks=4"
```

**Available pairs:** EUR/USD, GBP/USD, EUR/JPY, USD/JPY, GBP/JPY, EUR/GBP  
**Available timeframes:** M1, M5, M15, M30, H1, H4, D1, etc.

---

## Trade Signal Endpoints

### POST `/getTradeSignal` - Get Trade Signal (POST)
```bash
curl -X POST https://web-production-a7d00.up.railway.app/getTradeSignal \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "EUR/USD",
    "timeframe": "M30",
    "weeks": 6
  }'
```

### GET `/getTradeSignal/{pair}` - Get Trade Signal (GET)
```bash
# Basic request
curl "https://web-production-a7d00.up.railway.app/getTradeSignal/EUR%2FUSD"

# With query parameters
curl "https://web-production-a7d00.up.railway.app/getTradeSignal/EUR%2FUSD?timeframe=M30&weeks=4"
```

---

## News Endpoints

### POST `/news` - Get News Events (POST)
```bash
# Get all news events for today (scrapes if cache is empty)
curl -X POST https://web-production-a7d00.up.railway.app/news \
  -H "Content-Type: application/json" \
  -d '{
    "pair": null,
    "impact": null,
    "refresh": false
  }'

# Or simply pass empty body (will scrape today's news if cache is empty)
curl -X POST https://web-production-a7d00.up.railway.app/news \
  -H "Content-Type: application/json" \
  -d '{}'

# Get news for specific pair
curl -X POST https://web-production-a7d00.up.railway.app/news \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "EUR/USD",
    "impact": "high",
    "refresh": false
  }'

# Force refresh news cache
curl -X POST https://web-production-a7d00.up.railway.app/news \
  -H "Content-Type: application/json" \
  -d '{
    "refresh": true
  }'
```

### GET `/news` - Get News Events (GET)
```bash
# Get all news events for today (scrapes if cache is empty)
curl "https://web-production-a7d00.up.railway.app/news"

# Filter by impact
curl "https://web-production-a7d00.up.railway.app/news?impact=high"

# Force refresh
curl "https://web-production-a7d00.up.railway.app/news?refresh=true"
```

### GET `/news/{pair}` - Get News for Specific Pair
```bash
# Get news for EUR/USD
curl "https://web-production-a7d00.up.railway.app/news/EUR%2FUSD"

# Filter by impact
curl "https://web-production-a7d00.up.railway.app/news/EUR%2FUSD?impact=high"
```

### POST `/news/refresh` - Force Refresh News Cache
```bash
curl -X POST https://web-production-a7d00.up.railway.app/news/refresh
```

**Impact values:** `high`, `medium`, `low`

**Note:** When no parameters are passed, the endpoint will:
- Scrape today's news from ForexFactory if cache is empty
- Return ALL events from today (all currencies, all impact levels)
- News is scraped for the current UTC date only

---

## Order Endpoints

### POST `/order` - Execute Order
```bash
# LIMIT BUY order
curl -X POST https://web-production-a7d00.up.railway.app/order \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "EUR/USD",
    "order_type": "LIMIT",
    "side": "BUY",
    "volume": 0.01,
    "entry_price": 1.08500,
    "stop_loss": 1.08000,
    "take_profit": 1.09000,
    "expiration_minutes": 15
  }'

# STOP SELL order
curl -X POST https://web-production-a7d00.up.railway.app/order \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "GBP/USD",
    "order_type": "STOP",
    "side": "SELL",
    "volume": 0.05,
    "entry_price": 1.25000,
    "stop_loss": 1.25500,
    "take_profit": 1.24000
  }'
```

**Order types:** `LIMIT`, `STOP`  
**Sides:** `BUY`, `SELL`  
**Volume:** Minimum 0.01 lots, maximum 100 lots

### POST `/order/cancel` - Cancel Order
```bash
curl -X POST https://web-production-a7d00.up.railway.app/order/cancel \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": 12345
  }'
```

### DELETE `/order/{order_id}` - Cancel Order (DELETE)
```bash
curl -X DELETE https://web-production-a7d00.up.railway.app/order/12345
```

---

## Account Data Endpoints

### GET `/account-data` - Get Account Data
```bash
# Use account ID from environment variable
curl "https://web-production-a7d00.up.railway.app/account-data"

# Specify account ID as query parameter
curl "https://web-production-a7d00.up.railway.app/account-data?account_id=45303451"
```

### GET `/account-data/{account_id}` - Get Account Data by ID
```bash
curl "https://web-production-a7d00.up.railway.app/account-data/45303451"
```

**Returns:**
- `summary_stats`: Overall statistics and per-pair summaries
- `trades_by_symbol`: All closed trades grouped by symbol
- `account_info`: Account balance, equity, margin, etc.
- `open_positions`: Current open positions

---

## Example Responses

### Trendbar Response
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
    }
  ]
}
```

### Account Data Response
```json
{
  "summary_stats": {
    "total_trades": 150,
    "total_wins": 85,
    "total_losses": 65,
    "total_pnl": 1234.56,
    "overall_win_rate": 56.67,
    "pairs_summary": {
      "EUR_USD": {
        "total_trades": 50,
        "wins": 30,
        "losses": 20,
        "total_pnl": 500.00,
        "win_rate": 60.0
      }
    },
    "account_info": {
      "account_id": 45303451,
      "balance": 10000.00,
      "equity": 10123.45,
      "free_margin": 5000.00
    },
    "open_positions": []
  },
  "trades_by_symbol": {
    "EUR_USD": [
      {
        "Trade ID": 123,
        "pair": "EUR/USD",
        "Entry DateTime": "2025-01-10T10:30:00",
        "Buy/Sell": "BUY",
        "Entry Price": 1.08500,
        "SL": 1.08000,
        "TP": 1.09000,
        "Close Price": 1.08950,
        "Pips": 45.0,
        "Lots": 0.10,
        "PnL": 45.00,
        "Win/Lose": "WIN"
      }
    ]
  },
  "account_id": 45303451,
  "timestamp": "2025-01-15T10:30:00"
}
```

---

## Notes

- All endpoints require the trader to be initialized and authenticated
- News scraping happens on-demand (not on startup)
- Account data endpoint fetches closed deals, open positions, and account statistics
- Order endpoints require valid cTrader account credentials
- URL encoding: Use `%2F` for `/` in URL paths (e.g., `EUR%2FUSD`)

