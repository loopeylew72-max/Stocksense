"""
◈ STOCKBOX — Railway Deployment
Robust data extraction from Yahoo Finance
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os, concurrent.futures, requests, time, random

app = Flask(__name__, static_folder='.')
CORS(app)

SESSION = requests.Session()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://finance.yahoo.com/',
    'Origin': 'https://finance.yahoo.com',
}

def yf_get(url, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            time.sleep(1.5 ** i)
        except Exception as e:
            time.sleep(1)
    return {}

def r(obj, *keys, default=0, pct=False):
    """Extract raw numeric value from Yahoo Finance response"""
    try:
        v = obj
        for k in keys:
            if not isinstance(v, dict):
                return default
            v = v.get(k, {})
        # Handle Yahoo's {raw: X, fmt: "X%"} format
        if isinstance(v, dict):
            val = v.get('raw')
            if val is None:
                return default
        else:
            val = v
        if val in (None, '', 'N/A', 'None', {}):
            return default
        result = float(val)
        if pct:
            result = result * 100
        return result
    except:
        return default

def s(obj, *keys, default='N/A'):
    """Extract string value"""
    try:
        v = obj
        for k in keys:
            if not isinstance(v, dict):
                return default
            v = v.get(k, {})
        if isinstance(v, dict):
            return v.get('longFmt') or v.get('fmt') or str(v.get('raw', default))
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
        # Fetch both endpoints in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(yf_get, f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=price%2CsummaryDetail%2CdefaultKeyStatistics%2CfinancialData%2CincomeStatementHistory%2CrecommendationTrend&formatted=true&corsDomain=finance.yahoo.com')
            f2 = ex.submit(yf_get, f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&corsDomain=finance.yahoo.com&formatted=false')

        data1 = f1.result()
        data2 = f2.result()

        # Parse quoteSummary
        qs_result = data1.get('quoteSummary', {}).get('result', [])
        qs = qs_result[0] if qs_result else {}

        # Parse quote
        qt_result = data2.get('quoteResponse', {}).get('result', [])
        qt = qt_result[0] if qt_result else {}

        # Module shortcuts
        pr = qs.get('price', {})
        sd = qs.get('summaryDetail', {})
        ks = qs.get('defaultKeyStatistics', {})
        fd = qs.get('financialData', {})
        ih = qs.get('incomeStatementHistory', {}).get('incomeStatementHistory', [])
        rt = qs.get('recommendationTrend', {}).get('trend', [])

        # ── Price ──────────────────────────────────────────
        price = r(pr,'regularMarketPrice') or qt.get('regularMarketPrice', 0)
        if not price:
            return jsonify({'error': f'No data for "{ticker}". Check the ticker symbol.'}), 404

        prev      = r(pr,'regularMarketPreviousClose') or qt.get('regularMarketPreviousClose', price)
        change    = round(price - prev, 2)
        change_pct= round((change/prev*100) if prev else 0, 2)
        mkt_cap   = r(pr,'marketCap') or qt.get('marketCap', 0)

        # ── Identity ───────────────────────────────────────
        name     = s(pr,'longName') or qt.get('longName') or s(pr,'shortName') or ticker
        sector   = s(pr,'sector') or qt.get('sector', 'N/A')
        industry = qt.get('industry', 'N/A')
        exchange = s(pr,'exchangeName') or qt.get('fullExchangeName', '')

        # ── Valuation ──────────────────────────────────────
        pe_ratio  = r(sd,'trailingPE') or r(pr,'trailingPE') or qt.get('trailingPE', 0)
        fwd_pe    = r(ks,'forwardPE') or qt.get('forwardPE', 0)
        peg       = r(ks,'pegRatio')
        pb        = r(ks,'priceToBook')
        eps       = r(ks,'trailingEps') or qt.get('epsTrailingTwelveMonths', 0)
        w52hi     = r(sd,'fiftyTwoWeekHigh') or qt.get('fiftyTwoWeekHigh', 0)
        w52lo     = r(sd,'fiftyTwoWeekLow')  or qt.get('fiftyTwoWeekLow', 0)
        beta      = r(sd,'beta') or qt.get('beta', 1) or 1
        dividend  = r(sd,'dividendRate') or qt.get('dividendRate', 0)
        div_yield = r(sd,'dividendYield', pct=True) or (qt.get('dividendYield') or 0)*100
        tgt       = r(fd,'targetMeanPrice') or qt.get('targetMeanPrice', 0)

        # ── Profitability ──────────────────────────────────
        gross_m = r(fd,'grossMargins', pct=True)
        op_m    = r(fd,'operatingMargins', pct=True)
        net_m   = r(fd,'profitMargins', pct=True)
        roe     = r(fd,'returnOnEquity', pct=True)
        roa     = r(fd,'returnOnAssets', pct=True)
        roic    = round(roa * 1.4, 1)
        rev_g   = r(fd,'revenueGrowth', pct=True)
        earn_g  = r(fd,'earningsGrowth', pct=True)

        # ── Balance ────────────────────────────────────────
        de      = round(r(fd,'debtToEquity') / 100, 2) if r(fd,'debtToEquity') else 0
        cr      = r(fd,'currentRatio')
        qr      = r(fd,'quickRatio')
        cash    = r(fd,'totalCash')
        debt    = r(fd,'totalDebt')

        # ── Cash flow ──────────────────────────────────────
        fcf     = r(fd,'freeCashflow')
        ocf     = r(fd,'operatingCashflow')
        fcf_y   = round(fcf/mkt_cap*100, 2) if mkt_cap and fcf else 0

        # ── Ownership ──────────────────────────────────────
        ins_own = r(ks,'heldPercentInsiders', pct=True)
        inst_own= r(ks,'heldPercentInstitutions', pct=True)
        short_r = r(ks,'shortRatio')

        # ── Analyst ────────────────────────────────────────
        buy_ct = hold_ct = sell_ct = 0
        if rt:
            latest = rt[0]
            buy_ct  = latest.get('strongBuy',0) + latest.get('buy',0)
            hold_ct = latest.get('hold',0)
            sell_ct = latest.get('sell',0) + latest.get('strongSell',0)

        # ── Income history ─────────────────────────────────
        revenue = earnings = labels = []
        try:
            if ih:
                rv = [r(i,'totalRevenue') for i in reversed(ih)]
                ni = [r(i,'netIncome')    for i in reversed(ih)]
                lb = [(i.get('endDate',{}).get('fmt',''))[:4] for i in reversed(ih)]
                revenue  = [round(v/1e9,1) for v in rv if v]
                earnings = [round(v/1e9,2) for v in ni]
                labels   = lb[:len(revenue)]
                # Revenue growth from history if not available
                if not rev_g and len(ih) >= 2:
                    r1 = r(ih[0],'totalRevenue')
                    r2 = r(ih[1],'totalRevenue')
                    if r2: rev_g = round((r1-r2)/r2*100,1)
        except: pass

        # ── Fair value ─────────────────────────────────────
        fv  = round(eps*22,2) if eps > 0 else round(price*0.92,2)
        if not tgt: tgt = fv

        # ── Score ──────────────────────────────────────────
        sc = calc_score(pe_ratio, rev_g, net_m, cr, roe, change_pct)

        print(f"[{ticker}] ${price} PE:{round(pe_ratio,1)} Margin:{round(net_m,1)}% ROE:{round(roe,1)}% Score:{sc['total']}")

        return jsonify({
            'ticker':ticker,'name':name,'sector':sector,'industry':industry,
            'mktCap':fmt(mkt_cap),'exchange':exchange,
            'price':round(price,2),'change':change,'changePct':change_pct,
            'week52High':round(w52hi,2),'week52Low':round(w52lo,2),'beta':round(beta,2),
            'peRatio':round(pe_ratio,1),'fwdPE':round(fwd_pe,1),'peg':round(peg,2),
            'priceBook':round(pb,2),'eps':round(eps,2),
            'analystTarget':round(tgt,2),'buyCount':buy_ct,'holdCount':hold_ct,'sellCount':sell_ct,
            'grossMargin':round(gross_m,1),'opMargin':round(op_m,1),'netMargin':round(net_m,1),
            'roe':round(roe,1),'roa':round(roa,1),'roic':round(roic,1),
            'revenueGrowth':round(rev_g,1),'epsGrowth':round(earn_g,1),
            'debtEquity':de,'currentRatio':round(cr,2),'quickRatio':round(qr,2),
            'totalCash':fmt(cash),'totalDebt':fmt(debt),
            'fcfYield':fcf_y,'freeCashflow':fmt(fcf),'opCashflow':fmt(ocf),
            'dividend':round(dividend,2),'divYield':round(div_yield,2),
            'insiderOwn':round(ins_own,1),'instOwn':round(inst_own,1),'shortRatio':round(short_r,2),
            'fairValue':fv,'bull':round(max(tgt,fv)*1.2,2),'base':round((tgt+fv)/2,2),'bear':round(min(tgt,fv)*0.8,2),
            'score':sc['total'],'grade':sc['grade'],'verdict':sc['verdict'],'style':sc['style'],'scores':sc['breakdown'],
            'revenue':revenue,'earnings':earnings,'revenueLabels':labels,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/quotes')
def get_quotes():
    tickers = [t.strip() for t in request.args.get('tickers','').upper().split(',') if t.strip()][:10]
    def fetch(ticker):
        try:
            data = yf_get(f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}&corsDomain=finance.yahoo.com&formatted=false')
            qt = data.get('quoteResponse',{}).get('result',[])
            if qt:
                q = qt[0]
                price = q.get('regularMarketPrice',0)
                prev  = q.get('regularMarketPreviousClose',price)
                chg   = round(price-prev,2)
                chgp  = round((chg/prev*100) if prev else 0,2)
                pe    = q.get('trailingPE',0) or 0
                sc    = calc_score(pe,0,0,1,0,chgp)
                return {'ticker':ticker,'name':q.get('longName',ticker),'price':round(price,2),'change':chg,'changePct':chgp,'score':sc['total'],'verdict':sc['verdict']}
        except: pass
        return {'ticker':ticker,'name':ticker,'price':0,'change':0,'changePct':0,'score':50,'verdict':'HOLD'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(fetch, tickers))
    return jsonify(results)


@app.route('/api/macro')
def get_macro():
    symbols = {'sp500':'^GSPC','vix':'^VIX','gold':'GC=F','oil':'CL=F','bonds10':'^TNX','dxy':'DX-Y.NYB','btc':'BTC-USD'}
    result = {}
    def fetch(key, sym):
        try:
            data = yf_get(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d')
            meta = data.get('chart',{}).get('result',[{}])[0].get('meta',{})
            price = meta.get('regularMarketPrice',0)
            prev  = meta.get('chartPreviousClose',price)
            chg   = round(price-prev,2)
            chgp  = round((chg/prev*100) if prev else 0,2)
            result[key] = {'price':price,'change':chg,'changePct':chgp}
        except:
            result[key] = {'price':0,'change':0,'changePct':0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        [ex.submit(fetch,k,v) for k,v in symbols.items()]
    return jsonify(result)


def fmt(n):
    try:
        n = float(n)
        if n>=1e12: return f"${n/1e12:.2f}T"
        if n>=1e9:  return f"${n/1e9:.1f}B"
        if n>=1e6:  return f"${n/1e6:.0f}M"
        if n>0:     return f"${n:,.0f}"
    except: pass
    return 'N/A'

def sm(val, t, inv=False):
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
    b = {
        'valuation':    sm(pe,     [15,25,35,50],inv=True),
        'growth':       sm(rev_g,  [3,8,15,25]),
        'profitability':sm(net_m,  [5,10,20,35]),
        'balance':      sm(cr,     [0.8,1.2,1.8,2.5]),
        'momentum':     sm(chgp,   [-10,-2,2,10]),
        'quality':      sm(roe,    [5,12,25,40]),
        'macro':68,
    }
    total = round(sum(b.values())/len(b))
    grade = ('A+' if total>=90 else 'A' if total>=82 else 'A-' if total>=75 else
             'B+' if total>=68 else 'B' if total>=60 else 'B-' if total>=52 else 'C')
    verdict = 'BUY' if total>=78 else 'HOLD' if total>=62 else 'AVOID'
    style   = ('Growth' if b['growth']>80 else 'Value' if b['valuation']>80
               else 'Quality Compounder' if b['quality']>80 else 'Speculative')
    return {'total':total,'grade':grade,'verdict':verdict,'style':style,'breakdown':b}

if __name__ == '__main__':
    port = int(os.environ.get('PORT',5000))
    print(f"\n◈ STOCKBOX running on port {port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
