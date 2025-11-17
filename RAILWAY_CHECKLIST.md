# Railway Deployment Checklist

## ✅ Pre-Deployment Files (All Ready)

- [x] `Procfile` - Start command for Railway
- [x] `requirements.txt` - Python dependencies
- [x] `railway.json` - Railway configuration
- [x] `nixpacks.toml` - Chrome/Selenium setup for news scraping
- [x] `runtime.txt` - Python version (3.10.16)
- [x] `.gitignore` - Excludes sensitive files
- [x] `README.md` - API documentation
- [x] `DEPLOYMENT.md` - Detailed deployment guide

## 📋 Before Deploying

### 1. Environment Variables to Set in Railway

Make sure you have these ready:
- [ ] `CTRADER_CLIENT_ID`
- [ ] `CTRADER_CLIENT_SECRET`
- [ ] `CTRADER_ACCOUNT_ID`
- [ ] `CTRADER_ACCESS_TOKEN` (if needed)

### 2. Code Review

- [ ] All endpoints tested locally
- [ ] No hardcoded credentials
- [ ] `.env` file is in `.gitignore`
- [ ] All sensitive data removed from code

### 3. Git Repository

- [ ] Code pushed to GitHub
- [ ] All files committed
- [ ] No sensitive files in repository

## 🚀 Deployment Steps

1. [ ] Create Railway account
2. [ ] Create new project
3. [ ] Connect GitHub repository
4. [ ] Set environment variables
5. [ ] Deploy and monitor logs
6. [ ] Test endpoints after deployment
7. [ ] Set up custom domain (optional)

## 🧪 Post-Deployment Testing

Test these endpoints:
- [ ] `GET /` - Health check
- [ ] `GET /health` - Trader connection status
- [ ] `POST /trendbar` - Fetch trendbar data
- [ ] `GET /trendbar/{pair}` - GET trendbar endpoint
- [ ] `POST /news` - Get news
- [ ] `GET /news` - Get all news
- [ ] `GET /news/{pair}` - Get news for pair
- [ ] `POST /news/refresh` - Refresh news cache

## 📝 Notes

- First request may take 10-30 seconds (authentication + news scraping)
- Monitor Railway logs for any errors
- Check API docs at `/docs` endpoint after deployment
- Keep environment variables secure

## 🔄 Future Updates

When adding new endpoints:
1. Update `main.py` with new endpoint
2. Test locally
3. Push to GitHub
4. Railway will auto-deploy (if enabled)

