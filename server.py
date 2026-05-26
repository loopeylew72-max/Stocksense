"""
◈ STOCKBOX — Railway Deployment
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yfinance as yf
import os, concurrent.futures

app = Flask(__name__, static_folder='static')
CORS(app)

# FMP key from environment variable (set in Railway dashboard)
FMP_KEY  = os.environ.get('FMP_KEY', '')
FMP_BASE = 'https://financialmodelingprep.com/api'

# ── Serve frontend ─────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# ── Stock data ─────────────────────────────────────────────
@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        # Try yfinance first — always free, no CORS issues server-side
        t    = yf.Ticker(ticker)
        info = t.info

        price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        if not price:
            return jsonify({'error': f'Ticker "{ticker}" not found. Check the symbol and try again.'}), 404

        prev_close   = info.get('previousClose') or price
        change       = round(price - prev_close, 2)
        change_pct   = round((change / prev_close * 100) if prev_close else 0, 2)
        pe_ratio     = round(info.get('trailingPE')     or 0, 1)
        fwd_pe       = round(info.get('forwardPE')      or 0, 1)
        peg          = round(info.get('pegRatio')        or 0, 2)
        gross_margin = round((info.get('grossMargins')   or 0) * 100, 1)
        net_margin   = round((info.get('profitMargins')  or 0) * 100, 1)
        roe          = round((info.get('returnOnEquity') or 0) * 100, 1)
        roa          = round((info.get('returnOnAssets') or 0) * 100, 1)
        roic         = round(roa * 1.4, 1)
        rev_growth   = round((info.get('revenueGrowth')  or 0) * 100, 1)
        eps_growth   = round((info.get('earningsGrowth') or 0) * 100, 1)
        debt_equity  = round((info.get('debtToEquity')   or 0) / 100, 2)
        curr_ratio   = round(info.get('currentRatio')    or 0, 2)
        insider_own  = round((info.get('heldPercentInsiders')     or 0) * 100, 1)
        inst_own     = round((info.get('heldPercentInstitutions') or 0) * 100, 1)
        dividend     = info.get('dividendRate') or 0
        mkt_cap      = info.get('marketCap') or 1
        fcf          = info.get('freeCashflow') or 0
        fcf_yield    = round(fcf / mkt_cap * 100, 2) if mkt_cap else 0
        eps          = info.get('trailingEps') or 0
        fair_value   = round(eps * 22, 2) if eps > 0 else round(price * 0.92, 2)
        week52_high  = info.get('fiftyTwoWeekHigh') or 0
        week52_low   = info.get('fiftyTwoWeekLow') or 0
        beta         = info.get('beta') or 1
        analyst_target = info.get('targetMeanPrice') or fair_value

        # Revenue/EPS history
        try:
            hist     = t.financials
            if hist is not None and not hist.empty:
                cols     = [str(c)[:4] for c in hist.columns][::-1]
                rev_row  = hist.loc['Total Revenue'] if 'Total Revenue' in hist.index else None
                inc_row  = hist.loc['Net Income']    if 'Net Income'    in hist.index else None
                revenue  = [round((v or 0)/1e9, 1) for v in (rev_row.values[::-1] if rev_row is not None else [])]
                earnings = [round((v or 0)/1e9, 2) for v in (inc_row.values[::-1] if inc_row is not None else [])]
                labels   = cols
            else:
                revenue = earnings = labels = []
        except:
            revenue = earnings = labels = []

        score_data = calc_score(pe_ratio, rev_growth, net_margin, curr_ratio, roe, change_pct)

        print(f"[{ticker}] ${price} PE:{pe_ratio} Margin:{net_margin}% Score:{score_data['total']}")

        return jsonify({
            'ticker':        ticker,
            'name':          info.get('longName') or info.get('shortName') or ticker,
            'price':         round(price, 2),
            'change':        change,
            'changePct':     change_pct,
            'sector':        info.get('sector') or 'N/A',
            'industry':      info.get('industry') or 'N/A',
            'mktCap':        format_cap(mkt_cap),
            'exchange':      info.get('exchange') or '',
            'peRatio':       pe_ratio,
            'fwdPE':         fwd_pe,
            'peg':           peg,
            'roe':           roe,
            'roic':          roic,
            'grossMargin':   gross_margin,
            'netMargin':     net_margin,
            'revenueGrowth': rev_growth,
            'epsGrowth':     eps_growth,
            'debtEquity':    debt_equity,
            'currentRatio':  curr_ratio,
            'insiderOwn':    insider_own,
            'instOwn':       inst_own,
            'dividend':      dividend,
            'fcfYield':      fcf_yield,
            'beta':          round(beta, 2),
            'week52High':    round(week52_high, 2),
            'week52Low':     round(week52_low, 2),
            'analystTarget': round(analyst_target, 2),
            'fairValue':     fair_value,
            'bull':          round(max(analyst_target, fair_value) * 1.2, 2),
            'base':          round((analyst_target + fair_value) / 2, 2),
            'bear':          round(min(analyst_target, fair_value) * 0.8, 2),
            'score':         score_data['total'],
            'grade':         score_data['grade'],
            'verdict':       score_data['verdict'],
            'style':         score_data['style'],
            'scores':        score_data['breakdown'],
            'revenue':       revenue,
            'earnings':      earnings,
            'revenueLabels': labels,
            'description':   (info.get('longBusinessSummary') or '')[:500],
            'website':       info.get('website') or '',
            'employees':     info.get('fullTimeEmployees') or 0,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Watchlist batch quotes ─────────────────────────────────
@app.route('/api/quotes')
def get_quotes():
    tickers = request.args.get('tickers', '').upper().split(',')
    tickers = [t.strip() for t in tickers if t.strip()]
    results = []
    def fetch(ticker):
        try:
            info  = yf.Ticker(ticker).info
            price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
            prev  = info.get('previousClose') or price
            chg   = round(price - prev, 2)
            chgp  = round((chg / prev * 100) if prev else 0, 2)
            pe    = round(info.get('trailingPE') or 0, 1)
            score = calc_score(pe, 0, round((info.get('profitMargins') or 0)*100,1), info.get('currentRatio') or 1, round((info.get('returnOnEquity') or 0)*100,1), chgp)
            return {'ticker': ticker, 'name': info.get('shortName') or ticker, 'price': round(price,2), 'change': chg, 'changePct': chgp, 'score': score['total'], 'verdict': score['verdict']}
        except:
            return {'ticker': ticker, 'name': ticker, 'price': 0, 'change': 0, 'changePct': 0, 'score': 50, 'verdict': 'HOLD'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch, tickers))
    return jsonify(results)


# ── Helpers ────────────────────────────────────────────────
def format_cap(n):
    try:
        if n >= 1e12: return f"${n/1e12:.2f}T"
        if n >= 1e9:  return f"${n/1e9:.1f}B"
        if n >= 1e6:  return f"${n/1e6:.0f}M"
    except: pass
    return 'N/A'

def score_metric(val, thresholds, inverse=False):
    if not val or (isinstance(val, float) and val != val): return 50
    t1, t2, t3, t4 = thresholds
    if inverse:
        if val <= t1: return 90
        if val <= t2: return 75
        if val <= t3: return 55
        if val <= t4: return 35
        return 20
    if val >= t4: return 90
    if val >= t3: return 75
    if val >= t2: return 55
    if val >= t1: return 35
    return 20

def calc_score(pe, rev_growth, net_margin, curr_ratio, roe, change_pct):
    breakdown = {
        'valuation':     score_metric(pe,          [15, 25, 35, 50], inverse=True),
        'growth':        score_metric(rev_growth,  [3, 8, 15, 25]),
        'profitability': score_metric(net_margin,  [5, 10, 20, 35]),
        'balance':       score_metric(curr_ratio,  [0.8, 1.2, 1.8, 2.5]),
        'momentum':      score_metric(change_pct,  [-10, -2, 2, 10]),
        'quality':       score_metric(roe,         [5, 12, 25, 40]),
        'macro': 68,
    }
    total   = round(sum(breakdown.values()) / len(breakdown))
    grade   = ('A+' if total>=90 else 'A' if total>=82 else 'A-' if total>=75 else
               'B+' if total>=68 else 'B' if total>=60 else 'B-' if total>=52 else 'C')
    verdict = 'BUY' if total>=78 else 'HOLD' if total>=62 else 'AVOID'
    style   = ('Growth'             if breakdown['growth'] > 80      else
               'Value'              if breakdown['valuation'] > 80   else
               'Quality Compounder' if breakdown['quality'] > 80     else
               'Dividend'           if breakdown.get('profitability', 0) > 75 else 'Speculative')
    return {'total': total, 'grade': grade, 'verdict': verdict, 'style': style, 'breakdown': breakdown}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n◈ STOCKBOX running on port {port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
