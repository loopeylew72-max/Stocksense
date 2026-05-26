"""
◈ STOCKBOX — Railway Deployment
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, concurrent.futures, requests, json
from datetime import datetime

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Serve frontend ─────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# ── Yahoo Finance with proper headers ─────────────────────
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://finance.yahoo.com',
    'Origin': 'https://finance.yahoo.com',
}

def yahoo_fetch(ticker):
    """Fetch stock data from Yahoo Finance with proper headers"""
    url = f'https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=price,summaryDetail,defaultKeyStatistics,financialData,incomeStatementHistory'
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        result = data.get('quoteSummary', {}).get('result', [])
        if result:
            return result[0]
    except:
        pass
    # Try v8 as backup
    try:
        url2 = f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d'
        r2 = requests.get(url2, headers=HEADERS, timeout=15)
        data2 = r2.json()
        meta = data2.get('chart', {}).get('result', [{}])[0].get('meta', {})
        if meta:
            return {'_simple': True, 'meta': meta}
    except:
        pass
    return None

def get_val(obj, *keys, default=0, multiply=1):
    """Safely get nested value"""
    try:
        v = obj
        for k in keys:
            v = v.get(k, {})
        raw = v.get('raw', v) if isinstance(v, dict) else v
        return round(float(raw or 0) * multiply, 2) if raw else default
    except:
        return default

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        data = yahoo_fetch(ticker)
        
        if not data:
            return jsonify({'error': f'Could not fetch data for "{ticker}". Try again in a moment.'}), 404

        # Simple response from chart API
        if data.get('_simple'):
            meta = data['meta']
            price = meta.get('regularMarketPrice', 0)
            prev  = meta.get('chartPreviousClose', price)
            change = round(price - prev, 2)
            change_pct = round((change/prev*100) if prev else 0, 2)
            fair_value = round(price * 0.92, 2)
            score_data = calc_score(0, 0, 0, 1, 0, change_pct)
            return jsonify({
                'ticker': ticker, 'name': meta.get('longName', ticker),
                'price': round(price, 2), 'change': change, 'changePct': change_pct,
                'sector': 'N/A', 'mktCap': 'N/A', 'exchange': meta.get('exchangeName',''),
                'peRatio':0,'fwdPE':0,'peg':0,'roe':0,'roic':0,'grossMargin':0,
                'netMargin':0,'revenueGrowth':0,'epsGrowth':0,'debtEquity':0,
                'currentRatio':0,'insiderOwn':0,'instOwn':0,'dividend':0,'fcfYield':0,
                'beta':1,'week52High':meta.get('fiftyTwoWeekHigh',0),'week52Low':meta.get('fiftyTwoWeekLow',0),
                'analystTarget':round(price*1.1,2),'fairValue':fair_value,
                'bull':round(fair_value*1.25,2),'base':fair_value,'bear':round(fair_value*0.75,2),
                'score':score_data['total'],'grade':score_data['grade'],
                'verdict':score_data['verdict'],'style':score_data['style'],
                'scores':score_data['breakdown'],'revenue':[],'earnings':[],'revenueLabels':[],
                'description':'','website':'','employees':0,
            })

        # Full data from quoteSummary
        price_data = data.get('price', {})
        summary    = data.get('summaryDetail', {})
        key_stats  = data.get('defaultKeyStatistics', {})
        fin_data   = data.get('financialData', {})
        inc_hist   = data.get('incomeStatementHistory', {}).get('incomeStatementHistory', [])

        price      = get_val(price_data, 'regularMarketPrice')
        if not price:
            return jsonify({'error': f'No price data for "{ticker}"'}), 404

        prev_close  = get_val(price_data, 'regularMarketPreviousClose') or price
        change      = round(price - prev_close, 2)
        change_pct  = round((change/prev_close*100) if prev_close else 0, 2)
        pe_ratio    = get_val(summary, 'trailingPE') or get_val(price_data, 'trailingPE')
        fwd_pe      = get_val(key_stats, 'forwardPE')
        peg         = get_val(key_stats, 'pegRatio')
        gross_margin= get_val(fin_data, 'grossMargins', multiply=100)
        net_margin  = get_val(fin_data, 'profitMargins', multiply=100)
        roe         = get_val(fin_data, 'returnOnEquity', multiply=100)
        roa         = get_val(fin_data, 'returnOnAssets', multiply=100)
        roic        = round(roa * 1.4, 1)
        rev_growth  = get_val(fin_data, 'revenueGrowth', multiply=100)
        eps_growth  = get_val(fin_data, 'earningsGrowth', multiply=100)
        debt_equity = round(get_val(fin_data, 'debtToEquity') / 100, 2)
        curr_ratio  = get_val(fin_data, 'currentRatio')
        insider_own = get_val(key_stats, 'heldPercentInsiders', multiply=100)
        inst_own    = get_val(key_stats, 'heldPercentInstitutions', multiply=100)
        dividend    = get_val(summary, 'dividendRate')
        mkt_cap     = get_val(price_data, 'marketCap')
        fcf         = get_val(fin_data, 'freeCashflow')
        fcf_yield   = round(fcf/mkt_cap*100, 2) if mkt_cap else 0
        beta        = get_val(summary, 'beta') or 1
        w52_high    = get_val(summary, 'fiftyTwoWeekHigh')
        w52_low     = get_val(summary, 'fiftyTwoWeekLow')
        analyst_tgt = get_val(fin_data, 'targetMeanPrice')
        eps         = get_val(key_stats, 'trailingEps')
        fair_value  = round(eps*22, 2) if eps > 0 else round(price*0.92, 2)
        if not analyst_tgt: analyst_tgt = fair_value

        # Revenue/EPS history
        revenue = earnings = labels = []
        try:
            if inc_hist:
                revenue  = [round(get_val(i, 'totalRevenue')/1e9, 1) for i in reversed(inc_hist)]
                earnings = [round(get_val(i, 'netIncome')/1e9, 2)    for i in reversed(inc_hist)]
                labels   = [(i.get('endDate',{}).get('fmt',''))[:4]   for i in reversed(inc_hist)]
        except: pass

        score_data = calc_score(pe_ratio, rev_growth, net_margin, curr_ratio, roe, change_pct)
        print(f"[{ticker}] ${price} PE:{pe_ratio} Margin:{net_margin}% Score:{score_data['total']}")

        return jsonify({
            'ticker':        ticker,
            'name':          get_val(price_data, 'longName') or get_val(price_data, 'shortName') or ticker,
            'price':         round(price, 2),
            'change':        change,
            'changePct':     change_pct,
            'sector':        price_data.get('sector', {}).get('longFmt', '') or 'N/A',
            'industry':      'N/A',
            'mktCap':        format_cap(mkt_cap),
            'exchange':      price_data.get('exchangeName', ''),
            'peRatio':       round(pe_ratio, 1),
            'fwdPE':         round(fwd_pe, 1),
            'peg':           round(peg, 2),
            'roe':           round(roe, 1),
            'roic':          round(roic, 1),
            'grossMargin':   round(gross_margin, 1),
            'netMargin':     round(net_margin, 1),
            'revenueGrowth': round(rev_growth, 1),
            'epsGrowth':     round(eps_growth, 1),
            'debtEquity':    debt_equity,
            'currentRatio':  round(curr_ratio, 2),
            'insiderOwn':    round(insider_own, 1),
            'instOwn':       round(inst_own, 1),
            'dividend':      dividend,
            'fcfYield':      fcf_yield,
            'beta':          round(beta, 2),
            'week52High':    round(w52_high, 2),
            'week52Low':     round(w52_low, 2),
            'analystTarget': round(analyst_tgt, 2),
            'fairValue':     fair_value,
            'bull':          round(max(analyst_tgt, fair_value)*1.2, 2),
            'base':          round((analyst_tgt+fair_value)/2, 2),
            'bear':          round(min(analyst_tgt, fair_value)*0.8, 2),
            'score':         score_data['total'],
            'grade':         score_data['grade'],
            'verdict':       score_data['verdict'],
            'style':         score_data['style'],
            'scores':        score_data['breakdown'],
            'revenue':       revenue,
            'earnings':      earnings,
            'revenueLabels': labels,
            'description':   fin_data.get('longBusinessSummary', '') or '',
            'website':       fin_data.get('companyOfficers', '') or '',
            'employees':     0,
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
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d'
            r = requests.get(url, headers=HEADERS, timeout=10)
            meta = r.json().get('chart',{}).get('result',[{}])[0].get('meta',{})
            price = meta.get('regularMarketPrice', 0)
            prev  = meta.get('chartPreviousClose', price)
            chg   = round(price-prev, 2)
            chgp  = round((chg/prev*100) if prev else 0, 2)
            score = calc_score(0, 0, 0, 1, 0, chgp)
            return {'ticker':ticker,'name':meta.get('longName',ticker),'price':round(price,2),'change':chg,'changePct':chgp,'score':score['total'],'verdict':score['verdict']}
        except:
            return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(fetch, tickers))
    return jsonify(results)


def format_cap(n):
    try:
        if n >= 1e12: return f"${n/1e12:.2f}T"
        if n >= 1e9:  return f"${n/1e9:.1f}B"
        if n >= 1e6:  return f"${n/1e6:.0f}M"
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
