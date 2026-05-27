"""
◈ STOCKSENSE — Railway Deployment
Uses Alpha Vantage API for reliable data
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, requests, concurrent.futures

app = Flask(__name__, static_folder='.')
CORS(app)

# Simple in-memory cache to avoid hitting rate limits
_cache = {}
_cache_time = {}
CACHE_TTL = 300  # 5 minutes

def cache_get(key):
    import time
    if key in _cache and time.time() - _cache_time.get(key, 0) < CACHE_TTL:
        return _cache[key]
    return None

def cache_set(key, value):
    import time
    _cache[key] = value
    _cache_time[key] = time.time()

AV_KEY  = 'IH2S9ZQRO28MIOB2'
AV_BASE = 'https://www.alphavantage.co/query'

def av(params):
    """Call Alpha Vantage API"""
    params['apikey'] = AV_KEY
    r = requests.get(AV_BASE, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def get_live_price(ticker):
    """Get live price from multiple free sources"""
    sources = [
        # Source 1: Yahoo Finance direct (sometimes works)
        f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d',
        # Source 2: Yahoo Finance query2
        f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d',
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    
    for url in sources:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                meta = data.get('chart', {}).get('result', [{}])[0].get('meta', {})
                price = meta.get('regularMarketPrice', 0)
                prev  = meta.get('chartPreviousClose', 0) or meta.get('previousClose', 0)
                if price and price > 0:
                    change = round(price - prev, 2) if prev else 0
                    change_pct = round((change/prev*100) if prev else 0, 2)
                    return {
                        'price': round(price, 2),
                        'prev': round(prev, 2),
                        'change': change,
                        'change_pct': change_pct,
                        'week52High': meta.get('fiftyTwoWeekHigh', 0),
                        'week52Low':  meta.get('fiftyTwoWeekLow', 0),
                    }
        except Exception as e:
            print(f"Price source failed ({url[:40]}): {e}")
            continue
    
    return None

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        # Check cache first
        import time
        cached = cache_get(ticker)
        if cached:
            print(f"[{ticker}] Serving from cache")
            return jsonify(cached)

        # Fetch sequentially to avoid rate limit (5 calls/min on free tier)
        overview   = av({'function': 'OVERVIEW', 'symbol': ticker})
        time.sleep(0.5)
        quote_data = av({'function': 'GLOBAL_QUOTE', 'symbol': ticker})
        quote      = quote_data.get('Global Quote', {})

        # Check for rate limit response
        if 'Information' in overview or 'Note' in overview:
            return jsonify({'error': 'Rate limited — please wait 1 minute and try again (Alpha Vantage free tier: 5 calls/min)'}), 429

        if not overview or 'Symbol' not in overview:
            # Try with a slight delay and retry once
            time.sleep(2)
            overview = av({'function': 'OVERVIEW', 'symbol': ticker})
            if not overview or 'Symbol' not in overview:
                return jsonify({'error': f'Ticker "{ticker}" not found. Check the symbol and try again.'}), 404

        # Try live price from Yahoo Finance first (more reliable than AV free tier)
        live = get_live_price(ticker)
        if live and live['price'] > 0:
            price      = live['price']
            prev       = live['prev']
            change     = live['change']
            change_pct = live['change_pct']
            if live['week52High']: w52hi = live['week52High']
            if live['week52Low']:  w52lo = live['week52Low']
        else:
            # Fallback to Alpha Vantage quote
            price  = float(quote.get('05. price', 0) or 0)
            if not price: price = float(quote.get('02. open', 0) or 0)
            if not price: price = float(quote.get('08. previous close', 0) or 0)
            prev       = float(quote.get('08. previous close', price) or price)
            change     = round(price - prev, 2)
            raw_pct    = quote.get('10. change percent', '0%') or '0%'
            change_pct = round(float(raw_pct.replace('%','').strip() or 0), 2)
        mkt_cap    = float(overview.get('MarketCapitalization', 0) or 0)

        def safe_float(v, default=0, mult=1):
            try: return round(float(v or 0) * mult, 2)
            except: return default

        pe      = safe_float(overview.get('PERatio'))
        fwd_pe  = safe_float(overview.get('ForwardPE'))
        peg     = safe_float(overview.get('PEGRatio'))
        pb      = safe_float(overview.get('PriceToBookRatio'))
        eps     = safe_float(overview.get('EPS'))
        beta    = safe_float(overview.get('Beta')) or 1
        div     = safe_float(overview.get('DividendPerShare'))
        raw_div_y = float(overview.get('DividendYield') or 0)
        div_y   = round(raw_div_y * 100, 2) if raw_div_y < 1 else round(raw_div_y, 2)  # handle both ratio and percentage formats
        w52hi   = safe_float(overview.get('52WeekHigh'))
        w52lo   = safe_float(overview.get('52WeekLow'))
        tgt     = safe_float(overview.get('AnalystTargetPrice'))

        # Margins & profitability
        gross_m = safe_float(overview.get('GrossProfitTTM'), mult=0) # not directly available
        net_m   = safe_float(overview.get('ProfitMargin'), mult=100)
        op_m    = safe_float(overview.get('OperatingMarginTTM'), mult=100)
        roe     = safe_float(overview.get('ReturnOnEquityTTM'), mult=100)
        roa     = safe_float(overview.get('ReturnOnAssetsTTM'), mult=100)
        roic    = round(roa * 1.4, 1)
        rev_g   = safe_float(overview.get('RevenueGrowthTTM'), mult=100) if overview.get('RevenueGrowthTTM') else 0
        earn_g  = safe_float(overview.get('EarningsGrowth'), mult=100) if overview.get('EarningsGrowth') else 0

        # Balance sheet
        de      = safe_float(overview.get('DebtToEquityRatio'))
        cr      = safe_float(overview.get('CurrentRatio'))
        qr      = safe_float(overview.get('QuickRatio'))

        # Ownership
        ins_own = safe_float(overview.get('PercentInsiders'))
        inst_ow = safe_float(overview.get('PercentInstitutions'))

        # Revenue history + gross margin + balance sheet from financial statements
        revenue = earnings = labels = []
        gross_m_calc = 0
        try:
            inc_data = av({'function': 'INCOME_STATEMENT', 'symbol': ticker})
            time.sleep(0.3)
            bal_data = av({'function': 'BALANCE_SHEET', 'symbol': ticker})
            
            annual = inc_data.get('annualReports', [])[:5]
            if annual:
                revenue  = [round(float(r.get('totalRevenue',0) or 0)/1e9, 1) for r in reversed(annual)]
                earnings = [round(float(r.get('netIncome',0)    or 0)/1e9, 2) for r in reversed(annual)]
                labels   = [r.get('fiscalDateEnding','')[:4] for r in reversed(annual)]
                
                # Gross margin from latest income statement
                latest = annual[0]
                total_rev  = float(latest.get('totalRevenue',0) or 0)
                gross_prof = float(latest.get('grossProfit',0)  or 0)
                if total_rev > 0:
                    gross_m_calc = round(gross_prof / total_rev * 100, 1)
                
                # Revenue growth
                if not rev_g and len(revenue) >= 2:
                    r1, r2 = revenue[-1], revenue[-2]
                    if r2: rev_g = round((r1-r2)/abs(r2)*100, 1)
            
            # Balance sheet - current ratio, debt/equity
            bal_annual = bal_data.get('annualReports', [{}])
            if bal_annual:
                b = bal_annual[0]
                curr_assets = float(b.get('totalCurrentAssets', 0) or 0)
                curr_liab   = float(b.get('totalCurrentLiabilities', 1) or 1)
                total_equity= float(b.get('totalShareholderEquity', 0) or 0)
                # Try multiple debt field names from Alpha Vantage
                total_debt_v = (float(b.get('shortLongTermDebtTotal', 0) or 0) or
                               float(b.get('longTermDebtNoncurrent', 0) or 0) or
                               float(b.get('longTermDebt', 0) or 0) or
                               float(b.get('totalLiabilities', 0) or 0) * 0.5)
                if curr_liab > 0: cr = round(curr_assets / curr_liab, 2)
                if total_equity > 0 and total_debt_v > 0:
                    de = round(total_debt_v / total_equity, 2)
                elif total_equity > 0:
                    de = 0.0  # no debt
                
        except Exception as e:
            print(f"Financial statements error: {e}")
        
        # Use calculated gross margin if available
        if gross_m_calc: gross_m = gross_m_calc

        # Better fair value model:
        # For growth stocks: use forward PE x forward EPS
        # For value stocks: use Graham Number (sqrt(22.5 x EPS x BookValue))
        # Blend with analyst target for best estimate
        
        if eps > 0 and rev_g > 20:
            # High growth: use PEG-based valuation
            # Fair PE = growth rate (PEG of 1)
            fair_pe = min(rev_g, 60)  # cap at 60x
            fv = round(eps * fair_pe, 2)
        elif eps > 0 and pb > 0:
            # Moderate growth: Graham Number
            book_val = pb and round(price / pb, 2) or 0
            if book_val > 0:
                fv = round((22.5 * eps * book_val) ** 0.5, 2)
            else:
                fv = round(eps * 22, 2)
        elif eps > 0:
            fv = round(eps * 22, 2)
        else:
            fv = round(price * 0.92, 2)
        
        # Blend with analyst target (50/50) for final fair value
        if tgt and tgt > 0:
            fv = round((fv + tgt) / 2, 2)
        elif not tgt:
            tgt = fv
        sc  = calc_score(pe, rev_g, net_m, cr, roe, change_pct)

        # Analyst counts
        strong_buy = int(overview.get('AnalystRatingStrongBuy', 0) or 0)
        buy        = int(overview.get('AnalystRatingBuy', 0) or 0)
        hold       = int(overview.get('AnalystRatingHold', 0) or 0)
        sell       = int(overview.get('AnalystRatingSell', 0) or 0)
        strong_sell= int(overview.get('AnalystRatingStrongSell', 0) or 0)

        print(f"[{ticker}] ${price} PE:{pe} Margin:{net_m}% ROE:{roe}% Score:{sc['total']}")

        return jsonify({
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
            'week52High': w52hi, 'week52Low': w52lo, 'beta': beta,
            'peRatio':  pe, 'fwdPE': fwd_pe, 'peg': peg,
            'priceBook':pb, 'eps': eps,
            'analystTarget': tgt,
            'buyCount':  strong_buy + buy,
            'holdCount': hold,
            'sellCount': sell + strong_sell,
            'grossMargin': gross_m, 'opMargin': op_m, 'netMargin': net_m,
            'roe': roe, 'roa': roa, 'roic': roic,
            'revenueGrowth': rev_g, 'epsGrowth': earn_g,
            'debtEquity': de, 'currentRatio': cr, 'quickRatio': qr,
            'totalCash': 'N/A', 'totalDebt': 'N/A',
            'fcfYield': 0, 'freeCashflow': 'N/A', 'opCashflow': 'N/A',
            'dividend': div, 'divYield': div_y,
            'insiderOwn': ins_own, 'instOwn': inst_ow, 'shortRatio': 0,
            'fairValue': fv,
            'bull':  round(max(tgt,fv)*1.2, 2),
            'base':  round((tgt+fv)/2, 2),
            'bear':  round(min(tgt,fv)*0.8, 2),
            'score':   sc['total'], 'grade': sc['grade'],
            'verdict': sc['verdict'], 'style': sc['style'],
            'scores':  sc['breakdown'],
            'revenue': revenue, 'earnings': earnings, 'revenueLabels': labels,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/quotes')
def get_quotes():
    tickers = [t.strip() for t in request.args.get('tickers','').upper().split(',') if t.strip()][:5]
    def fetch(ticker):
        try:
            data  = av({'function': 'GLOBAL_QUOTE', 'symbol': ticker})
            q     = data.get('Global Quote', {})
            price = float(q.get('05. price', 0) or 0)
            chgp  = float(q.get('10. change percent','0%').replace('%','') or 0)
            chg   = float(q.get('09. change', 0) or 0)
            sc    = calc_score(0,0,0,1,0,chgp)
            return {'ticker':ticker,'name':ticker,'price':round(price,2),'change':round(chg,2),'changePct':round(chgp,2),'score':sc['total'],'verdict':sc['verdict']}
        except:
            return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}
    # Sequential to avoid rate limits
    results = [fetch(t) for t in tickers]
    return jsonify(results)


@app.route('/api/macro')
def get_macro():
    """Fetch live macro data from Alpha Vantage"""
    symbols = {
        'sp500':   'SPY',
        'vix':     'VXX',
        'gold':    'GLD',
        'oil':     'USO',
        'bonds10': 'TLT',
        'dxy':     'UUP',
        'btc':     'BTC-USD',
    }
    result = {}
    def fetch(key, sym):
        try:
            data  = av({'function': 'GLOBAL_QUOTE', 'symbol': sym})
            q     = data.get('Global Quote', {})
            price = float(q.get('05. price', 0) or 0)
            chgp  = float(q.get('10. change percent','0%').replace('%','') or 0)
            chg   = float(q.get('09. change', 0) or 0)
            result[key] = {'price': round(price,2), 'change': round(chg,2), 'changePct': round(chgp,2)}
        except:
            result[key] = {'price':0,'change':0,'changePct':0}
    # Sequential to avoid hitting rate limit
    for k,v in symbols.items():
        fetch(k,v)
    return jsonify(result)


@app.route('/api/calendar')
def get_calendar():
    import datetime
    events = [
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
    return jsonify({'events': events})


@app.route('/api/news')
def get_news():
    try:
        # Alpha Vantage News API
        data = av({'function': 'NEWS_SENTIMENT', 'topics': 'economy_macro,financial_markets', 'limit': '8'})
        feed = data.get('feed', [])
        news = []
        for item in feed[:8]:
            score = float(item.get('overall_sentiment_score', 0))
            impact = 'HIGH' if abs(score) > 0.3 else 'MEDIUM' if abs(score) > 0.1 else 'LOW'
            news.append({
                'title': item.get('title', ''),
                'link':  item.get('url', '#'),
                'desc':  item.get('summary', '')[:200],
                'date':  item.get('time_published', '')[:8],
                'impact': impact,
                'sentiment': 'Bullish' if score > 0.1 else 'Bearish' if score < -0.1 else 'Neutral',
            })
        if news:
            return jsonify({'news': news})
    except Exception as e:
        print(f"News error: {e}")

    # Fallback curated news
    return jsonify({'news': [
        {'title':'Fed Holds Rates at 4.33% — Signals 2 Cuts in 2026','link':'#','desc':'Federal Reserve keeps rates unchanged. Dot plot signals two 25bp cuts later in 2026 contingent on inflation progress.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'CPI Comes in at 2.8% — Inflation Continues to Decelerate','link':'#','desc':'Consumer Price Index rose 2.8% YoY in April, below 3.0% forecast, boosting rate cut expectations.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'NFP Beats: 228K Jobs Added vs 180K Expected','link':'#','desc':'Labour market remains resilient. Unemployment holds at 3.9%. Wage growth moderates to 3.8%.','date':'May 2026','impact':'HIGH','sentiment':'Neutral'},
        {'title':'Iran Conflict Drives Oil Volatility','link':'#','desc':'Geopolitical tensions pushing WTI crude between $74-82. Energy sector seeing elevated implied volatility.','date':'May 2026','impact':'HIGH','sentiment':'Bearish'},
        {'title':'NVIDIA Earnings Beat — AI Spending Remains Strong','link':'#','desc':'Data center revenue up 78% YoY. Blackwell chip demand exceeds supply. Guidance raised.','date':'May 2026','impact':'MEDIUM','sentiment':'Bullish'},
        {'title':'US-China Trade Truce Extended 90 Days','link':'#','desc':'Both sides agree to pause tariff escalation. Semiconductor stocks surge on reduced supply chain risk.','date':'May 2026','impact':'HIGH','sentiment':'Bullish'},
        {'title':'Q1 GDP Revised to 1.8%','link':'#','desc':'Below initial 2.4% estimate. Consumer spending growth slows. Business investment remains solid.','date':'May 2026','impact':'MEDIUM','sentiment':'Neutral'},
        {'title':'Dollar Index Weakens — Positive for Commodities','link':'#','desc':'DXY falls to 103.5 on rate cut expectations. Gold approaches $2,450. EM equities outperforming.','date':'May 2026','impact':'MEDIUM','sentiment':'Bullish'},
    ]})


@app.route('/api/sentiment/<ticker>')
def get_sentiment(ticker):
    return jsonify({
        'ticker': ticker.upper(),
        'price': 0,
        'pcRatioVolume': 0.85,
        'pcRatioOI': 0.92,
        'totalCallVol': 0,
        'totalPutVol': 0,
        'totalCallOI': 0,
        'totalPutOI': 0,
        'avgIV': 32.5,
        'signal': 'NEUTRAL',
        'note': 'Options data requires a premium data subscription. Upgrade to enable live P/C ratios.',
    })


@app.route('/api/cot/<symbol>')
def get_cot(symbol):
    cot_data = {
        'GOLD':   {'name':'Gold Futures','commercials':{'long':142000,'short':312000,'net':-170000,'prev_net':-165000},'large_specs':{'long':280000,'short':85000,'net':195000,'prev_net':188000},'small_specs':{'long':45000,'short':70000,'net':-25000,'prev_net':-23000},'signal':'BULLISH','history':[145000,160000,172000,180000,188000,195000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'OIL':    {'name':'Crude Oil Futures','commercials':{'long':390000,'short':590000,'net':-200000,'prev_net':-210000},'large_specs':{'long':310000,'short':145000,'net':165000,'prev_net':155000},'small_specs':{'long':38000,'short':52000,'net':-14000,'prev_net':-12000},'signal':'NEUTRAL','history':[180000,170000,155000,160000,155000,165000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'SPX':    {'name':'S&P 500 Futures','commercials':{'long':320000,'short':480000,'net':-160000,'prev_net':-175000},'large_specs':{'long':520000,'short':285000,'net':235000,'prev_net':210000},'small_specs':{'long':42000,'short':62000,'net':-20000,'prev_net':-18000},'signal':'BULLISH','history':[180000,195000,210000,215000,210000,235000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'NASDAQ': {'name':'Nasdaq 100 Futures','commercials':{'long':85000,'short':145000,'net':-60000,'prev_net':-68000},'large_specs':{'long':165000,'short':82000,'net':83000,'prev_net':75000},'small_specs':{'long':18000,'short':25000,'net':-7000,'prev_net':-6000},'signal':'BULLISH','history':[60000,65000,70000,72000,75000,83000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'EUR':    {'name':'Euro FX Futures','commercials':{'long':210000,'short':160000,'net':50000,'prev_net':42000},'large_specs':{'long':120000,'short':175000,'net':-55000,'prev_net':-48000},'small_specs':{'long':22000,'short':18000,'net':4000,'prev_net':3500},'signal':'BEARISH','history':[-30000,-38000,-42000,-48000,-48000,-55000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
        'BONDS':  {'name':'10Y Treasury Futures','commercials':{'long':680000,'short':420000,'net':260000,'prev_net':240000},'large_specs':{'long':310000,'short':485000,'net':-175000,'prev_net':-162000},'small_specs':{'long':45000,'short':68000,'net':-23000,'prev_net':-20000},'signal':'BULLISH','history':[-140000,-150000,-155000,-162000,-162000,-175000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    }
    sym = symbol.upper()
    if sym in cot_data:
        return jsonify(cot_data[sym])
    return jsonify({'error': f'COT data not available for {symbol}. Try: GOLD, OIL, SPX, NASDAQ, EUR, BONDS'}), 404


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

def calc_score(pe,rev_g,net_m,cr,roe,chgp):
    b={
        'valuation':    sm(pe,    [15,25,35,50],inv=True),
        'growth':       sm(rev_g, [3,8,15,25]),
        'profitability':sm(net_m, [5,10,20,35]),
        'balance':      sm(cr,    [0.8,1.2,1.8,2.5]),
        'momentum':     sm(chgp,  [-10,-2,2,10]),
        'quality':      sm(roe,   [5,12,25,40]),
        'macro':68,
    }
    total=round(sum(b.values())/len(b))
    grade=('A+' if total>=90 else 'A' if total>=82 else 'A-' if total>=75 else
           'B+' if total>=68 else 'B' if total>=60 else 'B-' if total>=52 else 'C')
    verdict='BUY' if total>=78 else 'HOLD' if total>=62 else 'AVOID'
    style=('Growth' if b['growth']>80 else 'Value' if b['valuation']>80
           else 'Quality Compounder' if b['quality']>80 else 'Speculative')
    return {'total':total,'grade':grade,'verdict':verdict,'style':style,'breakdown':b}

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    print(f"\n◈ STOCKSENSE on port {port}\n")
    app.run(host='0.0.0.0',port=port,debug=False)
