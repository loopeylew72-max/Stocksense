"""
◈ STOCKBOX — Railway Deployment (Full Data Edition)
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, concurrent.futures, requests, json

app = Flask(__name__, static_folder='.')
CORS(app)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

def yf_quote(ticker):
    """Get full quote data"""
    try:
        url = f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&fields=regularMarketPrice,regularMarketChange,regularMarketChangePercent,regularMarketPreviousClose,marketCap,trailingPE,forwardPE,prailingEps,bookValue,fiftyTwoWeekHigh,fiftyTwoWeekLow,beta,dividendRate,dividendYield,averageAnalystRating,targetMeanPrice,longName,shortName,sector,industry,exchange'
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        results = data.get('quoteResponse', {}).get('result', [])
        return results[0] if results else {}
    except:
        return {}

def yf_summary(ticker):
    """Get detailed financial summary"""
    try:
        modules = 'summaryDetail,defaultKeyStatistics,financialData,incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory,calendarEvents,recommendationTrend'
        url = f'https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}'
        r = requests.get(url, headers=HEADERS, timeout=20)
        result = r.json().get('quoteSummary', {}).get('result', [])
        return result[0] if result else {}
    except:
        return {}

def yf_chart(ticker):
    """Get price history for charts"""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1mo&range=5y'
        r = requests.get(url, headers=HEADERS, timeout=15)
        return r.json().get('chart', {}).get('result', [{}])[0]
    except:
        return {}

def raw(obj, *keys, mult=1, default=0):
    """Extract raw value from Yahoo Finance nested dict"""
    try:
        v = obj
        for k in keys:
            if isinstance(v, dict):
                v = v.get(k, {})
            else:
                return default
        if isinstance(v, dict):
            val = v.get('raw', v.get('fmt', default))
        else:
            val = v
        return round(float(val or 0) * mult, 4) if val not in (None, '', {}) else default
    except:
        return default

def strval(obj, *keys, default='N/A'):
    try:
        v = obj
        for k in keys:
            v = v.get(k, {})
        if isinstance(v, dict):
            return v.get('longFmt') or v.get('fmt') or str(v.get('raw','')) or default
        return str(v) if v else default
    except:
        return default

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        # Fetch in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_quote   = ex.submit(yf_quote, ticker)
            f_summary = ex.submit(yf_summary, ticker)
            f_chart   = ex.submit(yf_chart, ticker)
        
        quote   = f_quote.result()
        summary = f_summary.result()
        chart   = f_chart.result()

        # Unpack summary modules
        sum_detail = summary.get('summaryDetail', {})
        key_stats  = summary.get('defaultKeyStatistics', {})
        fin_data   = summary.get('financialData', {})
        inc_stmts  = summary.get('incomeStatementHistory', {}).get('incomeStatementHistory', [])
        bal_sheets = summary.get('balanceSheetHistory', {}).get('balanceSheetStatements', [])
        cf_stmts   = summary.get('cashflowStatementHistory', {}).get('cashflowStatements', [])
        rec_trend  = summary.get('recommendationTrend', {}).get('trend', [])

        # ── Price ──────────────────────────────────────────
        price       = quote.get('regularMarketPrice') or raw(sum_detail, 'regularMarketPrice')
        if not price:
            return jsonify({'error': f'Ticker "{ticker}" not found or market closed.'}), 404

        prev_close  = quote.get('regularMarketPreviousClose') or price
        change      = round(price - prev_close, 2)
        change_pct  = round((change/prev_close*100) if prev_close else 0, 2)
        mkt_cap     = quote.get('marketCap') or raw(sum_detail, 'marketCap')
        name        = quote.get('longName') or quote.get('shortName') or ticker
        sector      = quote.get('sector') or 'N/A'
        industry    = quote.get('industry') or 'N/A'
        exchange    = quote.get('exchange') or ''

        # ── Valuation ──────────────────────────────────────
        pe_ratio    = quote.get('trailingPE')  or raw(sum_detail, 'trailingPE')
        fwd_pe      = quote.get('forwardPE')   or raw(key_stats, 'forwardPE')
        peg         = raw(key_stats, 'pegRatio')
        price_book  = raw(key_stats, 'priceToBook')
        eps         = raw(key_stats, 'trailingEps')
        book_val    = quote.get('bookValue') or raw(key_stats, 'bookValue')
        w52_high    = quote.get('fiftyTwoWeekHigh')  or raw(sum_detail, 'fiftyTwoWeekHigh')
        w52_low     = quote.get('fiftyTwoWeekLow')   or raw(sum_detail, 'fiftyTwoWeekLow')
        beta        = quote.get('beta') or raw(sum_detail, 'beta') or 1
        dividend    = quote.get('dividendRate') or raw(sum_detail, 'dividendRate')
        div_yield   = quote.get('dividendYield') or raw(sum_detail, 'dividendYield', mult=100)
        analyst_tgt = raw(fin_data, 'targetMeanPrice')

        # ── Profitability ──────────────────────────────────
        gross_margin = raw(fin_data, 'grossMargins',   mult=100)
        net_margin   = raw(fin_data, 'profitMargins',  mult=100)
        op_margin    = raw(fin_data, 'operatingMargins',mult=100)
        roe          = raw(fin_data, 'returnOnEquity', mult=100)
        roa          = raw(fin_data, 'returnOnAssets', mult=100)
        roic         = round(roa * 1.4, 1)
        rev_growth   = raw(fin_data, 'revenueGrowth',  mult=100)
        earn_growth  = raw(fin_data, 'earningsGrowth', mult=100)

        # ── Balance sheet ──────────────────────────────────
        debt_equity  = round(raw(fin_data, 'debtToEquity') / 100, 2)
        curr_ratio   = raw(fin_data, 'currentRatio')
        quick_ratio  = raw(fin_data, 'quickRatio')
        total_cash   = raw(fin_data, 'totalCash')
        total_debt   = raw(fin_data, 'totalDebt')

        # ── Cash flow ──────────────────────────────────────
        fcf          = raw(fin_data, 'freeCashflow')
        op_cf        = raw(fin_data, 'operatingCashflow')
        fcf_yield    = round(fcf/mkt_cap*100, 2) if mkt_cap and fcf else 0

        # ── Ownership ──────────────────────────────────────
        insider_own  = raw(key_stats, 'heldPercentInsiders',     mult=100)
        inst_own     = raw(key_stats, 'heldPercentInstitutions', mult=100)
        short_ratio  = raw(key_stats, 'shortRatio')

        # ── Analyst ────────────────────────────────────────
        analyst_rating = quote.get('averageAnalystRating', '')
        buy_count = hold_count = sell_count = 0
        if rec_trend:
            latest = rec_trend[0]
            buy_count  = latest.get('strongBuy',0) + latest.get('buy',0)
            hold_count = latest.get('hold',0)
            sell_count = latest.get('sell',0) + latest.get('strongSell',0)

        # ── Revenue/EPS history from income statements ─────
        revenue = earnings = labels = []
        eps_history = []
        try:
            if inc_stmts:
                inc_rev  = [raw(i,'totalRevenue') for i in reversed(inc_stmts)]
                inc_ni   = [raw(i,'netIncome')    for i in reversed(inc_stmts)]
                inc_lbl  = [(i.get('endDate',{}).get('fmt',''))[:4] for i in reversed(inc_stmts)]
                revenue  = [round(v/1e9,1) for v in inc_rev]
                earnings = [round(v/1e9,2) for v in inc_ni]
                labels   = inc_lbl
        except: pass

        # ── Revenue growth from statements ─────────────────
        if len(inc_stmts) >= 2 and not rev_growth:
            try:
                r1 = raw(inc_stmts[0],'totalRevenue')
                r2 = raw(inc_stmts[1],'totalRevenue')
                if r2: rev_growth = round((r1-r2)/r2*100,1)
            except: pass

        # ── Fair value ─────────────────────────────────────
        fair_value = round(eps*22,2) if eps > 0 else round(price*0.92,2)
        if not analyst_tgt: analyst_tgt = fair_value

        # ── Score ──────────────────────────────────────────
        score_data = calc_score(pe_ratio, rev_growth, net_margin, curr_ratio, roe, change_pct)

        print(f"[{ticker}] ${price} | PE:{round(pe_ratio,1)} | Margin:{round(net_margin,1)}% | ROE:{round(roe,1)}% | Score:{score_data['total']}")

        return jsonify({
            # Identity
            'ticker':        ticker,
            'name':          name,
            'sector':        sector,
            'industry':      industry,
            'mktCap':        format_cap(mkt_cap),
            'exchange':      exchange,
            # Price
            'price':         round(price,2),
            'change':        change,
            'changePct':     change_pct,
            'week52High':    round(w52_high,2),
            'week52Low':     round(w52_low,2),
            'beta':          round(beta,2),
            # Valuation
            'peRatio':       round(pe_ratio,1),
            'fwdPE':         round(fwd_pe,1),
            'peg':           round(peg,2),
            'priceBook':     round(price_book,2),
            'eps':           round(eps,2),
            'analystTarget': round(analyst_tgt,2),
            'analystRating': analyst_rating,
            'buyCount':      buy_count,
            'holdCount':     hold_count,
            'sellCount':     sell_count,
            # Profitability
            'grossMargin':   round(gross_margin,1),
            'opMargin':      round(op_margin,1),
            'netMargin':     round(net_margin,1),
            'roe':           round(roe,1),
            'roa':           round(roa,1),
            'roic':          round(roic,1),
            # Growth
            'revenueGrowth': round(rev_growth,1),
            'epsGrowth':     round(earn_growth,1),
            # Balance sheet
            'debtEquity':    debt_equity,
            'currentRatio':  round(curr_ratio,2),
            'quickRatio':    round(quick_ratio,2),
            'totalCash':     format_cap(total_cash),
            'totalDebt':     format_cap(total_debt),
            # Cash flow
            'fcfYield':      fcf_yield,
            'freeCashflow':  format_cap(fcf),
            'opCashflow':    format_cap(op_cf),
            # Dividends
            'dividend':      round(dividend,2),
            'divYield':      round(div_yield,2),
            # Ownership
            'insiderOwn':    round(insider_own,1),
            'instOwn':       round(inst_own,1),
            'shortRatio':    round(short_ratio,2),
            # Fair value
            'fairValue':     fair_value,
            'bull':          round(max(analyst_tgt,fair_value)*1.2,2),
            'base':          round((analyst_tgt+fair_value)/2,2),
            'bear':          round(min(analyst_tgt,fair_value)*0.8,2),
            # Score
            'score':         score_data['total'],
            'grade':         score_data['grade'],
            'verdict':       score_data['verdict'],
            'style':         score_data['style'],
            'scores':        score_data['breakdown'],
            # Charts
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
    tickers = [t.strip() for t in tickers if t.strip()]
    def fetch(ticker):
        try:
            q = yf_quote(ticker)
            price = q.get('regularMarketPrice',0)
            prev  = q.get('regularMarketPreviousClose', price)
            chg   = round(price-prev,2)
            chgp  = round((chg/prev*100) if prev else 0, 2)
            pe    = q.get('trailingPE',0) or 0
            score = calc_score(pe,0,0,1,0,chgp)
            return {'ticker':ticker,'name':q.get('longName',ticker),'price':round(price,2),'change':chg,'changePct':chgp,'score':score['total'],'verdict':score['verdict']}
        except:
            return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch, tickers))
    return jsonify(results)


@app.route('/api/macro')
def get_macro():
    """Fetch live macro data"""
    tickers = {
        'sp500':  '^GSPC',
        'dxy':    'DX-Y.NYB',
        'gold':   'GC=F',
        'oil':    'CL=F',
        'vix':    '^VIX',
        'bonds10':'^TNX',
        'bonds2': '^IRX',
        'btc':    'BTC-USD',
    }
    results = {}
    def fetch(key, symbol):
        try:
            q = yf_quote(symbol)
            results[key] = {
                'price': q.get('regularMarketPrice',0),
                'change': q.get('regularMarketChange',0),
                'changePct': q.get('regularMarketChangePercent',0),
                'name': q.get('shortName', symbol),
            }
        except:
            results[key] = {'price':0,'change':0,'changePct':0,'name':symbol}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        [ex.submit(fetch,k,v) for k,v in tickers.items()]
    return jsonify(results)


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

