# Railway Deployment Guide

## Prerequisites

1. Railway account (sign up at https://railway.app)
2. GitHub repository with your code
3. cTrader API credentials

## Deployment Steps

### 1. Prepare Your Repository

All necessary files are already created:
- ✅ `Procfile` - Tells Railway how to start the server
- ✅ `requirements.txt` - Python dependencies
- ✅ `railway.json` - Railway configuration
- ✅ `nixpacks.toml` - Ensures Chrome/Chromium for Selenium
- ✅ `runtime.txt` - Python version specification
- ✅ `.gitignore` - Excludes sensitive files

### 2. Push to GitHub

```bash
git add .
git commit -m "Prepare for Railway deployment"
git push origin main
```

### 3. Deploy on Railway

1. **Create New Project:**
   - Go to https://railway.app
   - Click "New Project"
   - Select "Deploy from GitHub repo"
   - Choose your repository

2. **Set Environment Variables:**
   
   In Railway dashboard, go to your service → Variables tab, add:
   
   ```
   CTRADER_CLIENT_ID=your_client_id
   CTRADER_CLIENT_SECRET=your_client_secret
   CTRADER_ACCOUNT_ID=your_account_id
   CTRADER_ACCESS_TOKEN=your_access_token  # Optional
   PORT=8000  # Railway sets this automatically, but you can override
   ```

3. **Configure Build Settings:**
   - Railway will auto-detect Python
   - It will use `Procfile` for start command
   - Build should complete automatically

4. **Deploy:**
   - Railway will auto-deploy on push to main branch
   - Or click "Deploy" button manually

### 4. Access Your API

After deployment:
- Railway provides a public URL like: `https://your-app.railway.app`
- API docs available at: `https://your-app.railway.app/docs`
- Health check: `https://your-app.railway.app/health`

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `CTRADER_CLIENT_ID` | Yes | cTrader OpenAPI client ID |
| `CTRADER_CLIENT_SECRET` | Yes | cTrader OpenAPI client secret |
| `CTRADER_ACCOUNT_ID` | Yes | cTrader account ID (integer) |
| `CTRADER_ACCESS_TOKEN` | No | Access token if required |
| `PORT` | No | Server port (Railway sets automatically) |

## Important Notes

### Selenium/Chrome Setup
- The `nixpacks.toml` ensures Chromium is available
- Railway's Nixpacks builder will install Chrome automatically
- No additional setup needed for headless Chrome

### First Request
- First API request may take 10-30 seconds while:
  - cTrader connection establishes
  - Authentication completes
  - News cache is scraped (if needed)

### Scaling
- Railway free tier: 500 hours/month
- Upgrade for more resources if needed
- Consider adding health checks for monitoring

### Monitoring
- Check Railway logs for errors
- Monitor API response times
- Set up alerts if needed

## Troubleshooting

### Build Fails
- Check `requirements.txt` for version conflicts
- Verify Python version in `runtime.txt`
- Check Railway build logs

### Runtime Errors
- Verify all environment variables are set
- Check Railway logs for detailed error messages
- Ensure cTrader credentials are correct

### Chrome/Selenium Issues
- Verify `nixpacks.toml` is present
- Check Railway logs for Chrome initialization errors
- May need to adjust Chrome options in `trader.py`

### Connection Timeouts
- First request is slow (normal)
- Subsequent requests should be faster
- Check cTrader API status

## Custom Domain (Optional)

1. In Railway dashboard → Settings → Networking
2. Add custom domain
3. Railway will provide DNS instructions
4. Update DNS records as instructed

## Continuous Deployment

Railway auto-deploys on:
- Push to main branch (if connected to GitHub)
- Manual deploy button click

To disable auto-deploy:
- Railway dashboard → Settings → Deployments
- Toggle "Auto Deploy"

