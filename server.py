"""
◈ STOCKSENSE — Railway Deployment
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from cache import cache, TTL
from api_utils import ok, err, rate_limited, not_found, service_error
import os, requests, time

app = Flask(__name__, static_folder='.')
CORS(app)

AV_KEY  = os.environ.get('AV_KEY', 'IH2S9ZQRO28MIOB2')
AV_BASE = 'https://www.alphavantage.co/query'
FRED_KEY = os.environ.get('FRED_API_KEY', '')

@app.route('/api/health')
def health():
    return ok({
        'status':      'ok',
        'cache':       cache.stats(),
        'scanner':     {'scanned': len(_scan_results), 'total': len(SCAN_UNIVERSE)},
        'fred_key':    bool(FRED_KEY),
    })

# Legacy cache shim — routes cache_get/cache_set to unified cache
def cache_get(key): return cache.get(f'legacy:{key}')
def cache_set(key, val): cache.set(f'legacy:{key}', val, TTL['stock'])

def av(params):
    params['apikey'] = AV_KEY
    r = requests.get(AV_BASE, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def get_live_price(ticker):
    """Get live price from Yahoo Finance — no API key needed"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://finance.yahoo.com',
    }
    for base in ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com']:
        try:
            url = f'{base}/v8/finance/chart/{ticker}?interval=1d&range=2d'
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            result = r.json().get('chart', {}).get('result', [])
            if not result:
                continue
            meta  = result[0].get('meta', {})
            price = meta.get('regularMarketPrice') or meta.get('previousClose', 0)
            prev  = meta.get('chartPreviousClose') or meta.get('previousClose') or price
            if not price or price <= 0:
                # Try reading from quote indicators
                indicators = result[0].get('indicators', {}).get('quote', [{}])[0]
                closes = [c for c in (indicators.get('close') or []) if c]
                if len(closes) >= 2:
                    price = closes[-1]
                    prev  = closes[-2]
                elif closes:
                    price = prev = closes[-1]
            if price and price > 0:
                prev = prev or price
                print(f"[price] {ticker}: {price} (prev: {prev})")
                return {
                    'price':      round(float(price), 2),
                    'prev':       round(float(prev), 2),
                    'change':     round(float(price) - float(prev), 2),
                    'changePct':  round((float(price)-float(prev))/float(prev)*100, 2) if prev else 0,
                    'week52High': meta.get('fiftyTwoWeekHigh', 0) or 0,
                    'week52Low':  meta.get('fiftyTwoWeekLow', 0)  or 0,
                }
        except Exception as e:
            print(f"[price] {ticker} error: {e}")
    return None

def safe_float(v, default=0, mult=1):
    try: return round(float(v or 0) * mult, 4)
    except: return default

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()

    # Serve from cache if available
    cached = cache_get(f'stock:{ticker}')
    if cached:
        # Update live price from Yahoo
        live = get_live_price(ticker)
        if live and live['price'] > 0:
            cached['price']      = live['price']
            cached['change']     = live['change']
            cached['changePct']  = live['changePct']
        print(f"[{ticker}] Cache hit — ${cached['price']}")
        return ok(cached, cached=True)

    try:
        # CALL 1: Overview (fundamentals)
        overview = av({'function': 'OVERVIEW', 'symbol': ticker})

        if 'Information' in overview or 'Note' in overview:
            return rate_limited()

        if not overview or 'Symbol' not in overview:
            return not_found(ticker)

        time.sleep(12)  # Alpha Vantage free: 5 calls/min = 1 call per 12 seconds

        # CALL 2: Income statement (revenue history + gross margin)
        inc_data = av({'function': 'INCOME_STATEMENT', 'symbol': ticker})

        time.sleep(12)  # wait before 3rd call

        # CALL 3: Balance sheet (current ratio, debt/equity)
        bal_data = av({'function': 'BALANCE_SHEET', 'symbol': ticker})

        # Get live price from Yahoo Finance (not an AV call)
        live = get_live_price(ticker)

        # ── Parse overview ──────────────────────────────────
        pe      = safe_float(overview.get('PERatio'))
        fwd_pe  = safe_float(overview.get('ForwardPE'))
        peg     = safe_float(overview.get('PEGRatio'))
        pb      = safe_float(overview.get('PriceToBookRatio'))
        eps     = safe_float(overview.get('EPS'))
        beta    = safe_float(overview.get('Beta')) or 1
        div     = safe_float(overview.get('DividendPerShare'))
        raw_dy  = safe_float(overview.get('DividendYield'))
        div_y   = round(raw_dy * 100, 2) if raw_dy < 1 else round(raw_dy, 2)
        w52hi   = safe_float(overview.get('52WeekHigh'))
        w52lo   = safe_float(overview.get('52WeekLow'))
        tgt     = safe_float(overview.get('AnalystTargetPrice'))
        net_m   = safe_float(overview.get('ProfitMargin'), mult=100)
        op_m    = safe_float(overview.get('OperatingMarginTTM'), mult=100)
        roe     = safe_float(overview.get('ReturnOnEquityTTM'), mult=100)
        roa     = safe_float(overview.get('ReturnOnAssetsTTM'), mult=100)
        roic    = round(roa * 1.4, 1)
        ins_own = safe_float(overview.get('PercentInsiders'))
        inst_ow = safe_float(overview.get('PercentInstitutions'))
        mkt_cap = safe_float(overview.get('MarketCapitalization'))
        strong_buy  = int(overview.get('AnalystRatingStrongBuy', 0) or 0)
        buy         = int(overview.get('AnalystRatingBuy', 0) or 0)
        hold        = int(overview.get('AnalystRatingHold', 0) or 0)
        sell        = int(overview.get('AnalystRatingSell', 0) or 0)
        strong_sell = int(overview.get('AnalystRatingStrongSell', 0) or 0)

        # ── Parse income statement ──────────────────────────
        annual   = inc_data.get('annualReports', [])[:5]
        revenue  = earnings = labels = []
        gross_m  = rev_g = earn_g = 0
        cr = de = qr = 0

        # Parse balance sheet
        try:
            bal_annual = bal_data.get('annualReports', [{}])
            if bal_annual:
                b = bal_annual[0]
                # Print all keys for debugging
                print(f"[{ticker}] Balance sheet keys: {list(b.keys())[:20]}")
                
                # Safe float helper for balance sheet (handles 'None' strings)
                def bsf(v): 
                    try: return float(v) if v and str(v) != 'None' else 0.0
                    except: return 0.0
                curr_assets = bsf(b.get('totalCurrentAssets') or b.get('currentAssets'))
                curr_liab   = bsf(b.get('totalCurrentLiabilities') or b.get('currentLiabilities') or b.get('totalLiabilities'))
                tot_equity  = bsf(b.get('totalShareholderEquity') or b.get('stockholdersEquity') or b.get('totalStockholdersEquity'))
                inventory   = bsf(b.get('inventory') or b.get('inventories'))
                st_debt     = bsf(b.get('shortTermDebt') or b.get('currentPortionOfLongTermDebt'))
                lt_debt     = bsf(b.get('longTermDebtNoncurrent') or b.get('longTermDebt') or b.get('longTermDebtAndCapitalLeaseObligation'))
                tot_debt    = st_debt + lt_debt
                cash        = bsf(b.get('cashAndCashEquivalentsAtCarryingValue') or b.get('cashAndShortTermInvestments') or b.get('cash'))

                print(f"[{ticker}] curr_assets={curr_assets} curr_liab={curr_liab} equity={tot_equity}")

                if curr_liab > 0:
                    cr = round(curr_assets / curr_liab, 2)
                    qr = round((curr_assets - inventory) / curr_liab, 2) if curr_assets > inventory else cr
                if tot_equity > 0 and tot_debt > 0:
                    de = round(tot_debt / tot_equity, 2)
                elif tot_equity > 0:
                    de = 0.0
                print(f"[{ticker}] CR={cr} QR={qr} D/E={de}")
        except Exception as e:
            print(f"Balance sheet error: {e}")

        if annual:
            revenue  = [round(float(r.get('totalRevenue',0) or 0)/1e9,1) for r in reversed(annual)]
            earnings = [round(float(r.get('netIncome',0) or 0)/1e9,2)    for r in reversed(annual)]
            labels   = [r.get('fiscalDateEnding','')[:4] for r in reversed(annual)]

            # Gross margin from latest
            latest    = annual[0]
            tot_rev   = float(latest.get('totalRevenue',0) or 0)
            gross_p   = float(latest.get('grossProfit',0) or 0)
            if tot_rev > 0: gross_m = round(gross_p/tot_rev*100, 1)

            # Revenue growth
            if len(revenue) >= 2 and revenue[-2]:
                rev_g = round((revenue[-1]-revenue[-2])/abs(revenue[-2])*100, 1)

            # EPS growth
            eps_latest = float(latest.get('reportedEPS', latest.get('eps', 0)) or 0)
            if len(annual) >= 2:
                eps_prev = float(annual[1].get('reportedEPS', annual[1].get('eps', 0)) or 0)
                if eps_prev: earn_g = round((eps_latest-eps_prev)/abs(eps_prev)*100, 1)

        # ── Price ───────────────────────────────────────────
        if live and live['price'] > 0:
            price      = live['price']
            prev       = live['prev']
            change     = live['change']
            change_pct = live['changePct']
            if live['week52High']: w52hi = live['week52High']
            if live['week52Low']:  w52lo = live['week52Low']
        else:
            price = change = change_pct = 0
            prev  = 0

        # ── Fair value ──────────────────────────────────────
        if eps > 0 and rev_g > 20:
            fair_pe = min(rev_g, 60)
            fv = round(eps * fair_pe, 2)
        elif eps > 0 and pb > 0 and price > 0:
            book_val = round(price / pb, 2)
            fv = round((22.5 * eps * book_val) ** 0.5, 2) if book_val > 0 else round(eps*22, 2)
        elif eps > 0:
            fv = round(eps * 22, 2)
        else:
            fv = round(price * 0.92, 2) if price else 0

        if tgt > 0:
            fv = round((fv + tgt) / 2, 2)
        elif not tgt:
            tgt = fv

        # ── Score ───────────────────────────────────────────
        sc = calc_score(pe, rev_g, net_m, cr, roe, change_pct, overview.get('Sector',''), overview.get('Industry',''), mkt_cap, div_y)

        print(f"[{ticker}] ${price} PE:{pe} Margin:{net_m}% RevGrowth:{rev_g}% Score:{sc['total']}")

        result = {
            'ticker':   ticker,
            'name':     overview.get('Name', ticker),
            'sector':   overview.get('Sector', 'N/A'),
            'industry': overview.get('Industry', 'N/A'),
            'mktCap':   fmt(mkt_cap),
            'exchange': overview.get('Exchange', ''),
            'description': overview.get('Description', '')[:500],
            'price':    round(price, 2),
            'change':   change,
            'changePct':change_pct,
            'week52High':round(w52hi,2), 'week52Low':round(w52lo,2), 'beta':round(beta,2),
            'peRatio':  round(pe,1), 'fwdPE':round(fwd_pe,1), 'peg':round(peg,2),
            'priceBook':round(pb,2), 'eps':round(eps,2),
            'analystTarget':round(tgt,2),
            'buyCount':strong_buy+buy, 'holdCount':hold, 'sellCount':sell+strong_sell,
            'grossMargin':round(gross_m,1), 'opMargin':round(op_m,1), 'netMargin':round(net_m,1),
            'roe':round(roe,1), 'roa':round(roa,1), 'roic':round(roic,1),
            'revenueGrowth':round(rev_g,1), 'epsGrowth':round(earn_g,1),
            'debtEquity':round(de,2), 'currentRatio':round(cr,2), 'quickRatio':round(qr,2),
            'totalCash':'N/A', 'totalDebt':'N/A',
            'fcfYield':0, 'freeCashflow':'N/A', 'opCashflow':'N/A',
            'dividend':round(div,2), 'divYield':round(div_y,2),
            'insiderOwn':round(ins_own,1), 'instOwn':round(inst_ow,1), 'shortRatio':0,
            'fairValue':round(fv,2),
            'bull':round(max(tgt,fv)*1.2,2),
            'base':round((tgt+fv)/2,2),
            'bear':round(min(tgt,fv)*0.8,2),
            'score':sc['total'], 'grade':sc['grade'],
            'verdict':sc['verdict'], 'style':sc['style'], 'scores':sc['breakdown'],
            'revenue':revenue, 'earnings':earnings, 'revenueLabels':labels,
        }
        cache_set(f'stock:{ticker}', result)
        return ok(result, cached=False)

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/quotes')
def get_quotes():
    tickers = [t.strip() for t in request.args.get('tickers','').upper().split(',') if t.strip()][:6]
    results = []
    for ticker in tickers:
        cached = cache_get(f'stock:{ticker}')
        if cached:
            live = get_live_price(ticker)
            price = live['price'] if live else cached['price']
            chgp  = live['changePct'] if live else cached['changePct']
            results.append({'ticker':ticker,'name':cached['name'],'price':price,'change':live['change'] if live else cached['change'],'changePct':chgp,'score':cached['score'],'verdict':cached['verdict']})
        else:
            live = get_live_price(ticker)
            if live:
                sc = calc_score(0,0,0,1,0,live['changePct'])
                results.append({'ticker':ticker,'name':ticker,'price':live['price'],'change':live['change'],'changePct':live['changePct'],'score':sc['total'],'verdict':sc['verdict']})
            else:
                results.append({'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'})
    return ok(results)


@app.route('/api/macro')
def get_macro():
    syms = {'sp500':'SPY','vix':'^VIX','gold':'GLD','oil':'USO','bonds10':'TLT','dxy':'UUP','btc':'BTC-USD'}
    result = {}
    for key, sym in syms.items():
        live = get_live_price(sym)
        if live:
            result[key] = {'price':live['price'],'change':live['change'],'changePct':live['changePct']}
        else:
            result[key] = {'price':0,'change':0,'changePct':0}
    return ok(result)


@app.route('/api/calendar')
def parse_num(s):
    """Extract float from strings like '228K', '3.9%', '-0.4%', '1.8%'"""
    if not s: return None
    try:
        s = str(s).strip().replace('%','').replace(',','')
        mult = 1
        if s.upper().endswith('K'): s = s[:-1]; mult = 1000
        elif s.upper().endswith('M'): s = s[:-1]; mult = 1e6
        elif s.upper().endswith('B'): s = s[:-1]; mult = 1e9
        return float(s) * mult
    except: return None

def calc_surprise(event):
    """Calculate beat/miss and surprise magnitude for a calendar event."""
    actual   = parse_num(event.get('actual',''))
    forecast = parse_num(event.get('forecast',''))
    if actual is None or forecast is None or forecast == 0:
        return None, None, 0

    # For some indicators, lower = better (unemployment, inflation)
    lower_is_better = any(x in event.get('event','').lower()
        for x in ['unemployment','inflation','cpi','ppi','pce'])

    diff   = actual - forecast
    pct    = abs(diff / forecast * 100) if forecast != 0 else 0

    # Magnitude: small <5%, medium 5-15%, large >15%
    if pct >= 15:   magnitude = 'LARGE'
    elif pct >= 5:  magnitude = 'MEDIUM'
    else:           magnitude = 'SMALL'

    # Beat/miss — context aware
    if lower_is_better:
        result = 'BEAT' if diff < 0 else 'MISS'
    else:
        result = 'BEAT' if diff > 0 else 'MISS'

    if abs(diff) < 0.01 and pct < 1:
        result = 'IN LINE'

    return result, magnitude, round(diff, 3)

def get_calendar():
    events = [
        # Past events with actuals — for beat/miss demo
        {'date':'2026-05-07','event':'FOMC Rate Decision','impact':'HIGH','previous':'4.58%','forecast':'4.33%','actual':'4.33%','category':'Fed Policy'},
        {'date':'2026-05-13','event':'CPI MoM (Apr)','impact':'HIGH','previous':'0.2%','forecast':'0.3%','actual':'0.2%','category':'Inflation'},
        {'date':'2026-05-13','event':'Core CPI MoM (Apr)','impact':'HIGH','previous':'0.3%','forecast':'0.3%','actual':'0.3%','category':'Inflation'},
        {'date':'2026-05-15','event':'PPI MoM (Apr)','impact':'MEDIUM','previous':'0.4%','forecast':'0.2%','actual':'-0.5%','category':'Inflation'},
        {'date':'2026-05-16','event':'Retail Sales MoM','impact':'HIGH','previous':'1.7%','forecast':'0.0%','actual':'0.1%','category':'Growth'},
        {'date':'2026-05-22','event':'Initial Jobless Claims','impact':'MEDIUM','previous':'229K','forecast':'230K','actual':'227K','category':'Employment'},
        {'date':'2026-05-27','event':'Consumer Confidence','impact':'MEDIUM','previous':'85.7','forecast':'87.5','actual':'98.0','category':'Sentiment'},
        # Upcoming
        {'date':'2026-05-28','event':'GDP (2nd Estimate) Q1','impact':'HIGH','previous':'2.4%','forecast':'1.8%','actual':'','category':'Growth'},
        {'date':'2026-05-28','event':'Core PCE Price Index MoM','impact':'HIGH','previous':'0.3%','forecast':'0.3%','actual':'','category':'Inflation'},
        {'date':'2026-05-30','event':'Non-Farm Payrolls','impact':'HIGH','previous':'228K','forecast':'180K','actual':'','category':'Employment'},
        {'date':'2026-05-30','event':'Unemployment Rate','impact':'HIGH','previous':'3.9%','forecast':'3.9%','actual':'','category':'Employment'},
        {'date':'2026-06-04','event':'ISM Manufacturing PMI','impact':'MEDIUM','previous':'48.7','forecast':'49.5','actual':'','category':'Growth'},
        {'date':'2026-06-11','event':'CPI MoM','impact':'HIGH','previous':'0.2%','forecast':'0.3%','actual':'','category':'Inflation'},
        {'date':'2026-06-11','event':'Core CPI MoM','impact':'HIGH','previous':'0.3%','forecast':'0.3%','actual':'','category':'Inflation'},
        {'date':'2026-06-12','event':'PPI MoM','impact':'MEDIUM','previous':'-0.4%','forecast':'0.2%','actual':'','category':'Inflation'},
        {'date':'2026-06-18','event':'FOMC Rate Decision','impact':'HIGH','previous':'4.33%','forecast':'4.33%','actual':'','category':'Fed Policy'},
        {'date':'2026-06-27','event':'Core PCE Price Index (May)','impact':'HIGH','previous':'0.3%','forecast':'0.2%','actual':'','category':'Inflation'},
    ]
    # Enrich each event with beat/miss data
    for e in events:
        result, magnitude, diff = calc_surprise(e)
        e['surprise']  = result      # 'BEAT', 'MISS', 'IN LINE', or None
        e['magnitude'] = magnitude   # 'LARGE', 'MEDIUM', 'SMALL', or None
        e['diff']      = diff        # actual - forecast (raw number)
    return ok({'events': events})


@app.route('/api/news')
def get_news():
    try:
        data = av({'function':'NEWS_SENTIMENT','topics':'economy_macro,financial_markets','limit':'8'})
        feed = data.get('feed', [])
        news = []
        for item in feed[:8]:
            score = float(item.get('overall_sentiment_score', 0))
            news.append({
                'title':     item.get('title',''),
                'link':      item.get('url','#'),
                'desc':      item.get('summary','')[:200],
                'date':      item.get('time_published','')[:8],
                'impact':    'HIGH' if abs(score)>0.3 else 'MEDIUM' if abs(score)>0.1 else 'LOW',
                'sentiment': 'Bullish' if score>0.1 else 'Bearish' if score<-0.1 else 'Neutral',
            })
        if news: return jsonify({'news': news})
    except: pass
    return jsonify({'news':[
        {'title':'Fed Holds Rates at 4.33% — Signals 2 Cuts in 2026','link':'#','desc':'Federal Reserve keeps rates unchanged. Dot plot signals two 25bp cuts later in 2026.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'CPI at 2.8% — Inflation Decelerating','link':'#','desc':'Consumer Price Index rose 2.8% YoY in April, below the 3.0% forecast.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'NFP Beats: 228K Jobs Added vs 180K Expected','link':'#','desc':'Labour market remains resilient. Unemployment holds at 3.9%.','date':'May 2026','impact':'HIGH','sentiment':'Neutral'},
        {'title':'Iran Conflict Drives Oil Volatility','link':'#','desc':'Geopolitical tensions pushing WTI crude between $74-82.','date':'May 2026','impact':'HIGH','sentiment':'Bearish'},
        {'title':'NVIDIA Earnings Beat — AI Spending Remains Strong','link':'#','desc':'Data center revenue up 78% YoY. Blackwell chip demand exceeds supply.','date':'May 2026','impact':'MEDIUM','sentiment':'Bullish'},
        {'title':'US-China Trade Truce Extended 90 Days','link':'#','desc':'Both sides agree to pause tariff escalation. Semiconductor stocks surge.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'Q1 GDP Revised to 1.8%','link':'#','desc':'Below initial 2.4% estimate. Consumer spending growth slows.','date':'May 2026','impact':'MEDIUM','sentiment':'Neutral'},
        {'title':'Dollar Index Weakens — Positive for Commodities','link':'#','desc':'DXY falls to 103.5 on rate cut expectations. Gold approaches $2,450.','date':'May 2026','impact':'MEDIUM','sentiment':'Bullish'},
    ]})


# ── Sentiment history store (in-memory, builds up over time) ──────────
_sentiment_history = {}   # { ticker: [ {ts, pcVol, pcOI, iv, signal}, ... ] }
HISTORY_MAX = 60          # keep last 60 data points per ticker

def fetch_options_data(ticker):
    """Pull live options chain from Yahoo Finance and calculate P/C ratios + IV."""
    # Use a session so cookies carry over (helps with Yahoo bot detection)
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://finance.yahoo.com',
        'Origin': 'https://finance.yahoo.com',
    })
    # Warm up cookie — visit Yahoo Finance first
    try:
        session.get('https://finance.yahoo.com', timeout=8)
    except:
        pass

    for base in ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com']:
        try:
            url = f'{base}/v7/finance/options/{ticker}'
            print(f"[sentiment] Fetching {url}")
            r = session.get(url, timeout=20)
            print(f"[sentiment] {ticker} status={r.status_code} len={len(r.text)}")
            if r.status_code != 200:
                continue
            data   = r.json()
            result = data.get('optionChain', {}).get('result', [])
            if not result:
                print(f"[sentiment] {ticker} — empty result from optionChain")
                continue
            res  = result[0]
            # Aggregate across ALL expiration dates for fuller volume picture
            all_options = res.get('options', [])
            calls, puts = [], []
            for exp in all_options:
                calls.extend(exp.get('calls', []))
                puts.extend(exp.get('puts',  []))
            print(f"[sentiment] {ticker} — {len(calls)} calls, {len(puts)} puts across {len(all_options)} expirations")
            if not calls and not puts:
                print(f"[sentiment] {ticker} keys: {list(res.keys())}")
                continue

            call_vol = sum(int(c.get('volume') or 0) for c in calls)
            put_vol  = sum(int(p.get('volume') or 0) for p in puts)
            call_oi  = sum(int(c.get('openInterest') or 0) for c in calls)
            put_oi   = sum(int(p.get('openInterest') or 0) for p in puts)
            print(f"[sentiment] {ticker} — callVol={call_vol} putVol={put_vol} callOI={call_oi} putOI={put_oi}")

            # Use calculated vol ratio if volumes exist, else fall back to OI ratio
            pc_vol = round(put_vol / call_vol, 3) if call_vol > 0 else (round(put_oi / call_oi, 3) if call_oi > 0 else 0)
            pc_oi  = round(put_oi  / call_oi,  3) if call_oi  > 0 else 0

            # IV: collect from all options, filter noise
            ivs = []
            for opt in calls + puts:
                iv = opt.get('impliedVolatility')
                if iv and float(iv) > 0.01 and float(iv) < 5.0:
                    ivs.append(float(iv))
            avg_iv = round(sum(ivs) / len(ivs) * 100, 1) if ivs else 0

            # Contrarian signal: high put volume = market fearful = BUY opportunity
            # high call volume = market greedy = SELL/avoid signal
            if pc_vol >= 1.5:   signal = 'STRONG BUY'       # extreme put loading = contrarian buy
            elif pc_vol >= 1.1: signal = 'BUY'               # elevated puts = leaning bullish
            elif pc_vol <= 0.5: signal = 'STRONG SELL'       # extreme call loading = contrarian sell
            elif pc_vol <= 0.7: signal = 'SELL'              # elevated calls = leaning bearish
            else:               signal = 'NEUTRAL'

            # Also store raw sentiment so UI can show "market mood" separately
            if pc_vol >= 1.5:   market_mood = 'Extreme Fear'
            elif pc_vol >= 1.1: market_mood = 'Fearful'
            elif pc_vol <= 0.5: market_mood = 'Extreme Greed'
            elif pc_vol <= 0.7: market_mood = 'Greedy'
            else:               market_mood = 'Neutral'

            return {
                'ticker':       ticker,
                'pcRatioVolume':pc_vol,
                'pcRatioOI':    pc_oi,
                'totalCallVol': call_vol,
                'totalPutVol':  put_vol,
                'totalCallOI':  call_oi,
                'totalPutOI':   put_oi,
                'avgIV':        avg_iv,
                'signal':       signal,       # contrarian signal (your action)
                'marketMood':   market_mood,  # what the crowd is doing
                'expirations':  len(res.get('expirationDates', [])),
            }
        except Exception as e:
            print(f"[sentiment] {ticker} error: {e}")
    return None

def append_sentiment_history(ticker, snap):
    """Store snapshot in rolling history."""
    hist = _sentiment_history.setdefault(ticker, [])
    hist.append({
        'ts':       int(time.time()),
        'pcVol':    snap['pcRatioVolume'],
        'pcOI':     snap['pcRatioOI'],
        'iv':       snap['avgIV'],
        'signal':   snap['signal'],
        'mood':     snap.get('marketMood', 'Neutral'),
    })
    if len(hist) > HISTORY_MAX:
        _sentiment_history[ticker] = hist[-HISTORY_MAX:]

@app.route('/api/sentiment/<ticker>')
def get_sentiment(ticker):
    ticker = ticker.upper().strip()

    # Try live Yahoo data
    snap = fetch_options_data(ticker)
    if snap:
        append_sentiment_history(ticker, snap)
        snap['history'] = _sentiment_history.get(ticker, [])
        return jsonify(snap)

    # Fallback — return history only if we have it, otherwise placeholder
    hist = _sentiment_history.get(ticker, [])
    if hist:
        latest = hist[-1]
        return jsonify({
            'ticker':        ticker,
            'pcRatioVolume': latest['pcVol'],
            'pcRatioOI':     latest['pcOI'],
            'avgIV':         latest['iv'],
            'signal':        latest['signal'],
            'totalCallVol':  0, 'totalPutVol': 0,
            'totalCallOI':   0, 'totalPutOI':  0,
            'history':       hist,
            'note':          'Using cached data',
        })

    # Last resort: try to give a synthetic reading from price momentum
    # so the UI still shows something useful
    live = get_live_price(ticker)
    if live:
        chgp = live.get('changePct', 0)
        # Rough heuristic: falling price = more put buying = higher P/C
        synthetic_pc = round(1.0 - (chgp / 20), 2)  # -5% day → pc≈1.25, +5%→pc≈0.75
        synthetic_pc = max(0.3, min(2.5, synthetic_pc))
        if synthetic_pc >= 1.5:   sig, mood = 'STRONG BUY', 'Extreme Fear'
        elif synthetic_pc >= 1.1: sig, mood = 'BUY', 'Fearful'
        elif synthetic_pc <= 0.5: sig, mood = 'STRONG SELL', 'Extreme Greed'
        elif synthetic_pc <= 0.7: sig, mood = 'SELL', 'Greedy'
        else:                     sig, mood = 'NEUTRAL', 'Neutral'
        snap = {
            'ticker': ticker, 'pcRatioVolume': synthetic_pc, 'pcRatioOI': round(synthetic_pc*0.95,3),
            'totalCallVol': 0, 'totalPutVol': 0, 'totalCallOI': 0, 'totalPutOI': 0,
            'avgIV': 0, 'signal': sig, 'marketMood': mood,
            'note': 'Estimated from price momentum — options data unavailable',
        }
        append_sentiment_history(ticker, snap)
        snap['history'] = _sentiment_history.get(ticker, [])
        return jsonify(snap)

    return jsonify({
        'ticker': ticker, 'pcRatioVolume': 0, 'pcRatioOI': 0,
        'totalCallVol': 0, 'totalPutVol': 0,
        'totalCallOI': 0, 'totalPutOI': 0,
        'avgIV': 0, 'signal': 'NO_DATA', 'history': [],
        'note': 'No options data available for this ticker.',
    })


@app.route('/api/cot/<symbol>')
def get_cot(symbol):
    cot = {
        'GOLD':  {'name':'Gold Futures','commercials':{'long':142000,'short':312000,'net':-170000,'prev_net':-165000},'large_specs':{'long':280000,'short':85000,'net':195000,'prev_net':188000},'small_specs':{'long':45000,'short':70000,'net':-25000,'prev_net':-23000},'signal':'BULLISH','history':[145000,160000,172000,180000,188000,195000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'OIL':   {'name':'Crude Oil Futures','commercials':{'long':390000,'short':590000,'net':-200000,'prev_net':-210000},'large_specs':{'long':310000,'short':145000,'net':165000,'prev_net':155000},'small_specs':{'long':38000,'short':52000,'net':-14000,'prev_net':-12000},'signal':'NEUTRAL','history':[180000,170000,155000,160000,155000,165000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'SPX':   {'name':'S&P 500 Futures','commercials':{'long':320000,'short':480000,'net':-160000,'prev_net':-175000},'large_specs':{'long':520000,'short':285000,'net':235000,'prev_net':210000},'small_specs':{'long':42000,'short':62000,'net':-20000,'prev_net':-18000},'signal':'BULLISH','history':[180000,195000,210000,215000,210000,235000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'NASDAQ':{'name':'Nasdaq 100 Futures','commercials':{'long':85000,'short':145000,'net':-60000,'prev_net':-68000},'large_specs':{'long':165000,'short':82000,'net':83000,'prev_net':75000},'small_specs':{'long':18000,'short':25000,'net':-7000,'prev_net':-6000},'signal':'BULLISH','history':[60000,65000,70000,72000,75000,83000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'EUR':   {'name':'Euro FX Futures','commercials':{'long':210000,'short':160000,'net':50000,'prev_net':42000},'large_specs':{'long':120000,'short':175000,'net':-55000,'prev_net':-48000},'small_specs':{'long':22000,'short':18000,'net':4000,'prev_net':3500},'signal':'BEARISH','history':[-30000,-38000,-42000,-48000,-48000,-55000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'BONDS': {'name':'10Y Treasury Futures','commercials':{'long':680000,'short':420000,'net':260000,'prev_net':240000},'large_specs':{'long':310000,'short':485000,'net':-175000,'prev_net':-162000},'small_specs':{'long':45000,'short':68000,'net':-23000,'prev_net':-20000},'signal':'BULLISH','history':[-140000,-150000,-155000,-162000,-162000,-175000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    }
    sym = symbol.upper()
    if sym in cot: return jsonify(cot[sym])
    return jsonify({'error':f'No COT data for {symbol}. Try: GOLD, OIL, SPX, NASDAQ, EUR, BONDS'}), 404


def fmt(n):
    try:
        n=float(n)
        if n>=1e12: return f"${n/1e12:.2f}T"
        if n>=1e9:  return f"${n/1e9:.1f}B"
        if n>=1e6:  return f"${n/1e6:.0f}M"
        if n>0:     return f"${n:,.0f}"
    except: pass
    return 'N/A'

def sm(val,t,inv=False):
    if not val or (isinstance(val,float) and val!=val): return 50
    t1,t2,t3,t4=t
    if inv:
        if val<=t1:return 90
        if val<=t2:return 75
        if val<=t3:return 55
        if val<=t4:return 35
        return 20
    if val>=t4:return 90
    if val>=t3:return 75
    if val>=t2:return 55
    if val>=t1:return 35
    return 20

def get_sector_type(sector, industry='', mkt_cap=0, rev_g=0):
    s = (sector or '').lower()
    i = (industry or '').lower()
    # Semiconductors first (subset of tech)
    if any(x in i for x in ['semiconductor','chip']) or 'semiconductor' in s:
        return 'semis'
    # Mature/enterprise software (large cap + modest growth) vs high-growth SaaS
    if any(x in s for x in ['software','technology']) or any(x in i for x in ['software','saas','cloud','internet','application']):
        if mkt_cap > 50e9 and rev_g < 15:
            return 'software_mature'  # SAP, MSFT, ORCL style
        return 'software_growth'      # high-growth SaaS
    if any(x in s for x in ['financial','bank','insurance']) or any(x in i for x in ['bank','insurance','asset management']):
        return 'financials'
    if any(x in s for x in ['utilities','real estate']):
        return 'defensive'
    if any(x in s for x in ['energy','material','industrial']):
        return 'cyclical'
    if any(x in s for x in ['health','biotech','pharma']):
        return 'healthcare'
    return 'default'

# Sector thresholds: [poor, fair, good, great]
# sm() maps: >=t1=35, >=t2=55, >=t3=75, >=t4=90  (inv=True reverses)
SECTOR_THRESHOLDS = {
    #                        pe_inv            rev_g           net_m            cr                roe
    'software_mature': { 'pe':[18,30,45,65], 'rev_g':[2,5,9,16],   'net_m':[8,16,26,38],  'cr':[0.5,0.8,1.2,1.8],  'roe':[7,13,22,35]  },
    'software_growth': { 'pe':[30,50,70,100],'rev_g':[10,20,35,55],'net_m':[3,12,22,35],  'cr':[0.6,0.9,1.3,2.0],  'roe':[8,15,25,40]  },
    'semis':           { 'pe':[15,28,50,80], 'rev_g':[5,15,28,45], 'net_m':[10,20,32,48], 'cr':[1.0,1.5,2.5,3.5],  'roe':[10,20,38,55] },
    'financials':      { 'pe':[8,12,18,25],  'rev_g':[2,5,10,18],  'net_m':[15,22,30,40], 'cr':[1.0,1.0,1.0,1.0],  'roe':[8,12,18,25]  },
    'defensive':       { 'pe':[12,18,25,35], 'rev_g':[1,3,6,10],   'net_m':[5,10,16,22],  'cr':[0.8,1.1,1.5,2.0],  'roe':[5,10,16,22]  },
    'cyclical':        { 'pe':[8,14,20,30],  'rev_g':[3,8,15,25],  'net_m':[4,8,14,20],   'cr':[1.0,1.4,2.0,3.0],  'roe':[6,12,20,30]  },
    'healthcare':      { 'pe':[15,25,40,60], 'rev_g':[4,8,15,25],  'net_m':[8,15,25,38],  'cr':[1.2,1.8,2.5,3.5],  'roe':[8,15,25,40]  },
    'default':         { 'pe':[15,25,35,50], 'rev_g':[3,8,15,25],  'net_m':[5,10,20,35],  'cr':[0.8,1.2,1.8,2.5],  'roe':[5,12,25,40]  },
}

def calc_score(pe, rev_g, net_m, cr, roe, chgp, sector='', industry='', mkt_cap=0, div_yield=0):
    st = get_sector_type(sector, industry, mkt_cap, rev_g)
    t  = SECTOR_THRESHOLDS[st]
    b  = {
        'valuation':    sm(pe,    t['pe'],    inv=True),
        'growth':       sm(rev_g, t['rev_g']),
        'profitability':sm(net_m, t['net_m']),
        'balance':      sm(cr,    t['cr'])    if st != 'financials' else 68,
        'momentum':     sm(chgp,  [-10,-2,2,10]),
        'quality':      sm(roe,   t['roe']),
        'macro': 68,
    }
    total = round(sum(b.values()) / len(b))

    # Sector-aware verdict thresholds — growth sectors need higher bars for BUY
    verdict_thresholds = {
        'software_growth': (72, 55),   # BUY>=72, HOLD>=55
        'semis':           (72, 55),
        'software_mature': (65, 52),   # mature cos — steady = HOLD is fine
        'financials':      (65, 52),
        'healthcare':      (68, 54),
        'cyclical':        (63, 50),
        'defensive':       (62, 50),
        'default':         (68, 54),
    }
    buy_t, hold_t = verdict_thresholds.get(st, (68, 54))
    verdict = 'BUY' if total >= buy_t else 'HOLD' if total >= hold_t else 'AVOID'

    grade = ('A+' if total>=90 else 'A' if total>=82 else 'A-' if total>=75 else
             'B+' if total>=68 else 'B' if total>=60 else 'B-' if total>=52 else 'C')
    # Style — based on raw fundamentals, not scores
    if rev_g >= 20 and net_m >= 10:
        style = 'High Growth'
    elif net_m >= 15 and roe >= 12 and cr >= 0.8:
        style = 'Quality Compounder'
    elif pe > 0 and pe < 18 and div_yield >= 2.0:
        style = 'Dividend Value'
    elif pe > 0 and pe < 18:
        style = 'Value'
    elif div_yield >= 2.5 and rev_g < 10:
        style = 'Dividend Income'
    elif st == 'financials':
        style = 'Financial'
    elif rev_g >= 10 and net_m >= 8:
        style = 'Growth'
    elif total < 55:
        style = 'Speculative'
    else:
        style = 'Blend'
    return {'total':total,'grade':grade,'verdict':verdict,'style':style,'breakdown':b,'sectorType':st}


# ══════════════════════════════════════════════════════════════════
# ◈ OPPORTUNITY SCANNER — Background scan across asset universe
# ══════════════════════════════════════════════════════════════════
import threading

# Full universe: indices, sectors, bonds, metals, crypto-adjacent ETFs
SCAN_UNIVERSE = [
    # Broad indices
    {'t':'SPY',  'n':'S&P 500',          'cat':'Index'},
    {'t':'QQQ',  'n':'Nasdaq 100',        'cat':'Index'},
    {'t':'DIA',  'n':'Dow Jones',         'cat':'Index'},
    {'t':'IWM',  'n':'Russell 2000',      'cat':'Index'},
    {'t':'VT',   'n':'World Stocks',      'cat':'Index'},
    # Bonds
    {'t':'TLT',  'n':'20Y Treasury',      'cat':'Bonds'},
    {'t':'HYG',  'n':'High Yield Corp',   'cat':'Bonds'},
    {'t':'LQD',  'n':'Investment Grade',  'cat':'Bonds'},
    {'t':'TIP',  'n':'TIPS Inflation',    'cat':'Bonds'},
    # Precious metals
    {'t':'GLD',  'n':'Gold',              'cat':'Metals'},
    {'t':'SLV',  'n':'Silver',            'cat':'Metals'},
    {'t':'GDX',  'n':'Gold Miners',       'cat':'Metals'},
    {'t':'GDXJ', 'n':'Jr Gold Miners',    'cat':'Metals'},
    {'t':'PPLT', 'n':'Platinum',          'cat':'Metals'},
    # Tech / AI
    {'t':'NVDA', 'n':'NVIDIA',            'cat':'Tech'},
    {'t':'AAPL', 'n':'Apple',             'cat':'Tech'},
    {'t':'MSFT', 'n':'Microsoft',         'cat':'Tech'},
    {'t':'GOOGL','n':'Alphabet',          'cat':'Tech'},
    {'t':'META', 'n':'Meta',              'cat':'Tech'},
    {'t':'AMD',  'n':'AMD',               'cat':'Tech'},
    {'t':'TSM',  'n':'TSMC',              'cat':'Tech'},
    {'t':'ASML', 'n':'ASML',             'cat':'Tech'},
    {'t':'CRM',  'n':'Salesforce',        'cat':'Tech'},
    {'t':'NOW',  'n':'ServiceNow',        'cat':'Tech'},
    # Financials
    {'t':'JPM',  'n':'JPMorgan',          'cat':'Financials'},
    {'t':'GS',   'n':'Goldman Sachs',     'cat':'Financials'},
    {'t':'BAC',  'n':'Bank of America',   'cat':'Financials'},
    {'t':'BLK',  'n':'BlackRock',         'cat':'Financials'},
    {'t':'V',    'n':'Visa',              'cat':'Financials'},
    # Energy
    {'t':'XOM',  'n':'ExxonMobil',        'cat':'Energy'},
    {'t':'CVX',  'n':'Chevron',           'cat':'Energy'},
    {'t':'XLE',  'n':'Energy ETF',        'cat':'Energy'},
    {'t':'OXY',  'n':'Occidental',        'cat':'Energy'},
    # Healthcare
    {'t':'JNJ',  'n':'Johnson & Johnson', 'cat':'Healthcare'},
    {'t':'UNH',  'n':'UnitedHealth',      'cat':'Healthcare'},
    {'t':'LLY',  'n':'Eli Lilly',         'cat':'Healthcare'},
    {'t':'ABBV', 'n':'AbbVie',            'cat':'Healthcare'},
    # Consumer
    {'t':'AMZN', 'n':'Amazon',            'cat':'Consumer'},
    {'t':'TSLA', 'n':'Tesla',             'cat':'Consumer'},
    {'t':'WMT',  'n':'Walmart',           'cat':'Consumer'},
    {'t':'COST', 'n':'Costco',            'cat':'Consumer'},
    # Industrials / Defence
    {'t':'CAT',  'n':'Caterpillar',       'cat':'Industrials'},
    {'t':'LMT',  'n':'Lockheed Martin',   'cat':'Industrials'},
    {'t':'RTX',  'n':'RTX Corp',          'cat':'Industrials'},
    {'t':'DE',   'n':'John Deere',        'cat':'Industrials'},
    # Sector ETFs
    {'t':'XLF',  'n':'Financials ETF',    'cat':'Sector ETF'},
    {'t':'XLK',  'n':'Tech ETF',          'cat':'Sector ETF'},
    {'t':'XLV',  'n':'Health ETF',        'cat':'Sector ETF'},
    {'t':'XLI',  'n':'Industrials ETF',   'cat':'Sector ETF'},
    {'t':'XLP',  'n':'Staples ETF',       'cat':'Sector ETF'},
    {'t':'XLU',  'n':'Utilities ETF',     'cat':'Sector ETF'},
    {'t':'XLRE', 'n':'Real Estate ETF',   'cat':'Sector ETF'},
    {'t':'XLB',  'n':'Materials ETF',     'cat':'Sector ETF'},
    # Commodities
    {'t':'USO',  'n':'Oil ETF',           'cat':'Commodities'},
    {'t':'CORN', 'n':'Corn ETF',          'cat':'Commodities'},
    {'t':'WEAT', 'n':'Wheat ETF',         'cat':'Commodities'},
    # Currency ETFs / country indices
    {'t':'UUP',  'n':'USD Bullish ETF',   'cat':'Forex'},
    {'t':'FXE',  'n':'Euro ETF',          'cat':'Forex'},
    {'t':'FXB',  'n':'GBP ETF',           'cat':'Forex'},
    {'t':'FXY',  'n':'JPY ETF',           'cat':'Forex'},
    {'t':'FXA',  'n':'AUD ETF',           'cat':'Forex'},
    {'t':'FXC',  'n':'CAD ETF',           'cat':'Forex'},
    {'t':'FXF',  'n':'CHF ETF',           'cat':'Forex'},
    {'t':'EWJ',  'n':'Japan Index',       'cat':'Intl Index'},
    {'t':'EWU',  'n':'UK Index',          'cat':'Intl Index'},
    {'t':'EZU',  'n':'Eurozone Index',    'cat':'Intl Index'},
    {'t':'MCHI', 'n':'China Index',       'cat':'Intl Index'},
    {'t':'EWG',  'n':'Germany Index',     'cat':'Intl Index'},
    {'t':'EWA',  'n':'Australia Index',   'cat':'Intl Index'},
    {'t':'EWC',  'n':'Canada Index',      'cat':'Intl Index'},
    {'t':'EEM',  'n':'Emerging Markets',  'cat':'Intl Index'},
]

# Scanner state
_scan_results   = {}   # ticker → opportunity data
_scan_status    = {'running': False, 'progress': 0, 'total': len(SCAN_UNIVERSE), 'last_run': 0, 'current': ''}
_scan_thread    = None

# Macro context for sector alignment scoring
MACRO_TAILWINDS = {
    # Based on current macro: falling rates + AI boom + commodities mixed
    'Tech':        85,
    'Index':       70,
    'Metals':      75,   # dollar weakness = gold positive
    'Bonds':       65,   # rate cut expectation = bonds positive
    'Healthcare':  68,
    'Financials':  60,
    'Energy':      55,
    'Consumer':    62,
    'Industrials': 65,
    'Sector ETF':  60,
    'Commodities': 58,
    'Forex':       65,
    'Intl Index':  62,
}

def opp_score(stock_data, cat):
    """Compute a composite opportunity score 0-100 across 4 dimensions."""
    s = stock_data

    # 1. Fundamental score (already computed) — 0-100
    fund = s.get('score', 50)

    # 2. Value opportunity — how far below fair value?
    price    = s.get('price', 0)
    fv       = s.get('fairValue', price or 1)
    discount = ((fv - price) / fv * 100) if fv > 0 and price > 0 else 0
    if   discount >= 30: val_score = 95
    elif discount >= 20: val_score = 85
    elif discount >= 10: val_score = 75
    elif discount >= 0:  val_score = 60
    elif discount >= -10:val_score = 45
    else:                val_score = 30

    # 3. Macro alignment
    macro = MACRO_TAILWINDS.get(cat, 60)

    # 4. Momentum — 52w position (low in range = opportunity)
    w52hi = s.get('week52High', 0)
    w52lo = s.get('week52Low',  0)
    if w52hi > w52lo > 0:
        pos = (price - w52lo) / (w52hi - w52lo) * 100
        # Sweet spot: 20-50% of range = recovering but not extended
        if   pos <= 20:  mom_score = 85   # near 52w low — oversold
        elif pos <= 40:  mom_score = 78
        elif pos <= 60:  mom_score = 65
        elif pos <= 80:  mom_score = 50
        else:            mom_score = 35   # near 52w high — extended
    else:
        mom_score = 55

    # Composite — weighted
    composite = round(
        fund      * 0.35 +
        val_score * 0.25 +
        macro     * 0.20 +
        mom_score * 0.20
    )

    # Signal flags
    flags = []
    if discount >= 15:               flags.append('Undervalued')
    if fund >= 75:                   flags.append('Strong Fundamentals')
    if macro >= 75:                  flags.append('Macro Tailwind')
    if mom_score >= 78:              flags.append('Oversold / Recovering')
    if s.get('changePct', 0) > 2:   flags.append('Momentum')
    if s.get('divYield', 0) > 2.5:  flags.append('Income')

    # Opportunity tier
    if   composite >= 80: tier = 'STRONG'
    elif composite >= 68: tier = 'WATCH'
    elif composite >= 55: tier = 'NEUTRAL'
    else:                 tier = 'AVOID'

    return {
        'composite':  composite,
        'tier':       tier,
        'fundamental':fund,
        'value':      val_score,
        'macro':      macro,
        'momentum':   mom_score,
        'discount':   round(discount, 1),
        'flags':      flags,
        'w52pos':     round(pos if w52hi > w52lo > 0 else 50, 1),
    }

def scan_one(item):
    """Fetch full data for one ticker and store opportunity score."""
    ticker = item['t']
    cat    = item['cat']
    _scan_status['current'] = ticker

    # Use cache if fresh (< 4 hours)
    cached = cache_get(f'stock:{ticker}')
    if cached:
        opp = opp_score(cached, cat)
        _scan_results[ticker] = {**cached, **opp, 'cat': cat, 'displayName': item['n'], 'scanned': int(time.time())}
        return

    try:
        # AV Call 1: Overview
        overview = av({'function': 'OVERVIEW', 'symbol': ticker})
        if 'Information' in overview or 'Note' in overview or 'Symbol' not in overview:
            # Fall back to Yahoo-only (ETFs, metals don't have AV fundamentals)
            live = get_live_price(ticker)
            if live:
                stub = {
                    'ticker': ticker, 'name': item['n'], 'price': live['price'],
                    'change': live['change'], 'changePct': live['changePct'],
                    'week52High': live.get('week52High', 0), 'week52Low': live.get('week52Low', 0),
                    'score': 55, 'fairValue': live['price'], 'divYield': 0,
                    'peRatio': 0, 'revenueGrowth': 0, 'netMargin': 0,
                }
                opp = opp_score(stub, cat)
                _scan_results[ticker] = {**stub, **opp, 'cat': cat, 'displayName': item['n'], 'scanned': int(time.time())}
            return

        time.sleep(13)
        inc_data = av({'function': 'INCOME_STATEMENT', 'symbol': ticker})
        time.sleep(13)
        bal_data = av({'function': 'BALANCE_SHEET', 'symbol': ticker})
        live = get_live_price(ticker)

        # Parse — reuse same logic as main stock endpoint
        pe      = safe_float(overview.get('PERatio'))
        fwd_pe  = safe_float(overview.get('ForwardPE'))
        peg     = safe_float(overview.get('PEGRatio'))
        pb      = safe_float(overview.get('PriceToBookRatio'))
        eps     = safe_float(overview.get('EPS'))
        beta    = safe_float(overview.get('Beta')) or 1
        div     = safe_float(overview.get('DividendPerShare'))
        raw_dy  = safe_float(overview.get('DividendYield'))
        div_y   = round(raw_dy * 100, 2) if raw_dy < 1 else round(raw_dy, 2)
        w52hi   = safe_float(overview.get('52WeekHigh'))
        w52lo   = safe_float(overview.get('52WeekLow'))
        tgt     = safe_float(overview.get('AnalystTargetPrice'))
        net_m   = safe_float(overview.get('ProfitMargin'), mult=100)
        op_m    = safe_float(overview.get('OperatingMarginTTM'), mult=100)
        roe     = safe_float(overview.get('ReturnOnEquityTTM'), mult=100)
        roa     = safe_float(overview.get('ReturnOnAssetsTTM'), mult=100)
        mkt_cap = safe_float(overview.get('MarketCapitalization'))

        annual  = inc_data.get('annualReports', [])[:5]
        revenue = earnings = labels = []
        rev_g   = earn_g = gross_m = 0
        cr = de = 0

        try:
            bal_annual = bal_data.get('annualReports', [{}])
            if bal_annual:
                b = bal_annual[0]
                def bsf(v):
                    try: return float(v) if v and str(v) != 'None' else 0.0
                    except: return 0.0
                curr_assets = bsf(b.get('totalCurrentAssets') or b.get('currentAssets'))
                curr_liab   = bsf(b.get('totalCurrentLiabilities') or b.get('currentLiabilities') or b.get('totalLiabilities'))
                tot_equity  = bsf(b.get('totalShareholderEquity') or b.get('stockholdersEquity') or b.get('totalStockholdersEquity'))
                st_debt     = bsf(b.get('shortTermDebt') or b.get('currentPortionOfLongTermDebt'))
                lt_debt     = bsf(b.get('longTermDebtNoncurrent') or b.get('longTermDebt') or b.get('longTermDebtAndCapitalLeaseObligation'))
                tot_debt    = st_debt + lt_debt
                if curr_liab > 0: cr = round(curr_assets / curr_liab, 2)
                if tot_equity > 0: de = round(tot_debt / tot_equity, 2) if tot_debt > 0 else 0
        except: pass

        if annual:
            rev_list = [round(float(r.get('totalRevenue',0) or 0)/1e9,1) for r in reversed(annual)]
            revenue  = rev_list
            labels   = [r.get('fiscalDateEnding','')[:4] for r in reversed(annual)]
            earnings = [round(float(r.get('netIncome',0) or 0)/1e9,2) for r in reversed(annual)]
            latest   = annual[0]
            tot_rev  = float(latest.get('totalRevenue',0) or 0)
            gross_p  = float(latest.get('grossProfit',0) or 0)
            if tot_rev > 0: gross_m = round(gross_p/tot_rev*100,1)
            if len(rev_list)>=2 and rev_list[-2]:
                rev_g = round((rev_list[-1]-rev_list[-2])/abs(rev_list[-2])*100,1)

        price = change = change_pct = 0
        if live and live['price'] > 0:
            price = live['price']
            change = live['change']
            change_pct = live['changePct']
            if live.get('week52High'): w52hi = live['week52High']
            if live.get('week52Low'):  w52lo = live['week52Low']

        fv = 0
        if eps > 0 and rev_g > 20:
            fv = round(eps * min(rev_g, 60), 2)
        elif eps > 0:
            fv = round(eps * 22, 2)
        else:
            fv = round(price * 0.92, 2) if price else 0
        if tgt > 0: fv = round((fv + tgt) / 2, 2)
        elif not tgt: tgt = fv

        sc = calc_score(pe, rev_g, net_m, cr, roe, change_pct,
                        overview.get('Sector',''), overview.get('Industry',''), mkt_cap, div_y)

        stock_data = {
            'ticker': ticker, 'name': overview.get('Name', ticker),
            'sector': overview.get('Sector','N/A'), 'industry': overview.get('Industry','N/A'),
            'price': round(price,2), 'change': change, 'changePct': change_pct,
            'week52High': round(w52hi,2), 'week52Low': round(w52lo,2),
            'peRatio': round(pe,1), 'fwdPE': round(fwd_pe,1), 'eps': round(eps,2),
            'grossMargin': round(gross_m,1), 'netMargin': round(net_m,1),
            'roe': round(roe,1), 'revenueGrowth': round(rev_g,1),
            'debtEquity': round(de,2), 'currentRatio': round(cr,2),
            'fairValue': round(fv,2), 'analystTarget': round(tgt,2),
            'divYield': round(div_y,2), 'mktCap': fmt(mkt_cap),
            'score': sc['total'], 'grade': sc['grade'], 'verdict': sc['verdict'],
            'style': sc['style'], 'revenue': revenue, 'earnings': earnings, 'revenueLabels': labels,
        }
        cache_set(f'stock:{ticker}', stock_data)
        opp = opp_score(stock_data, cat)
        _scan_results[ticker] = {**stock_data, **opp, 'cat': cat, 'displayName': item['n'], 'scanned': int(time.time())}
        print(f"[scanner] {ticker} ✓ composite={opp['composite']} tier={opp['tier']}")
    except Exception as e:
        print(f"[scanner] {ticker} error: {e}")

def run_scanner():
    """Background thread — scans universe continuously with error isolation.""";
    _scan_status['running'] = True
    while True:
        try:
            print("[scanner] Starting full universe scan...")
            _scan_status['progress'] = 0
            for i, item in enumerate(SCAN_UNIVERSE):
                try:
                    _scan_status['progress'] = i + 1
                    scan_one(item)
                    time.sleep(2)
                except Exception as e:
                    print(f"[scanner] item error {item.get('t')}: {e}")
                    continue
            _scan_status['last_run'] = int(time.time())
            print(f"[scanner] Scan complete — {len(_scan_results)} results")
        except Exception as e:
            print(f"[scanner] Thread error: {e}")
        time.sleep(1800)

def start_scanner():
    global _scan_thread
    if _scan_thread and _scan_thread.is_alive():
        return
    # Only start on worker with PID closest to master (avoid duplicate threads across gunicorn workers)
    import os
    if os.environ.get('SCANNER_STARTED'):
        return
    os.environ['SCANNER_STARTED'] = '1'
    _scan_thread = threading.Thread(target=run_scanner, daemon=True)
    _scan_thread.start()
    print("[scanner] Background scanner started")

# Start scanner when app loads
start_scanner()


@app.route('/api/scanner')
def get_scanner():
    cat_filter = request.args.get('cat', '')
    tier_filter = request.args.get('tier', '')
    results = list(_scan_results.values())
    if cat_filter:
        results = [r for r in results if r.get('cat') == cat_filter]
    if tier_filter:
        results = [r for r in results if r.get('tier') == tier_filter]
    results.sort(key=lambda r: r.get('composite', 0), reverse=True)
    return ok({
        'results':  results,
        'status':   _scan_status,
        'scanned':  len(_scan_results),
        'total':    len(SCAN_UNIVERSE),
    })

@app.route('/api/scanner/status')
def get_scanner_status():
    return jsonify(_scan_status)


# ══════════════════════════════════════════════════════════════════
# ◈ GLOBAL MACRO — Historical economic indicator data
# FRED for US, World Bank for international
# ══════════════════════════════════════════════════════════════════

# FRED API key — get a free one at https://fred.stlouisfed.org/docs/api/api_key.html
# Set as Railway environment variable: FRED_API_KEY
FRED_KEY  = os.environ.get('FRED_API_KEY', '')
FRED_BASE = 'https://api.stlouisfed.org/fred/series/observations'

# World Bank API — no key needed
WB_BASE = 'https://api.worldbank.org/v2/country/{country}/indicator/{indicator}'

# FRED series IDs for US indicators
FRED_SERIES = {
    'cpi':          'CPIAUCSL',       # CPI All Urban
    'core_cpi':     'CPILFESL',       # Core CPI (ex food/energy)
    'ppi':          'PPIACO',         # PPI All Commodities
    'nfp':          'PAYEMS',         # Non-Farm Payrolls
    'unemployment': 'UNRATE',         # Unemployment Rate
    'gdp':          'GDP',            # GDP (quarterly)
    'gdp_growth':   'A191RL1Q225SBEA',# Real GDP Growth Rate
    'fed_rate':     'FEDFUNDS',       # Fed Funds Rate
    'yield_10y':    'GS10',           # 10Y Treasury
    'yield_2y':     'GS2',            # 2Y Treasury
    'retail_sales': 'RSAFS',          # Retail Sales
    'ism_mfg':      'MANEMP',         # Manufacturing Employment proxy
    'housing':      'HOUST',          # Housing Starts
    'consumer_sent':'UMCSENT',        # U of Michigan Consumer Sentiment
}

# World Bank indicator codes per economy
WB_INDICATORS = {
    'gdp_growth':   'NY.GDP.MKTP.KD.ZG',   # GDP growth %
    'inflation':    'FP.CPI.TOTL.ZG',       # CPI inflation %
    'unemployment': 'SL.UEM.TOTL.ZS',       # Unemployment %
    'current_acct': 'BN.CAB.XOKA.GD.ZS',   # Current account % GDP
    'debt_gdp':     'GC.DOD.TOTL.GD.ZS',   # Government debt % GDP
}

WB_COUNTRIES = {
    'US':       'US',   # World Bank also has US data as fallback
    'UK':       'GB',
    'Eurozone': 'XC',   # Euro area aggregate
    'China':    'CN',
    'Japan':    'JP',
    'Germany':  'DE',
}

# World Bank equivalents for US indicators (fallback when no FRED key)
FRED_TO_WB = {
    'cpi':          ('US', 'FP.CPI.TOTL.ZG'),   # CPI inflation %
    'core_cpi':     ('US', 'FP.CPI.TOTL.ZG'),   # approximate
    'unemployment': ('US', 'SL.UEM.TOTL.ZS'),
    'gdp_growth':   ('US', 'NY.GDP.MKTP.KD.ZG'),
    'fed_rate':     None,   # no WB equivalent
    'yield_10y':    None,
    'yield_2y':     None,
    'ppi':          None,
    'nfp':          None,
    'retail_sales': None,
    'consumer_sent':None,
    'housing':      None,
}

# Macro cache now uses unified cache with TTL['fred'] / TTL['wb']

def get_fred_series(series_id, years=2):
    """Fetch a FRED time series for the last N years."""
    if not FRED_KEY:
        print(f'[macro] FRED_API_KEY not set — skipping FRED fetch for {series_id}')
        return None
    cache_key = f'fred:{series_id}:{years}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=365*years)).isoformat()
        params = {
            'series_id':         series_id,
            'observation_start': start,
            'file_type':         'json',
            'sort_order':        'asc',
            'api_key':           FRED_KEY,
        }
        r = requests.get(FRED_BASE, params=params, timeout=15)
        print(f'[macro] FRED {series_id}: {r.status_code}')
        if r.status_code != 200:
            return None
        obs = r.json().get('observations', [])
        data = [
            {'date': o['date'], 'value': float(o['value'])}
            for o in obs if o.get('value') not in (None, '.', '')
        ]
        cache.set(cache_key, data, TTL['fred'])
        print(f'[macro] FRED {series_id}: {len(data)} points')
        return data
    except Exception as e:
        print(f'[macro] FRED {series_id} error: {e}')
        return None

def get_wb_series(country_code, indicator, years=2):
    """Fetch a World Bank indicator series."""
    cache_key = f'wb:{country_code}:{indicator}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import datetime
        start_year = datetime.date.today().year - years - 1
        url = WB_BASE.format(country=country_code, indicator=indicator)
        r = requests.get(url, params={
            'format': 'json',
            'date':   f'{start_year}:{datetime.date.today().year}',
            'per_page': 100,
            'mrv': 10,
        }, timeout=15)
        if r.status_code != 200:
            return None
        result = r.json()
        if not isinstance(result, list) or len(result) < 2:
            return None
        records = result[1] or []
        data = sorted([
            {'date': str(rec['date']), 'value': rec['value']}
            for rec in records if rec.get('value') is not None
        ], key=lambda x: x['date'])
        cache.set(cache_key, data, TTL['wb'])
        return data
    except Exception as e:
        print(f'[macro] WorldBank {country_code}/{indicator} error: {e}')
        return None


@app.route('/api/macro/us')
def get_macro_us():
    """US economic indicators — FRED primary, World Bank fallback."""
    indicators = request.args.get('indicators', 'cpi,core_cpi,ppi,unemployment,nfp,gdp_growth,fed_rate,yield_10y,yield_2y,retail_sales,consumer_sent,housing').split(',')
    years = int(request.args.get('years', 2))
    result = {}
    has_fred = bool(FRED_KEY)
    for ind in indicators:
        data = None
        source = None
        # Try FRED first
        series_id = FRED_SERIES.get(ind)
        if series_id and has_fred:
            data = get_fred_series(series_id, years)
            if data: source = 'FRED'
        # Fall back to World Bank
        if not data:
            wb_map = FRED_TO_WB.get(ind)
            if wb_map:
                country_code, wb_indicator = wb_map
                data = get_wb_series(country_code, wb_indicator, years)
                if data: source = 'World Bank'
        if data:
            # For index-based series (CPI, PPI), convert to YoY % change
            INDEX_SERIES = {'cpi', 'core_cpi', 'ppi', 'retail_sales', 'nfp', 'housing'}
            if ind in INDEX_SERIES and source == 'FRED' and len(data) > 12:
                # Calculate YoY % change: (current - 12 months ago) / 12 months ago * 100
                yoy_data = []
                for i in range(12, len(data)):
                    curr = data[i]['value']
                    prev_yr = data[i-12]['value']
                    if prev_yr and prev_yr != 0:
                        yoy = round((curr - prev_yr) / prev_yr * 100, 2)
                        yoy_data.append({'date': data[i]['date'], 'value': yoy})
                if yoy_data:
                    data = yoy_data
            result[ind] = {
                'series_id': series_id or ind,
                'source':    source,
                'data':      data,
                'latest':    data[-1] if data else None,
                'prev':      data[-2] if len(data) > 1 else None,
            }
    return jsonify({'country': 'US', 'indicators': result, 'has_fred': has_fred})



# Current macro snapshots -- updated May 2026
CURRENT_MACRO_SNAPSHOT = {
    'US':       {'gdp_growth': 2.0,  'inflation': 4.0,  'unemployment': 4.3,  'rate': 4.33},
    'UK':       {'gdp_growth': 1.1,  'inflation': 3.3,  'unemployment': 5.1,  'rate': 3.75},
    'Eurozone': {'gdp_growth': 1.1,  'inflation': 2.6,  'unemployment': 6.2,  'rate': 2.00},
    'China':    {'gdp_growth': 4.5,  'inflation': 0.4,  'unemployment': 5.1,  'rate': 3.10},
    'Japan':    {'gdp_growth': 0.8,  'inflation': 2.3,  'unemployment': 2.5,  'rate': 0.50},
    'Germany':  {'gdp_growth': 1.4,  'inflation': 2.9,  'unemployment': 4.0,  'rate': 2.00},
}

@app.route('/api/macro/international')
def get_macro_international():
    """International macro data -- curated snapshot injected as latest."""
    countries = request.args.get('countries', 'UK,Eurozone,China,Japan,Germany').split(',')
    indicators = request.args.get('indicators', 'gdp_growth,inflation,unemployment').split(',')
    years = int(request.args.get('years', 2))
    result = {}
    for country in countries:
        result[country] = {}
        snap = CURRENT_MACRO_SNAPSHOT.get(country, {})
        for ind in indicators:
            code    = WB_COUNTRIES.get(country)
            wb_code = WB_INDICATORS.get(ind)
            data    = []
            if code and wb_code:
                wb_data = get_wb_series(code, wb_code, years + 2)
                if wb_data:
                    data = list(wb_data)
            latest_val = snap.get(ind)
            if latest_val is not None:
                current_point = {'date': '2026', 'value': latest_val}
                if data and data[-1]['date'] != '2026':
                    data = data + [current_point]
                elif not data:
                    data = [current_point]
            if data:
                result[country][ind] = {
                    'data':   data,
                    'latest': {'date': data[-1]['date'], 'value': latest_val if latest_val is not None else data[-1]['value']},
                    'prev':   data[-2] if len(data) > 1 else None,
                    'source': 'Curated May 2026 + World Bank historical',
                }
    return jsonify({'international': result, 'countries': countries})


# ══════════════════════════════════════════════════════════════════
# ◈ ECONOMIC HEAT — Composite health score per economy
# ══════════════════════════════════════════════════════════════════

def score_pillar(value, thresholds, invert=False):
    """Score a single indicator 0-100. thresholds = [poor, weak, ok, good, great]"""
    if value is None: return 50  # neutral if no data
    p, w, o, g, gr = thresholds
    if invert:  # lower = better (unemployment, inflation)
        if value <= gr: return 95
        if value <= g:  return 80
        if value <= o:  return 60
        if value <= w:  return 40
        if value <= p:  return 20
        return 10
    else:       # higher = better (GDP growth)
        if value >= gr: return 95
        if value >= g:  return 80
        if value >= o:  return 60
        if value >= w:  return 40
        if value >= p:  return 20
        return 10

def calc_econ_health(gdp, inflation, unemployment, prev_gdp=None, prev_inflation=None, prev_unemployment=None):
    """Compute composite economic health score and breakdown."""

    # Score each pillar
    gdp_score  = score_pillar(gdp,         [-2, 0, 1.5, 3, 5])
    inf_score  = score_pillar(inflation,    [8, 5, 3.5, 2.5, 1.5], invert=True)
    une_score  = score_pillar(unemployment, [10, 7, 5.5, 4, 3],    invert=True)

    # Momentum: direction of change adds/subtracts points
    momentum = 0
    signals  = []
    if prev_gdp is not None and gdp is not None:
        delta = gdp - prev_gdp
        if   delta >  1.0: momentum += 12; signals.append('GDP accelerating')
        elif delta >  0.3: momentum += 6;  signals.append('GDP improving')
        elif delta < -1.0: momentum -= 12; signals.append('GDP slowing sharply')
        elif delta < -0.3: momentum -= 6;  signals.append('GDP softening')
    if prev_inflation is not None and inflation is not None:
        delta = inflation - prev_inflation
        if   delta < -0.5: momentum += 8;  signals.append('Inflation falling')
        elif delta < -0.1: momentum += 4;  signals.append('Inflation easing')
        elif delta >  0.5: momentum -= 8;  signals.append('Inflation rising')
        elif delta >  0.1: momentum -= 4;  signals.append('Inflation ticking up')
    if prev_unemployment is not None and unemployment is not None:
        delta = unemployment - prev_unemployment
        if   delta < -0.3: momentum += 6;  signals.append('Jobs improving')
        elif delta >  0.3: momentum -= 6;  signals.append('Jobs deteriorating')

    # Composite weighted score
    composite = round(
        gdp_score  * 0.35 +
        inf_score  * 0.30 +
        une_score  * 0.25 +
        max(-20, min(20, momentum)) * 0.10 * 5  # normalise momentum
    )
    composite = max(0, min(100, composite))

    # Heat tier
    if   composite >= 75: tier, heat = 'HOT',      '#48d597'
    elif composite >= 60: tier, heat = 'WARM',     '#7de8b8'
    elif composite >= 45: tier, heat = 'NEUTRAL',  '#f6c90e'
    elif composite >= 30: tier, heat = 'COOL',     '#f8a0a0'
    else:                 tier, heat = 'COLD',     '#f56565'

    # One-line narrative
    if not signals:
        narrative = 'Stable — no significant momentum shifts'
    elif composite >= 70:
        narrative = ' · '.join(signals[:2]) + ' — economy running well'
    elif composite >= 50:
        narrative = ' · '.join(signals[:2]) + ' — mixed picture'
    else:
        narrative = ' · '.join(signals[:2]) + ' — headwinds building'

    return {
        'composite':    composite,
        'tier':         tier,
        'heat':         heat,
        'pillars': {
            'gdp':          {'score': gdp_score,  'value': gdp,          'label': 'GDP Growth'},
            'inflation':    {'score': inf_score,  'value': inflation,    'label': 'Inflation'},
            'unemployment': {'score': une_score,  'value': unemployment, 'label': 'Unemployment'},
            'momentum':     {'score': max(0, min(100, 50 + momentum*2)), 'value': round(momentum,1), 'label': 'Momentum'},
        },
        'narrative': narrative,
        'signals':   signals,
    }


@app.route('/api/economic-heat')
def get_economic_heat():
    """Composite economic health scores for all 6 economies."""
    result = {}

    # ── US — pull from FRED (or WB fallback) ──────────────────
    us_inds = {}
    for ind, wb_map in [
        ('gdp_growth',   ('US', 'NY.GDP.MKTP.KD.ZG')),
        ('inflation',    ('US', 'FP.CPI.TOTL.ZG')),
        ('unemployment', ('US', 'SL.UEM.TOTL.ZS')),
    ]:
        # Try FRED first
        fred_id = FRED_SERIES.get('gdp_growth' if ind == 'gdp_growth'
                                  else 'unemployment' if ind == 'unemployment'
                                  else 'cpi')
        data = None
        if FRED_KEY and fred_id:
            raw = get_fred_series(fred_id, years=3)
            if raw and len(raw) > 12 and ind in ('gdp_growth',):
                data = raw  # GDP growth already a % from FRED
            elif raw and len(raw) > 12 and ind in ('unemployment',):
                data = raw
            elif raw and len(raw) > 13 and ind == 'inflation':
                # Convert CPI index to YoY %
                yoy = []
                for i in range(12, len(raw)):
                    curr = raw[i]['value']; prev = raw[i-12]['value']
                    if prev: yoy.append({'date': raw[i]['date'], 'value': round((curr-prev)/prev*100, 2)})
                data = yoy if yoy else None
        if not data:
            data = get_wb_series(wb_map[0], wb_map[1], years=4)
        if data:
            us_inds[ind] = data

    us_gdp   = us_inds.get('gdp_growth', [])
    us_inf   = us_inds.get('inflation',  [])
    us_une   = us_inds.get('unemployment', [])
    result['US'] = {
        'name': 'United States', 'flag': '🇺🇸',
        **calc_econ_health(
            gdp          = us_gdp[-1]['value']  if us_gdp  else None,
            inflation    = us_inf[-1]['value']  if us_inf  else None,
            unemployment = us_une[-1]['value']  if us_une  else None,
            prev_gdp          = us_gdp[-2]['value']  if len(us_gdp)  > 1 else None,
            prev_inflation    = us_inf[-2]['value']  if len(us_inf)  > 1 else None,
            prev_unemployment = us_une[-2]['value']  if len(us_une)  > 1 else None,
        ),
        'latest': {
            'gdp':          round(us_gdp[-1]['value'], 2)  if us_gdp  else None,
            'gdp_date':     us_gdp[-1]['date']             if us_gdp  else None,
            'inflation':    round(us_inf[-1]['value'], 2)  if us_inf  else None,
            'unemployment': round(us_une[-1]['value'], 2)  if us_une  else None,
        }
    }

    # ── International — World Bank ─────────────────────────────
    intl_map = {
        'UK':       ('GB', '🇬🇧', 'United Kingdom'),
        'Eurozone': ('XC', '🇪🇺', 'Eurozone'),
        'China':    ('CN', '🇨🇳', 'China'),
        'Japan':    ('JP', '🇯🇵', 'Japan'),
        'Germany':  ('DE', '🇩🇪', 'Germany'),
    }
    for key, (code, flag, name) in intl_map.items():
        # Use curated snapshot as primary -- WB data is lagged 1-2 years
        snap = CURRENT_MACRO_SNAPSHOT.get(key, {})
        # Try WB for prev-year comparison only
        gdp_d = get_wb_series(code, 'NY.GDP.MKTP.KD.ZG', years=3) or []
        inf_d = get_wb_series(code, 'FP.CPI.TOTL.ZG',    years=3) or []
        une_d = get_wb_series(code, 'SL.UEM.TOTL.ZS',    years=3) or []
        # Use curated for current, WB[-1] for prev
        gdp_cur  = snap.get('gdp_growth')
        inf_cur  = snap.get('inflation')
        une_cur  = snap.get('unemployment')
        gdp_prev = gdp_d[-1]['value'] if gdp_d else None
        inf_prev = inf_d[-1]['value'] if inf_d else None
        une_prev = une_d[-1]['value'] if une_d else None
        result[key] = {
            'name': name, 'flag': flag,
            **calc_econ_health(
                gdp=gdp_cur, inflation=inf_cur, unemployment=une_cur,
                prev_gdp=gdp_prev, prev_inflation=inf_prev, prev_unemployment=une_prev,
            ),
            'latest': {
                'gdp':          gdp_cur,
                'gdp_date':     '2026-Q1',
                'inflation':    inf_cur,
                'unemployment': une_cur,
            }
        }

    # Sort by composite score
    ranked = sorted(result.items(), key=lambda x: x[1].get('composite', 0), reverse=True)
    return jsonify({'economies': dict(ranked), 'generated': int(time.time())})


# ══════════════════════════════════════════════════════════════════
# ◈ FOREX — Currency heat map, strength index, carry trade signals
# ══════════════════════════════════════════════════════════════════

CURRENCIES = {
    # Rates updated May 2026
    'USD': {'name': 'US Dollar',         'flag': '🇺🇸', 'rate': 4.33},  # Fed Funds — held Apr 2026
    'EUR': {'name': 'Euro',              'flag': '🇪🇺', 'rate': 2.00},  # ECB deposit rate — held Apr 2026
    'GBP': {'name': 'British Pound',     'flag': '🇬🇧', 'rate': 3.75},  # BoE — held Apr 30 2026
    'JPY': {'name': 'Japanese Yen',      'flag': '🇯🇵', 'rate': 0.50},  # BoJ — held, gradual hike path
    'CHF': {'name': 'Swiss Franc',       'flag': '🇨🇭', 'rate': 0.00},  # SNB — at zero
    'AUD': {'name': 'Australian Dollar', 'flag': '🇦🇺', 'rate': 4.35},  # RBA — hiked May 2026
    'CAD': {'name': 'Canadian Dollar',   'flag': '🇨🇦', 'rate': 2.75},  # BoC — held
    'NZD': {'name': 'New Zealand Dollar','flag': '🇳🇿', 'rate': 2.25},  # RBNZ — current rate
    'CNY': {'name': 'Chinese Yuan',      'flag': '🇨🇳', 'rate': 3.10},  # PBOC LPR
}

# Yahoo Finance FX pair symbols — always quoted as XXX/USD or USD/XXX
FX_PAIRS = {
    'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X', 'USDJPY': 'USDJPY=X',
    'USDCHF': 'USDCHF=X', 'AUDUSD': 'AUDUSD=X', 'USDCAD': 'USDCAD=X',
    'NZDUSD': 'NZDUSD=X', 'USDCNY': 'USDCNY=X',
    'EURGBP': 'EURGBP=X', 'EURJPY': 'EURJPY=X', 'GBPJPY': 'GBPJPY=X',
    'AUDJPY': 'AUDJPY=X', 'CADJPY': 'CADJPY=X', 'CHFJPY': 'CHFJPY=X',
    'EURCHF': 'EURCHF=X', 'GBPCHF': 'GBPCHF=X', 'AUDCAD': 'AUDCAD=X',
    'AUDNZD': 'AUDNZD=X', 'EURCAD': 'EURCAD=X', 'GBPCAD': 'GBPCAD=X',
}

# Equity index correlations — which index reflects each currency
CURRENCY_INDEX = {
    'USD': 'SPY',  'EUR': 'EZU',  'GBP': 'EWU',  'JPY': 'EWJ',
    'CHF': 'EWL',  'AUD': 'EWA',  'CAD': 'EWC',  'NZD': None,
    'CNY': 'MCHI',
}

# FX cache now uses unified cache with TTL['fx']

def get_fx_price(symbol):
    """Fetch a single FX pair price from Yahoo Finance."""
    cached = cache.get(f'fx:{symbol}')
    if cached is not None:
        return cached
    for base in ['https://query1.finance.yahoo.com', 'https://query2.finance.yahoo.com']:
        try:
            url = f'{base}/v8/finance/chart/{symbol}?interval=1d&range=65d'
            r = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Referer': 'https://finance.yahoo.com',
            }, timeout=10)
            if r.status_code != 200:
                continue
            meta = r.json().get('chart', {}).get('result', [{}])[0].get('meta', {})
            price = meta.get('regularMarketPrice', 0)
            prev  = meta.get('chartPreviousClose', price) or price
            # Get 5-day close prices for momentum
            chart  = r.json().get('chart', {}).get('result', [{}])[0]
            closes = chart.get('indicators', {}).get('quote', [{}])[0].get('close', [])
            closes = [c for c in closes if c is not None]
            if price and price > 0:
                data = {
                    'price':     round(price, 5),
                    'prev':      round(prev, 5),
                    'change':    round(price - prev, 5),
                    'changePct': round((price - prev) / prev * 100, 4) if prev else 0,
                    'closes':    closes,
                    'w1_chg':    round((price - closes[0]) / closes[0] * 100, 4) if len(closes) > 1 and closes[0] else 0,
                    'd1_chg':    round((price - prev) / prev * 100, 4) if prev else 0,
                    'd5_chg':    round((price - closes[-5]) / closes[-5] * 100, 4) if len(closes) >= 5 and closes[-5] else 0,
                    'd20_chg':   round((price - closes[-20]) / closes[-20] * 100, 4) if len(closes) >= 20 and closes[-20] else 0,
                    'd65_chg':   round((price - closes[-65]) / closes[-65] * 100, 4) if len(closes) >= 65 and closes[-65] else 0,
                }
                cache.set(f'fx:{symbol}', data, TTL['fx'])
                return data
        except Exception as e:
            print(f'[forex] {symbol} error: {e}')
    return None

PAIR_MAP = {
    'EURUSD': ('EUR','USD'), 'GBPUSD': ('GBP','USD'), 'AUDUSD': ('AUD','USD'),
    'NZDUSD': ('NZD','USD'), 'USDCAD': ('USD','CAD'), 'USDCHF': ('USD','CHF'),
    'USDJPY': ('USD','JPY'), 'USDCNY': ('USD','CNY'), 'EURGBP': ('EUR','GBP'),
    'EURJPY': ('EUR','JPY'), 'GBPJPY': ('GBP','JPY'), 'AUDJPY': ('AUD','JPY'),
    'CADJPY': ('CAD','JPY'), 'CHFJPY': ('CHF','JPY'), 'EURCHF': ('EUR','CHF'),
    'GBPCHF': ('GBP','CHF'), 'AUDCAD': ('AUD','CAD'), 'AUDNZD': ('AUD','NZD'),
    'EURCAD': ('EUR','CAD'), 'GBPCAD': ('GBP','CAD'),
}

def normalise_strength(raw_scores):
    """Normalise a dict of {cur: raw_score} to 0-100."""
    if not raw_scores: return {}
    mn, mx = min(raw_scores.values()), max(raw_scores.values())
    spread = (mx - mn) or 0.0001
    return {cur: round((v - mn) / spread * 100) for cur, v in raw_scores.items()}

def calc_currency_strength(timeframe='1D'):
    """
    Currency strength index for a given timeframe.
    timeframe: '1D' | '1W' | '1M' | '3M'
    """
    pair_data = {}
    for pair, symbol in FX_PAIRS.items():
        data = get_fx_price(symbol)
        if data:
            pair_data[pair] = data

    # Map timeframe to change key
    chg_key = {
        '1D': 'd1_chg',
        '1W': 'd5_chg',
        '1M': 'd20_chg',
        '3M': 'd65_chg',
    }.get(timeframe, 'd1_chg')

    # Accumulate raw scores per currency
    raw = {c: [] for c in CURRENCIES}
    for pair, (base, quote) in PAIR_MAP.items():
        if pair not in pair_data: continue
        d   = pair_data[pair]
        chg = d.get(chg_key) or d.get('changePct', 0)
        if base in raw: raw[base].append(chg)
        if quote in raw: raw[quote].append(-chg)

    # Average and normalise
    avg_scores = {}
    for cur, vals in raw.items():
        if vals: avg_scores[cur] = sum(vals) / len(vals)

    normed = normalise_strength(avg_scores)

    # Build all 4 timeframes for each currency (for the multi-bar display)
    all_tf = {}
    for tf in ('1D', '1W', '1M', '3M'):
        ck = {'1D':'d1_chg','1W':'d5_chg','1M':'d20_chg','3M':'d65_chg'}[tf]
        r = {}
        for pair, (base, quote) in PAIR_MAP.items():
            if pair not in pair_data: continue
            chg = pair_data[pair].get(ck) or pair_data[pair].get('changePct', 0)
            r.setdefault(base, []).append(chg)
            r.setdefault(quote, []).append(-chg)
        avgs = {c: sum(v)/len(v) for c,v in r.items() if v}
        all_tf[tf] = normalise_strength(avgs)

    result = {}
    for cur, norm in normed.items():
        if   norm >= 75: signal = 'STRONG'
        elif norm >= 58: signal = 'BULLISH'
        elif norm >= 42: signal = 'NEUTRAL'
        elif norm >= 25: signal = 'BEARISH'
        else:            signal = 'WEAK'
        result[cur] = {
            'strength':    norm,
            'signal':      signal,
            'policy_rate': CURRENCIES[cur]['rate'],
            'name':        CURRENCIES[cur]['name'],
            'flag':        CURRENCIES[cur]['flag'],
            # All timeframe scores for sparkline display
            'tf': {
                '1D': all_tf['1D'].get(cur, 50),
                '1W': all_tf['1W'].get(cur, 50),
                '1M': all_tf['1M'].get(cur, 50),
                '3M': all_tf['3M'].get(cur, 50),
            }
        }
    return result, pair_data

def calc_carry_trades(strength_data):
    """
    Identify best carry trade opportunities:
    Borrow low-rate currency, buy high-rate currency.
    Score = rate differential + momentum alignment.
    """
    carries = []
    curs = list(strength_data.items())
    for i, (fund, fd) in enumerate(curs):
        for j, (carry, cd) in enumerate(curs):
            if fund == carry: continue
            rate_diff = cd['policy_rate'] - fd['policy_rate']
            if rate_diff < 0.5: continue  # need meaningful differential
            # Momentum alignment: carry currency should be strong, fund weak
            momentum_score = cd['strength'] - fd['strength']
            total = round(rate_diff * 10 + momentum_score * 0.5)
            # Risk: JPY/CHF as funding — safe havens can reverse sharply
            risk = 'HIGH' if fund in ('JPY','CHF') and carry in ('AUD','NZD') else                    'MEDIUM' if fund in ('JPY','CHF') else 'LOW'
            carries.append({
                'pair':          f'{carry}/{fund}',
                'carry_cur':     carry,
                'fund_cur':      fund,
                'carry_flag':    cd['flag'],
                'fund_flag':     fd['flag'],
                'rate_diff':     round(rate_diff, 2),
                'carry_rate':    cd['policy_rate'],
                'fund_rate':     fd['policy_rate'],
                'carry_strength':cd['strength'],
                'fund_strength': fd['strength'],
                'momentum_score':round(momentum_score),
                'total_score':   total,
                'risk':          risk,
                'signal':        'BUY' if momentum_score > 10 else 'WATCH' if momentum_score > 0 else 'AVOID',
            })
    carries.sort(key=lambda x: x['total_score'], reverse=True)
    return carries[:10]

def calc_equity_correlation(pair_data):
    """
    Map currency strength to equity market implications.
    Strong USD = headwind for EM/commodities. Weak JPY = Nikkei boost etc.
    """
    signals = []
    usd = pair_data.get('EURUSD', {})
    jpy = pair_data.get('USDJPY', {})
    # USD strength (EURUSD falling = USD rising)
    if usd:
        usd_chg = -usd['changePct']  # invert since EURUSD
        if   usd_chg >  0.3: signals.append({'signal': 'USD STRONG', 'implication': 'Headwind for commodities, EM equities, gold', 'col': '#f56565', 'assets': ['GLD','EEM','USO']})
        elif usd_chg < -0.3: signals.append({'signal': 'USD WEAK',   'implication': 'Tailwind for gold, commodities, EM, international equities', 'col': '#48d597', 'assets': ['GLD','EEM','GDX']})
    # JPY weakness (USDJPY rising = JPY weak)
    if jpy:
        jpy_chg = jpy['changePct']
        if   jpy_chg >  0.3: signals.append({'signal': 'JPY WEAK',   'implication': 'Nikkei bullish, carry trades intact, risk-on', 'col': '#48d597', 'assets': ['EWJ','SPY']})
        elif jpy_chg < -0.3: signals.append({'signal': 'JPY STRONG', 'implication': 'Risk-off signal, carry unwind risk, watch equities', 'col': '#f56565', 'assets': ['TLT','GLD']})
    # AUD as risk proxy
    aud = pair_data.get('AUDUSD', {})
    if aud:
        if   aud['changePct'] >  0.3: signals.append({'signal': 'AUD STRONG', 'implication': 'Risk-on, commodities bid, China optimism', 'col': '#48d597', 'assets': ['GDX','EWA','VALE']})
        elif aud['changePct'] < -0.3: signals.append({'signal': 'AUD WEAK',   'implication': 'Risk-off, commodity weakness, China concerns', 'col': '#f56565', 'assets': ['TLT','GLD']})
    # CHF safe haven
    chf = pair_data.get('USDCHF', {})
    if chf:
        chf_chg = -chf['changePct']  # invert
        if chf_chg >  0.2: signals.append({'signal': 'CHF STRONG', 'implication': 'Safe haven demand — risk-off across markets', 'col': '#f56565', 'assets': ['TLT','GLD','VIX']})
    return signals


@app.route('/api/forex')
def get_forex():
    """Full forex data: strength index, heat map pairs, carry trades, equity signals."""
    timeframe = request.args.get('tf', '1D').upper()
    if timeframe not in ('1D','1W','1M','3M'): timeframe = '1D'
    strength, pair_data = calc_currency_strength(timeframe)
    carries  = calc_carry_trades(strength)
    eq_sigs  = calc_equity_correlation(pair_data)

    # Build heat map matrix — pct change for each cross
    matrix = {}
    pair_map = {
        'EURUSD':('EUR','USD'),'GBPUSD':('GBP','USD'),'AUDUSD':('AUD','USD'),
        'NZDUSD':('NZD','USD'),'USDCAD':('USD','CAD'),'USDCHF':('USD','CHF'),
        'USDJPY':('USD','JPY'),'USDCNY':('USD','CNY'),'EURGBP':('EUR','GBP'),
        'EURJPY':('EUR','JPY'),'GBPJPY':('GBP','JPY'),'AUDJPY':('AUD','JPY'),
        'CADJPY':('CAD','JPY'),'CHFJPY':('CHF','JPY'),'EURCHF':('EUR','CHF'),
        'GBPCHF':('GBP','CHF'),'AUDCAD':('AUD','CAD'),'AUDNZD':('AUD','NZD'),
        'EURCAD':('EUR','CAD'),'GBPCAD':('GBP','CAD'),
    }
    cur_list = list(CURRENCIES.keys())
    for base in cur_list:
        matrix[base] = {}
        for quote in cur_list:
            if base == quote:
                matrix[base][quote] = 0.0
                continue
            pair = base + quote
            inv  = quote + base
            if pair in pair_map and pair in pair_data:
                matrix[base][quote] = round(pair_data[pair]['changePct'], 4)
            elif inv in pair_map and inv in pair_data:
                matrix[base][quote] = round(-pair_data[inv]['changePct'], 4)
            else:
                matrix[base][quote] = None

    return ok({
        'strength':         strength,
        'pairs':            {k: v for k, v in pair_data.items()},
        'matrix':           matrix,
        'currencies':       cur_list,
        'carry_trades':     carries,
        'equity_signals':   eq_sigs,
        'currency_index':   CURRENCY_INDEX,
        'generated':        int(time.time()),
    })

@app.route('/api/forex/pair/<pair>')
def get_forex_pair(pair):
    """Single pair detail."""
    symbol = FX_PAIRS.get(pair.upper())
    if not symbol:
        return jsonify({'error': f'Unknown pair: {pair}'}), 404
    data = get_fx_price(symbol)
    if not data:
        return jsonify({'error': 'Could not fetch pair data'}), 503
    return jsonify({'pair': pair.upper(), **data})

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    print(f"\n◈ STOCKSENSE on port {port}\n")
    app.run(host='0.0.0.0',port=port,debug=False)
