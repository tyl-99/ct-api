# How to Run API Server on Localhost

## Prerequisites

1. **Activate conda environment:**
   ```bash
   conda activate trader-env
   ```

2. **Set up environment variables:**
   
   Create a `.env` file in the project root with:
   ```
   CTRADER_CLIENT_ID=your_client_id
   CTRADER_CLIENT_SECRET=your_client_secret
   CTRADER_ACCOUNT_ID=your_account_id
   CTRADER_ACCESS_TOKEN=your_access_token  # Optional
   PORT=8000  # Optional, defaults to 8000
   ```

## Running the Server

### Method 1: Using Python directly
```bash
conda activate trader-env
cd "d:\Projects\Visual Studio Code\Bots\API"
python main.py
```

### Method 2: Using uvicorn directly
```bash
conda activate trader-env
cd "d:\Projects\Visual Studio Code\Bots\API"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The `--reload` flag enables auto-reload on code changes (useful for development).

## Accessing the API

Once running, the API will be available at:
- **API Base URL:** `http://localhost:8000`
- **API Docs (Swagger UI):** `http://localhost:8000/docs`
- **Alternative API Docs (ReDoc):** `http://localhost:8000/redoc`

## Test Endpoints

### Health Check
```bash
curl http://localhost:8000/
```

### Get Trendbar Data (POST)
```bash
curl -X POST "http://localhost:8000/trendbar" \
  -H "Content-Type: application/json" \
  -d "{\"pair\": \"EUR/USD\", \"weeks\": 6}"
```

### Get Trendbar Data (GET)
```bash
curl "http://localhost:8000/trendbar/EUR%2FUSD?weeks=6"
```

## Troubleshooting

- **Port already in use:** Change PORT in `.env` or use `--port 8001`
- **Connection errors:** Make sure cTrader credentials are correct in `.env`
- **Import errors:** Make sure conda environment is activated

