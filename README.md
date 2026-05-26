# ◈ STOCKBOX — Railway Deployment

## Files
- `server.py` — Flask backend (fetches live stock data via yfinance)
- `static/index.html` — Full dashboard frontend
- `requirements.txt` — Python dependencies
- `Procfile` — Process config for Railway
- `railway.toml` — Railway settings

## Deploy to Railway (5 minutes)

### Step 1 — Upload to GitHub
1. Go to github.com → New repository → name it `stockbox`
2. Upload all these files (drag and drop)
3. Click "Commit changes"

### Step 2 — Deploy on Railway
1. Go to railway.app → Sign up free with GitHub
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your `stockbox` repo
4. Railway auto-detects Python and deploys

### Step 3 — Add your domain
1. In Railway dashboard → your project → Settings → Domains
2. Click "Generate Domain" — you get a free URL like `stockbox-production.up.railway.app`

### Step 4 — Done!
Visit your URL in Chrome. Search any stock. Works on mobile too.

## Features
- Any stock, ETF or crypto (AAPL, VOD.L, BTC-USD, etc.)
- Live prices via Yahoo Finance (no API key needed)
- Full analysis: Overview, Valuation, Financials, Score Engine, AI
- Portfolio tracker (saves to browser)
- Watchlist (saves to browser)
- Macro dashboard
- AI analysis (add Anthropic key in Macro tab)

## Notes
- Free Railway tier: 500 hours/month (enough for personal use)
- Data source: Yahoo Finance (yfinance) — accurate real-time prices
- No API key required for stock data
