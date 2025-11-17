# Quick Railway Deployment Guide

## ✅ Pre-Deployment Checklist

### 1. Files Ready
- [x] `Procfile` - Start command
- [x] `requirements.txt` - Dependencies
- [x] `railway.json` - Railway config
- [x] `nixpacks.toml` - Chrome/Selenium setup
- [x] `runtime.txt` - Python version
- [x] `.gitignore` - Excludes sensitive files

### 2. Code Ready
- [x] FastAPI server (`main.py`)
- [x] Trader logic (`trader.py`)
- [x] All endpoints implemented:
  - `GET /` - Health check
  - `GET /health` - Trader connection status
  - `POST /trendbar` - Get trendbar data
  - `GET /trendbar/{pair}` - GET trendbar endpoint
  - `POST /news` - Get forex news
  - `GET /news` - Get all news
  - `GET /news/{pair}` - Get news for pair
  - `POST /news/refresh` - Refresh news cache
  - `POST /order` - Execute LIMIT/STOP order (LIMIT default)
  - `POST /order/cancel` - Cancel order
  - `DELETE /order/{order_id}` - Cancel order (DELETE)

### 3. Environment Variables Needed

Set these in Railway dashboard → Variables:

```
CTRADER_CLIENT_ID=your_client_id
CTRADER_CLIENT_SECRET=your_client_secret
CTRADER_ACCOUNT_ID=your_account_id
CTRADER_ACCESS_TOKEN=your_access_token  # Optional
```

## 🚀 Deployment Steps

### Step 1: Push to GitHub
```bash
git add .
git commit -m "Ready for Railway deployment"
git push origin main
```

### Step 2: Deploy on Railway

1. Go to https://railway.app
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Choose your repository: `tyl-99/ct-api`
5. Railway will auto-detect Python and start building

### Step 3: Set Environment Variables

1. In Railway dashboard → Your service → **Variables** tab
2. Add all required environment variables (see above)
3. Save changes

### Step 4: Monitor Deployment

1. Check **Deployments** tab for build progress
2. Check **Logs** tab for startup messages
3. Wait for "Connected to cTrader" message
4. Your API will be live at: `https://your-app.railway.app`

## 🔍 Post-Deployment Testing

### Test Health Check
```bash
curl https://your-app.railway.app/health
```

### Test API Docs
Open in browser:
```
https://your-app.railway.app/docs
```

### Test Trendbar Endpoint
```bash
curl -X POST "https://your-app.railway.app/trendbar" \
  -H "Content-Type: application/json" \
  -d '{"pair": "EUR/USD", "weeks": 6}'
```

### Test Order Execution
```bash
curl -X POST "https://your-app.railway.app/order" \
  -H "Content-Type: application/json" \
  -d '{
    "pair": "EUR/USD",
    "side": "BUY",
    "volume": 0.01,
    "entry_price": 1.08000,
    "stop_loss": 1.07900,
    "take_profit": 1.08200
  }'
```

## 📝 Important Notes

### Connection Behavior
- ✅ **Connection stays alive** while Railway service is running
- ✅ **Single authentication** at startup
- ✅ **Ready for requests** immediately after startup
- ⚠️ **Reconnects automatically** on Railway restarts

### First Request
- First API request may take **10-30 seconds** while:
  - cTrader connection establishes
  - Authentication completes
  - News cache is scraped (if needed)

### Monitoring
- Check Railway **Logs** tab for connection status
- Use `/health` endpoint to verify trader connection
- Monitor for any disconnection errors

### Scaling
- Railway free tier: **500 hours/month**
- Upgrade if you need more resources
- Connection is per-instance (each Railway service has its own connection)

## 🐛 Troubleshooting

### Build Fails
- Check `requirements.txt` for version conflicts
- Verify Python version in `runtime.txt`
- Check Railway build logs

### Connection Errors
- Verify all environment variables are set correctly
- Check Railway logs for authentication errors
- Ensure cTrader credentials are valid

### Chrome/Selenium Issues
- Verify `nixpacks.toml` is present
- Check Railway logs for Chrome initialization
- May need to adjust Chrome options if issues persist

## 🎉 Success Indicators

You'll know it's working when:
- ✅ Build completes successfully
- ✅ Logs show "Connected to cTrader"
- ✅ Logs show "User authenticated"
- ✅ `/health` endpoint returns `"trader_connected": true`
- ✅ API docs load at `/docs`

## 📚 Documentation

- Full deployment guide: See `DEPLOYMENT.md`
- API documentation: See `README.md`
- Railway checklist: See `RAILWAY_CHECKLIST.md`

---

**Ready to deploy!** 🚀

