"""
◈ STOCKBOX — Railway Deployment
Uses multiple data sources with automatic fallback
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, concurrent.futures, requests, time, random

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Try multiple Yahoo Finance endpoints with rotation ─────
YF_HEADERS_LIST = [
    {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://finance.yahoo.com/',
        'Origin': 'https://finance.yahoo.com',
        'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
    },
    {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Accept': 'application/json',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'https://finance.yahoo.com/',
    },
    {
        'User-Agent': 'python-requests/2.31.0',
        'Accept': '*/*',
    }
]

SESSION = requests.Session()

def get_headers():
    return random.choice(YF_HEADERS_LIST)

def yf_get(url, retries=3):
    """Fetch from Yahoo Finance with retries and header rotation"""
    for i in range(retries):
        try:
            headers = get_headers()
            r = SESSION.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(2 ** i)  # exponential backoff
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1)
    return {}

def get_full_data(ticker):
    """Get comprehensive stock data using multiple Yahoo endpoints"""
    result = {}
    
    # Try quoteSummary with all modules
    try:
        modules = 'price,summaryDetail,defaultKeyStatistics,financialData,incomeStatementHistory,recommendationTrend,calendarEvents'
        url = f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}&corsDomain=finance.yahoo.com&formatted=false'
        data = yf_get(url)
        summary = data.get('quoteSummary', {}).get('result', [])
        if summary:
            result['summary'] = summary[0]
    except:
        pass

    # Try quote endpoint as backup/supplement
    try:
        url2 = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&fields=regularMarketPrice,regularMarketChange,regularMarketChangePercent,regularMarketPreviousClose,marketCap,trailingPE,forwardPE,fiftyTwoWeekHigh,fiftyTwoWeekLow,beta,longName,shortName,sector,industry,exchange,dividendRate,dividendYield,targetMeanPrice,averageAnalystRating&corsDomain=finance.yahoo.com'
        data2 = yf_get(url2)
        quotes = data2.get('quoteResponse', {}).get('result', [])
        if quotes:
            result['quote'] = quotes[0]
    except:
        pass

    # Try v8 chart as final fallback for price
    if not result:
        try:
            url3 = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d&includePrePost=false'
            data3 = yf_get(url3)
            chart_result = data3.get('chart', {}).get('result', [])
            if chart_result:
                result['chart'] = chart_result[0]
        except:
            pass

    return result

def extract_val(obj, *keys, default=0, mult=1):
    """Safely extract a numeric value"""
    try:
        v = obj
        for k in keys:
            if isinstance(v, dict):
                v = v.get(k, {})
            else:
                return default
        if isinstance(v, dict):
            raw = v.get('raw', v.get('fmt', default))
        else:
            raw = v
        if raw in (None, '', {}, 'N/A', 'None'):
            return default
        return round(float(raw) * mult, 6)
    except:
        return default

def extract_str(obj, *keys, default='N/A'):
    try:
        v = obj
        for k in keys:
            v = v.get(k, {}) if isinstance(v, dict) else {}
        if isinstance(v, dict):
            return v.get('longFmt') or v.get('fmt') or str(v.get('raw','')) or default
        return str(v) if v else default
    except:
        return default

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        data = get_full_data(ticker)
        
        if not data:
            return jsonify({'error': f'Could not fetch "{ticker}". Yahoo Finance may be rate limiting — try again in 30 seconds.'}), 503

        summary = data.get('summary', {})
        quote   = data.get('quote', {})
        chart   = data.get('chart', {})

        price_mod  = summary.get('price', {})
        sum_detail = summary.get('summaryDetail', {})
        key_stats  = summary.get('defaultKeyStatistics', {})
        fin_data   = summary.get('financialData', {})
        inc_stmts  = summary.get('incomeStatementHistory', {}).get('incomeStatementHistory', [])
        rec_trend  = summary.get('recommendationTrend', {}).get('trend', [])

        # ── Price (try multiple sources) ────────────────────
        price = (extract_val(price_mod, 'regularMarketPrice') or
                 quote.get('regularMarketPrice') or
                 (chart.get('meta', {}).get('regularMarketPrice') if chart else 0))
        
        if not price or price == 0:
            return jsonify({'error': f'No price data for "{ticker}". Check the ticker symbol.'}), 404

        prev_close = (extract_val(price_mod, 'regularMarketPreviousClose') or
                      quote.get('regularMarketPreviousClose') or price)
        change     = round(price - prev_close, 2)
        change_pct = round((change/prev_close*100) if prev_close else 0, 2)

        name     = (extract_str(price_mod, 'longName') or quote.get('longName') or
                    extract_str(price_mod, 'shortName') or quote.get('shortName') or ticker)
        sector   = extract_str(price_mod, 'sector') or quote.get('sector', 'N/A')
        industry = quote.get('industry', 'N/A')
        exchange = (extract_str(price_mod, 'exchangeName') or quote.get('exchange', ''))
        mkt_cap  = (extract_val(price_mod, 'marketCap') or quote.get('marketCap', 0))

        # ── Valuation ───────────────────────────────────────
        pe_ratio   = (extract_val(sum_detail, 'trailingPE') or quote.get('trailingPE', 0))
        fwd_pe     = (extract_val(key_stats, 'forwardPE') or quote.get('forwardPE', 0))
        peg        = extract_val(key_stats, 'pegRatio')
        price_book = extract_val(key_stats, 'priceToBook')
        eps        = extract_val(key_stats, 'trailingEps')
        w52_high   = (extract_val(sum_detail, 'fiftyTwoWeekHigh') or quote.get('fiftyTwoWeekHigh', 0))
        w52_low    = (extract_val(sum_detail, 'fiftyTwoWeekLow') or quote.get('fiftyTwoWeekLow', 0))
        beta       = (extract_val(sum_detail, 'beta') or quote.get('beta', 1) or 1)
        dividend   = (extract_val(sum_detail, 'dividendRate') or quote.get('dividendRate', 0))
        div_yield  = (extract_val(sum_detail, 'dividendYield', mult=100) or (quote.get('dividendYield', 0) or 0) * 100)
        analyst_tgt= (extract_val(fin_data, 'targetMeanPrice') or quote.get('targetMeanPrice', 0))

        # ── Profitability ───────────────────────────────────
        gross_margin= extract_val(fin_data, 'grossMargins',    mult=100)
        op_margin   = extract_val(fin_data, 'operatingMargins',mult=100)
        net_margin  = extract_val(fin_data, 'profitMargins',   mult=100)
        roe         = extract_val(fin_data, 'returnOnEquity',  mult=100)
        roa         = extract_val(fin_data, 'returnOnAssets',  mult=100)
        roic        = round(roa * 1.4, 1)
        rev_growth  = extract_val(fin_data, 'revenueGrowth',   mult=100)
        earn_growth = extract_val(fin_data, 'earningsGrowth',  mult=100)

        # ── Balance sheet ───────────────────────────────────
        debt_equity = round(extract_val(fin_data, 'debtToEquity') / 100, 2)
        curr_ratio  = extract_val(fin_data, 'currentRatio')
        quick_ratio = extract_val(fin_data, 'quickRatio')
        total_cash  = extract_val(fin_data, 'totalCash')
        total_debt  = extract_val(fin_data, 'totalDebt')

        # ── Cash flow ───────────────────────────────────────
        fcf         = extract_val(fin_data, 'freeCashflow')
        op_cf       = extract_val(fin_data, 'operatingCashflow')
        fcf_yield   = round(fcf/mkt_cap*100, 2) if mkt_cap and fcf else 0

        # ── Ownership ───────────────────────────────────────
        insider_own = extract_val(key_stats, 'heldPercentInsiders',     mult=100)
        inst_own    = extract_val(key_stats, 'heldPercentInstitutions', mult=100)
        short_ratio = extract_val(key_stats, 'shortRatio')

        # ── Analyst recommendations ─────────────────────────
        buy_count = hold_count = sell_count = 0
        if rec_trend:
            latest = rec_trend[0]
            buy_count  = latest.get('strongBuy',0) + latest.get('buy',0)
            hold_count = latest.get('hold',0)
            sell_count = latest.get('sell',0) + latest.get('strongSell',0)

        # ── Revenue/EPS history ─────────────────────────────
        revenue = earnings = labels = []
        try:
            if inc_stmts:
                rev_list  = [extract_val(i,'totalRevenue') for i in reversed(inc_stmts)]
                ni_list   = [extract_val(i,'netIncome')    for i in reversed(inc_stmts)]
                lbl_list  = [(i.get('endDate',{}).get('fmt',''))[:4] for i in reversed(inc_stmts)]
                revenue   = [round(v/1e9,1) for v in rev_list if v]
                earnings  = [round(v/1e9,2) for v in ni_list  if v is not None]
                labels    = lbl_list[:len(revenue)]
        except: pass

        # Revenue growth from statements if not available
        if not rev_growth and len(inc_stmts) >= 2:
            try:
                r1 = extract_val(inc_stmts[0],'totalRevenue')
                r2 = extract_val(inc_stmts[1],'totalRevenue')
                if r2: rev_growth = round((r1-r2)/r2*100,1)
            except: pass

        # ── Fair value ──────────────────────────────────────
        fair_value = round(eps*22,2) if eps > 0 else round(price*0.92,2)
        if not analyst_tgt: analyst_tgt = fair_value

        # ── Score ───────────────────────────────────────────
        score_data = calc_score(pe_ratio, rev_growth, net_margin, curr_ratio, roe, change_pct)

        print(f"[{ticker}] ${price} | PE:{round(pe_ratio,1)} | Margin:{round(net_margin,1)}% | ROE:{round(roe,1)}% | Score:{score_data['total']}")

        return jsonify({
            'ticker':        ticker,
            'name':          name,
            'sector':        sector,
            'industry':      industry,
            'mktCap':        format_cap(mkt_cap),
            'exchange':      exchange,
            'price':         round(price,2),
            'change':        change,
            'changePct':     change_pct,
            'week52High':    round(w52_high,2),
            'week52Low':     round(w52_low,2),
            'beta':          round(beta,2),
            'peRatio':       round(pe_ratio,1),
            'fwdPE':         round(fwd_pe,1),
            'peg':           round(peg,2),
            'priceBook':     round(price_book,2),
            'eps':           round(eps,2),
            'analystTarget': round(analyst_tgt,2),
            'buyCount':      buy_count,
            'holdCount':     hold_count,
            'sellCount':     sell_count,
            'grossMargin':   round(gross_margin,1),
            'opMargin':      round(op_margin,1),
            'netMargin':     round(net_margin,1),
            'roe':           round(roe,1),
            'roa':           round(roa,1),
            'roic':          round(roic,1),
            'revenueGrowth': round(rev_growth,1),
            'epsGrowth':     round(earn_growth,1),
            'debtEquity':    debt_equity,
            'currentRatio':  round(curr_ratio,2),
            'quickRatio':    round(quick_ratio,2),
            'totalCash':     format_cap(total_cash),
            'totalDebt':     format_cap(total_debt),
            'fcfYield':      fcf_yield,
            'freeCashflow':  format_cap(fcf),
            'opCashflow':    format_cap(op_cf),
            'dividend':      round(dividend,2),
            'divYield':      round(div_yield,2),
            'insiderOwn':    round(insider_own,1),
            'instOwn':       round(inst_own,1),
            'shortRatio':    round(short_ratio,2),
            'fairValue':     fair_value,
            'bull':          round(max(analyst_tgt,fair_value)*1.2,2),
            'base':          round((analyst_tgt+fair_value)/2,2),
            'bear':          round(min(analyst_tgt,fair_value)*0.8,2),
            'score':         score_data['total'],
            'grade':         score_data['grade'],
            'verdict':       score_data['verdict'],
            'style':         score_data['style'],
            'scores':        score_data['breakdown'],
            'revenue':       revenue,
            'earnings':      earnings,
            'revenueLabels': labels,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/quotes')
def get_quotes():
    tickers = request.args.get('tickers','').upper().split(',')
    tickers = [t.strip() for t in tickers if t.strip()][:10]

    def fetch_quote(ticker):
        try:
            url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&fields=regularMarketPrice,regularMarketChange,regularMarketChangePercent,regularMarketPreviousClose,trailingPE,longName,shortName&corsDomain=finance.yahoo.com'
            data = yf_get(url)
            results = data.get('quoteResponse', {}).get('result', [])
            if results:
                q = results[0]
                price = q.get('regularMarketPrice', 0)
                prev  = q.get('regularMarketPreviousClose', price)
                chg   = round(price - prev, 2)
                chgp  = round((chg/prev*100) if prev else 0, 2)
                pe    = q.get('trailingPE', 0) or 0
                score = calc_score(pe, 0, 0, 1, 0, chgp)
                return {'ticker':ticker,'name':q.get('longName',ticker),'price':round(price,2),'change':chg,'changePct':chgp,'score':score['total'],'verdict':score['verdict']}
        except: pass
        return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(fetch_quote, tickers))
    return jsonify(results)


@app.route('/api/macro')
def get_macro():
    symbols = {'sp500':'^GSPC','vix':'^VIX','gold':'GC=F','oil':'CL=F','bonds10':'^TNX','dxy':'DX-Y.NYB','btc':'BTC-USD'}
    result = {}
    
    def fetch_symbol(key, sym):
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d'
            data = yf_get(url)
            meta = data.get('chart',{}).get('result',[{}])[0].get('meta',{})
            price = meta.get('regularMarketPrice', 0)
            prev  = meta.get('chartPreviousClose', price)
            chg   = round(price - prev, 2)
            chgp  = round((chg/prev*100) if prev else 0, 2)
            result[key] = {'price':price,'change':chg,'changePct':chgp,'name':meta.get('longName',sym)}
        except:
            result[key] = {'price':0,'change':0,'changePct':0,'name':sym}

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        [ex.submit(fetch_symbol, k, v) for k,v in symbols.items()]
    return jsonify(result)


def format_cap(n):
    try:
        n = float(n)
        if n >= 1e12: return f"${n/1e12:.2f}T"
        if n >= 1e9:  return f"${n/1e9:.1f}B"
        if n >= 1e6:  return f"${n/1e6:.0f}M"
        if n > 0:     return f"${n:,.0f}"
    except: pass
    return 'N/A'

def score_metric(val, thresholds, inverse=False):
    if not val or (isinstance(val,float) and val!=val): return 50
    t1,t2,t3,t4 = thresholds
    if inverse:
        if val<=t1: return 90
        if val<=t2: return 75
        if val<=t3: return 55
        if val<=t4: return 35
        return 20
    if val>=t4: return 90
    if val>=t3: return 75
    if val>=t2: return 55
    if val>=t1: return 35
    return 20

def calc_score(pe, rev_growth, net_margin, curr_ratio, roe, change_pct):
    breakdown = {
        'valuation':     score_metric(pe,          [15,25,35,50], inverse=True),
        'growth':        score_metric(rev_growth,  [3,8,15,25]),
        'profitability': score_metric(net_margin,  [5,10,20,35]),
        'balance':       score_metric(curr_ratio,  [0.8,1.2,1.8,2.5]),
        'momentum':      score_metric(change_pct,  [-10,-2,2,10]),
        'quality':       score_metric(roe,         [5,12,25,40]),
        'macro': 68,
    }
    total   = round(sum(breakdown.values())/len(breakdown))
    grade   = ('A+' if total>=90 else 'A' if total>=82 else 'A-' if total>=75 else
               'B+' if total>=68 else 'B' if total>=60 else 'B-' if total>=52 else 'C')
    verdict = 'BUY' if total>=78 else 'HOLD' if total>=62 else 'AVOID'
    style   = ('Growth' if breakdown['growth']>80 else 'Value' if breakdown['valuation']>80
               else 'Quality Compounder' if breakdown['quality']>80 else 'Speculative')
    return {'total':total,'grade':grade,'verdict':verdict,'style':style,'breakdown':breakdown}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n◈ STOCKBOX running on port {port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
