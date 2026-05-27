"""
◈ STOCKBOX — Railway Deployment
Uses yfinance with curl_cffi to bypass Yahoo Finance bot detection
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, concurrent.futures

app = Flask(__name__, static_folder='.')
CORS(app)

# Use curl_cffi session to impersonate Chrome — bypasses Yahoo bot detection
try:
    from curl_cffi import requests as curl_requests
    CURL_SESSION = curl_requests.Session(impersonate="chrome120")
    USE_CURL = True
    print("✓ curl_cffi available — using Chrome impersonation")
except ImportError:
    import requests
    CURL_SESSION = requests.Session()
    USE_CURL = False
    print("⚠ curl_cffi not available — using standard requests")

import yfinance as yf

def get_ticker_data(ticker):
    """Get comprehensive stock data using yfinance"""
    if USE_CURL:
        # Patch yfinance to use curl_cffi session
        import yfinance.utils as yf_utils
        original_get = yf_utils.requests.get
        def patched_get(url, *args, **kwargs):
            try:
                return CURL_SESSION.get(url, impersonate="chrome120", timeout=20)
            except:
                return original_get(url, *args, **kwargs)
        yf_utils.requests.get = patched_get

    t = yf.Ticker(ticker)
    info = t.info

    if USE_CURL:
        import yfinance.utils as yf_utils
        yf_utils.requests.get = original_get

    return t, info

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    ticker = ticker.upper().strip()
    try:
        t, info = get_ticker_data(ticker)

        price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        if not price:
            return jsonify({'error': f'No data for "{ticker}". Check the ticker symbol or try again.'}), 404

        prev       = info.get('previousClose') or info.get('regularMarketPreviousClose') or price
        change     = round(price - prev, 2)
        change_pct = round((change/prev*100) if prev else 0, 2)
        mkt_cap    = info.get('marketCap') or 0

        # Profitability
        gross_m = round((info.get('grossMargins')    or 0)*100, 1)
        op_m    = round((info.get('operatingMargins') or 0)*100, 1)
        net_m   = round((info.get('profitMargins')    or 0)*100, 1)
        roe     = round((info.get('returnOnEquity')   or 0)*100, 1)
        roa     = round((info.get('returnOnAssets')   or 0)*100, 1)
        roic    = round(roa*1.4, 1)
        rev_g   = round((info.get('revenueGrowth')    or 0)*100, 1)
        earn_g  = round((info.get('earningsGrowth')   or 0)*100, 1)

        # Valuation
        pe      = round(info.get('trailingPE')    or 0, 1)
        fwd_pe  = round(info.get('forwardPE')     or 0, 1)
        peg     = round(info.get('pegRatio')       or 0, 2)
        pb      = round(info.get('priceToBook')    or 0, 2)
        eps     = round(info.get('trailingEps')    or 0, 2)
        w52hi   = info.get('fiftyTwoWeekHigh')     or 0
        w52lo   = info.get('fiftyTwoWeekLow')      or 0
        beta    = info.get('beta')                 or 1
        div     = info.get('dividendRate')         or 0
        div_y   = round((info.get('dividendYield') or 0)*100, 2)
        tgt     = info.get('targetMeanPrice')      or 0

        # Balance sheet
        de      = round((info.get('debtToEquity')  or 0)/100, 2)
        cr      = round(info.get('currentRatio')   or 0, 2)
        qr      = round(info.get('quickRatio')     or 0, 2)
        cash    = info.get('totalCash')            or 0
        debt    = info.get('totalDebt')            or 0

        # Cash flow
        fcf     = info.get('freeCashflow')         or 0
        ocf     = info.get('operatingCashflow')    or 0
        fcf_y   = round(fcf/mkt_cap*100, 2) if mkt_cap and fcf else 0

        # Ownership
        ins_own = round((info.get('heldPercentInsiders')      or 0)*100, 1)
        inst_ow = round((info.get('heldPercentInstitutions')  or 0)*100, 1)
        short_r = round(info.get('shortRatio') or 0, 2)

        # Revenue/earnings history
        revenue = earnings = labels = []
        try:
            fin = t.financials
            if fin is not None and not fin.empty:
                cols = [str(c)[:4] for c in fin.columns][::-1]
                rv   = fin.loc['Total Revenue'] if 'Total Revenue' in fin.index else None
                ni   = fin.loc['Net Income']    if 'Net Income'    in fin.index else None
                revenue  = [round(v/1e9,1) for v in (rv.values[::-1] if rv is not None else [])]
                earnings = [round(v/1e9,2) for v in (ni.values[::-1] if ni is not None else [])]
                labels   = cols[:len(revenue)]
        except: pass

        # Revenue growth from history
        if not rev_g and len(revenue) >= 2:
            try:
                r1,r2 = revenue[-1], revenue[-2]
                if r2: rev_g = round((r1-r2)/abs(r2)*100, 1)
            except: pass

        # Fair value
        fv  = round(eps*22, 2) if eps > 0 else round(price*0.92, 2)
        if not tgt: tgt = fv

        sc = calc_score(pe, rev_g, net_m, cr, roe, change_pct)

        print(f"[{ticker}] ${price} | PE:{pe} | Margin:{net_m}% | ROE:{roe}% | Score:{sc['total']}")

        return jsonify({
            'ticker':ticker,
            'name': info.get('longName') or info.get('shortName') or ticker,
            'sector': info.get('sector') or 'N/A',
            'industry': info.get('industry') or 'N/A',
            'mktCap': fmt(mkt_cap),
            'exchange': info.get('exchange') or '',
            'price': round(price,2), 'change': change, 'changePct': change_pct,
            'week52High': round(w52hi,2), 'week52Low': round(w52lo,2), 'beta': round(beta,2),
            'peRatio': pe, 'fwdPE': fwd_pe, 'peg': peg, 'priceBook': pb, 'eps': eps,
            'analystTarget': round(tgt,2),
            'buyCount': info.get('numberOfAnalystOpinions') or 0,
            'holdCount': 0, 'sellCount': 0,
            'grossMargin': gross_m, 'opMargin': op_m, 'netMargin': net_m,
            'roe': roe, 'roa': roa, 'roic': roic,
            'revenueGrowth': rev_g, 'epsGrowth': earn_g,
            'debtEquity': de, 'currentRatio': cr, 'quickRatio': qr,
            'totalCash': fmt(cash), 'totalDebt': fmt(debt),
            'fcfYield': fcf_y, 'freeCashflow': fmt(fcf), 'opCashflow': fmt(ocf),
            'dividend': round(div,2), 'divYield': div_y,
            'insiderOwn': ins_own, 'instOwn': inst_ow, 'shortRatio': short_r,
            'fairValue': fv,
            'bull': round(max(tgt,fv)*1.2, 2),
            'base': round((tgt+fv)/2, 2),
            'bear': round(min(tgt,fv)*0.8, 2),
            'score': sc['total'], 'grade': sc['grade'],
            'verdict': sc['verdict'], 'style': sc['style'], 'scores': sc['breakdown'],
            'revenue': revenue, 'earnings': earnings, 'revenueLabels': labels,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/quotes')
def get_quotes():
    tickers = [t.strip() for t in request.args.get('tickers','').upper().split(',') if t.strip()][:10]
    def fetch(ticker):
        try:
            info = yf.Ticker(ticker).info
            price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
            prev  = info.get('previousClose') or price
            chg   = round(price-prev, 2)
            chgp  = round((chg/prev*100) if prev else 0, 2)
            pe    = info.get('trailingPE') or 0
            sc    = calc_score(pe,0,0,1,0,chgp)
            return {'ticker':ticker,'name':info.get('longName',ticker),'price':round(price,2),'change':chg,'changePct':chgp,'score':sc['total'],'verdict':sc['verdict']}
        except:
            return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(fetch, tickers))
    return jsonify(results)


@app.route('/api/macro')
def get_macro():
    syms = {'sp500':'^GSPC','vix':'^VIX','gold':'GC=F','oil':'CL=F','bonds10':'^TNX','dxy':'DX-Y.NYB','btc':'BTC-USD'}
    result = {}
    def fetch(key, sym):
        try:
            info = yf.Ticker(sym).fast_info
            price = info.last_price or 0
            prev  = info.previous_close or price
            chg   = round(price-prev, 2)
            chgp  = round((chg/prev*100) if prev else 0, 2)
            result[key] = {'price': round(price,2), 'change': chg, 'changePct': chgp}
        except:
            result[key] = {'price':0,'change':0,'changePct':0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        [ex.submit(fetch,k,v) for k,v in syms.items()]
    return jsonify(result)


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
    print(f"\n◈ STOCKBOX on port {port}\n")
    app.run(host='0.0.0.0',port=port,debug=False)
