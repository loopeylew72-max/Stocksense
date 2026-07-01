"""
◈ STOCKSENSE — Railway Deployment
"""
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from cache import cache, TTL
from api_utils import ok, err, rate_limited, not_found, service_error
import os, requests, time
try:
    import store
except Exception as _store_err:
    store = None
    print(f'[STORE] module unavailable, persistence disabled: {_store_err}')
import scoring
try:
    import crypto_layer as crypto_engine
    CRYPTO_AVAILABLE = True
except ImportError as _cry_err:
    CRYPTO_AVAILABLE = False
    crypto_engine = None
    print(f'[CRYPTO] module unavailable: {_cry_err}')

try:
    import rotation
    ROTATION_AVAILABLE = True
except ImportError as _rot_err:
    ROTATION_AVAILABLE = False
    rotation = None
    print(f'[ROTATION] module unavailable: {_rot_err}')
try:
    from rie import run_rie
    RIE_AVAILABLE = True
except ImportError:
    RIE_AVAILABLE = False
    def run_rie(*a, **kw): return {}

app = Flask(__name__, static_folder='.')
CORS(app)

AV_KEY  = os.environ.get('AV_KEY', 'SC3UWE252HJ8T1JK')
AV_BASE = 'https://www.alphavantage.co/query'
FRED_KEY = os.environ.get('FRED_API_KEY', 'b17da6c1b0c06a96d98770c16354050a')

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

def _yahoo_chart(ticker, range_='1y'):
    """Shared Yahoo chart fetch — returns raw result[0] or None."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json', 'Referer': 'https://finance.yahoo.com',
    }
    for base in ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com']:
        try:
            r = requests.get(f'{base}/v8/finance/chart/{ticker}?interval=1d&range={range_}',
                             headers=headers, timeout=15)
            if r.status_code != 200: continue
            results = r.json().get('chart', {}).get('result', [])
            if results: return results[0]
        except Exception as e:
            print(f'[yahoo] {ticker} error: {e}')
    return None


def get_price_closes(ticker, range_='1y'):
    """Daily closes oldest-first as plain floats. Cached 6hr."""
    ck = f'closes:{ticker}:{range_}'
    cached = cache.get(ck)
    if cached is not None: return cached
    result = _yahoo_chart(ticker, range_)
    if not result:
        cache.set(ck, None, 3600); return None
    closes = [c for c in (result.get('indicators', {}).get('quote', [{}])[0].get('close', [])) if c is not None]
    cache.set(ck, closes or None, 21600)
    return closes or None


def get_price_closes_with_dates(ticker, range_='1y'):
    """Daily closes as [(unix_ts, close), ...] oldest-first. Cached 6hr."""
    ck = f'closes_dated:{ticker}:{range_}'
    cached = cache.get(ck)
    if cached is not None: return cached
    result = _yahoo_chart(ticker, range_)
    if not result:
        cache.set(ck, None, 3600); return None
    timestamps = result.get('timestamp', [])
    closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
    pairs = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
    cache.set(ck, pairs or None, 21600)
    return pairs or None


def get_moving_averages(ticker):
    """Fetch 1yr daily closes from Yahoo, compute 20/50/200 SMAs. Cached 6hr."""
    cached = cache.get(f'ma:{ticker}')
    if cached is not None:
        return cached
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json', 'Referer': 'https://finance.yahoo.com',
    }
    for base in ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com']:
        try:
            url = f'{base}/v8/finance/chart/{ticker}?interval=1d&range=1y'
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            result = r.json().get('chart', {}).get('result', [])
            if not result:
                continue
            closes = result[0].get('indicators', {}).get('quote', [{}])[0].get('close', [])
            closes = [c for c in closes if c is not None]
            if len(closes) < 200:
                print(f'[MA] {ticker}: only {len(closes)} closes, need 200')
                cache.set(f'ma:{ticker}', None, 3600)
                return None
            ma_20  = sum(closes[-20:]) / 20
            ma_50  = sum(closes[-50:]) / 50
            ma_200 = sum(closes[-200:]) / 200
            price  = closes[-1]
            data = {
                'ma_20': round(ma_20, 2), 'ma_50': round(ma_50, 2), 'ma_200': round(ma_200, 2),
                'price': round(price, 2),
                'pct_from_20':  round((price - ma_20)  / ma_20  * 100, 2),
                'pct_from_50':  round((price - ma_50)  / ma_50  * 100, 2),
                'pct_from_200': round((price - ma_200) / ma_200 * 100, 2),
                'golden_cross': ma_50 > ma_200,
            }
            cache.set(f'ma:{ticker}', data, 21600)
            return data
        except Exception as e:
            print(f'[MA] {ticker} error: {e}')
    cache.set(f'ma:{ticker}', None, 3600)
    return None


def get_live_price(ticker):
    """Get live price from Yahoo Finance — no API key needed.
    Cached 60s: prices don't need sub-minute freshness for a swing-trading
    macro terminal, and this function is called from ~20 places across the
    app (Top Setups alone hits it for 41 tickers). Uncached, every page that
    touches prices was making fresh Yahoo HTTP calls (up to 12s timeout ×2
    retries each) on every single request — this was the single biggest
    source of page-load slowness across the app.
    """
    ck = f'price:{ticker}'
    cached = cache.get(ck)
    if cached is not None:
        return cached

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://finance.yahoo.com',
    }
    for base in ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com']:
        try:
            url = f'{base}/v8/finance/chart/{ticker}?interval=1d&range=2d'
            r = requests.get(url, headers=headers, timeout=5)
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
                out = {
                    'price':      round(float(price), 2),
                    'prev':       round(float(prev), 2),
                    'change':     round(float(price) - float(prev), 2),
                    'changePct':  round((float(price)-float(prev))/float(prev)*100, 2) if prev else 0,
                    'week52High': meta.get('fiftyTwoWeekHigh', 0) or 0,
                    'week52Low':  meta.get('fiftyTwoWeekLow', 0)  or 0,
                }
                cache.set(ck, out, 300)  # 5 min — swing trading doesn't need sub-minute prices
                return out
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
    if not ticker: return not_found('(empty)')

    # Cache hit
    cached = cache_get(f'stock:{ticker}')
    if cached:
        try:
            live = get_live_price(ticker)
            if live and live.get('price',0) > 0:
                cached['price']     = live['price']
                cached['change']    = live['change']
                cached['changePct'] = live['changePct']
        except: pass
        return ok(cached, cached=True)

    # Always get live price first — fast, no AV rate limit
    try:
        live = get_live_price(ticker)
    except Exception as e:
        return service_error(f'Price fetch failed: {e}')

    if not live or not live.get('price'):
        return not_found(ticker)

    # Try AV fundamentals — always fall back gracefully
    try:
        overview = av({'function': 'OVERVIEW', 'symbol': ticker})

        if not overview or 'Information' in overview or 'Note' in overview:
            result = _build_yahoo_only(ticker, live)
            result['note'] = 'Rate limited — price data only. Retry in 60s.'
            return ok(result)

        if 'Symbol' not in overview:
            return ok(_build_yahoo_only(ticker, live))

        time.sleep(0.5)
        inc_data = av({'function': 'INCOME_STATEMENT', 'symbol': ticker})
        if not inc_data or 'Information' in inc_data or 'Note' in inc_data:
            inc_data = {}

        time.sleep(0.5)
        bal_data = av({'function': 'BALANCE_SHEET', 'symbol': ticker})
        if not bal_data or 'Information' in bal_data or 'Note' in bal_data:
            bal_data = {}

        earnings_cal = {}
        try:
            time.sleep(0.5)
            ec = av({'function': 'EARNINGS', 'symbol': ticker})
            if ec and ('annualEarnings' in ec or 'quarterlyEarnings' in ec):
                earnings_cal = ec
        except: pass

        result = _build_from_overview(ticker, overview, live, inc_data, bal_data, earnings_cal)
        cache_set(f'stock:{ticker}', result)
        return ok(result)

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f'[{ticker}] get_stock exception: {e}')
        try:
            result = _build_yahoo_only(ticker, live)
            result['note'] = f'Using price data only: {str(e)[:80]}'
            return ok(result)
        except Exception as e2:
            print(f'[{ticker}] yahoo fallback also failed: {e2}')
            return service_error(f'Could not load {ticker}')



def _build_yahoo_only(ticker, live):
    """Minimal stock result from Yahoo Finance price data only."""
    price = live.get('price', 0)
    sc = calc_score(0, 0, 0, 0, 0, live.get('changePct', 0))
    return {
        'ticker': ticker, 'name': ticker, 'sector': '', 'industry': '', 'exchange': '',
        'price': round(price, 2), 'change': round(live.get('change', 0), 2),
        'changePct': round(live.get('changePct', 0), 2),
        'week52High': round(live.get('week52High', price * 1.1), 2),
        'week52Low':  round(live.get('week52Low',  price * 0.9), 2),
        'mktCap': 'N/A', 'beta': 1, 'peRatio': 0, 'fwdPE': 0, 'peg': 0, 'priceBook': 0,
        'eps': 0, 'analystTarget': price, 'buyCount': 0, 'holdCount': 0, 'sellCount': 0,
        'grossMargin': 0, 'opMargin': 0, 'netMargin': 0, 'roe': 0, 'roa': 0, 'roic': 0,
        'revenueGrowth': 0, 'epsGrowth': 0, 'debtEquity': 0, 'currentRatio': 0, 'quickRatio': 0,
        'totalCash': 'N/A', 'totalDebt': 'N/A', 'fcfYield': 0, 'freeCashflow': 'N/A',
        'opCashflow': 'N/A', 'dividend': 0, 'divYield': 0, 'insiderOwn': 0, 'instOwn': 0, 'shortRatio': 0,
        'fairValue': round(price, 2), 'bull': round(price * 1.2, 2),
        'base': round(price, 2), 'bear': round(price * 0.8, 2),
        'score': sc['total'], 'grade': sc['grade'], 'verdict': sc['verdict'],
        'style': sc['style'], 'scores': sc.get('breakdown', {}),
        'revenue': [], 'earnings': [], 'revenueLabels': [],
        'qRevenue': [], 'qEarnings': [], 'qLabels': [],
        'epsActual': [], 'epsEstimate': [], 'epsSurprise': [], 'epsLabels': [],
        'annEps': [], 'annEpsLabels': [], 'yahoo_only': True,
    }


def _build_from_overview(ticker, overview, live, inc_data, bal_data, earnings_cal=None):
    """Full stock result from AV data with safe fallback."""
    try:
        return _build_from_overview_inner(ticker, overview, live, inc_data, bal_data, earnings_cal or {})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f'[{ticker}] build error: {e}')
        result = _build_yahoo_only(ticker, live)
        result['note'] = f'Partial data: {str(e)[:60]}'
        return result


def _build_from_overview_inner(ticker, overview, live, inc_data, bal_data, earnings_cal=None):
    earnings_cal = earnings_cal or {}
    price     = live.get('price', 0)
    changePct = live.get('changePct', 0)

    def sf(k, mult=1): return safe_float(overview.get(k), mult=mult)

    pe      = sf('PERatio'); fwd_pe = sf('ForwardPE'); peg  = sf('PEGRatio')
    pb      = sf('PriceToBookRatio'); eps   = sf('EPS'); beta  = sf('Beta') or 1
    div     = sf('DividendPerShare'); raw_dy = sf('DividendYield')
    div_y   = round(raw_dy * 100, 2) if raw_dy and raw_dy < 1 else round(raw_dy, 2)
    w52hi   = sf('52WeekHigh') or live.get('week52High', 0)
    w52lo   = sf('52WeekLow')  or live.get('week52Low', 0)
    tgt     = sf('AnalystTargetPrice'); net_m = sf('ProfitMargin', 100)
    op_m    = sf('OperatingMarginTTM', 100); roe = sf('ReturnOnEquityTTM', 100)
    roa     = sf('ReturnOnAssetsTTM', 100); mkt_cap = sf('MarketCapitalization')
    # roic is calculated later from real balance sheet + income statement data
    # (AV's OVERVIEW endpoint has no ReturnOnCapitalEmployedTTM field — it never existed)
    strong_buy  = int(sf('AnalystRatingStrongBuy')  or 0)
    buy         = int(sf('AnalystRatingBuy')         or 0)
    hold        = int(sf('AnalystRatingHold')        or 0)
    sell        = int(sf('AnalystRatingSell')        or 0)
    strong_sell = int(sf('AnalystRatingStrongSell') or 0)
    ins_own = sf('PercentInsiders'); inst_ow = sf('PercentInstitutions')

    # Annual income statement
    revenue = earnings = labels = []
    rev_g = earn_g = gross_m = 0
    annual = (inc_data or {}).get('annualReports', [])[:5]
    if annual:
        try:
            rev_list = [round(float(r.get('totalRevenue',0) or 0)/1e9, 1) for r in reversed(annual)]
            revenue  = rev_list
            labels   = [r.get('fiscalDateEnding','')[:4] for r in reversed(annual)]
            earnings = [round(float(r.get('netIncome',0) or 0)/1e9, 2) for r in reversed(annual)]
            latest   = annual[0]
            tot_rev  = float(latest.get('totalRevenue',0) or 0)
            gross_p  = float(latest.get('grossProfit',0) or 0)
            if tot_rev > 0: gross_m = round(gross_p/tot_rev*100, 1)
            if len(rev_list) >= 2 and rev_list[-2]:
                rev_g = round((rev_list[-1]-rev_list[-2])/abs(rev_list[-2])*100, 1)
            net_inc = [float(r.get('netIncome',0) or 0) for r in annual[:2]]
            if len(net_inc) == 2 and net_inc[1]:
                earn_g = round((net_inc[0]-net_inc[1])/abs(net_inc[1])*100, 1)
        except: pass

    # Quarterly income statement
    q_revenue = q_earnings = q_labels = []
    quarterly = (inc_data or {}).get('quarterlyReports', [])[:8]
    print(f'[{ticker}] quarterly reports: {len(quarterly)} found, inc_data keys: {list((inc_data or {}).keys())}')
    if quarterly:
        try:
            q_revenue  = [round(float(r.get('totalRevenue',0) or 0)/1e9, 2) for r in reversed(quarterly)]
            q_earnings = [round(float(r.get('netIncome',0) or 0)/1e9, 2) for r in reversed(quarterly)]
            q_labels   = [r.get('fiscalDateEnding','')[:7] for r in reversed(quarterly)]
            print(f'[{ticker}] quarterly parsed: {len(q_revenue)} revenue points, first={q_revenue[0] if q_revenue else None}')
        except Exception as qe:
            print(f'[{ticker}] quarterly parse error: {qe}')

    # Balance sheet
    cr = de = qr = 0
    roic = 0
    bal_annual = (bal_data or {}).get('annualReports', [{}])
    if bal_annual:
        try:
            b = bal_annual[0]
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
            if curr_liab > 0:
                cr = round(curr_assets / curr_liab, 2)
                qr = round((curr_assets - inventory) / curr_liab, 2)
            if tot_equity > 0:
                de = round(tot_debt / tot_equity, 2) if tot_debt > 0 else 0

            # ROIC — real calculation from balance sheet + income statement components.
            # AV's OVERVIEW endpoint has no ReturnOnCapitalEmployedTTM field (it never
            # existed — this was previously always reading as 0% for every ticker).
            # ROIC = NOPAT / Invested Capital, where Invested Capital = Debt + Equity − Cash.
            # Using trailing net income as a NOPAT proxy (reasonable for non-financials;
            # slightly understates true NOPAT for leveraged firms since it's post-interest).
            invested_capital = tot_debt + tot_equity - cash
            ttm_net_income = bsf(annual[0].get('netIncome')) if annual else 0
            if invested_capital > 0 and ttm_net_income:
                roic = round(ttm_net_income / invested_capital * 100, 1)
        except: pass

    # Earnings estimates
    ann_eps_act = ann_eps_lbl = []
    qtr_actual = qtr_estimate = qtr_surprise = qtr_elabels = []
    try:
        ann_earn = earnings_cal.get('annualEarnings', [])[:4]
        qtr_earn = earnings_cal.get('quarterlyEarnings', [])[:8]
        ann_eps_act = [round(float(r.get('reportedEPS',0) or 0), 2) for r in reversed(ann_earn) if r.get('reportedEPS') not in (None,'None','')]
        ann_eps_lbl = [r.get('fiscalDateEnding','')[:4] for r in reversed(ann_earn) if r.get('reportedEPS') not in (None,'None','')]
        for r in reversed(qtr_earn):
            act = r.get('reportedEPS'); est = r.get('estimatedEPS')
            if act not in (None,'None','') and est not in (None,'None',''):
                try:
                    af, ef = float(act), float(est)
                    qtr_actual.append(round(af,2)); qtr_estimate.append(round(ef,2))
                    qtr_surprise.append(round((af-ef)/abs(ef)*100,1) if ef else 0)
                    qtr_elabels.append(r.get('fiscalDateEnding','')[:7])
                except: pass
    except: pass

    # Fair value
    fv = round(eps * min(rev_g, 60), 2) if eps > 0 and rev_g > 20 else round(eps * 22, 2) if eps > 0 else round(price * 0.92, 2)
    if tgt > 0: fv = round((fv + tgt) / 2, 2)
    if not tgt: tgt = fv

    sc = calc_score(pe, rev_g, net_m, cr, roe, changePct,
                    overview.get('Sector',''), overview.get('Industry',''), mkt_cap, div_y)
    print(f"[{ticker}] ${price} PE:{pe} Margin:{net_m}% Rev:{rev_g}% Score:{sc['total']}")

    return {
        'ticker': ticker, 'name': overview.get('Name', ticker),
        'sector': overview.get('Sector',''), 'industry': overview.get('Industry',''),
        'exchange': overview.get('Exchange',''),
        'price': round(price,2), 'change': round(live.get('change',0),2), 'changePct': round(changePct,2),
        'mktCap': fmt(mkt_cap), 'week52High': round(w52hi,2), 'week52Low': round(w52lo,2),
        'beta': round(beta,2), 'peRatio': round(pe,1), 'fwdPE': round(fwd_pe,1),
        'peg': round(peg,2), 'priceBook': round(pb,2), 'eps': round(eps,2),
        'analystTarget': round(tgt,2), 'buyCount': strong_buy+buy, 'holdCount': hold, 'sellCount': sell+strong_sell,
        'grossMargin': round(gross_m,1), 'opMargin': round(op_m,1), 'netMargin': round(net_m,1),
        'roe': round(roe,1), 'roa': round(roa,1), 'roic': round(roic,1),
        'revenueGrowth': round(rev_g,1), 'epsGrowth': round(earn_g,1),
        'debtEquity': round(de,2), 'currentRatio': round(cr,2), 'quickRatio': round(qr,2),
        'totalCash': 'N/A', 'totalDebt': 'N/A', 'fcfYield': 0, 'freeCashflow': 'N/A', 'opCashflow': 'N/A',
        'dividend': round(div,2), 'divYield': round(div_y,2),
        'insiderOwn': round(ins_own,1), 'instOwn': round(inst_ow,1), 'shortRatio': 0,
        'fairValue': round(fv,2), 'bull': round(max(tgt,fv)*1.2,2),
        'base': round((tgt+fv)/2,2), 'bear': round(min(tgt,fv)*0.8,2),
        'score': sc['total'], 'grade': sc['grade'], 'verdict': sc['verdict'],
        'style': sc['style'], 'scores': sc.get('breakdown', {}),
        'revenue': revenue, 'earnings': earnings, 'revenueLabels': labels,
        'qRevenue': q_revenue, 'qEarnings': q_earnings, 'qLabels': q_labels,
        'epsActual': qtr_actual, 'epsEstimate': qtr_estimate,
        'epsSurprise': qtr_surprise, 'epsLabels': qtr_elabels,
        'annEps': ann_eps_act, 'annEpsLabels': ann_eps_lbl,
    }


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
    syms = {'sp500':'^GSPC','vix':'^VIX','gold':'GC=F','oil':'CL=F','bonds10':'^TNX','dxy':'DX-Y.NYB','btc':'BTC-USD'}
    result = {}
    for key, sym in syms.items():
        live = get_live_price(sym)
        if live:
            result[key] = {'price':live['price'],'change':live['change'],'changePct':live['changePct']}
        else:
            result[key] = {'price':0,'change':0,'changePct':0}
    return ok(result)


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

@app.route('/api/calendar')
def get_calendar():
    # Default view: this week's HIGH-impact releases only (the score-moving drivers).
    # ?range=all bypasses the week filter; ?impact=all bypasses the high-only filter.
    def _filter(events):
        import datetime
        want_week = request.args.get('range', 'week') != 'all'
        high_only = request.args.get('impact', 'high') != 'all'
        today  = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
        sunday = monday + datetime.timedelta(days=6)
        out = []
        for e in events:
            if high_only and str(e.get('impact', '')).upper() != 'HIGH':
                continue
            if want_week:
                try:
                    d = datetime.datetime.strptime(str(e.get('date', ''))[:10], '%Y-%m-%d').date()
                except Exception:
                    continue
                if not (monday <= d <= sunday):
                    continue
            out.append(e)
        return out

    # Use FMP calendar if key available
    if FMP_KEY:
        fmp_events = get_fmp_economic_calendar()
        if fmp_events:
            for e in fmp_events:
                result, magnitude, diff = calc_surprise(e)
                e['surprise']  = result
                e['magnitude'] = magnitude
                e['diff']      = diff
            return ok({'events': _filter(fmp_events), 'source': 'FMP'})
    # Fall through to manual calendar below
    events = [
        # ── MAY 2026 — Real data from Forex Factory ─────────────
        # May 8
        {'date':'2026-05-08','event':'Average Hourly Earnings MoM','impact':'HIGH','previous':'0.2%','forecast':'0.3%','actual':'0.2%','category':'Employment'},
        {'date':'2026-05-08','event':'Non-Farm Employment Change','impact':'HIGH','previous':'65K','forecast':'185K','actual':'115K','category':'Employment'},
        {'date':'2026-05-08','event':'Unemployment Rate','impact':'HIGH','previous':'4.3%','forecast':'4.3%','actual':'4.3%','category':'Employment'},
        # May 12
        {'date':'2026-05-12','event':'Core CPI MoM','impact':'HIGH','previous':'0.2%','forecast':'0.3%','actual':'0.4%','category':'Inflation'},
        {'date':'2026-05-12','event':'Core CPI YoY','impact':'HIGH','previous':'2.6%','forecast':'2.7%','actual':'2.8%','category':'Inflation'},
        {'date':'2026-05-12','event':'CPI MoM','impact':'HIGH','previous':'0.9%','forecast':'0.6%','actual':'0.6%','category':'Inflation'},
        {'date':'2026-05-12','event':'CPI YoY','impact':'HIGH','previous':'3.3%','forecast':'3.7%','actual':'3.8%','category':'Inflation'},
        # May 13
        {'date':'2026-05-13','event':'Core PPI MoM','impact':'MEDIUM','previous':'0.2%','forecast':'0.3%','actual':'1.0%','category':'Inflation'},
        {'date':'2026-05-13','event':'PPI MoM','impact':'MEDIUM','previous':'0.7%','forecast':'0.5%','actual':'1.4%','category':'Inflation'},
        # May 14
        {'date':'2026-05-14','event':'Core Retail Sales MoM','impact':'HIGH','previous':'1.9%','forecast':'0.7%','actual':'0.7%','category':'Growth'},
        {'date':'2026-05-14','event':'Retail Sales MoM','impact':'HIGH','previous':'1.6%','forecast':'0.5%','actual':'1.6%','category':'Growth'},
        # May 20
        {'date':'2026-05-20','event':'FOMC Meeting Minutes','impact':'HIGH','previous':'','forecast':'','actual':'','category':'Fed Policy'},
        # May 28
        {'date':'2026-05-28','event':'Core PCE Price Index MoM','impact':'HIGH','previous':'0.3%','forecast':'0.3%','actual':'0.2%','category':'Inflation'},
        {'date':'2026-05-28','event':'Prelim GDP QoQ','impact':'HIGH','previous':'0.7%','forecast':'2.0%','actual':'1.6%','category':'Growth'},
        # ── JUNE 2026 — Upcoming ─────────────────────────────────
        {'date':'2026-06-04','event':'ISM Manufacturing PMI','impact':'HIGH','previous':'48.7','forecast':'49.5','actual':'','category':'Growth'},
        {'date':'2026-06-05','event':'Initial Jobless Claims','impact':'MEDIUM','previous':'227K','forecast':'230K','actual':'','category':'Employment'},
        {'date':'2026-06-06','event':'Non-Farm Payrolls','impact':'HIGH','previous':'115K','forecast':'140K','actual':'','category':'Employment'},
        {'date':'2026-06-06','event':'Unemployment Rate','impact':'HIGH','previous':'4.3%','forecast':'4.3%','actual':'','category':'Employment'},
        {'date':'2026-06-11','event':'Core CPI MoM','impact':'HIGH','previous':'0.4%','forecast':'0.3%','actual':'','category':'Inflation'},
        {'date':'2026-06-11','event':'CPI YoY','impact':'HIGH','previous':'3.8%','forecast':'3.6%','actual':'','category':'Inflation'},
        {'date':'2026-06-12','event':'Core PPI MoM','impact':'MEDIUM','previous':'1.0%','forecast':'0.3%','actual':'','category':'Inflation'},
        {'date':'2026-06-18','event':'FOMC Rate Decision','impact':'HIGH','previous':'4.33%','forecast':'4.33%','actual':'','category':'Fed Policy'},
        {'date':'2026-06-25','event':'Consumer Confidence','impact':'MEDIUM','previous':'49.8','forecast':'55.0','actual':'','category':'Sentiment'},
        {'date':'2026-06-27','event':'Core PCE Price Index MoM','impact':'HIGH','previous':'0.2%','forecast':'0.2%','actual':'','category':'Inflation'},
    ]
    # Enrich each event with beat/miss data
    for e in events:
        result, magnitude, diff = calc_surprise(e)
        e['surprise']  = result      # 'BEAT', 'MISS', 'IN LINE', or None
        e['magnitude'] = magnitude   # 'LARGE', 'MEDIUM', 'SMALL', or None
        e['diff']      = diff        # actual - forecast (raw number)
    return ok({'events': _filter(events)})


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


# ══════════════════════════════════════════════════════════════════
# ◈ COT — Commitments of Traders (real CFTC data)
# Source: CFTC Public Reporting Socrata API, Legacy Futures-Only
#   dataset 6dca-aqww. Legacy report classifies open interest into
#   non-commercial (large specs), commercial (hedgers), and
#   non-reportable (small specs) — exactly the 3 categories the UI shows.
# Released weekly (Fri 3:30pm ET, as of prior Tuesday) → cache 12h.
# Falls back to labelled sample data if the live fetch fails.
# ══════════════════════════════════════════════════════════════════
COT_SOCRATA_URL = 'https://publicreporting.cftc.gov/resource/6dca-aqww.json'
CFTC_APP_TOKEN  = os.environ.get('CFTC_APP_TOKEN', '')  # optional, raises rate limit

# symbol → CFTC contract market code (+ a name hint used as a fallback query)
COT_MARKETS = {
    'GOLD':   {'name': 'Gold Futures',             'code': '088691', 'like': 'GOLD',                        'price_proxy': 'GLD'},
    'OIL':    {'name': 'Crude Oil Futures',        'code': '067651', 'like': 'CRUDE OIL, LIGHT SWEET-WTI', 'price_proxy': 'USO'},
    'SPX':    {'name': 'E-mini S&P 500 Futures',   'code': '13874A', 'like': 'E-MINI S&P 500',             'price_proxy': 'SPY'},
    'NASDAQ': {'name': 'E-mini Nasdaq 100 Futures','code': '209742', 'like': 'NASDAQ-100',                  'price_proxy': 'QQQ'},
    'EUR':    {'name': 'Euro FX Futures',          'code': '099741', 'like': 'EURO FX',                     'price_proxy': 'FXE'},
    'BONDS':  {'name': '10Y Treasury Futures',     'code': '043602', 'like': '10-YEAR U.S. TREASURY',       'price_proxy': 'TLT'},
    'DXY':    {'name': 'US Dollar Index Futures',  'code': '098662', 'like': 'U.S. DOLLAR INDEX',           'price_proxy': 'UUP'},
}

# Labelled sample fallback — used only when the live CFTC fetch fails.
COT_SAMPLE = {
    'GOLD':  {'name':'Gold Futures','commercials':{'long':142000,'short':312000,'net':-170000,'prev_net':-165000},'large_specs':{'long':280000,'short':85000,'net':195000,'prev_net':188000},'small_specs':{'long':45000,'short':70000,'net':-25000,'prev_net':-23000},'signal':'BULLISH','history':[145000,160000,172000,180000,188000,195000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'OIL':   {'name':'Crude Oil Futures','commercials':{'long':390000,'short':590000,'net':-200000,'prev_net':-210000},'large_specs':{'long':310000,'short':145000,'net':165000,'prev_net':155000},'small_specs':{'long':38000,'short':52000,'net':-14000,'prev_net':-12000},'signal':'NEUTRAL','history':[180000,170000,155000,160000,155000,165000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'SPX':   {'name':'E-mini S&P 500 Futures','commercials':{'long':320000,'short':480000,'net':-160000,'prev_net':-175000},'large_specs':{'long':520000,'short':285000,'net':235000,'prev_net':210000},'small_specs':{'long':42000,'short':62000,'net':-20000,'prev_net':-18000},'signal':'BULLISH','history':[180000,195000,210000,215000,210000,235000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'NASDAQ':{'name':'E-mini Nasdaq 100 Futures','commercials':{'long':85000,'short':145000,'net':-60000,'prev_net':-68000},'large_specs':{'long':165000,'short':82000,'net':83000,'prev_net':75000},'small_specs':{'long':18000,'short':25000,'net':-7000,'prev_net':-6000},'signal':'BULLISH','history':[60000,65000,70000,72000,75000,83000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'EUR':   {'name':'Euro FX Futures','commercials':{'long':210000,'short':160000,'net':50000,'prev_net':42000},'large_specs':{'long':120000,'short':175000,'net':-55000,'prev_net':-48000},'small_specs':{'long':22000,'short':18000,'net':4000,'prev_net':3500},'signal':'BEARISH','history':[-30000,-38000,-42000,-48000,-48000,-55000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'BONDS': {'name':'10Y Treasury Futures','commercials':{'long':680000,'short':420000,'net':260000,'prev_net':240000},'large_specs':{'long':310000,'short':485000,'net':-175000,'prev_net':-162000},'small_specs':{'long':45000,'short':68000,'net':-23000,'prev_net':-20000},'signal':'BULLISH','history':[-140000,-150000,-155000,-162000,-162000,-175000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
    'DXY':   {'name':'US Dollar Index Futures','commercials':{'long':22000,'short':38000,'net':-16000,'prev_net':-14000},'large_specs':{'long':32000,'short':15000,'net':17000,'prev_net':15000},'small_specs':{'long':4500,'short':6500,'net':-2000,'prev_net':-1800},'signal':'BULLISH','history':[10000,11000,13000,14000,15000,17000],'weeks':['W-5','W-4','W-3','W-2','W-1','Now']},
}


def _cot_int(row, *keys):
    """Read the first present field from a Socrata row and coerce to int."""
    for k in keys:
        v = row.get(k)
        if v not in (None, ''):
            try: return int(round(float(v)))
            except (ValueError, TypeError): continue
    return 0


def _cot_signal(spec_hist, open_interest):
    """
    Signal from large-spec (trend-follower) net positioning momentum,
    scaled by open interest so it's comparable across markets.
    """
    if len(spec_hist) < 2:
        return 'NEUTRAL'
    chg = spec_hist[-1] - spec_hist[-2]
    ratio = chg / max(open_interest, 1)
    if ratio >  0.01: return 'BULLISH'
    if ratio < -0.01: return 'BEARISH'
    return 'NEUTRAL'


def fetch_cot_live(symbol):
    """Fetch + parse the last ~6 weeks of legacy COT for one symbol. None on failure."""
    mkt = COT_MARKETS.get(symbol)
    if not mkt:
        return None

    headers = {'X-App-Token': CFTC_APP_TOKEN} if CFTC_APP_TOKEN else {}
    base = {'$order': 'report_date_as_yyyy_mm_dd DESC', '$limit': 6}

    rows = []
    for params in (
        {**base, 'cftc_contract_market_code': mkt['code']},
        {**base, '$where': f"upper(market_and_exchange_names) like '%{mkt['like'].upper()}%'"},
    ):
        try:
            r = requests.get(COT_SOCRATA_URL, params=params, headers=headers, timeout=12)
            if r.status_code == 200 and r.json():
                rows = r.json()
                break
        except Exception as e:
            print(f'[COT] {symbol} fetch error: {e}')

    if not rows:
        return None

    rows = list(reversed(rows))  # oldest → newest

    # Grow the durable history for Flow/Crowding (dedupes by date; cheap, keeps weekly cadence).
    try:
        _store_cot_history(symbol, [_cot_parse_row(r) for r in rows if (r.get('report_date_as_yyyy_mm_dd') or '')])
    except Exception as e:
        print(f'[COT] live persist error {symbol}: {e}')

    def cat(row, side):  # side: 'long' or 'short'
        return {
            'comm':  _cot_int(row, f'comm_positions_{side}_all'),
            'spec':  _cot_int(row, f'noncomm_positions_{side}_all'),
            'small': _cot_int(row, f'nonrept_positions_{side}_all'),
        }

    spec_hist = []
    for row in rows:
        L, S = cat(row, 'long'), cat(row, 'short')
        spec_hist.append(L['spec'] - S['spec'])

    latest, prev = rows[-1], (rows[-2] if len(rows) >= 2 else rows[-1])
    L, S   = cat(latest, 'long'), cat(latest, 'short')
    pL, pS = cat(prev, 'long'),   cat(prev, 'short')
    oi = _cot_int(latest, 'open_interest_all')

    n = len(rows)
    weeks = [f'W-{n-1-i}' if i < n - 1 else 'Now' for i in range(n)]

    return {
        'name':        mkt['name'],
        'commercials': {'long': L['comm'],  'short': S['comm'],  'net': L['comm']-S['comm'],   'prev_net': pL['comm']-pS['comm']},
        'large_specs': {'long': L['spec'],  'short': S['spec'],  'net': L['spec']-S['spec'],   'prev_net': pL['spec']-pS['spec']},
        'small_specs': {'long': L['small'], 'short': S['small'], 'net': L['small']-S['small'], 'prev_net': pL['small']-pS['small']},
        'signal':      _cot_signal(spec_hist, oi),
        'history':     spec_hist,
        'weeks':       weeks,
        'report_date': (latest.get('report_date_as_yyyy_mm_dd') or '')[:10],
        'source':      'live',
    }


# ── Positioning Intelligence · Stage 0: COT history → durable store ──
# Weekly net positioning per market & trader category, plus open interest and
# large-spec net as % of OI (the crowding-robust measure that survives OI growth).
def _cot_ts(date_str):
    """report_date 'YYYY-MM-DD' → unix ts (UTC midnight). 0 on failure."""
    try:
        import calendar
        y, m, d = (int(x) for x in date_str[:10].split('-'))
        return int(calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0)))
    except Exception:
        return 0


def _cot_parse_row(row):
    """One Socrata row → dated net-positioning dict (shared by history + live persist)."""
    date = (row.get('report_date_as_yyyy_mm_dd') or '')[:10]
    return {
        'ts':        _cot_ts(date),
        'date':      date,
        'specs_net': _cot_int(row, 'noncomm_positions_long_all') - _cot_int(row, 'noncomm_positions_short_all'),
        'comm_net':  _cot_int(row, 'comm_positions_long_all')    - _cot_int(row, 'comm_positions_short_all'),
        'small_net': _cot_int(row, 'nonrept_positions_long_all')  - _cot_int(row, 'nonrept_positions_short_all'),
        'oi':        _cot_int(row, 'open_interest_all'),
    }


def fetch_cot_history(symbol, weeks=300):
    """Fetch up to `weeks` of legacy COT for one market, oldest→newest, parsed for storage."""
    mkt = COT_MARKETS.get(symbol)
    if not mkt:
        return []
    headers = {'X-App-Token': CFTC_APP_TOKEN} if CFTC_APP_TOKEN else {}
    base = {'$order': 'report_date_as_yyyy_mm_dd DESC', '$limit': int(weeks)}
    rows = []
    for params in (
        {**base, 'cftc_contract_market_code': mkt['code']},
        {**base, '$where': f"upper(market_and_exchange_names) like '%{mkt['like'].upper()}%'"},
    ):
        try:
            r = requests.get(COT_SOCRATA_URL, params=params, headers=headers, timeout=25)
            if r.status_code == 200 and r.json():
                rows = r.json()
                break
        except Exception as e:
            print(f'[COT] {symbol} history fetch error: {e}')
    if not rows:
        return []
    parsed = [_cot_parse_row(r) for r in reversed(rows)]   # oldest → newest
    return [p for p in parsed if p['ts']]


def _store_cot_history(symbol, hist):
    """Persist a parsed COT history list into the store as weekly series. Returns points written."""
    if not (store and hist):
        return 0
    specs = [(h['ts'], h['specs_net']) for h in hist]
    comm  = [(h['ts'], h['comm_net'])  for h in hist]
    small = [(h['ts'], h['small_net']) for h in hist]
    oi    = [(h['ts'], h['oi'])        for h in hist if h['oi']]
    pctoi = [(h['ts'], round(h['specs_net'] / h['oi'] * 100, 3)) for h in hist if h['oi']]
    n = 0
    try:
        n += store.record_indicators_bulk(f'cot_{symbol}_specs_net',   specs)
        n += store.record_indicators_bulk(f'cot_{symbol}_comm_net',    comm)
        n += store.record_indicators_bulk(f'cot_{symbol}_small_net',   small)
        n += store.record_indicators_bulk(f'cot_{symbol}_oi',          oi)
        n += store.record_indicators_bulk(f'cot_{symbol}_specs_pctoi', pctoi)
    except Exception as e:
        print(f'[COT] store error {symbol}: {e}')
    return n


def backfill_cot(symbols=None, weeks=300):
    """Backfill COT history for the given markets (default all). Returns {symbol: points_written}."""
    syms = symbols or list(COT_MARKETS.keys())
    out = {}
    for sym in syms:
        hist = fetch_cot_history(sym, weeks=weeks)
        out[sym] = _store_cot_history(sym, hist) if hist else 0
    return out


@app.route('/api/cot/backfill')
def cot_backfill():
    weeks = int(request.args.get('weeks', 300))
    syms  = request.args.get('symbols')
    syms  = [s.strip().upper() for s in syms.split(',')] if syms else None
    res   = backfill_cot(syms, weeks=weeks)
    return ok({'backfilled': res, 'total_points': sum(res.values()),
               'series_per_market': ['specs_net', 'comm_net', 'small_net', 'oi', 'specs_pctoi']})


# ── Positioning Intelligence · Stage 1: Flow + Crowding scoring ──
import statistics as _stats

_FLOW_BANDS  = [(80, 'Strong Inflows'), (60, 'Inflows'), (40, 'Neutral'),
                (20, 'Outflows'), (0, 'Strong Outflows')]
_CROWD_BANDS = [(80, 'Crowded'), (60, 'Owned'), (40, 'Neutral'),
                (20, 'Under-Owned'), (0, 'Extremely Under-Owned')]


def _band(score, bands):
    for thr, lbl in bands:
        if score is not None and score >= thr:
            return lbl
    return bands[-1][1]


def _horizon_flow(vals, lag):
    """Current lag-step change scaled by the market's own typical move size (std of changes).
    Centered at zero-change = neutral, so a rising net reads as inflow, falling as outflow."""
    if len(vals) < lag + 10:
        return None
    changes = [vals[i] - vals[i - lag] for i in range(lag, len(vals))]
    sd = _stats.pstdev(changes)
    if sd == 0:
        return 0.0
    return max(-3.0, min(3.0, changes[-1] / sd))


def _positioning_interp(flow, crowd):
    """Honest templated read combining Flow + Crowding. Returns (text, flags[])."""
    flags = []
    if crowd is None:
        return 'Flow computed; crowding needs more history to rank.', flags
    c1 = ('Strong capital inflows continue' if flow >= 80 else
          'Capital is flowing in'           if flow >= 60 else
          'Strong capital outflows'          if flow <= 20 else
          'Capital is leaving'               if flow < 40 else
          'Flows are roughly balanced')
    c2 = ('positioning is increasingly crowded' if crowd >= 70 else
          'positioning is under-owned'           if crowd <= 30 else
          'positioning is near its historical norm')
    inflow, outflow = flow >= 60, flow < 40
    crowded, light  = crowd >= 70, crowd <= 30
    c3, fl = '', None
    if inflow and crowded:   c3, fl = 'trend intact but positioning risk is rising', 'CROWDED_TREND'
    elif outflow and crowded: c3, fl = 'a crowded trade now losing capital — potential warning', 'UNWIND_RISK'
    elif outflow and light:   c3, fl = 'heavily under-owned — short-squeeze risk increasing', 'SQUEEZE_RISK'
    elif inflow and light:    c3, fl = 'early accumulation from a low base', 'EARLY_ACCUMULATION'
    if fl:
        flags.append(fl)
    text = f'{c1}; {c2}.'
    if c3:
        text += f' {c3[0].upper() + c3[1:]}.'
    return text, flags


def _pct_change(cur, prev):
    """% change with sign — None if prev is zero or missing (can't divide cleanly)."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _detect_price_divergence(symbol, net_series):
    """
    Compare price trend vs large-spec positioning trend over the last ~4 weeks.
    Returns (label, detail) or (None, None) if no proxy/insufficient data.

    Logic:
      - Price up + positioning up   -> Confirmed (trend supported by flows)
      - Price down + positioning down -> Confirmed (trend supported by flows)
      - Price up + positioning down  -> Bearish divergence (rally not backed by specs)
      - Price down + positioning up  -> Bullish divergence (selloff not backed by specs)
    """
    mkt = COT_MARKETS.get(symbol, {})
    proxy = mkt.get('price_proxy')
    if not proxy or len(net_series) < 5:
        return None, None
    try:
        closes = get_price_closes(proxy, '3mo')
        if not closes or len(closes) < 25:
            return None, None
        price_chg = (closes[-1] - closes[-25]) / closes[-25] * 100  # ~4 weeks of trading days
    except Exception:
        return None, None

    pos_chg_pct = _pct_change(net_series[-1], net_series[-5])  # 4-week positioning change
    if pos_chg_pct is None:
        return None, None

    price_up   = price_chg > 1.5
    price_down = price_chg < -1.5
    pos_up     = pos_chg_pct > 5
    pos_down   = pos_chg_pct < -5

    if price_up and pos_down:
        return ('BEARISH_DIVERGENCE',
                f'Price +{price_chg:.1f}% over 4w while large specs cut net position {pos_chg_pct:.0f}% — rally not backed by fresh positioning.')
    if price_down and pos_up:
        return ('BULLISH_DIVERGENCE',
                f'Price {price_chg:.1f}% over 4w while large specs grew net position +{pos_chg_pct:.0f}% — selloff not backed by fresh positioning.')
    if price_up and pos_up:
        return ('CONFIRMED_BULLISH', f'Price +{price_chg:.1f}% over 4w with specs adding {pos_chg_pct:+.0f}% — trend confirmed by positioning.')
    if price_down and pos_down:
        return ('CONFIRMED_BEARISH', f'Price {price_chg:.1f}% over 4w with specs cutting {pos_chg_pct:.0f}% — trend confirmed by positioning.')
    return ('NONE', 'No clear divergence — price and positioning roughly aligned or both flat.')


def compute_positioning(symbol):
    """Net direction + Flow + Crowding + divergence for one COT market, from stored history."""
    if not store:
        return {'symbol': symbol, 'available': False, 'note': 'store unavailable'}
    def ser(suffix):
        return [v for _, v in store.get_series(f'cot_{symbol}_{suffix}', window_days=2600, max_points=400)]
    net, pctoi, comm = ser('specs_net'), ser('specs_pctoi'), ser('comm_net')
    n = len(net)
    if n < 20:
        return {'symbol': symbol, 'available': False, 'samples': n,
                'note': 'insufficient history — run /api/cot/backfill'}

    # ── Net direction — are large specs (trend-followers) net long or short, and by how much ──
    net_now = net[-1]
    direction = 'NET LONG' if net_now > 0 else 'NET SHORT' if net_now < 0 else 'FLAT'

    # ── % change week-over-week and month-over-month (not just raw contracts) ──
    chg_1w_pct = _pct_change(net_now, net[-2])  if n > 1 else None
    chg_4w_pct = _pct_change(net_now, net[-5])  if n > 4 else None

    # FLOW — blended multi-horizon (weekly / monthly / quarterly), large-specs primary
    parts = [(z, w) for z, w in ((_horizon_flow(net, 1), 0.25),
                                 (_horizon_flow(net, 4), 0.35),
                                 (_horizon_flow(net, 13), 0.40)) if z is not None]
    zf   = sum(z * w for z, w in parts) / sum(w for _, w in parts) if parts else 0.0
    flow = round(max(0, min(100, 50 + zf * 20)))

    # CROWDING — percentile of large-spec net as % of open interest vs ~5y
    cur_pctoi = pctoi[-1] if pctoi else None
    crowd_pct = store.percentile_rank(f'cot_{symbol}_specs_pctoi', cur_pctoi, window_days=2600) if cur_pctoi is not None else None
    crowd     = round(crowd_pct) if crowd_pct is not None else None

    def chg(lag):
        return round(net[-1] - net[-1 - lag]) if n > lag else None

    interp, flags = _positioning_interp(flow, crowd)
    div_label, div_detail = _detect_price_divergence(symbol, net)
    if div_label and div_label not in ('NONE',):
        flags.append(div_label)

    return {
        'symbol':         symbol,
        'name':           COT_MARKETS.get(symbol, {}).get('name', symbol),
        'available':      True,
        'samples':        n,
        'direction':      direction,
        'flow_score':     flow,  'flow_label':     _band(flow, _FLOW_BANDS),
        'crowding_score': crowd, 'crowding_label': _band(crowd, _CROWD_BANDS) if crowd is not None else 'No data',
        'components': {
            'net_now':        round(net_now),
            'chg_1w':         chg(1),
            'chg_1w_pct':     chg_1w_pct,
            'chg_4w':         chg(4),
            'chg_4w_pct':     chg_4w_pct,
            'chg_13w':        chg(13),
            'net_pct_oi':     round(cur_pctoi, 2) if cur_pctoi is not None else None,
            'commercial_net': round(comm[-1]) if comm else None,
        },
        'divergence': {
            'label':  div_label or 'NONE',
            'detail': div_detail or 'No price comparison available.',
        },
        'interpretation': interp,
        'flags':          flags,
    }


# Reverse lookup: scorecard ticker -> COT market symbol, built from price_proxy.
# Only the 6 tickers that are the exact proxy for a COT market get the overlay —
# e.g. GLD gets GOLD's COT data, but SLV/GDX (other gold-adjacent tickers) do not,
# since their positioning can diverge from the futures market itself.
TICKER_TO_COT = {v['price_proxy']: k for k, v in COT_MARKETS.items() if v.get('price_proxy')}

_COT_OVERLAY_CACHE_TTL = 1800  # 30 min — matches the background refresh cadence


def _cot_overlay(ticker):
    """
    Small, capped confirming/contrarian modifier to the asset composite score —
    NOT a core weighted factor. Only applies to the 6 tickers that are direct
    proxies for a COT-covered futures market (GLD, USO, SPY, QQQ, FXE, TLT).

    Logic (deliberately conservative — COT confirms or warns, it doesn't drive):
      Flow bullish + not crowded  -> +5  (trend supported, room to run)
      Flow bullish + crowded      -> +2  (supported but chase risk — muted)
      Flow bearish + not crowded  -> -5
      Flow bearish + crowded      -> -2  (already priced in, less marginal downside)
      Bullish price divergence    -> +3 additional (price down, specs accumulating)
      Bearish price divergence    -> -3 additional (price up, specs distributing)
      Neutral flow                -> 0

    Returns (adjustment_int, detail_dict | None).
    detail_dict is always returned (even when adjustment is 0) so the frontend
    can show "COT checked, no edge" rather than silently omitting the section —
    detail_dict is None only when the ticker has no COT coverage at all.
    """
    sym = TICKER_TO_COT.get(ticker)
    if not sym:
        return 0, None
    try:
        ck = f'cot_overlay:{sym}'
        cached = cache.get(ck)
        if cached is not None:
            pos = cached
        else:
            pos = compute_positioning(sym)
            cache.set(ck, pos, _COT_OVERLAY_CACHE_TTL)

        if not pos.get('available'):
            return 0, {'symbol': sym, 'applied': False, 'reason': pos.get('note', 'No COT data')}

        flow  = pos.get('flow_score')
        crowd = pos.get('crowding_score')
        if flow is None:
            return 0, {'symbol': sym, 'applied': False, 'reason': 'Flow score unavailable'}

        bullish_flow = flow >= 60
        bearish_flow = flow < 40
        crowded      = (crowd or 0) >= 70

        adj = 0
        reason = 'Flow neutral — no COT edge applied.'
        if bullish_flow and not crowded:
            adj, reason = 5, f'Flow {flow} bullish, not crowded ({crowd}) — confirming tailwind.'
        elif bullish_flow and crowded:
            adj, reason = 2, f'Flow {flow} bullish but crowded ({crowd}) — muted, chase risk elevated.'
        elif bearish_flow and not crowded:
            adj, reason = -5, f'Flow {flow} bearish, not crowded ({crowd}) — confirming headwind.'
        elif bearish_flow and crowded:
            adj, reason = -2, f'Flow {flow} bearish and crowded ({crowd}) — already priced in, muted.'

        div_label = (pos.get('divergence') or {}).get('label')
        div_adj = 0
        if div_label == 'BULLISH_DIVERGENCE':
            div_adj = 3
            reason += ' Bullish price divergence detected — specs accumulating into weakness.'
        elif div_label == 'BEARISH_DIVERGENCE':
            div_adj = -3
            reason += ' Bearish price divergence detected — specs distributing into strength.'

        total_adj = max(-8, min(8, adj + div_adj))
        return total_adj, {
            'symbol':       sym,
            'applied':      True,
            'adjustment':   total_adj,
            'flow_score':   flow,
            'crowding_score': crowd,
            'direction':    pos.get('direction'),
            'divergence':   div_label,
            'reason':       reason,
        }
    except Exception as e:
        print(f'[COT overlay] {ticker}/{sym} error: {e}')
        return 0, {'symbol': sym, 'applied': False, 'reason': f'Error: {e}'}


@app.route('/api/positioning/<symbol>')
def get_positioning(symbol):
    sym = symbol.upper()
    if sym not in COT_MARKETS:
        return jsonify({'error': f'No positioning data for {symbol}. Markets: ' + ', '.join(COT_MARKETS)}), 404
    return ok(compute_positioning(sym))


@app.route('/api/positioning')
def get_positioning_all():
    """Compute all COT markets in parallel — isolates per-market errors/slowness."""
    import concurrent.futures
    def _safe(sym):
        try:
            return compute_positioning(sym)
        except Exception as e:
            print(f'[positioning] {sym} error: {e}')
            return {'symbol': sym, 'available': False, 'note': str(e)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(_safe, COT_MARKETS.keys(), timeout=18))
    return ok({'markets': results})


@app.route('/api/cot/<symbol>')
def get_cot(symbol):
    sym = symbol.upper()
    if sym not in COT_MARKETS:
        return jsonify({'error': f'No COT data for {symbol}. Try: ' + ', '.join(COT_MARKETS)}), 404

    cached = cache.get(f'cot:{sym}')
    if cached:
        return jsonify(cached)

    data = fetch_cot_live(sym)
    if not data:
        data = {**COT_SAMPLE[sym], 'source': 'sample', 'report_date': ''}

    cache.set(f'cot:{sym}', data, 43200)  # 12h
    return jsonify(data)


@app.route('/api/cot/<symbol>/refresh')
def refresh_cot(symbol):
    cache.delete(f'cot:{symbol.upper()}')
    return jsonify({'cleared': True})


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

        time.sleep(0.5)  # Premium rate
        inc_data = av({'function': 'INCOME_STATEMENT', 'symbol': ticker})
        time.sleep(0.5)  # Premium rate
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
            'exchange': overview.get('Exchange',''),
            'price': round(price,2), 'change': change, 'changePct': change_pct,
            'week52High': round(w52hi,2), 'week52Low': round(w52lo,2),
            'peRatio': round(pe,1), 'fwdPE': round(fwd_pe,1), 'eps': round(eps,2),
            'peg': round(peg,2), 'priceBook': round(pb,2), 'beta': round(beta,2),
            'grossMargin': round(gross_m,1), 'netMargin': round(net_m,1), 'opMargin': round(op_m,1),
            'roe': round(roe,1), 'roa': round(roa,1), 'revenueGrowth': round(rev_g,1),
            'debtEquity': round(de,2), 'currentRatio': round(cr,2), 'quickRatio': 0,
            'fairValue': round(fv,2), 'analystTarget': round(tgt,2), 'mktCap': fmt(mkt_cap),
            'divYield': round(div_y,2), 'dividend': round(div,2),
            'buyCount': 0, 'holdCount': 0, 'sellCount': 0,
            'insiderOwn': 0, 'instOwn': 0, 'fcfYield': 0, 'shortRatio': 0,
            'totalCash': 'N/A', 'totalDebt': 'N/A', 'freeCashflow': 'N/A', 'opCashflow': 'N/A',
            'bull': round(max(tgt,fv)*1.2,2), 'base': round((tgt+fv)/2,2), 'bear': round(min(tgt,fv)*0.8,2),
            'score': sc['total'], 'grade': sc['grade'], 'verdict': sc['verdict'],
            'style': sc['style'], 'scores': sc.get('breakdown', {}),
            'revenue': revenue, 'earnings': earnings, 'revenueLabels': labels,
            'qRevenue': [], 'qEarnings': [], 'qLabels': [],
            'epsActual': [], 'epsEstimate': [], 'epsSurprise': [], 'epsLabels': [],
            'annEps': [], 'annEpsLabels': [],
        }
        # Only cache if no richer version exists (scanner data lacks quarterly/estimates)
        existing = cache_get(f'stock:{ticker}')
        if not existing or not existing.get('qRevenue'):
            cache_set(f'stock:{ticker}', stock_data)
        opp = opp_score(stock_data, cat)
        _scan_results[ticker] = {**stock_data, **opp, 'cat': cat, 'displayName': item['n'], 'scanned': int(time.time())}
        print(f"[scanner] {ticker} ✓ composite={opp['composite']} tier={opp['tier']}")
    except Exception as e:
        print(f"[scanner] {ticker} error: {e}")

def _cot_needs_refresh():
    """True if any COT market's latest stored point is more than 6 days old."""
    if not store:
        return False
    try:
        for sym in COT_MARKETS:
            series = store.get_series(f'cot_{sym}_specs_net', window_days=14, max_points=5)
            if not series:
                return True  # no data at all — needs initial backfill
            latest_ts = series[-1][0]
            age_days = (time.time() - latest_ts) / 86400
            if age_days > 6:
                return True
        return False
    except Exception as e:
        print(f'[cot-auto] freshness check error: {e}')
        return False


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
                    time.sleep(0.3)  # Premium key
                except Exception as e:
                    print(f"[scanner] item error {item.get('t')}: {e}")
                    continue
            _scan_status['last_run'] = int(time.time())
            print(f"[scanner] Scan complete — {len(_scan_results)} results")
        except Exception as e:
            print(f"[scanner] Thread error: {e}")

        # ── Setups matrix pre-warm ──
        try:
            if not cache.get('setups:matrix'):
                print("[scanner] Pre-warming setups matrix...")
                build_setups_matrix()
                print("[scanner] Setups matrix pre-warm done")
        except Exception as e:
            print(f"[scanner] setups pre-warm error: {e}")

        # ── COT auto-refresh — CFTC releases new data every Friday ──
        # Checked on the same 30-min cadence as the stock scanner; only
        # triggers a real fetch when stored data is genuinely stale (>6 days).
        try:
            if _cot_needs_refresh():
                print("[cot-auto] Stored COT data is stale — refreshing from CFTC...")
                res = backfill_cot(weeks=12)  # only need recent weeks for a freshness top-up
                print(f"[cot-auto] Refresh complete: {res}")
        except Exception as e:
            print(f"[cot-auto] refresh error: {e}")

        # ── Rotation pre-warm — keep cache hot so first user visit is fast ──
        try:
            if ROTATION_AVAILABLE and not cache.get('rotation:snapshot'):
                import json as _json
                print("[rotation-warm] Cache cold — pre-warming rotation snapshot...")
                result, _ = _compute_rotation_snapshot()
                result_safe = _json.loads(_json.dumps(result, default=lambda x: float(x) if hasattr(x, '__float__') else str(x)))
                cache.set('rotation:snapshot', result_safe, 1800)
                print(f"[rotation-warm] Done — {len(result_safe.get('themes', []))} themes cached")
        except Exception as e:
            print(f"[rotation-warm] error: {e}")

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

    # Setups matrix pre-warm — runs immediately on startup
    def _warm_setups():
        import time as _t
        _t.sleep(10)  # after price cache has had a chance to populate
        try:
            if not cache.get('setups:matrix'):
                print("[setups-warm] Starting startup pre-warm...")
                build_setups_matrix()
                print("[setups-warm] Done")
        except Exception as e:
            print(f"[setups-warm] error: {e}")
    threading.Thread(target=_warm_setups, daemon=True).start()

    # Rotation pre-warm — runs immediately on startup in its own thread
    # so the 30-min scanner cycle doesn't delay the first cache build.
    if ROTATION_AVAILABLE:
        def _warm_rotation():
            import time as _time, json as _json
            _time.sleep(5)  # let gunicorn finish binding before hammering Yahoo
            try:
                if not cache.get('rotation:snapshot'):
                    print("[rotation-warm] Starting immediate pre-warm...")
                    result, _ = _compute_rotation_snapshot()
                    result_safe = _json.loads(_json.dumps(result, default=lambda x: float(x) if hasattr(x, '__float__') else str(x)))
                    cache.set('rotation:snapshot', result_safe, 1800)
                    print(f"[rotation-warm] Done — {len(result_safe.get('themes', []))} themes cached")
            except Exception as e:
                print(f"[rotation-warm] startup error: {e}")
                import traceback; traceback.print_exc()
        threading.Thread(target=_warm_rotation, daemon=True).start()

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
# FRED_KEY set at top of file
FRED_BASE = 'https://api.stlouisfed.org/fred/series/observations'
_FRED_STATUS = {}   # series_id -> last HTTP status / 'error' (for diagnostics)
# Self-throttle so we stay under FRED's ~120 req/min ceiling and stop tripping 429s.
_FRED_LOCK = threading.Lock()
_FRED_LAST = [0.0]       # last request start time
_FRED_MIN_GAP = 0.6      # min seconds between FRED calls (~100/min)

def fred_last_status(series_id):
    return _FRED_STATUS.get(series_id)

# World Bank API — no key needed
WB_BASE = 'https://api.worldbank.org/v2/country/{country}/indicator/{indicator}'

# FRED series IDs for US indicators
FRED_SERIES = {
    'cpi':          'CPIAUCNS',       # CPI All Urban
    'core_cpi':     'CPILFENS',       # Core CPI (ex food/energy)
    'ppi':          'PPIFID',         # PPI Final Demand (headline)
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
        # Pace request starts so bursts (cold loads, multi-country) stay under the limit
        with _FRED_LOCK:
            wait = _FRED_MIN_GAP - (time.time() - _FRED_LAST[0])
            if wait > 0:
                time.sleep(wait)
            _FRED_LAST[0] = time.time()

        r = requests.get(FRED_BASE, params=params, timeout=15)
        _FRED_STATUS[series_id] = r.status_code
        print(f'[macro] FRED {series_id}: {r.status_code}')
        if r.status_code == 429:
            # rate-limited — wait long enough for the window to clear, then retry once
            time.sleep(3.0)
            with _FRED_LOCK:
                _FRED_LAST[0] = time.time()
            r = requests.get(FRED_BASE, params=params, timeout=15)
            _FRED_STATUS[series_id] = r.status_code
            print(f'[macro] FRED {series_id}: {r.status_code} (retry)')
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
        _FRED_STATUS[series_id] = f'error: {type(e).__name__}'
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
    # Updated May 2026 from Forex Factory & Federal Reserve
    'US':       {'gdp_growth': 1.6,  'inflation': 3.8,  'unemployment': 4.3,  'rate': 4.33},
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
    'NZDJPY': 'NZDJPY=X',
    'EURCHF': 'EURCHF=X', 'GBPCHF': 'GBPCHF=X', 'AUDCHF': 'AUDCHF=X',
    'CADCHF': 'CADCHF=X', 'NZDCHF': 'NZDCHF=X',
    'AUDCAD': 'AUDCAD=X', 'AUDNZD': 'AUDNZD=X', 'NZDCAD': 'NZDCAD=X',
    'EURCAD': 'EURCAD=X', 'GBPCAD': 'GBPCAD=X',
    'EURNZD': 'EURNZD=X', 'GBPNZD': 'GBPNZD=X',
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
    'CADJPY': ('CAD','JPY'), 'CHFJPY': ('CHF','JPY'), 'NZDJPY': ('NZD','JPY'),
    'EURCHF': ('EUR','CHF'), 'GBPCHF': ('GBP','CHF'), 'AUDCHF': ('AUD','CHF'),
    'CADCHF': ('CAD','CHF'), 'NZDCHF': ('NZD','CHF'),
    'AUDCAD': ('AUD','CAD'), 'AUDNZD': ('AUD','NZD'), 'NZDCAD': ('NZD','CAD'),
    'EURCAD': ('EUR','CAD'), 'GBPCAD': ('GBP','CAD'),
    'EURNZD': ('EUR','NZD'), 'GBPNZD': ('GBP','NZD'),
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
    # Debug: log top/bottom currencies per timeframe
    if strength:
        ranked = sorted(strength.items(), key=lambda x: x[1]['strength'], reverse=True)
        print(f"[forex] {timeframe} ranking: {' > '.join(f"{c}({d['strength']})" for c,d in ranked[:4])} ... {' < '.join(f"{c}({d['strength']})" for c,d in ranked[-2:])}")
    carries  = calc_carry_trades(strength)
    eq_sigs  = calc_equity_correlation(pair_data)

    # Build heat map matrix — pct change for each cross
    matrix = {}
    pair_map = {
        'EURUSD':('EUR','USD'),'GBPUSD':('GBP','USD'),'AUDUSD':('AUD','USD'),
        'NZDUSD':('NZD','USD'),'USDCAD':('USD','CAD'),'USDCHF':('USD','CHF'),
        'USDJPY':('USD','JPY'),'USDCNY':('USD','CNY'),'EURGBP':('EUR','GBP'),
        'EURJPY':('EUR','JPY'),'GBPJPY':('GBP','JPY'),'AUDJPY':('AUD','JPY'),
        'CADJPY':('CAD','JPY'),'CHFJPY':('CHF','JPY'),'NZDJPY':('NZD','JPY'),
        'EURCHF':('EUR','CHF'),'GBPCHF':('GBP','CHF'),'AUDCHF':('AUD','CHF'),
        'CADCHF':('CAD','CHF'),'NZDCHF':('NZD','CHF'),
        'AUDCAD':('AUD','CAD'),'AUDNZD':('AUD','NZD'),'NZDCAD':('NZD','CAD'),
        'EURCAD':('EUR','CAD'),'GBPCAD':('GBP','CAD'),
        'EURNZD':('EUR','NZD'),'GBPNZD':('GBP','NZD'),
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


# ══════════════════════════════════════════════════════════════════
# ◈ MARKETS HUB — Multi-asset intelligence, auto-scored
# ══════════════════════════════════════════════════════════════════

MARKETS_UNIVERSE = {
    'indices': [
        # US Indices
        {'t':'SPY',  'n':'S&P 500',          'region':'US'},
        {'t':'QQQ',  'n':'Nasdaq 100',        'region':'US'},
        {'t':'DIA',  'n':'Dow Jones',         'region':'US'},
        {'t':'IWM',  'n':'Russell 2000',      'region':'US'},
        {'t':'VT',   'n':'World Stocks',      'region':'US'},
        # International
        {'t':'EWU',  'n':'UK FTSE 100',       'region':'UK'},
        {'t':'EZU',  'n':'Eurozone',          'region':'EU'},
        {'t':'EWJ',  'n':'Japan Nikkei',      'region':'JP'},
        {'t':'MCHI', 'n':'China CSI',         'region':'CN'},
        {'t':'EWG',  'n':'Germany DAX',       'region':'DE'},
        {'t':'EWA',  'n':'Australia ASX',     'region':'AU'},
        {'t':'EWC',  'n':'Canada TSX',        'region':'CA'},
        {'t':'EWZ',  'n':'Brazil Bovespa',    'region':'BR'},
        {'t':'EEM',  'n':'Emerging Markets',  'region':'EM'},
        {'t':'VEA',  'n':'Developed Markets', 'region':'INT'},
    ],
    'commodities': [
        {'t':'GLD',  'n':'Gold',              'unit':'$/oz'},
        {'t':'SLV',  'n':'Silver',            'unit':'$/oz'},
        {'t':'GDX',  'n':'Gold Miners',       'unit':'ETF'},
        {'t':'GDXJ', 'n':'Jr Gold Miners',    'unit':'ETF'},
        {'t':'USO',  'n':'Crude Oil',         'unit':'$/bbl'},
        {'t':'UNG',  'n':'Natural Gas',       'unit':'ETF'},
        {'t':'CORN', 'n':'Corn',              'unit':'ETF'},
        {'t':'WEAT', 'n':'Wheat',             'unit':'ETF'},
        {'t':'CPER', 'n':'Copper',            'unit':'ETF'},
        {'t':'PPLT', 'n':'Platinum',          'unit':'ETF'},
        {'t':'DBO',  'n':'Oil Fund',          'unit':'ETF'},
        {'t':'PALL', 'n':'Palladium',         'unit':'ETF'},
    ],
    'bonds': [
        {'t':'TLT',  'n':'US 20Y Treasury',   'yield_proxy':True},
        {'t':'IEF',  'n':'US 10Y Treasury',   'yield_proxy':True},
        {'t':'SHY',  'n':'US 2Y Treasury',    'yield_proxy':True},
        {'t':'HYG',  'n':'High Yield Corp',   'yield_proxy':False},
        {'t':'LQD',  'n':'Investment Grade',  'yield_proxy':False},
        {'t':'TIP',  'n':'TIPS Inflation',    'yield_proxy':False},
        {'t':'EMB',  'n':'EM Bonds',          'yield_proxy':False},
        {'t':'BND',  'n':'Total Bond Market', 'yield_proxy':False},
    ],
    'forex_etf': [
        {'t':'UUP',  'n':'USD (Dollar Index)', 'currency':'USD'},
        {'t':'FXE',  'n':'EUR (Euro)',          'currency':'EUR'},
        {'t':'FXB',  'n':'GBP (Sterling)',      'currency':'GBP'},
        {'t':'FXY',  'n':'JPY (Yen)',           'currency':'JPY'},
        {'t':'FXA',  'n':'AUD (Aussie)',        'currency':'AUD'},
        {'t':'FXC',  'n':'CAD (Canadian)',      'currency':'CAD'},
        {'t':'FXF',  'n':'CHF (Swiss)',         'currency':'CHF'},
    ],
    # ── S&P 500 by sector ──────────────────────────────────────
    'tech': [
        {'t':'AAPL', 'n':'Apple',                'region':'US'},
        {'t':'MSFT', 'n':'Microsoft',            'region':'US'},
        {'t':'NVDA', 'n':'NVIDIA',               'region':'US'},
        {'t':'GOOGL','n':'Alphabet',             'region':'US'},
        {'t':'META', 'n':'Meta',                 'region':'US'},
        {'t':'AMZN', 'n':'Amazon',               'region':'US'},
        {'t':'TSM',  'n':'TSMC',                 'region':'US'},
        {'t':'AMD',  'n':'AMD',                  'region':'US'},
        {'t':'AVGO', 'n':'Broadcom',             'region':'US'},
        {'t':'ORCL', 'n':'Oracle',               'region':'US'},
        {'t':'CRM',  'n':'Salesforce',           'region':'US'},
        {'t':'NOW',  'n':'ServiceNow',           'region':'US'},
        {'t':'ASML', 'n':'ASML',                 'region':'US'},
        {'t':'INTC', 'n':'Intel',                'region':'US'},
        {'t':'QCOM', 'n':'Qualcomm',             'region':'US'},
        {'t':'TXN',  'n':'Texas Instruments',    'region':'US'},
        {'t':'MU',   'n':'Micron',               'region':'US'},
        {'t':'AMAT', 'n':'Applied Materials',    'region':'US'},
        {'t':'LRCX', 'n':'Lam Research',         'region':'US'},
        {'t':'ADBE', 'n':'Adobe',                'region':'US'},
        {'t':'SNOW', 'n':'Snowflake',            'region':'US'},
        {'t':'PLTR', 'n':'Palantir',             'region':'US'},
        {'t':'UBER', 'n':'Uber',                 'region':'US'},
        {'t':'SHOP', 'n':'Shopify',              'region':'US'},
        {'t':'NET',  'n':'Cloudflare',           'region':'US'},
    ],
    'financials': [
        {'t':'JPM',  'n':'JPMorgan',             'region':'US'},
        {'t':'BAC',  'n':'Bank of America',      'region':'US'},
        {'t':'WFC',  'n':'Wells Fargo',          'region':'US'},
        {'t':'GS',   'n':'Goldman Sachs',        'region':'US'},
        {'t':'MS',   'n':'Morgan Stanley',       'region':'US'},
        {'t':'BLK',  'n':'BlackRock',            'region':'US'},
        {'t':'V',    'n':'Visa',                 'region':'US'},
        {'t':'MA',   'n':'Mastercard',           'region':'US'},
        {'t':'AXP',  'n':'Amex',                 'region':'US'},
        {'t':'PYPL', 'n':'PayPal',               'region':'US'},
        {'t':'SCHW', 'n':'Charles Schwab',       'region':'US'},
        {'t':'C',    'n':'Citigroup',            'region':'US'},
        {'t':'USB',  'n':'US Bancorp',           'region':'US'},
        {'t':'BX',   'n':'Blackstone',           'region':'US'},
        {'t':'ICE',  'n':'Intercontinental Exch','region':'US'},
    ],
    'healthcare': [
        {'t':'LLY',  'n':'Eli Lilly',            'region':'US'},
        {'t':'JNJ',  'n':'Johnson & Johnson',    'region':'US'},
        {'t':'UNH',  'n':'UnitedHealth',         'region':'US'},
        {'t':'ABBV', 'n':'AbbVie',               'region':'US'},
        {'t':'MRK',  'n':'Merck',                'region':'US'},
        {'t':'TMO',  'n':'Thermo Fisher',        'region':'US'},
        {'t':'ABT',  'n':'Abbott Labs',          'region':'US'},
        {'t':'DHR',  'n':'Danaher',              'region':'US'},
        {'t':'PFE',  'n':'Pfizer',               'region':'US'},
        {'t':'AMGN', 'n':'Amgen',                'region':'US'},
        {'t':'GILD', 'n':'Gilead',               'region':'US'},
        {'t':'ISRG', 'n':'Intuitive Surgical',   'region':'US'},
        {'t':'VRTX', 'n':'Vertex Pharma',        'region':'US'},
        {'t':'REGN', 'n':'Regeneron',            'region':'US'},
        {'t':'BSX',  'n':'Boston Scientific',    'region':'US'},
    ],
    'consumer': [
        {'t':'TSLA', 'n':'Tesla',                'region':'US'},
        {'t':'WMT',  'n':'Walmart',              'region':'US'},
        {'t':'COST', 'n':'Costco',               'region':'US'},
        {'t':'HD',   'n':'Home Depot',           'region':'US'},
        {'t':'MCD',  'n':'McDonalds',           'region':'US'},
        {'t':'NKE',  'n':'Nike',                 'region':'US'},
        {'t':'SBUX', 'n':'Starbucks',            'region':'US'},
        {'t':'TGT',  'n':'Target',               'region':'US'},
        {'t':'LOW',  'n':'Lowes',              'region':'US'},
        {'t':'BKNG', 'n':'Booking Holdings',     'region':'US'},
        {'t':'ABNB', 'n':'Airbnb',               'region':'US'},
        {'t':'NFLX', 'n':'Netflix',              'region':'US'},
        {'t':'DIS',  'n':'Disney',               'region':'US'},
        {'t':'AMZN', 'n':'Amazon Consumer',      'region':'US'},
        {'t':'LULU', 'n':'Lululemon',            'region':'US'},
    ],
    'energy': [
        {'t':'XOM',  'n':'ExxonMobil',           'region':'US'},
        {'t':'CVX',  'n':'Chevron',              'region':'US'},
        {'t':'COP',  'n':'ConocoPhillips',       'region':'US'},
        {'t':'SLB',  'n':'SLB (Schlumberger)',   'region':'US'},
        {'t':'EOG',  'n':'EOG Resources',        'region':'US'},
        {'t':'PXD',  'n':'Pioneer Natural',      'region':'US'},
        {'t':'OXY',  'n':'Occidental',           'region':'US'},
        {'t':'MPC',  'n':'Marathon Petroleum',   'region':'US'},
        {'t':'PSX',  'n':'Phillips 66',          'region':'US'},
        {'t':'VLO',  'n':'Valero Energy',        'region':'US'},
        {'t':'XLE',  'n':'Energy Sector ETF',    'region':'US'},
        {'t':'BP',   'n':'BP plc',               'region':'UK'},
        {'t':'SHEL', 'n':'Shell',                'region':'UK'},
        {'t':'TTE',  'n':'TotalEnergies',        'region':'EU'},
    ],
    'industrials': [
        {'t':'CAT',  'n':'Caterpillar',          'region':'US'},
        {'t':'DE',   'n':'John Deere',           'region':'US'},
        {'t':'HON',  'n':'Honeywell',            'region':'US'},
        {'t':'UPS',  'n':'UPS',                  'region':'US'},
        {'t':'RTX',  'n':'RTX Corp',             'region':'US'},
        {'t':'LMT',  'n':'Lockheed Martin',      'region':'US'},
        {'t':'GE',   'n':'GE Aerospace',         'region':'US'},
        {'t':'BA',   'n':'Boeing',               'region':'US'},
        {'t':'NOC',  'n':'Northrop Grumman',     'region':'US'},
        {'t':'GD',   'n':'General Dynamics',     'region':'US'},
        {'t':'MMM',  'n':'3M',                   'region':'US'},
        {'t':'FDX',  'n':'FedEx',                'region':'US'},
        {'t':'CSX',  'n':'CSX Rail',             'region':'US'},
        {'t':'EMR',  'n':'Emerson Electric',     'region':'US'},
    ],
    'ftse100': [
        # FTSE 100 — major UK stocks (Yahoo uses .L suffix)
        {'t':'AZN',  'n':'AstraZeneca',          'region':'UK'},
        {'t':'HSBA.L','n':'HSBC',                'region':'UK'},
        {'t':'ULVR.L','n':'Unilever',            'region':'UK'},
        {'t':'RIO',  'n':'Rio Tinto',            'region':'UK'},
        {'t':'BP',   'n':'BP',                   'region':'UK'},
        {'t':'SHEL', 'n':'Shell',                'region':'UK'},
        {'t':'GSK',  'n':'GSK',                  'region':'UK'},
        {'t':'DGE.L','n':'Diageo',               'region':'UK'},
        {'t':'REL.L','n':'RELX',                 'region':'UK'},
        {'t':'EXPN.L','n':'Experian',            'region':'UK'},
        {'t':'NG.L', 'n':'National Grid',        'region':'UK'},
        {'t':'LSEG.L','n':'London Stock Exch',   'region':'UK'},
        {'t':'RR.L', 'n':'Rolls-Royce',          'region':'UK'},
        {'t':'VOD',  'n':'Vodafone',             'region':'UK'},
        {'t':'BT-A.L','n':'BT Group',            'region':'UK'},
        {'t':'BARC.L','n':'Barclays',            'region':'UK'},
        {'t':'LLOY.L','n':'Lloyds Banking',      'region':'UK'},
        {'t':'NWG.L','n':'NatWest Group',        'region':'UK'},
        {'t':'IMB.L','n':'Imperial Brands',      'region':'UK'},
        {'t':'BATS.L','n':'BAT',                 'region':'UK'},
    ],
    'global': [
        # Major global stocks
        {'t':'SAP',  'n':'SAP SE',               'region':'DE'},
        {'t':'ASML', 'n':'ASML',                 'region':'EU'},
        {'t':'NVO',  'n':'Novo Nordisk',         'region':'EU'},
        {'t':'LVMH.PA','n':'LVMH',              'region':'EU'},
        {'t':'MC.PA','n':'LVMH (Paris)',         'region':'EU'},
        {'t':'TM',   'n':'Toyota',              'region':'JP'},
        {'t':'SONY', 'n':'Sony',                'region':'JP'},
        {'t':'9984.T','n':'SoftBank',           'region':'JP'},
        {'t':'BABA', 'n':'Alibaba',             'region':'CN'},
        {'t':'TCEHY','n':'Tencent',             'region':'CN'},
        {'t':'PDD',  'n':'PDD Holdings',        'region':'CN'},
        {'t':'SE',   'n':'Sea Limited (SEA)',   'region':'EM'},
        {'t':'NU',   'n':'Nu Holdings',         'region':'BR'},
        {'t':'VALE', 'n':'Vale SA',             'region':'BR'},
        {'t':'SHOP', 'n':'Shopify',             'region':'CA'},
        {'t':'RY',   'n':'Royal Bank Canada',  'region':'CA'},
        {'t':'TD',   'n':'TD Bank',             'region':'CA'},
        {'t':'BHP',  'n':'BHP Group',           'region':'AU'},
        {'t':'CBA.AX','n':'Commonwealth Bank', 'region':'AU'},
        {'t':'WBC.AX','n':'Westpac',           'region':'AU'},
    ],
    'sector_etfs': [
        {'t':'XLK',  'n':'Tech ETF',             'region':'US'},
        {'t':'XLF',  'n':'Financials ETF',       'region':'US'},
        {'t':'XLV',  'n':'Healthcare ETF',       'region':'US'},
        {'t':'XLI',  'n':'Industrials ETF',      'region':'US'},
        {'t':'XLP',  'n':'Staples ETF',          'region':'US'},
        {'t':'XLU',  'n':'Utilities ETF',        'region':'US'},
        {'t':'XLRE', 'n':'Real Estate ETF',      'region':'US'},
        {'t':'XLB',  'n':'Materials ETF',        'region':'US'},
        {'t':'XLE',  'n':'Energy ETF',           'region':'US'},
        {'t':'XLC',  'n':'Comms ETF',            'region':'US'},
        {'t':'XLY',  'n':'Consumer Disc ETF',    'region':'US'},
    ],
}

def get_macro_context():
    """
    Pull current macro context for use in asset scoring.
    Returns a dict of key macro signals.
    Cached for 5 minutes.
    """
    cached = cache.get('macro:context')
    if cached: return cached

    ctx = {
        'usd_chg':    0,    # USD 1-day % change (positive = USD strong)
        'vix':        18,   # VIX level
        'sp_chg':     0,    # SPY 1-day % change
        'gold_chg':   0,    # GLD 1-day % change
        'tlt_chg':    0,    # TLT 1-day % change (positive = yields falling)
        'oil_chg':    0,    # USO 1-day % change
        'us_cpi':     4.0,  # Latest US CPI YoY %
        'us_gdp':     2.0,  # Latest US GDP growth %
        'regime':     'NEUTRAL',  # RISK-ON / NEUTRAL / RISK-OFF
    }

    try:
        # Live prices for key macro instruments
        uup  = get_live_price('UUP')   # USD ETF
        spy  = get_live_price('SPY')
        vix  = get_live_price('^VIX')
        tlt  = get_live_price('TLT')
        gld  = get_live_price('GLD')
        uso  = get_live_price('USO')

        if uup:  ctx['usd_chg']  = uup['changePct']
        if spy:  ctx['sp_chg']   = spy['changePct']
        if vix:  ctx['vix']      = vix['price']
        if tlt:  ctx['tlt_chg']  = tlt['changePct']
        if gld:  ctx['gold_chg'] = gld['changePct']
        if uso:  ctx['oil_chg']  = uso['changePct']

        # Get CPI from FRED if available
        if FRED_KEY:
            cpi_data = get_fred_series('CPIAUCNS', years=2)
            if cpi_data and len(cpi_data) > 12:
                curr = cpi_data[-1]['value']
                prev = cpi_data[-13]['value']
                if prev: ctx['us_cpi'] = round((curr - prev) / prev * 100, 2)

        # Determine regime
        v = ctx['vix']
        s = ctx['sp_chg']
        if   v > 25:                     ctx['regime'] = 'RISK-OFF'
        elif v > 18 and s < 0:           ctx['regime'] = 'CAUTIOUS'
        elif v < 15 and s > 0:           ctx['regime'] = 'RISK-ON'
        elif s > 0.3:                    ctx['regime'] = 'RISK-ON'
        elif s < -0.3:                   ctx['regime'] = 'CAUTIOUS'
        else:                            ctx['regime'] = 'NEUTRAL'

    except Exception as e:
        print(f'[macro_ctx] error: {e}')

    cache.set('macro:context', ctx, TTL['macro'])
    return ctx


def score_asset(ticker, changePct, w52hi, w52lo, price, asset_type='default', item=None, macro=None):
    """
    Multi-factor scoring — returns -10 to +10 scale like Edge Finder.
    Broken into 3 sub-scores:
      technical:    -4 to +4  (range position + momentum)
      macro:        -3 to +3  (regime, USD, rates, inflation)
      fundamental:  -3 to +3  (rate differentials, econ health, asset-specific)
    Composite = sum, clamped to -10..+10
    """
    if not price or price <= 0:
        return 0, 'NEUTRAL', [], 50, {'technical':0,'macro_score':0,'fundamental':0}

    if macro is None:
        macro = get_macro_context()

    signals  = []
    item     = item or {}

    regime   = macro.get('regime', 'NEUTRAL')
    usd_chg  = macro.get('usd_chg', 0)
    vix      = macro.get('vix', 18)
    us_cpi   = macro.get('us_cpi', 3.0)
    tlt_chg  = macro.get('tlt_chg', 0)
    sp_chg   = macro.get('sp_chg', 0)

    # ── 52w range position ───────────────────────────────────────
    if w52hi > w52lo > 0:
        range_pos = (price - w52lo) / (w52hi - w52lo) * 100
    else:
        range_pos = 50

    # ── TECHNICAL SCORE (-4 to +4) ──────────────────────────────
    # Range position component (-2 to +2)
    if   range_pos <= 10:  range_pts = 2;  signals.append('Near 52w low')
    elif range_pos <= 25:  range_pts = 1
    elif range_pos <= 50:  range_pts = 0
    elif range_pos <= 75:  range_pts = -1
    elif range_pos <= 90:  range_pts = -1
    else:                  range_pts = -2; signals.append('Near 52w high')

    # Momentum component (-2 to +2)
    if   changePct >= 3.0: mom_pts = 2;  signals.append(f'+{changePct:.1f}% strong move')
    elif changePct >= 1.0: mom_pts = 1
    elif changePct >= -1.0:mom_pts = 0
    elif changePct >= -3.0:mom_pts = -1
    else:                  mom_pts = -2; signals.append(f'{changePct:.1f}% heavy selling')

    technical = range_pts + mom_pts  # -4 to +4

    # ── MACRO SCORE (-3 to +3) ───────────────────────────────────
    macro_pts = 0

    if asset_type == 'indices':
        # Regime
        reg = {'RISK-ON':2,'NEUTRAL':0,'CAUTIOUS':-1,'RISK-OFF':-2}.get(regime, 0)
        macro_pts += reg
        if regime == 'RISK-ON':    signals.append('Risk-on tailwind')
        elif regime == 'RISK-OFF': signals.append('Risk-off headwind')
        # VIX
        if   vix < 14: macro_pts += 1
        elif vix > 25: macro_pts -= 1; signals.append(f'VIX {vix:.0f} elevated')

    elif asset_type == 'commodities':
        # USD (inverse)
        if   usd_chg > 0.5:  macro_pts -= 2; signals.append('USD strength = headwind')
        elif usd_chg > 0.1:  macro_pts -= 1
        elif usd_chg < -0.5: macro_pts += 2; signals.append('USD weakness = tailwind')
        elif usd_chg < -0.1: macro_pts += 1
        # Regime
        if ticker in ('GLD','SLV','GDX','GDXJ'):
            reg = {'RISK-OFF':2,'CAUTIOUS':1,'NEUTRAL':0,'RISK-ON':-1}.get(regime, 0)
        else:
            reg = {'RISK-ON':1,'NEUTRAL':0,'CAUTIOUS':-1,'RISK-OFF':-2}.get(regime, 0)
        macro_pts += reg

    elif asset_type == 'bonds':
        # Yield direction
        if   tlt_chg > 0.5:  macro_pts += 2; signals.append('Yields falling')
        elif tlt_chg > 0.1:  macro_pts += 1
        elif tlt_chg < -0.5: macro_pts -= 2; signals.append('Yields rising')
        elif tlt_chg < -0.1: macro_pts -= 1
        # Regime
        if ticker in ('TLT','IEF','SHY','TIP'):
            reg = {'RISK-OFF':1,'CAUTIOUS':1,'NEUTRAL':0,'RISK-ON':-1}.get(regime, 0)
        else:  # HYG/LQD = credit, acts like equities
            reg = {'RISK-ON':1,'NEUTRAL':0,'CAUTIOUS':-1,'RISK-OFF':-2}.get(regime, 0)
        macro_pts += reg

    elif asset_type == 'forex_etf':
        # USD regime affects most pairs
        currency = item.get('currency','')
        if currency == 'USD':
            reg = {'RISK-ON':1,'NEUTRAL':0,'CAUTIOUS':0,'RISK-OFF':1}.get(regime, 0)
        elif currency in ('JPY','CHF'):
            reg = {'RISK-OFF':2,'CAUTIOUS':1,'NEUTRAL':0,'RISK-ON':-1}.get(regime, 0)
            if regime in ('RISK-OFF','CAUTIOUS'): signals.append(f'{currency} safe haven bid')
        else:
            reg = {'RISK-ON':1,'NEUTRAL':0,'CAUTIOUS':-1,'RISK-OFF':-2}.get(regime, 0)
        macro_pts += reg

    macro_score = max(-3, min(3, macro_pts))

    # ── FUNDAMENTAL SCORE (-3 to +3) ────────────────────────────
    fund_pts = 0

    if asset_type == 'indices':
        region = item.get('region','US')
        snap = CURRENT_MACRO_SNAPSHOT.get(
            'US' if region=='US' else 'UK' if region=='UK' else
            'Eurozone' if region in ('EU','EZ') else 'China' if region=='CN' else
            'Japan' if region=='JP' else 'Germany' if region=='DE' else 'US', {}
        )
        gdp = snap.get('gdp_growth', 1.5)
        inf = snap.get('inflation', 3.0)
        # GDP
        if   gdp >= 3:    fund_pts += 2; signals.append(f'GDP +{gdp}%')
        elif gdp >= 1.5:  fund_pts += 1
        elif gdp >= 0:    fund_pts += 0
        else:             fund_pts -= 2; signals.append(f'GDP contraction')
        # Inflation impact on equities
        if   inf > 5:     fund_pts -= 1; signals.append(f'High inflation {inf}%')
        elif inf < 2.5:   fund_pts += 1

    elif asset_type == 'commodities':
        # Inflation
        if   us_cpi > 4:   fund_pts += 2; signals.append(f'CPI {us_cpi}% supports metals')
        elif us_cpi > 2.5: fund_pts += 1
        elif us_cpi < 2:   fund_pts -= 1

    elif asset_type == 'bonds':
        # Inflation is enemy of bonds
        if   us_cpi > 5:   fund_pts -= 2; signals.append(f'CPI {us_cpi}% — real yield risk')
        elif us_cpi > 3.5: fund_pts -= 1
        elif us_cpi < 2.5: fund_pts += 2; signals.append('Low inflation favours bonds')
        elif us_cpi < 3.5: fund_pts += 1

    elif asset_type == 'forex_etf':
        currency = item.get('currency','')
        rate_map = {'USD':4.33,'EUR':2.00,'GBP':3.75,'JPY':0.50,'CHF':0.00,'AUD':4.35,'CAD':2.75,'NZD':2.25}
        avg_rate = 2.77
        cur_rate = rate_map.get(currency, avg_rate)
        diff = cur_rate - avg_rate
        if   diff > 1.5:  fund_pts += 2; signals.append(f'{currency} yield {cur_rate}% — carry appeal')
        elif diff > 0.5:  fund_pts += 1
        elif diff < -1.5: fund_pts -= 2; signals.append(f'{currency} low yield — funding')
        elif diff < -0.5: fund_pts -= 1
        # Econ health
        econ_map = {'USD':'US','EUR':'Eurozone','GBP':'UK','JPY':'Japan','CHF':'Germany','AUD':'US','CAD':'US'}
        snap = CURRENT_MACRO_SNAPSHOT.get(econ_map.get(currency,'US'),{})
        gdp  = snap.get('gdp_growth',1.5)
        if   gdp >= 3:   fund_pts += 1
        elif gdp < 0:    fund_pts -= 1

    fundamental = max(-3, min(3, fund_pts))

    # ── COMPOSITE ────────────────────────────────────────────────
    composite = max(-10, min(10, technical + macro_score + fundamental))

    if   composite >= 3:  direction = 'BULLISH'
    elif composite <= -3: direction = 'BEARISH'
    else:                 direction = 'NEUTRAL'

    sub_scores = {'technical': technical, 'macro_score': macro_score, 'fundamental': fundamental}
    return composite, direction, signals[:2], round(range_pos, 1), sub_scores



@app.route('/api/stock/<ticker>/refresh')
def refresh_stock(ticker):
    ticker = ticker.upper().strip()
    cache_set(f'stock:{ticker}', None)   # overwrite with None forces re-fetch
    # Actually delete it
    try:
        k = f'legacy:stock:{ticker}'
        with cache._lock:
            cache._store.pop(k, None)
    except: pass
    return ok({'cleared': ticker})

@app.route('/api/markets/refresh')
def refresh_markets():
    cache.delete('markets:full')
    cache.delete('macro:context')
    cache.delete('heatmap:us')
    return ok({'cleared': True})

@app.route('/api/markets')
def get_markets():
    import concurrent.futures, traceback
    cached = cache.get('markets:full')
    if cached: return ok(cached, cached=True)

    try:
        all_items = [(ac, item) for ac, items in MARKETS_UNIVERSE.items() for item in items]

        def fetch_one(args):
            ac, item = args
            try:
                live = get_live_price(item['t'])
                return ac, item['t'], item, live
            except:
                return ac, item['t'], item, None

        # Parallel fetch with safe timeout
        prices = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(fetch_one, a) for a in all_items]
            for f in concurrent.futures.as_completed(futs, timeout=25):
                try:
                    ac, t, item, live = f.result()
                    if live: prices[(ac, t)] = (item, live)
                except: pass

        print(f'[markets] fetched {len(prices)}/{len(all_items)} prices')
        macro = get_macro_context()
        result = {k: [] for k in MARKETS_UNIVERSE}
        all_setups = []

        for asset_class, items in MARKETS_UNIVERSE.items():
            for item in items:
                key = (asset_class, item['t'])
                if key not in prices: continue
                _, live = prices[key]
                price, changePct = live['price'], live['changePct']
                w52hi, w52lo = live.get('week52High',0), live.get('week52Low',0)
                score_type = {
                    'tech':'indices','financials':'indices','healthcare':'indices',
                    'consumer':'indices','industrials':'indices','ftse100':'indices',
                    'global':'indices','sector_etfs':'indices','energy':'commodities',
                }.get(asset_class, asset_class)
                score, direction, signals, range_pos, subs = score_asset(
                    item['t'], changePct, w52hi, w52lo, price,
                    asset_type=score_type, item=item, macro=macro
                )
                entry = {
                    **item,
                    'price': round(price,2), 'changePct': round(changePct,3),
                    'w52hi': round(w52hi,2), 'w52lo': round(w52lo,2),
                    'score': score, 'direction': direction,
                    'signals': signals, 'rangePos': range_pos,
                    'assetClass': asset_class,
                    'technical': subs['technical'],
                    'macroScore': subs['macro_score'],
                    'fundamental': subs['fundamental'],
                }
                result[asset_class].append(entry)
                if abs(score) >= 3: all_setups.append(entry)
            result[asset_class].sort(key=lambda x: x['score'], reverse=True)

        all_setups.sort(key=lambda x: abs(x['score']), reverse=True)
        result['top_setups'] = all_setups[:8]
        result['macro'] = macro
        cache.set('markets:full', result, TTL['quote'])
        return ok(result)

    except Exception as e:
        traceback.print_exc()
        return service_error(f'Markets scan failed: {str(e)}')


# ══════════════════════════════════════════════════════════════════
# ◈ ASSET SCORECARD — EdgeFinder-style multi-factor bias engine
# ══════════════════════════════════════════════════════════════════

SCORECARD_ASSETS = {
    # Indices
    'SPY':  {'n':'S&P 500',        'type':'index',     'region':'US'},
    'QQQ':  {'n':'Nasdaq 100',     'type':'index',     'region':'US'},
    'DIA':  {'n':'Dow Jones',      'type':'index',     'region':'US'},
    'IWM':  {'n':'Russell 2000',   'type':'index',     'region':'US'},
    'EWU':  {'n':'UK FTSE 100',    'type':'index',     'region':'UK'},
    'EZU':  {'n':'Eurozone',       'type':'index',     'region':'EU'},
    'EWJ':  {'n':'Japan Nikkei',   'type':'index',     'region':'JP'},
    'MCHI': {'n':'China CSI',      'type':'index',     'region':'CN'},
    'EEM':  {'n':'Emerging Markets','type':'index',    'region':'EM'},
    # Commodities
    'GLD':  {'n':'Gold',           'type':'commodity', 'region':'US'},
    'SLV':  {'n':'Silver',         'type':'commodity', 'region':'US'},
    'GDX':  {'n':'Gold Miners',    'type':'commodity', 'region':'US'},
    'USO':  {'n':'Crude Oil',      'type':'commodity', 'region':'US'},
    'UNG':  {'n':'Natural Gas',    'type':'commodity', 'region':'US'},
    'CPER': {'n':'Copper',         'type':'commodity', 'region':'US'},
    # Bonds
    'TLT':  {'n':'US 20Y Treasury','type':'bond',      'region':'US'},
    'IEF':  {'n':'US 10Y Treasury','type':'bond',      'region':'US'},
    'SHY':  {'n':'US 2Y Treasury', 'type':'bond',      'region':'US'},
    'HYG':  {'n':'High Yield Corp','type':'bond',      'region':'US'},
    'TIP':  {'n':'TIPS Inflation', 'type':'bond',      'region':'US'},
    # Forex ETFs
    'UUP':  {'n':'USD Index',      'type':'forex',     'currency':'USD'},
    'FXE':  {'n':'Euro',           'type':'forex',     'currency':'EUR'},
    'FXB':  {'n':'British Pound',  'type':'forex',     'currency':'GBP'},
    'FXY':  {'n':'Japanese Yen',   'type':'forex',     'currency':'JPY'},
    'FXA':  {'n':'Aussie Dollar',  'type':'forex',     'currency':'AUD'},
    'FXF':  {'n':'Swiss Franc',    'type':'forex',     'currency':'CHF'},
    # Mega-cap Equities (regime/technical overlay)
    'AAPL': {'n':'Apple',          'type':'equity',    'region':'US'},
    'MSFT': {'n':'Microsoft',      'type':'equity',    'region':'US'},
    'NVDA': {'n':'Nvidia',         'type':'equity',    'region':'US'},
    'AMZN': {'n':'Amazon',         'type':'equity',    'region':'US'},
    'GOOGL':{'n':'Alphabet',       'type':'equity',    'region':'US'},
    'META': {'n':'Meta',           'type':'equity',    'region':'US'},
    'TSLA': {'n':'Tesla',          'type':'equity',    'region':'US'},
    'JPM':  {'n':'JPMorgan',       'type':'equity',    'region':'US'},
    # Sector ETFs
    'XLK':  {'n':'Technology',     'type':'etf',       'region':'US'},
    'XLF':  {'n':'Financials',     'type':'etf',       'region':'US'},
    'XLE':  {'n':'Energy',         'type':'etf',       'region':'US'},
    'XLV':  {'n':'Health Care',    'type':'etf',       'region':'US'},
    'XLI':  {'n':'Industrials',    'type':'etf',       'region':'US'},
    'XLU':  {'n':'Utilities',      'type':'etf',       'region':'US'},
    # Crypto
    'BTC-USD': {'n':'Bitcoin',     'type':'crypto',    'region':'GLOBAL'},
}


def get_scorecard_macro():
    """Get all macro data needed for scorecards — cached 20 min."""
    cached = cache.get('scorecard:macro')
    if cached: return cached

    import concurrent.futures
    data = {}

    # Live prices — fetch in parallel
    def _fetch_price(args):
        sym, key = args
        try:
            p = get_live_price(sym)
            return key, p
        except: return key, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futs = [pool.submit(_fetch_price, (sym, key))
                for sym, key in [('SPY','spy'),('TLT','tlt'),('UUP','uup'),
                                  ('^VIX','vix'),('GLD','gld'),('USO','uso'),('HYG','hyg')]]
        for f in concurrent.futures.as_completed(futs, timeout=15):
            try:
                key, p = f.result()
                if p: data[key] = p
            except: pass

    # FRED economic data — fetch all series in parallel
    if FRED_KEY:
        fred_series = {
            'cpi':       ('CPIAUCNS', 2, 'yoy'),
            'core_cpi':  ('CPILFENS', 2, 'yoy'),
            'ppi':       ('PPIFID',   2, 'yoy'),
            'nfp':       ('PAYEMS',   2, None),
            'unemp':     ('UNRATE',   2, None),
            'gdp':       ('A191RL1Q225SBEA', 3, None),
            'retail':    ('RSAFS',    2, 'mom_pct'),
            'mfg_pmi':   ('MANEMP',   2, None),
            'real_yield':('DFII10',   2, None),
        }

        def _fetch_fred(args):
            key, (series, years, transform) = args
            try:
                pts = get_fred_series(series, years=years)
                return key, pts, transform
            except Exception as e:
                return key, None, transform

        fred_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
            futs = [pool.submit(_fetch_fred, item) for item in fred_series.items()]
            for f in concurrent.futures.as_completed(futs, timeout=20):
                try:
                    key, pts, transform = f.result()
                    fred_results[key] = (pts, transform)
                except: pass

        for key, (pts, transform) in fred_results.items():
            try:
                if not pts or len(pts) < 2:
                    continue
                if transform == 'yoy' and len(pts) >= 13:
                    # Match year-ago by DATE not position — series gaps cause
                    # pts[-13] to land on the wrong month. Same fix as heatmap.
                    def _mi(d): return int(d[:4]) * 12 + (int(d[5:7]) - 1)
                    by_m = {_mi(p['date']): p['value'] for p in pts if p.get('value')}
                    cm   = _mi(pts[-1]['date'])
                    cur_v = pts[-1]['value']
                    v12 = by_m.get(cm - 12)
                    vp  = by_m.get(cm - 1)
                    v13 = by_m.get(cm - 13)
                    if v12:
                        curr = (cur_v - v12) / v12 * 100
                    else:
                        curr = (pts[-1]['value'] / pts[-13]['value'] - 1) * 100
                    if vp and v13:
                        prev = (vp - v13) / v13 * 100
                    elif len(pts) >= 14:
                        prev = (pts[-2]['value'] / pts[-14]['value'] - 1) * 100
                    else:
                        prev = curr
                    change = curr - prev
                elif transform == 'mom_pct':
                    curr = (pts[-1]['value'] / pts[-2]['value'] - 1) * 100
                    prev = (pts[-2]['value'] / pts[-3]['value'] - 1) * 100 if len(pts) >= 3 else curr
                    change = curr            # the MoM % growth itself is the signal
                else:
                    curr = pts[-1]['value']
                    prev = pts[-2]['value']
                    change = curr - prev
                data[key] = {
                    'current':  round(curr, 2),
                    'previous': round(prev, 2),
                    'change':   round(change, 4),
                    'date':     pts[-1]['date'],
                }
            except: pass

    # Trend + surprise enrichment — so scoring.py's factor readings react to direction
    # and consensus surprises, not just absolute levels.
    _SC_HIGHER_GOOD = {'gdp', 'retail', 'nfp', 'mfg_pmi'}
    _SC_LOWER_GOOD  = {'cpi', 'core_cpi', 'ppi', 'unemp'}
    for key in list(data.keys()):
        d = data[key]
        if not isinstance(d, dict) or 'current' not in d:
            continue
        c, p = d.get('current'), d.get('previous')
        if c is not None and p is not None:
            if key in _SC_HIGHER_GOOD:
                d['trend'] = 'improving' if c > p else ('deteriorating' if c < p else 'stable')
            elif key in _SC_LOWER_GOOD:
                d['trend'] = 'improving' if c < p else ('deteriorating' if c > p else 'stable')

    try:
        fc_map = _heatmap_forecasts()
        for hm_key, fc_raw in fc_map.items():
            sc_key = hm_key  # keys align (cpi, nfp, etc.)
            if sc_key in data and isinstance(data[sc_key], dict) and data[sc_key].get('current') is not None:
                fc = _align_forecast(fc_raw, data[sc_key]['current'])
                if fc is not None:
                    diff = data[sc_key]['current'] - fc
                    sp = abs(diff / fc * 100) if fc else 0
                    if sp < 1.0:
                        data[sc_key]['surprise'] = 'inline'
                    elif sc_key in _SC_HIGHER_GOOD:
                        data[sc_key]['surprise'] = 'beat' if diff > 0 else 'miss'
                    elif sc_key in _SC_LOWER_GOOD:
                        data[sc_key]['surprise'] = 'beat' if diff < 0 else 'miss'
                    else:
                        data[sc_key]['surprise'] = 'inline'
                    data[sc_key]['surprise_pct'] = round(sp, 1)
    except Exception:
        pass

    # Liquidity pillar from the regime engine (the 25% factor the matrix was missing)
    try:
        snap = compute_regime_snapshot()
        data['liquidity_pillar'] = (snap.get('pillar_scores') or {}).get('liquidity', 50)
    except Exception as e:
        print(f'[scorecard] liquidity pillar unavailable: {e}')
        data['liquidity_pillar'] = 50

    # ── Percentile-normalisation inputs (self-activate once >=30 samples exist) ──
    # Each scoring factor reads off its own ~20y distribution instead of hand-set
    # thresholds. We also append the latest reading so the series keeps growing.
    if store and store.available():
        try:
            import datetime as _dt
            pctl = {}
            for fac, sname in [('cpi', 'macro_cpi'), ('core_cpi', 'macro_core_cpi'),
                               ('ppi', 'macro_ppi'), ('real_yield', 'macro_real_yield')]:
                d = data.get(fac) or {}
                cur = d.get('current')
                if cur is None:
                    continue
                if d.get('date'):
                    try:
                        ts = int(_dt.datetime.strptime(d['date'][:10], '%Y-%m-%d')
                                 .replace(tzinfo=_dt.timezone.utc).timestamp())
                        store.record_indicator(sname, ts, cur)  # deduped by (name, ts)
                    except Exception:
                        pass
                p = store.percentile_rank(sname, cur)
                if p is not None:
                    pctl[fac] = p
            if pctl:
                data['_pctl'] = pctl
        except Exception as e:
            print(f'[scorecard] percentile inputs unavailable: {e}')

    cache.set('scorecard:macro', data, 1800)  # 30 min — macro data doesn't change that fast — macro data rarely changes faster
    return data


def asset_macro_sensitivity(asset_type, ticker):
    """
    How an asset responds to strong GROWTH, HOT inflation, and strong JOBS:
      +1 = benefits (bullish), -1 = hurt (bearish), 0 = ~indifferent.
    This is what makes the matrix differentiate by asset — e.g. hot inflation
    is bullish for gold (+1) but bearish for bonds (-1) and equities (-1),
    and strong jobs/growth is bearish for bonds (rate fear) but bullish for risk assets.
    """
    if asset_type == 'bond':
        return {'growth': -1, 'infl': -1, 'jobs': -1}   # rate-sensitive: strong data → bearish
    if asset_type == 'commodity':
        return {'growth': +1, 'infl': +1, 'jobs': +1}   # real assets benefit from inflation/growth
    if asset_type == 'forex':
        return {'growth': 0,  'infl': 0,  'jobs': 0}     # currency bias handled by the USD factor
    return {'growth': +1, 'infl': -1, 'jobs': +1}        # equities / indices / sectors (risk assets)


def build_scorecard(ticker, asset_info, price_data, macro):
    """EdgeFinder-style scorecard via the FROZEN weighted model (scoring.py)."""
    asset_type = asset_info.get('type', 'index')

    price     = price_data.get('price', 0)
    chg_pct   = price_data.get('changePct', 0)
    w52hi     = price_data.get('week52High', 0)
    w52lo     = price_data.get('week52Low', 0)
    range_pos = ((price - w52lo) / (w52hi - w52lo) * 100) if w52hi > w52lo > 0 else 50

    # ── Weighted, asset-specific 0-100 scoring ───────────────────
    raw = scoring.compute_raw_readings(macro, chg_pct, range_pos, pctl=macro.get('_pctl'))
    composite, overall, asset_class, breakdown = scoring.score_asset(asset_type, ticker, raw)

    # ── COT positioning overlay — small, capped, only where real CFTC data exists ──
    # Confirming/contrarian modifier, NOT a core weighted factor (see _cot_overlay docstring).
    cot_adj, cot_detail = _cot_overlay(ticker)
    composite_base = composite
    if cot_adj:
        composite = max(0, min(100, composite + cot_adj))
        overall = ('Very Bullish' if composite >= 68 else 'Bullish' if composite >= 57 else
                   'Neutral'      if composite >= 44 else 'Bearish' if composite >= 33 else 'Very Bearish')

    # Underlying readings, surfaced in the drill-down for transparency
    def reading_notes(fkey):
        notes = []
        if fkey == 'growth':
            for k, lbl, unit in [('gdp', 'GDP QoQ', '%'), ('nfp', 'Payrolls', 'K'),
                                 ('unemp', 'Unemployment', '%'), ('retail', 'Retail MoM', '')]:
                d = macro.get(k) or {}
                v = d.get('current') if d.get('current') is not None else d.get('change')
                if v is not None:
                    notes.append({'label': lbl, 'value': f'{v}{unit}'})
        elif fkey == 'infl':
            for k, lbl in [('cpi', 'CPI YoY'), ('core_cpi', 'Core CPI'), ('ppi', 'PPI')]:
                d = macro.get(k) or {}
                if d.get('current') is not None:
                    notes.append({'label': lbl, 'value': f'{d["current"]}%'})
        elif fkey == 'ry':
            d = macro.get('real_yield') or {}
            if d.get('current') is not None:
                notes.append({'label': '10Y Real Yield', 'value': f'{d["current"]}%'})
        elif fkey == 'liq':
            notes.append({'label': 'Regime Liquidity Pillar', 'value': f'{macro.get("liquidity_pillar", 50)}/100'})
        elif fkey == 'usd':
            d = macro.get('uup') or {}
            if d.get('changePct') is not None:
                notes.append({'label': 'USD (UUP) 1D', 'value': f'{d["changePct"]:+.2f}%'})
        elif fkey == 'mom':
            notes.append({'label': 'Price 1D', 'value': f'{chg_pct:+.2f}%'})
            notes.append({'label': '52W Range Position', 'value': f'{range_pos:.0f}%'})
        return notes

    def grp(fkey):
        b = breakdown.get(fkey) or {}
        return {
            'score':   b.get('favour', 50),   # 0-100 favourability for THIS asset
            'weight':  b.get('weight', 0),     # % importance for this asset class
            'points':  b.get('points', 0),     # contribution to the 0-100 composite
            'raw':     raw.get(fkey, 50),       # asset-independent reading strength
            'factors': reading_notes(fkey),
        }

    return {
        'ticker':      ticker,
        'name':        asset_info['n'],
        'price':       round(price, 2),
        'changePct':   round(chg_pct, 2),
        'rangePos':    round(range_pos, 1),
        'composite':   composite,          # 0-100, after COT overlay
        'composite_base': composite_base,  # 0-100, before COT overlay (for transparency)
        'overall':     overall,
        'asset_class': asset_class,
        'raw':         raw,
        'growth':      grp('growth'),
        'inflation':   grp('infl'),
        'real_yields': grp('ry'),
        'liquidity':   grp('liq'),
        'usd':         grp('usd'),
        'momentum':    grp('mom'),
        'cot_overlay': cot_detail,   # None if no COT coverage for this ticker
    }


@app.route('/api/scorecard/<ticker>')
def get_scorecard(ticker):
    ticker = ticker.upper()
    if ticker not in SCORECARD_ASSETS:
        return not_found(ticker)

    cached = cache.get(f'scorecard:{ticker}')
    if cached: return ok(cached, cached=True)

    asset_info = SCORECARD_ASSETS[ticker]
    price_data = get_live_price(ticker) or {}
    macro      = get_scorecard_macro()

    card = build_scorecard(ticker, asset_info, price_data, macro)
    cache.set(f'scorecard:{ticker}', card, 600)  # 10 min cache
    return ok(card)


@app.route('/api/scorecard')
def get_scorecard_list():
    """List of all scorecard-eligible assets."""
    return ok({t: {'n': v['n'], 'type': v['type']} for t, v in SCORECARD_ASSETS.items()})


# ══════════════════════════════════════════════════════════════════
# ◈ TOP SETUPS MATRIX — EdgeFinder-style per-asset × per-factor grid
# Batch-builds every scorecard in one pass (shared macro fetch),
# collapses each factor GROUP to a single bias cell, and ranks by
# conviction (|composite|). Powers the Markets › Top Setups grid.
# ══════════════════════════════════════════════════════════════════

# Column order for the matrix (group key, display label, scored?)
SETUP_FACTORS = [
    ('growth',      'Growth', True),
    ('inflation',   'Infl',   True),
    ('real_yields', 'RYield', True),
    ('liquidity',   'Liq',    True),
    ('usd',         'USD',    True),
    ('momentum',    'Mom',    True),
]

ASSET_CLASS_LABELS = {
    'index':     'Equity Indices',
    'equity':    'Equities',
    'etf':       'Sector ETFs',
    'commodity': 'Commodities',
    'bond':      'Bonds & Rates',
    'forex':     'Currencies',
}


def _group_bias(score):
    """Collapse a 0-100 factor favourability to a bias label."""
    if score >= 57:  return 'Bullish'
    if score <= 43:  return 'Bearish'
    return 'Neutral'


def build_setup_row(card):
    """Flatten a full scorecard into one matrix row (cells per factor group)."""
    cells = {}
    for key, _label, scored in SETUP_FACTORS:
        grp = card.get(key) or {}
        sc  = grp.get('score', 0)
        cells[key] = {
            'bias':    _group_bias(sc),
            'score':   sc,
            'weight':  grp.get('weight', 0),
            'count':   len(grp.get('factors') or []),
            'scored':  scored,
        }
    return {
        'ticker':    card['ticker'],
        'name':      card['name'],
        'type':      card.get('type', 'index'),
        'price':     card.get('price', 0),
        'changePct': card.get('changePct', 0),
        'rangePos':  card.get('rangePos', 50),
        'composite': card['composite'],
        'composite_base': card.get('composite_base', card['composite']),
        'overall':   card['overall'],
        'cells':     cells,
        'cot_overlay': card.get('cot_overlay'),
    }


@app.route('/api/setups')
def get_setups():
    """
    Top Setups matrix — every scorecard asset as a row, factor groups as
    columns, ranked by conviction. One shared macro fetch for all assets.
    Cache: 5 min.
    """
    return ok(build_setups_matrix())


def build_setups_matrix():
    """Build (or return cached) the full scorecard matrix. Reused by /api/dashboard."""
    cached = cache.get('setups:matrix')
    if cached:
        return cached

    import concurrent.futures

    # Fetch macro data AND all prices in parallel simultaneously —
    # previously macro was fetched first (serial), then prices.
    # Running both at once cuts cold build time by ~40%.
    def _fetch_price(ticker):
        try:    return ticker, (get_live_price(ticker) or {})
        except: return ticker, {}

    def _fetch_macro():
        try:    return get_scorecard_macro()
        except: return {}

    prices = {}
    macro  = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        # Submit macro fetch as one task alongside all price fetches
        macro_fut  = pool.submit(_fetch_macro)
        price_futs = {pool.submit(_fetch_price, t): t for t in SCORECARD_ASSETS}

        # Collect prices as they complete
        for f in concurrent.futures.as_completed(price_futs, timeout=20):
            try:
                t, pd = f.result(timeout=8)
                prices[t] = pd
            except: pass

        # Get macro result (should be done by now since prices took at least as long)
        try:
            macro = macro_fut.result(timeout=15) or {}
        except:
            macro = get_scorecard_macro()  # fallback — try again synchronously

    rows = []

    # Pre-warm COT overlay cache in parallel — avoids 6 serial Yahoo calls
    # inside the scorecard loop that each trigger get_price_closes.
    cot_tickers = [t for t in SCORECARD_ASSETS if t in TICKER_TO_COT]
    if cot_tickers:
        def _warm_cot(ticker):
            try:
                sym = TICKER_TO_COT[ticker]
                ck = f'cot_overlay:{sym}'
                if cache.get(ck) is None:
                    pos = compute_positioning(sym)
                    cache.set(ck, pos, _COT_OVERLAY_CACHE_TTL)
            except Exception as e:
                print(f'[SETUPS] COT pre-warm {ticker} error: {e}')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_warm_cot, cot_tickers, timeout=15))

    for ticker, asset_info in SCORECARD_ASSETS.items():
        try:
            card = build_scorecard(ticker, asset_info, prices.get(ticker, {}), macro)
            card['type'] = asset_info.get('type', 'index')
            rows.append(build_setup_row(card))
        except Exception as e:
            print(f'[SETUPS] {ticker} error: {e}')

    # Conviction ranking — strongest deviation from neutral (50) first
    ranked = sorted(rows, key=lambda r: abs(r['composite'] - 50), reverse=True)

    # ── Persist per-ticker composite scores for backtest history ──────
    # Stored once per fresh matrix build (cache miss), not on every request.
    # Uses the same generic indicator API as COT/regime data.
    # Key pattern: 'score_{ticker}' → e.g. 'score_SPY', 'score_TLT'
    if store:
        try:
            ts = int(time.time())
            for r in rows:
                tk = r.get('ticker')
                sc = r.get('composite')
                if tk and sc is not None:
                    store.record_indicators_bulk(f'score_{tk}', [(ts, float(sc))])
        except Exception as e:
            print(f'[SETUPS] score persist error: {e}')

    # Group by asset class (preserve label order)
    by_class = {}
    for r in rows:
        by_class.setdefault(r['type'], []).append(r)
    for cls in by_class:
        by_class[cls].sort(key=lambda r: r['composite'], reverse=True)

    grouped = [
        {
            'key':    cls,
            'label':  ASSET_CLASS_LABELS.get(cls, cls.title()),
            'assets': by_class[cls],
        }
        for cls in ['index', 'equity', 'etf', 'commodity', 'bond', 'forex']
        if cls in by_class
    ]

    # Split the headline movers for the hero strip
    bullish = [r for r in ranked if r['composite'] >= 57][:5]
    bearish = [r for r in ranked if r['composite'] <= 43][:5]

    result = {
        'columns':  [{'key': k, 'label': l, 'scored': s} for k, l, s in SETUP_FACTORS],
        'grouped':  grouped,
        'ranked':   ranked,
        'top_bullish': bullish,
        'top_bearish': bearish,
        'count':    len(rows),
        'timestamp': int(time.time()),
    }

    cache.set('setups:matrix', result, 1800)  # 30 min
    return result


@app.route('/api/setups/refresh')
def refresh_setups():
    cache.delete('setups:matrix')
    cache.delete('scorecard:macro')
    return ok({'cleared': True})


# ══════════════════════════════════════════════════════════════════
# ◈ ASSET DASHBOARD — intelligence-first exploration by asset class
# Fuses per-asset scorecard scores (granular) with inherited Regime
# Engine outputs (trend + confidence). Answers "what is strongest /
# weakest in the current regime?" — not "browse a database."
# ══════════════════════════════════════════════════════════════════
DASH_CLASSES = [
    ('index',     'Indices'),
    ('equity',    'Equities'),
    ('etf',       'Sector ETFs'),
    ('bond',      'Bonds'),
    ('commodity', 'Commodities'),
    ('forex',     'Forex'),
    ('crypto',    'Crypto'),     # future — no data yet
]

_TREND_NUM    = {'Improving': 1, 'Stable': 0, 'Deteriorating': -1}
_NUM_TREND    = {1: 'Improving', 0: 'Stable', -1: 'Deteriorating'}
_INVERT_TREND = {'Improving': 'Deteriorating', 'Deteriorating': 'Improving', 'Stable': 'Stable'}


def _dash_bucket(ticker, atype):
    """Regime-Engine bucket an asset inherits trend/confidence from (+invert flag)."""
    if atype in ('index', 'equity', 'etf'): return ('US_EQUITIES', False)
    if atype == 'bond':                      return ('BONDS', False)
    if atype == 'commodity':
        return ('OIL', False) if ticker in ('USO', 'UNG', 'CPER') else ('GOLD', False)
    if atype == 'forex':
        return ('USD', False) if ticker == 'UUP' else ('USD', True)
    if atype == 'crypto':                    return ('US_EQUITIES', False)  # BTC tracks risk-on
    return ('US_EQUITIES', False)


def _dash_fit(score):
    if score >= 60: return 'Strong'
    if score >= 42: return 'Moderate'
    return 'Weak'


@app.route('/api/dashboard')
def get_dashboard():
    """Asset Dashboard — per-class regime intelligence + per-asset rows. Cache 5 min."""
    cached = cache.get('dashboard:data')
    if cached:
        return ok(cached, cached=True)

    matrix   = build_setups_matrix()
    regime   = compute_regime_snapshot()
    r_assets = regime.get('asset_scores', {})

    rows_by_type = {}
    for r in matrix.get('ranked', []):
        rows_by_type.setdefault(r['type'], []).append(r)

    classes = []
    for ckey, clabel in DASH_CLASSES:
        if ckey == 'crypto' or ckey not in rows_by_type:
            classes.append({'key': ckey, 'label': clabel, 'available': False})
            continue

        assets = []
        for r in rows_by_type[ckey]:
            score = max(0, min(100, round(r['composite'])))  # composite is already 0-100
            bucket, invert = _dash_bucket(r['ticker'], ckey)
            ra = r_assets.get(bucket, {})
            trend = ra.get('trend', 'Stable')
            if invert:
                trend = _INVERT_TREND.get(trend, trend)
            assets.append({
                'ticker':     r['ticker'],
                'name':       r['name'],
                'score':      score,
                'composite':  r['composite'],
                'changePct':  r.get('changePct', 0),
                'confidence': ra.get('confidence', regime.get('confidence', 50)),
                'trend':      trend,
                'regime_fit': _dash_fit(score),
                'inherits':   bucket,
            })

        assets.sort(key=lambda a: a['score'], reverse=True)
        scores = [a['score'] for a in assets]
        avg  = round(sum(scores) / len(scores)) if scores else 50
        tnum = sum(_TREND_NUM.get(a['trend'], 0) for a in assets)
        ctrend = _NUM_TREND[1 if tnum > 0 else -1 if tnum < 0 else 0]

        classes.append({
            'key':       ckey,
            'label':     clabel,
            'available': True,
            'avg_score': avg,
            'avg_fit':   _dash_fit(avg),
            'trend':     ctrend,
            'count':     len(assets),
            'strongest': [{'ticker': a['ticker'], 'name': a['name'], 'score': a['score']} for a in assets[:3]],
            'weakest':   [{'ticker': a['ticker'], 'name': a['name'], 'score': a['score']} for a in assets[-3:][::-1]],
            'assets':    assets,
        })

    result = {
        'classes':      classes,
        'regime_score': regime.get('regime_score', 50),
        'regime_label': regime.get('regime_label', 'Neutral'),
        'timestamp':    int(time.time()),
    }
    cache.set('dashboard:data', result, 900)
    return ok(result)


@app.route('/api/dashboard/refresh')
def refresh_dashboard():
    cache.delete('dashboard:data')
    cache.delete('setups:matrix')
    return ok({'cleared': True})




# ══════════════════════════════════════════════════════════════════
# ◈ FINANCIAL MODELING PREP (FMP) — Economic Data Integration
# ══════════════════════════════════════════════════════════════════

FMP_KEY  = os.environ.get('FMP_KEY', 'JF75BoBiWT5H9HxS1NO5KqyL3rmWeZzL')
FMP_BASE        = 'https://financialmodelingprep.com/stable'   # was v3 — retired Aug 2025
FMP_BASE4       = 'https://financialmodelingprep.com/stable'   # was v4 — retired Aug 2025
FMP_BASE_STABLE = 'https://financialmodelingprep.com/stable'   # canonical stable endpoint

_FMP_LAST = {}   # last FMP request status/body, for /api/fmp/diagnostic

def fmp_get(endpoint, params=None, base=FMP_BASE_STABLE):
    """Make a request to FMP API."""
    if not FMP_KEY:
        _FMP_LAST.clear(); _FMP_LAST.update({'endpoint': endpoint, 'status': 'no_key'})
        return None
    try:
        p = params or {}
        p['apikey'] = FMP_KEY
        r = requests.get(f'{base}/{endpoint}', params=p,
                        headers={'User-Agent': 'StockSense/1.0'}, timeout=12)
        # record status + a short body snippet (no key — key is only in params)
        _FMP_LAST.clear()
        _FMP_LAST.update({'endpoint': endpoint, 'base': base, 'status': r.status_code, 'body': r.text[:300]})
        if r.status_code == 200:
            return r.json()
        print(f'[fmp] {endpoint} → {r.status_code}: {r.text[:200]}')
    except Exception as e:
        _FMP_LAST.clear(); _FMP_LAST.update({'endpoint': endpoint, 'status': 'exception', 'body': str(e)})
        print(f'[fmp] error: {e}')
    return None


@app.route('/api/fmp/diagnostic')
def fmp_diagnostic():
    """Pinpoint why FMP fails: tests key validity + which economic endpoint works."""
    if not FMP_KEY:
        return ok({'key_set': False, 'note': 'No FMP key configured'})
    tests = [
        ('stable quote — AAPL',              'quote/AAPL',                    FMP_BASE_STABLE),
        ('stable economic-indicators — GDP', 'economic-indicators?name=GDP',  FMP_BASE_STABLE),
        ('stable economic-calendar',         'economic-calendar',             FMP_BASE_STABLE),
    ]
    out = []
    for label, ep, base in tests:
        res = fmp_get(ep, base=base)
        out.append({
            'test':   label,
            'ok':     bool(res),
            'rows':   len(res) if isinstance(res, list) else (1 if res else 0),
            'status': _FMP_LAST.get('status'),
            'body':   _FMP_LAST.get('body'),
        })
    return ok({
        'key_set':  True,
        'key_len':  len(FMP_KEY),
        'tests':    out,
        'hint': ('All calls now use the stable endpoint (v3/v4 retired Aug 2025). '
                 'If quote passes but economic fails → plan does not cover that indicator. '
                 'If everything 401s → key itself is invalid or expired.'),
    })


def get_fmp_economic_calendar(from_date=None, to_date=None):
    """
    FMP Economic Calendar — returns events with actual/forecast/previous.
    Endpoint: /stable/economic-calendar (v3 retired Aug 2025).
    """
    cached = cache.get('fmp:calendar')
    if cached: return cached

    import datetime
    today = datetime.date.today()
    if not from_date: from_date = (today - datetime.timedelta(days=30)).isoformat()
    if not to_date:   to_date   = (today + datetime.timedelta(days=60)).isoformat()

    data = fmp_get('economic-calendar', {'from': from_date, 'to': to_date}, base=FMP_BASE_STABLE)
    if not data:
        return None

    # Normalise to our internal format
    events = []
    for item in data:
        # US-only: match on currency (most reliable) or country variants
        ccy     = (item.get('currency', '') or '').upper()
        country = (item.get('country', '') or '').upper()
        if ccy != 'USD' and country not in ('US', 'USA', 'UNITED STATES'):
            continue

        impact_map = {'High': 'HIGH', 'Medium': 'MEDIUM', 'Low': 'LOW'}
        impact     = impact_map.get(item.get('impact', ''), 'LOW')

        events.append({
            'date':     item.get('date', '')[:10],
            'time':     item.get('date', '')[11:16],
            'event':    item.get('event', ''),
            'impact':   impact,
            'actual':   str(item.get('actual',   '') or ''),
            'forecast': str(item.get('estimate', '') or ''),
            'previous': str(item.get('previous', '') or ''),
            'currency': item.get('currency', 'USD'),
            'source':   'FMP',
        })

    cache.set('fmp:calendar', events, 3600)  # 1 hour cache
    return events


def get_fmp_economic_indicators():
    """
    FMP Economic Indicators — actual data points for key US series.
    Used to power the economic heatmap with real values.
    """
    cached = cache.get('fmp:indicators')
    if cached: return cached

    # Key FMP economic indicator names
    indicators = {
        'GDP':                  ('gdp',          'Growth',     'positive', 'positive', '%'),
        'realGDP':              ('real_gdp',      'Growth',     'positive', 'positive', '%'),
        'CPI':                  ('cpi',           'Inflation',  'positive', 'negative', '%'),
        'inflationRate':        ('inflation',     'Inflation',  'positive', 'negative', '%'),
        'totalNonfarmPayroll':  ('nfp',           'Employment', 'positive', 'positive', 'K'),
        'unemploymentRate':     ('unemp',         'Employment', 'negative', 'negative', '%'),
        'retailSales':          ('retail',        'Growth',     'positive', 'positive', 'B'),
        'consumerSentiment':    ('consumer_sent', 'Sentiment',  'positive', 'positive', ''),
        'initialClaims':        ('jobless',       'Employment', 'negative', 'negative', 'K'),
        'federalFunds':         ('fed_rate',      'Fed Policy', 'positive', 'negative', '%'),
        'coreInflationRate':    ('core_cpi',      'Inflation',  'positive', 'negative', '%'),
    }

    results = {}
    for fmp_name, (key, cat, usd_dir, stocks_dir, unit) in indicators.items():
        try:
            data = fmp_get(f'economic-indicators?name={fmp_name}', base=FMP_BASE_STABLE)
            if data and len(data) >= 2:
                curr = data[0]
                prev = data[1]
                results[key] = {
                    'label':        curr.get('name', fmp_name),
                    'category':     cat,
                    'actual':       curr.get('value'),
                    'previous':     prev.get('value'),
                    'date':         curr.get('date', '')[:7],
                    'unit':         unit,
                    'usd_dir':      usd_dir,
                    'stocks_dir':   stocks_dir,
                    'source':       'FMP',
                }
        except Exception as e:
            print(f'[fmp] indicator {fmp_name} error: {e}')

    cache.set('fmp:indicators', results, 1800)
    return results

# ══════════════════════════════════════════════════════════════════
# ◈ US ECONOMIC HEATMAP
# ══════════════════════════════════════════════════════════════════

US_INDICATORS = [
    # (key, label, category, fred_series, usd_dir, stocks_dir, unit, yoy_calc)
    # yoy_calc=True means calculate YoY % change from index level
    # yoy_calc=False means use MoM change directly
    # yoy_calc='mom_pct' means calculate % change from consecutive values
    ('gdp',        'GDP Growth QoQ',        'Growth',     'A191RL1Q225SBEA', 'positive', 'positive',  '%',   False),
    ('retail',     'Retail Sales MoM',      'Growth',     'RSXFS',           'positive', 'positive',  '%',   'mom_pct'),
    ('cpi',        'CPI YoY',               'Inflation',  'CPIAUCNS',        'positive', 'negative',  '%',   True),
    ('core_cpi',   'Core CPI YoY',          'Inflation',  'CPILFENS',        'positive', 'negative',  '%',   True),
    ('ppi',        'PPI YoY',               'Inflation',  'PPIFID',          'positive', 'negative',  '%',   True),
    ('pce',        'PCE YoY',               'Inflation',  'PCEPI',           'positive', 'negative',  '%',   True),
    ('nfp',        'Non-Farm Payrolls',     'Employment', 'PAYEMS',          'positive', 'positive',  'K',   'mom_k'),
    ('unemp',      'Unemployment Rate',     'Employment', 'UNRATE',          'negative', 'negative',  '%',   False),
    ('jobless',    'Initial Jobless Claims','Employment', 'ICSA',            'negative', 'negative',  'K',   False),
    ('jolts',      'JOLTS Job Openings',    'Employment', 'JTSJOL',          'positive', 'positive',  'M',   False),
    ('consumer_sent','Consumer Sentiment',  'Sentiment',  'UMCSENT',         'positive', 'positive',  '',    False),
    ('fed_rate',   'Fed Funds Rate',        'Fed Policy', 'FEDFUNDS',        'positive', 'negative',  '%',   False),
]

# Bias-score weighting: weight by macro CATEGORY, then split a category's weight across its
# members — so 4 correlated inflation rows (CPI/Core/PPI/PCE) count as ONE category's worth of
# signal, not 4×, and a single soft sentiment reading can't rival the hard data. (Fixes the
# audit findings: inflation over-counted, sentiment over-weighted.)
HEATMAP_CATEGORY_WEIGHTS = {
    'Employment': 0.30, 'Inflation': 0.25, 'Growth': 0.20,
    'Fed Policy': 0.15, 'Sentiment': 0.10,
}
_HEATMAP_CAT_COUNTS = {}
for _row in US_INDICATORS:
    _HEATMAP_CAT_COUNTS[_row[2]] = _HEATMAP_CAT_COUNTS.get(_row[2], 0) + 1

def _indicator_weight(category):
    """A single indicator's share of the overall bias score (category weight ÷ members)."""
    return HEATMAP_CATEGORY_WEIGHTS.get(category, 0.10) / max(1, _HEATMAP_CAT_COUNTS.get(category, 1))


def calc_usd_stocks_impact(key, actual, previous, usd_dir, stocks_dir, unit, forecast=None):
    """USD/Stocks impact. Driven by SURPRISE vs forecast when a forecast exists (the real
    market-moving basis — e.g. NFP 172K vs a 130K forecast is a BEAT even if below last
    month's 179K). Falls back to month-over-month direction when no forecast is available.
    The returned change value is always month-over-month, for display."""
    if actual is None or previous is None:
        return 'Neutral', 'Neutral', 0

    # Fed Funds moves in discrete 25bp steps; sub-policy drift in the effective rate (a few bp
    # month-to-month) is noise, not a signal. Require a meaningful move, else Neutral. (The level
    # isn't really the signal anyway — the expected PATH is, which needs futures/OIS data.)
    if key == 'fed_rate' and forecast is None and abs(actual - previous) < 0.13:
        return 'Neutral', 'Neutral', round(actual - previous, 3)

    mom_change = actual - previous          # for the CHANGE column (always MoM)
    use_fc = forecast is not None
    basis  = (actual - forecast) if use_fc else mom_change   # what drives the verdict
    ref    = abs(forecast) if use_fc else abs(previous)

    higher = basis > 0                       # did the reading come in above the baseline?
    surprise_pct = abs(basis / ref * 100) if ref else 0

    if surprise_pct < 0.5:
        usd_impact = stocks_impact = 'Neutral'
    else:
        # usd_dir/stocks_dir = does a HIGHER reading help that market?
        # 'positive' → higher is bullish; 'negative' → higher is bearish (e.g. unemployment,
        # jobless claims — a higher print is bad). This single rule handles both correctly.
        usd_bull    = higher if usd_dir    == 'positive' else (not higher)
        stocks_bull = higher if stocks_dir == 'positive' else (not higher)
        usd_impact    = 'Bullish' if usd_bull    else 'Bearish'
        stocks_impact = 'Bullish' if stocks_bull else 'Bearish'

    return usd_impact, stocks_impact, round(mom_change, 3)


def fmt_value(val, unit):
    if val is None: return '—'
    if unit == '%':  return f'{val:.1f}%'
    if unit == 'K':
        # FRED jobless claims in raw thousands, NFP in thousands
        if abs(val) >= 1000: return f'{val/1000:.0f}K'
        return f'{val:.0f}K'
    if unit == 'M':
        # values arrive in thousands: levels (>=1000K) show as M, small changes as K
        if abs(val) >= 1000: return f'{val/1000:.2f}M'
        return f'{val:.0f}K'
    if unit == 'B':  return f'${val/1e9:.1f}B'
    return f'{val:.1f}'


_HEATMAP_CAL_KW = {
    'gdp':           ['gdp'],
    'retail':        ['retail sales'],
    'core_cpi':      ['core cpi', 'core inflation', 'core consumer price'],
    'pce':           ['pce'],
    'cpi':           ['cpi', 'consumer price', 'inflation rate'],
    'ppi':           ['ppi', 'producer price'],
    'nfp':           ['non farm', 'nonfarm', 'non-farm', 'payroll'],
    'unemp':         ['unemployment rate'],
    'jobless':       ['jobless claims', 'initial claims'],
    'jolts':         ['jolts', 'job openings'],
    'consumer_sent': ['consumer sentiment', 'michigan'],
}

def _align_forecast(fc, actual):
    """Match a calendar forecast to the heatmap's actual scale. Calendar values come in
    display units (NFP '85', JOLTS '6.88') while FRED actuals vary (172, 7618), so try
    common power-of-1000 scalings and accept whichever lands within an order of magnitude.
    Returns the aligned forecast, or None if it can't be reconciled (never guesses wrong)."""
    if fc is None or not actual:
        return None
    for scale in (1, 1000.0, 0.001, 1e6, 1e-6):
        cand = fc * scale
        if 0.1 <= abs(cand) / abs(actual) <= 10:
            return cand
    return None


def _heatmap_forecasts():
    """{heatmap_key: forecast_float} from the live US calendar, for beat/miss scoring.
    Specific keys matched before generic ones so 'Core CPI' can't bleed into the CPI key.
    MoM events are excluded for YoY inflation rows, and broad unemployment variants (U-6)
    are excluded for the headline rate. Only events within the last 14 days are considered,
    so a stale prior-month forecast can't match over the current release.
    Forecast is left in the calendar's own scale; the caller normalises to heatmap units."""
    _EXCLUDE = {
        'cpi':      ('mom', 'm/m', 'monthly'),
        'core_cpi': ('mom', 'm/m', 'monthly'),
        'ppi':      ('mom', 'm/m', 'monthly', 'core', 'ex food', 'ex-food', 'excluding food', 'without food'),
        'pce':      ('mom', 'm/m', 'monthly', 'core'),
        'retail':   ('yoy', 'y/y', 'annual', 'year-over-year'),
        'unemp':    ('u-6', 'u6', 'u 6', 'underemployment', 'participation', 'youth'),
    }
    _YOY_HINT = ('yoy', 'y/y', 'annual', 'year-over-year', 'year over year')
    out = {}
    try:
        events = get_fmp_economic_calendar() or []
    except Exception:
        events = []
    if not events:
        return out

    # Only consider events from the last 14 days — stale prior-month releases carry
    # outdated forecasts that mismatch the current actual (e.g. last month's CPI forecast
    # of 3.9% matched against this month's 4.2% actual → phantom "BEAT").
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=14)).strftime('%Y-%m-%d')
    recent = [(i, e) for i, e in enumerate(events) if (e.get('date') or '') >= cutoff]

    used = set()
    for key in ['core_cpi', 'pce', 'gdp', 'retail', 'ppi', 'nfp', 'unemp',
                'jobless', 'jolts', 'consumer_sent', 'cpi']:
        excl = _EXCLUDE.get(key, ())
        matches = []
        for i, e in recent:
            if i in used:
                continue
            name = str(e.get('event', '')).lower()
            if key == 'cpi' and 'core' in name:
                continue
            if any(x in name for x in excl):
                continue
            if any(kw in name for kw in _HEATMAP_CAL_KW[key]):
                fc = parse_num(e.get('forecast', ''))
                prev = parse_num(e.get('previous', ''))
                # Guard: if forecast equals previous exactly, it's almost certainly stale
                # data (FMP sometimes fills forecast with the prior actual). Real consensus
                # forecasts rarely match the last print to the decimal. Skip it.
                if fc is not None and prev is not None and abs(fc - prev) < 0.01:
                    continue
                if fc is not None:
                    matches.append((i, name, fc, e.get('date', '')))
        if not matches:
            continue
        # Prefer the most recent event, then YoY-marked over ambiguous
        matches.sort(key=lambda m: m[3], reverse=True)  # newest first
        pick = None
        if key in ('cpi', 'core_cpi', 'ppi', 'pce'):
            pick = next((m for m in matches if any(h in m[1] for h in _YOY_HINT)), None)
        pick = pick or matches[0]
        out[key] = pick[2]
        used.add(pick[0])
    return out


@app.route('/api/heatmap/us')
def get_us_heatmap():
    """US Economic Heatmap — FRED data, with calendar forecasts joined for beat/miss."""
    cached = cache.get('heatmap:us')
    if cached: return ok(cached, cached=True)

    # FRED-only: it applies the correct YoY/QoQ transforms. FMP's economic-indicators
    # return raw index LEVELS (CPI ~332, GDP ~31819) which are wrong for this view, so
    # we do not use FMP here even when it's available. (FMP is still used for the calendar.)
    fmp_data = {}

    # Use manual consensus forecasts instead of FMP (FMP is unreliable/wrong variant)
    manual_forecasts = _get_consensus_forecasts()
    # Build forecasts dict in same format as before: {key: float}
    forecasts = {k: v['forecast'] for k, v in manual_forecasts.items() if v.get('forecast') is not None}

    rows = []
    usd_bull = usd_bear = stocks_bull = stocks_bear = 0

    for key, label, category, series, usd_dir, stocks_dir, unit, yoy_calc in US_INDICATORS:
        # Use FMP data if available for this indicator
        if fmp_data and key in fmp_data:
            fd = fmp_data[key]
            actual   = fd.get('actual')
            previous = fd.get('previous')
            if actual is not None and previous is not None:
                change = round(actual - previous, 3)
                usd_impact, stocks_impact, _ = calc_usd_stocks_impact(
                    key, actual, previous, usd_dir, stocks_dir, unit)
                row = {
                    'key': key, 'label': label, 'category': category, 'unit': unit,
                    'actual': actual, 'previous': previous, 'change': change,
                    'date': fd.get('date', '—'),
                    'usd_impact': usd_impact, 'stocks_impact': stocks_impact,
                    'actual_fmt':   fmt_value(actual, unit),
                    'previous_fmt': fmt_value(previous, unit),
                    'change_fmt': ('+' if change > 0 else '') + fmt_value(change, unit),
                    'source': 'FMP',
                }
                if usd_impact    == 'Bullish': usd_bull    += 1
                elif usd_impact  == 'Bearish': usd_bear    += 1
                if stocks_impact == 'Bullish': stocks_bull += 1
                elif stocks_impact=='Bearish': stocks_bear += 1
                rows.append(row)
                continue
        row = {
            'key': key, 'label': label, 'category': category,
            'unit': unit, 'usd_dir': usd_dir, 'stocks_dir': stocks_dir,
            'actual': None, 'previous': None, 'change': None,
            'date': '—', 'usd_impact': 'Neutral', 'stocks_impact': 'Neutral',
            'actual_fmt': '—', 'previous_fmt': '—', 'change_fmt': '—',
            'source': 'none', 'reason': None,
        }

        if FRED_KEY:
            try:
                years_needed = 3 if yoy_calc is True else 2
                pts = get_fred_series(series, years=years_needed)
                if pts and len(pts) >= 2:
                    row['source'] = 'FRED'
                    curr_pt = pts[-1]
                    # Format date: quarterly series (GDP) → "Q1 2026", monthly → "2026-05"
                    raw_date = curr_pt['date'][:7]  # e.g. "2026-01"
                    if series in ('A191RL1Q225SBEA',):
                        # Quarterly — convert month to quarter label
                        yr, mo = int(raw_date[:4]), int(raw_date[5:7])
                        q = (mo - 1) // 3 + 1
                        row['date'] = f'Q{q} {yr}'
                    else:
                        row['date'] = raw_date

                    if yoy_calc is True:
                        # YoY from index level. Match the year-ago point by DATE (12 calendar
                        # months before the latest), NOT by position. Series differ in length
                        # and can have gaps, which made pts[-13] land 13 months back on the
                        # 34-point CPI/Core series and overstate YoY by ~0.3pp.
                        def _mi(d): return int(d[:4]) * 12 + (int(d[5:7]) - 1)
                        by_m  = {_mi(p['date']): p['value'] for p in pts if p.get('value')}
                        cm    = _mi(curr_pt['date'])
                        cur_v = curr_pt['value']
                        v12 = by_m.get(cm - 12)   # same month, one year earlier
                        vp  = by_m.get(cm - 1)    # previous month
                        v13 = by_m.get(cm - 13)   # previous month, one year earlier
                        if v12:
                            actual = round((cur_v - v12) / v12 * 100, 2)
                        elif len(pts) >= 13:                      # positional fallback
                            actual = round((cur_v - pts[-13]['value']) / pts[-13]['value'] * 100, 2) if pts[-13]['value'] else 0
                        else:
                            actual = 0
                        if vp and v13:
                            previous = round((vp - v13) / v13 * 100, 2)
                        else:
                            previous = actual
                        change = round(actual - previous, 3)
                    elif yoy_calc == 'mom_k':
                        # Monthly change in thousands (for NFP)
                        curr_val = curr_pt['value']
                        prev_val = pts[-2]['value']
                        actual   = round(curr_val - prev_val, 1)   # monthly jobs added
                        previous = round(pts[-2]['value'] - pts[-3]['value'], 1) if len(pts) >= 3 else 0
                        change   = round(actual - previous, 1)
                    elif yoy_calc == 'mom_pct':
                        curr_val = curr_pt['value']
                        prev_val = pts[-2]['value']
                        actual   = round((curr_val - prev_val) / prev_val * 100, 2) if prev_val else 0
                        previous = 0
                        change   = actual
                    else:
                        actual   = curr_pt['value']
                        previous = pts[-2]['value']
                        change   = round(actual - previous, 3)

                    row['actual']   = actual
                    row['previous'] = previous if yoy_calc not in ('mom_k','mom_pct') else pts[-2]['value'] if yoy_calc=='mom_k' else None
                    row['previous'] = round(previous, 2)

                    # Beat/miss vs consensus is the real release-reaction basis (fixes the
                    # NFP case: 172K beats a ~130K forecast even if below last month's 179K).
                    fc = forecasts.get(key)  # manual consensus — trusted, no guard needed
                    # For inflation, MoM direction is the right impact basis: rising CPI =
                    # bearish stocks regardless of a slight forecast miss (+0.5pp jump matters
                    # more than a 0.1pp miss). For employment/growth, surprise vs forecast IS
                    # the right basis (NFP 172K beating 90K is the signal, not the MoM dip).
                    # The beat/miss BADGE still shows on inflation rows (informative) — only
                    # the impact COLORS use the direction.
                    impact_fc = None if category == 'Inflation' else fc
                    usd_impact, stocks_impact, _ = calc_usd_stocks_impact(
                        key, actual, previous, usd_dir, stocks_dir, unit, forecast=impact_fc)
                    row['usd_impact']    = usd_impact
                    row['stocks_impact'] = stocks_impact
                    row['change']        = change
                    if fc is not None:
                        diff = actual - fc
                        beat = (diff < 0) if usd_dir == 'negative' else (diff > 0)
                        pct  = abs(diff / fc * 100) if fc else 0
                        row['forecast_fmt'] = fmt_value(fc, unit)
                        row['surprise']  = ('BEAT' if beat else 'MISS') if pct >= 0.5 else 'IN LINE'
                        row['magnitude'] = 'LARGE' if pct >= 15 else 'MEDIUM' if pct >= 5 else 'SMALL'

                    row['actual_fmt']   = fmt_value(actual, unit)
                    row['previous_fmt'] = fmt_value(previous, unit) if previous is not None else '—'
                    row['change_fmt']   = ('+' if change > 0 else '') + fmt_value(change, unit) if change is not None else '—'

                    w = _indicator_weight(category)
                    if usd_impact    == 'Bullish': usd_bull    += w
                    elif usd_impact  == 'Bearish': usd_bear    += w
                    if stocks_impact == 'Bullish': stocks_bull += w
                    elif stocks_impact=='Bearish': stocks_bear += w
                else:
                    st = fred_last_status(series)
                    if pts is None:
                        row['reason'] = f'FRED returned no data (status {st})'
                    else:
                        row['reason'] = f'FRED returned {len(pts)} point(s) — insufficient history'
            except Exception as e:
                row['reason'] = f'parse error: {type(e).__name__}: {e}'
                print(f'[heatmap] {key} error: {e}')
        else:
            # Use curated snapshot if no FRED key
            snapshot = CURRENT_MACRO_SNAPSHOT.get('US', {})
            if key == 'cpi':       row['actual_fmt'] = f"{snapshot.get('inflation', 4.0)}%"
            elif key == 'unemp':   row['actual_fmt'] = f"{snapshot.get('unemployment', 4.3)}%"
            elif key == 'gdp':     row['actual_fmt'] = f"{snapshot.get('gdp_growth', 2.0)}%"
            elif key == 'fed_rate':row['actual_fmt'] = f"{snapshot.get('rate', 4.33)}%"

        rows.append(row)

    total_scored = usd_bull + usd_bear
    usd_pct    = round(usd_bull    / total_scored * 100) if total_scored else 50
    stocks_scored = stocks_bull + stocks_bear
    stocks_pct = round(stocks_bull / stocks_scored * 100) if stocks_scored else 50

    result = {
        'rows':       rows,
        'usd_pct':    usd_pct,
        'stocks_pct': stocks_pct,
        'usd_bull':   usd_bull,
        'usd_bear':   usd_bear,
        'stocks_bull':stocks_bull,
        'stocks_bear':stocks_bear,
        'generated':  int(time.time()),
        'fmp_available': bool(fmp_data),
        'fred_key_set':  bool(FRED_KEY),
    }
    # Only cache a result that actually has data — never poison the cache with a
    # transient FRED failure (rate-limit/outage), so it self-heals on the next load.
    if any(r.get('actual') is not None for r in rows):
        cache.set('heatmap:us', result, 1800)  # 30 min — FRED data doesn't change often
    return ok(result)


# ── Multi-country heatmaps (FRED international / OECD-harmonised series) ──
# Coverage is a verified CORE set; FRED's non-US series are patchier than the US,
# so some rows may be blank for a given country until IDs are confirmed live.
# CPALTT01{cc}{freq}659N = CPI YoY rate directly; LRHUTTTT{cc}{freq}156S = unemployment rate.
COUNTRIES = {
    'us': {'name': 'United States', 'ccy': 'USD', 'flag': '🇺🇸'},  # served by get_us_heatmap
    'gb': {'name': 'United Kingdom', 'ccy': 'GBP', 'flag': '🇬🇧', 'indicators': [
        ('cpi',   'CPI YoY',           'Inflation',  'CPALTT01GBM659N', 'positive', 'negative', '%', False),
        ('unemp', 'Unemployment Rate', 'Employment', 'LRHUTTTTGBM156S', 'negative', 'negative', '%', False),
    ]},
    'eu': {'name': 'Eurozone', 'ccy': 'EUR', 'flag': '🇪🇺', 'indicators': [
        ('cpi',   'HICP YoY',          'Inflation',  'CP0000EZ19M086NEST', 'positive', 'negative', '%', True),
        ('unemp', 'Unemployment Rate', 'Employment', 'LRHUTTTTEZM156S',    'negative', 'negative', '%', False),
    ]},
    'jp': {'name': 'Japan', 'ccy': 'JPY', 'flag': '🇯🇵', 'indicators': [
        ('cpi',   'CPI YoY',           'Inflation',  'CPALTT01JPM659N', 'positive', 'negative', '%', False),
        ('unemp', 'Unemployment Rate', 'Employment', 'LRHUTTTTJPM156S', 'negative', 'negative', '%', False),
    ]},
    'ca': {'name': 'Canada', 'ccy': 'CAD', 'flag': '🇨🇦', 'indicators': [
        ('cpi',   'CPI YoY',           'Inflation',  'CPALTT01CAM659N', 'positive', 'negative', '%', False),
        ('unemp', 'Unemployment Rate', 'Employment', 'LRHUTTTTCAM156S', 'negative', 'negative', '%', False),
    ]},
    'au': {'name': 'Australia', 'ccy': 'AUD', 'flag': '🇦🇺', 'indicators': [
        ('cpi',   'CPI YoY (Qtr)',     'Inflation',  'CPALTT01AUQ659N', 'positive', 'negative', '%', False),
        ('unemp', 'Unemployment Rate', 'Employment', 'LRHUTTTTAUM156S', 'negative', 'negative', '%', False),
    ]},
    'nz': {'name': 'New Zealand', 'ccy': 'NZD', 'flag': '🇳🇿', 'indicators': [
        ('cpi',   'CPI YoY (Qtr)',     'Inflation',  'CPALTT01NZQ659N', 'positive', 'negative', '%', False),
        ('unemp', 'Unemployment Rate (Qtr)', 'Employment', 'LRHUTTTTNZQ156S', 'negative', 'negative', '%', False),
    ]},
}


@app.route('/api/heatmap/refresh')
def refresh_heatmap():
    """Clear heatmap + FMP indicator caches to force fresh FRED + forecast fetch."""
    for k in ['heatmap:us', 'fmp:indicators', 'fmp:calendar']:
        cache.delete(k)
    return ok({'cleared': True})


# ── Manual Consensus Forecasts ───────────────────────────────────
# Stored in Postgres. User enters consensus before each release.
# App compares vs actual FRED reading to compute beat/miss.

def _ensure_consensus_table():
    try:
        import psycopg2, os
        url = os.environ.get('DATABASE_URL')
        if not url: return False
        conn = psycopg2.connect(url, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consensus_forecasts (
                    id          SERIAL PRIMARY KEY,
                    indicator   VARCHAR(30) NOT NULL,  -- e.g. 'cpi', 'nfp', 'ppi'
                    forecast    FLOAT       NOT NULL,
                    release_date DATE,
                    note        TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Index for fast latest-per-indicator lookup
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_consensus_indicator
                ON consensus_forecasts(indicator, created_at DESC)
            """)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[CONSENSUS] table error: {e}')
        return False


def _get_consensus_forecasts():
    """Return {indicator: {forecast, release_date, note}} — latest per indicator."""
    ck = 'consensus:forecasts'
    cached = cache.get(ck)
    if cached is not None: return cached
    try:
        import psycopg2, os
        url = os.environ.get('DATABASE_URL')
        if not url: return {}
        conn = psycopg2.connect(url, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (indicator)
                    indicator, forecast, release_date, note, created_at
                FROM consensus_forecasts
                ORDER BY indicator, created_at DESC
            """)
            rows = cur.fetchall()
        conn.close()
        result = {}
        for indicator, forecast, release_date, note, created_at in rows:
            result[indicator] = {
                'forecast':     forecast,
                'release_date': release_date.isoformat() if release_date else None,
                'note':         note,
                'updated_at':   created_at.isoformat() if created_at else None,
            }
        cache.set(ck, result, 300)  # 5 min cache
        return result
    except Exception as e:
        print(f'[CONSENSUS] fetch error: {e}')
        return {}


@app.route('/api/consensus', methods=['GET'])
def get_consensus():
    """Get all stored consensus forecasts."""
    return ok(_get_consensus_forecasts())


@app.route('/api/consensus', methods=['POST'])
def set_consensus():
    """Set a consensus forecast for an indicator."""
    _ensure_consensus_table()
    data = request.get_json() or {}
    indicator = (data.get('indicator') or '').lower().strip()
    forecast  = data.get('forecast')
    if not indicator or forecast is None:
        return ok({'error': 'indicator and forecast required'}), 400
    try:
        import psycopg2, os, datetime
        url = os.environ.get('DATABASE_URL')
        if not url: return ok({'error': 'database unavailable'})
        conn = psycopg2.connect(url, sslmode='require')
        release_date = data.get('release_date')
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO consensus_forecasts (indicator, forecast, release_date, note)
                VALUES (%s, %s, %s, %s)
            """, (indicator, float(forecast), release_date, data.get('note')))
        conn.commit()
        conn.close()
        cache.delete('consensus:forecasts')
        cache.delete('heatmap:us')  # force heatmap to recompute with new forecast
        return ok({'saved': True, 'indicator': indicator, 'forecast': forecast})
    except Exception as e:
        return ok({'error': str(e)})


@app.route('/api/consensus/<indicator>', methods=['DELETE'])
def delete_consensus(indicator):
    """Clear the consensus forecast for an indicator."""
    try:
        import psycopg2, os
        url = os.environ.get('DATABASE_URL')
        if not url: return ok({'error': 'database unavailable'})
        conn = psycopg2.connect(url, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("DELETE FROM consensus_forecasts WHERE indicator=%s", (indicator.lower(),))
        conn.commit()
        conn.close()
        cache.delete('consensus:forecasts')
        cache.delete('heatmap:us')
        return ok({'deleted': True, 'indicator': indicator})
    except Exception as e:
        return ok({'error': str(e)})


@app.route('/api/heatmap/countries')
def heatmap_countries():
    """List available country heatmaps for the dropdown."""
    return ok({'countries': [
        {'code': c, 'name': v['name'], 'ccy': v['ccy'], 'flag': v['flag']}
        for c, v in COUNTRIES.items()
    ]})


@app.route('/api/heatmap/<country>')
def get_country_heatmap(country):
    country = (country or '').lower()
    cfg = COUNTRIES.get(country)
    if not cfg or 'indicators' not in cfg:
        return ok({'error': 'unknown country', 'available': list(COUNTRIES.keys())})
    ck = f'heatmap:{country}'
    cached = cache.get(ck)
    if cached: return ok(cached, cached=True)

    rows = []
    ccy_bull = ccy_bear = 0
    stk_bull = stk_bear = 0
    for key, label, category, series, ccy_dir, stocks_dir, unit, yoy_calc in cfg['indicators']:
        row = {'key': key, 'label': label, 'category': category, 'unit': unit,
               'actual': None, 'previous': None, 'change': None, 'date': '—',
               'usd_impact': 'Neutral', 'stocks_impact': 'Neutral',
               'actual_fmt': '—', 'previous_fmt': '—', 'change_fmt': '—',
               'source': 'none', 'reason': None}
        if FRED_KEY:
            try:
                pts = get_fred_series(series, years=3 if yoy_calc is True else 2)
                if pts and len(pts) >= 2:
                    row['source'] = 'FRED'
                    row['date'] = pts[-1]['date'][:7]
                    if yoy_calc is True and len(pts) >= 14:
                        cv, ya, pm, ya2 = pts[-1]['value'], pts[-13]['value'], pts[-2]['value'], pts[-14]['value']
                        actual   = round((cv - ya) / ya * 100, 2) if ya else 0
                        previous = round((pm - ya2) / ya2 * 100, 2) if ya2 else actual
                    else:
                        actual   = pts[-1]['value']
                        previous = pts[-2]['value']
                    change = round(actual - previous, 3)
                    ci, si, _ = calc_usd_stocks_impact(key, actual, previous, ccy_dir, stocks_dir, unit)
                    row.update({'actual': round(actual, 2), 'previous': round(previous, 2), 'change': change,
                                'usd_impact': ci, 'stocks_impact': si,
                                'actual_fmt': fmt_value(actual, unit),
                                'previous_fmt': fmt_value(previous, unit),
                                'change_fmt': ('+' if change > 0 else '') + fmt_value(change, unit)})
                    if ci == 'Bullish': ccy_bull += 1
                    elif ci == 'Bearish': ccy_bear += 1
                    if si == 'Bullish': stk_bull += 1
                    elif si == 'Bearish': stk_bear += 1
                else:
                    row['reason'] = f'FRED returned {0 if not pts else len(pts)} point(s)'
            except Exception as e:
                row['reason'] = f'{type(e).__name__}: {e}'
                print(f'[heatmap {country}] {key}: {e}')
        rows.append(row)

    scored = ccy_bull + ccy_bear
    sscored = stk_bull + stk_bear
    result = {'rows': rows, 'country': country, 'ccy': cfg['ccy'], 'name': cfg['name'], 'flag': cfg['flag'],
              'usd_pct': round(ccy_bull / scored * 100) if scored else 50,
              'stocks_pct': round(stk_bull / sscored * 100) if sscored else 50,
              'generated': int(time.time()), 'fred_key_set': bool(FRED_KEY)}
    if any(r.get('actual') is not None for r in rows):
        cache.set(ck, result, 1800)
    return ok(result)



# ══════════════════════════════════════════════════════════════════
# ◈ REGIME INTELLIGENCE ENGINE — Primary API
# ══════════════════════════════════════════════════════════════════

@app.route('/api/regime')
def get_regime():
    """
    Full RIE snapshot — the central intelligence API.
    Powers: Markets dashboard, Opportunities, Portfolio alignment.
    Cache: 15 minutes (updates frequently enough for daily use).
    """
    cached = cache.get('rie:snapshot')
    if cached: return ok(cached, cached=True)
    return ok(compute_regime_snapshot())


def compute_regime_snapshot():
    """Gather inputs, run the engine, cache and return the snapshot. Reused by /api/dashboard."""
    cached = cache.get('rie:snapshot')
    if cached: return cached

    # ── Gather all inputs ───────────────────────────────────────
    # 1. FRED economic data
    fred_data = {}
    fred_series = {
        'gdp':        ('A191RL1Q225SBEA', 3, False),
        'cpi':        ('CPIAUCNS',        3, True),   # needs YoY calc
        'core_cpi':   ('CPILFENS',        3, True),
        'ppi':        ('PPIFID',          3, True),
        'pce':        ('PCEPI',           3, True),
        'nfp':        ('PAYEMS',          2, 'mom_k'),
        'unemp':      ('UNRATE',          2, False),
        'jobless':    ('ICSA',            2, False),
        'jolts':      ('JTSJOL',          2, False),
        'retail':     ('RSXFS',           2, 'mom_pct'),
        'm2':         ('M2SL',            2, 'mom_pct'),
        'real_yield': ('DFII10',          2, False),
        'fed_balance':('WALCL',           2, 'mom_pct'),
        'reverse_repo':('RRPONTSYD',      2, False),   # $B, absolute change
        'tga':        ('WTREGEN',         2, False),   # Treasury Gen Acct, $B
        'yield_curve':('T10Y2Y',          2, False),   # 2s10s spread, level
        'consumer_sent': ('UMCSENT',      2, False),
    }

    if FRED_KEY:
        import concurrent.futures as _cf

        def _fetch_hm(args):
            key, (series, years, calc_type) = args
            try:
                return key, get_fred_series(series, years=years), calc_type
            except:
                return key, None, calc_type

        hm_raw = {}
        with _cf.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(_fetch_hm, item) for item in fred_series.items()]
            for f in _cf.as_completed(futs, timeout=20):
                try:
                    key, pts, calc_type = f.result()
                    hm_raw[key] = (pts, calc_type)
                except: pass

        for key, (pts, calc_type) in hm_raw.items():
            try:
                if not pts or len(pts) < 2:
                    continue
                curr = pts[-1]
                prev = pts[-2]

                if calc_type is True:
                    # YoY from index
                    if len(pts) >= 14:
                        yoy_curr = (curr['value'] - pts[-13]['value']) / pts[-13]['value'] * 100
                        yoy_prev = (prev['value'] - pts[-14]['value']) / pts[-14]['value'] * 100 if len(pts) >= 14 else yoy_curr
                        fred_data[key] = {
                            'actual':   round(yoy_curr, 2),
                            'previous': round(yoy_prev, 2),
                            'change':   round(yoy_curr - yoy_prev, 3),
                            'date':     curr['date'][:7],
                        }
                elif calc_type == 'mom_k':
                    # Monthly change in thousands
                    fred_data[key] = {
                        'actual':   round(curr['value'] - prev['value'], 1),
                        'previous': round(prev['value'] - pts[-3]['value'], 1) if len(pts) >= 3 else 0,
                        'change':   0,
                        'date':     curr['date'][:7],
                    }
                elif calc_type == 'mom_pct':
                    pct = (curr['value'] - prev['value']) / prev['value'] * 100 if prev['value'] else 0
                    fred_data[key] = {
                        'actual':   round(pct, 2),
                        'previous': prev['value'],
                        'change':   round(pct, 2),
                        'date':     curr['date'][:7],
                    }
                else:
                    fred_data[key] = {
                        'actual':   curr['value'],
                        'previous': prev['value'],
                        'change':   round(curr['value'] - prev['value'], 3),
                        'date':     curr['date'][:7],
                    }
            except Exception as e:
                print(f'[RIE] FRED {key} error: {e}')

    # 2. Live price data for key instruments
    price_tickers = {
        'spy': 'SPY', 'qqq': 'QQQ', 'iwm': 'IWM', 'dia': 'DIA',
        'rsp': 'RSP', 'tlt': 'TLT', 'hyg': 'HYG', 'uup': 'UUP',
        'gld': 'GLD', 'uso': 'USO', 'vix': '^VIX',
        # Internals rotation proxies (offense/defense, risk-appetite, semis)
        'xly': 'XLY', 'xlp': 'XLP', 'sphb': 'SPHB', 'splv': 'SPLV', 'smh': 'SMH',
    }
    price_data = {}
    import concurrent.futures
    def _fetch_px(item):
        key, ticker = item
        try:    return key, get_live_price(ticker)
        except: return key, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_fetch_px, it) for it in price_tickers.items()]
        for f in concurrent.futures.as_completed(futs, timeout=25):
            try:
                key, p = f.result()
                if p: price_data[key] = p
            except: pass

    # Enrich SPY with moving average data for the Price Action pillar
    try:
        spy_ma = get_moving_averages('SPY')
        if spy_ma and 'spy' in price_data:
            price_data['spy']['ma_data'] = spy_ma
    except Exception as e:
        print(f'[RIE] MA enrichment error: {e}')

    # ── Enrich fred_data: trend + surprise + PMIs ────────────────
    # Trend: is each indicator improving or deteriorating vs the prior release?
    _HIGHER_GOOD = {'gdp', 'retail', 'nfp', 'jolts', 'consumer_sent'}
    _LOWER_GOOD  = {'cpi', 'core_cpi', 'ppi', 'pce', 'unemp', 'jobless'}
    for key, d in fred_data.items():
        a, p = d.get('actual'), d.get('previous')
        if a is not None and p is not None:
            if key in _HIGHER_GOOD:
                d['trend'] = 'improving' if a > p else ('deteriorating' if a < p else 'stable')
            elif key in _LOWER_GOOD:
                d['trend'] = 'improving' if a < p else ('deteriorating' if a > p else 'stable')
            else:
                d['trend'] = 'stable'

    # Surprise: beat/miss vs consensus forecast (reuses the heatmap matcher)
    try:
        forecasts = _heatmap_forecasts()
        for key, fc_raw in forecasts.items():
            if key in fred_data and fred_data[key].get('actual') is not None:
                fc = _align_forecast(fc_raw, fred_data[key]['actual'])
                if fc is not None:
                    diff = fred_data[key]['actual'] - fc
                    sp = abs(diff / fc * 100) if fc else 0
                    if sp < 1.0:
                        fred_data[key]['surprise'] = 'inline'
                    elif key in _HIGHER_GOOD:
                        fred_data[key]['surprise'] = 'beat' if diff > 0 else 'miss'
                    elif key in _LOWER_GOOD:
                        fred_data[key]['surprise'] = 'beat' if diff < 0 else 'miss'
                    else:
                        fred_data[key]['surprise'] = 'inline'
                    fred_data[key]['surprise_pct'] = round(sp, 1)
    except Exception as e:
        print(f'[RIE] surprise enrichment error: {e}')

    # PMIs: extract ISM Manufacturing + Services from the calendar
    try:
        cal_events = get_fmp_economic_calendar() or []
        for e in cal_events:
            name = str(e.get('event', '')).lower()
            actual_val = parse_num(e.get('actual', ''))
            if actual_val is None:
                continue
            prev_val = parse_num(e.get('previous', ''))
            fc_val = parse_num(e.get('forecast', ''))
            if ('ism' in name and 'manufacturing' in name and 'non' not in name
                    and 'price' not in name and 'ism_mfg' not in fred_data):
                d = {'actual': actual_val, 'previous': prev_val, 'date': e.get('date', '')}
                d['trend'] = 'improving' if prev_val and actual_val > prev_val else ('deteriorating' if prev_val and actual_val < prev_val else 'stable')
                if fc_val:
                    sp = abs(actual_val - fc_val) / fc_val * 100 if fc_val else 0
                    d['surprise'] = ('beat' if actual_val > fc_val else 'miss') if sp >= 1.0 else 'inline'
                    d['surprise_pct'] = round(sp, 1)
                fred_data['ism_mfg'] = d
            elif ('ism' in name and ('service' in name or 'non-manufacturing' in name)
                  and 'price' not in name and 'ism_svc' not in fred_data):
                d = {'actual': actual_val, 'previous': prev_val, 'date': e.get('date', '')}
                d['trend'] = 'improving' if prev_val and actual_val > prev_val else ('deteriorating' if prev_val and actual_val < prev_val else 'stable')
                if fc_val:
                    sp = abs(actual_val - fc_val) / fc_val * 100 if fc_val else 0
                    d['surprise'] = ('beat' if actual_val > fc_val else 'miss') if sp >= 1.0 else 'inline'
                    d['surprise_pct'] = round(sp, 1)
                fred_data['ism_svc'] = d
    except Exception as e:
        print(f'[RIE] PMI enrichment error: {e}')

    # ── Gather sentiment inputs (Pillar 5) ───────────────────────
    sentiment_data = build_sentiment_inputs(fred_data)

    # ── Hydrate engine history from durable store (survives restarts) ──
    if store:
        try:
            hist = store.get_snapshots(since_ts=int(time.time()) - 95 * 86400)
            if hist:
                cache.set('rie:history', hist, 95 * 86400)
        except Exception as e:
            print(f'[STORE] hydrate error: {e}')

    # ── Run the engine ───────────────────────────────────────────
    snapshot = run_rie(fred_data, price_data, sentiment_data)

    # ── Persist snapshot + raw indicators (for trend + percentiles) ──
    if store:
        try:
            ts = snapshot.get('timestamp') or int(time.time())
            wrote = store.record_snapshot(ts, snapshot['regime_score'], {
                'pillars': snapshot.get('pillar_scores', {}),
                'assets':  {k: v.get('score') for k, v in snapshot.get('asset_scores', {}).items()},
                'label':   snapshot.get('regime_label'),
            })
            if wrote:
                store.record_indicator('regime_score', ts, snapshot['regime_score'])
                for k, v in snapshot.get('pillar_scores', {}).items():
                    store.record_indicator('pillar_' + k, ts, v)
                for key, d in fred_data.items():
                    if isinstance(d, dict) and d.get('actual') is not None:
                        store.record_indicator('macro_' + key, ts, d['actual'])
                vix = (price_data.get('vix') or {}).get('price')
                if vix is not None:
                    store.record_indicator('macro_vix', ts, vix)
        except Exception as e:
            print(f'[STORE] persist error: {e}')

    cache.set('rie:snapshot', snapshot, 900)  # 15 min cache
    return snapshot


# ══════════════════════════════════════════════════════════════════
# ◈ SENTIMENT INPUTS — COT positioning + manual Put/Call & AAII
# Put/Call and AAII have no free API, so they're supplied manually via
# /api/sentiment/inputs and auto-expire to neutral when stale (P/C >3d,
# AAII >10d) so the pillar never scores off forgotten week-old numbers.
# ══════════════════════════════════════════════════════════════════
PC_MAX_AGE_S   = 3  * 86400   # put/call considered stale after 3 days
AAII_MAX_AGE_S = 10 * 86400   # AAII considered stale after 10 days


def build_sentiment_inputs(fred_data):
    """Assemble the optional sentiment_data dict the engine consumes."""
    out = {}
    now = time.time()

    # 1. COT positioning — S&P 500 large specs, only if the feed is LIVE
    try:
        cot = cache.get('cot:SPX') or fetch_cot_live('SPX')
        if cot and cot.get('source') == 'live':
            ls = cot.get('large_specs', {})
            out['cot_spx'] = {'long': ls.get('long', 0), 'short': ls.get('short', 0), 'net': ls.get('net', 0)}
    except Exception as e:
        print(f'[SENTIMENT] COT input error: {e}')

    # 2. Manual Put/Call & AAII — with staleness guard
    manual = cache.get('sentiment:manual') or {}
    pc = manual.get('put_call')
    if pc and (now - pc.get('ts', 0)) <= PC_MAX_AGE_S:
        out['put_call'] = pc['value']
    aaii = manual.get('aaii')
    if aaii and (now - aaii.get('ts', 0)) <= AAII_MAX_AGE_S:
        out['aaii_spread'] = round(aaii['bullish'] - aaii['bearish'], 1)

    # 3. Consumer sentiment (UMCSENT) — free, already fetched
    cons = fred_data.get('consumer_sent')
    if cons:
        out['consumer_sent'] = cons

    return out


@app.route('/api/sentiment/inputs', methods=['GET'])
def get_sentiment_inputs():
    """Show the currently stored manual inputs + whether each is live or stale."""
    now = time.time()
    manual = cache.get('sentiment:manual') or {}
    def status(entry, max_age):
        if not entry: return {'set': False}
        age = now - entry.get('ts', 0)
        return {'set': True, 'age_days': round(age / 86400, 1), 'stale': age > max_age, 'ts': entry.get('ts')}
    pc = manual.get('put_call'); aaii = manual.get('aaii')
    return ok({
        'put_call': {**({'value': pc['value']} if pc else {}), **status(pc, PC_MAX_AGE_S)},
        'aaii':     {**({'bullish': aaii['bullish'], 'bearish': aaii['bearish']} if aaii else {}), **status(aaii, AAII_MAX_AGE_S)},
        'note': 'Put/Call stale after 3 days, AAII after 10 days — then they revert to neutral automatically.',
    })


@app.route('/api/sentiment/inputs', methods=['POST'])
def set_sentiment_inputs():
    """
    Update manual sentiment inputs. Token-guarded: send header
    'X-Sentiment-Token' matching env SENTIMENT_TOKEN (if that env is set).
    Body JSON: {"put_call": 0.74, "aaii_bullish": 33.1, "aaii_bearish": 35.5}
    Any field may be sent on its own.
    """
    token = os.environ.get('SENTIMENT_TOKEN', '')
    if token and request.headers.get('X-Sentiment-Token', '') != token:
        return jsonify({'error': 'unauthorized'}), 401

    body = request.get_json(silent=True) or {}
    manual = cache.get('sentiment:manual') or {}
    now = int(time.time())
    updated = []

    if 'put_call' in body:
        try:
            manual['put_call'] = {'value': float(body['put_call']), 'ts': now}
            updated.append('put_call')
        except (ValueError, TypeError):
            return jsonify({'error': 'put_call must be a number'}), 400

    if 'aaii_bullish' in body and 'aaii_bearish' in body:
        try:
            manual['aaii'] = {'bullish': float(body['aaii_bullish']), 'bearish': float(body['aaii_bearish']), 'ts': now}
            updated.append('aaii')
        except (ValueError, TypeError):
            return jsonify({'error': 'aaii_bullish/aaii_bearish must be numbers'}), 400

    if not updated:
        return jsonify({'error': 'nothing updated — send put_call and/or aaii_bullish+aaii_bearish'}), 400

    cache.set('sentiment:manual', manual, 90 * 86400)  # persist 90d; staleness handled on read
    cache.delete('rie:snapshot')                       # force regime recompute with new inputs
    return ok({'updated': updated})


@app.route('/api/regime/refresh')
def refresh_regime():
    cache.delete('rie:snapshot')
    return ok({'cleared': True})


# ══════════════════════════════════════════════════════════════════
# ◈ THEME ROTATION RADAR — Capital Flow Detection Engine
# ══════════════════════════════════════════════════════════════════

def _rotation_history(theme_key):
    """Pull rank/RS history for theme_key from durable store."""
    out = {'rank_1w_ago': None, 'rank_4w_ago': None, 'rs_4w_ago': None, 'rs_percentile_2y': None}
    if not store or not ROTATION_AVAILABLE:
        return out
    try:
        rank_series = store.get_series(rotation.series_key(theme_key, 'rank'), window_days=40, max_points=60)
        rs_series   = store.get_series(rotation.series_key(theme_key, 'rs'),   window_days=40, max_points=60)
        now = time.time()
        def closest_before(series, days_ago):
            target = now - days_ago * 86400
            cands = [v for ts, v in series if ts <= target]
            return cands[-1] if cands else None
        out['rank_1w_ago'] = closest_before(rank_series, 7)
        out['rank_4w_ago'] = closest_before(rank_series, 28)
        out['rs_4w_ago']   = closest_before(rs_series, 28)
        rs_now_series = store.get_series(rotation.series_key(theme_key, 'rs'), window_days=730, max_points=500)
        if rs_now_series:
            cur_rs = rs_now_series[-1][1]
            pct = store.percentile_rank(rotation.series_key(theme_key, 'rs'), cur_rs, window_days=730)
            out['rs_percentile_2y'] = pct
    except Exception as e:
        print(f'[ROTATION] history error for {theme_key}: {e}')
    return out


def _rotation_save_history(snapshots):
    """Persist today's rank + RS per theme so future runs can compute deltas."""
    if not store or not ROTATION_AVAILABLE:
        return
    ts = int(time.time())
    for key, s in snapshots.items():
        try:
            if s.get('rank_now') is not None:
                store.record_indicator(rotation.series_key(key, 'rank'), ts, float(s['rank_now']))
            if s.get('rs_vs_spy') is not None:
                store.record_indicator(rotation.series_key(key, 'rs'), ts, float(s['rs_vs_spy']))
        except Exception as e:
            print(f'[ROTATION] save error for {key}: {e}')


def _compute_rotation_snapshot():
    """Build the full rotation snapshot dict. Returns (result, closes_by_ticker)."""
    import concurrent.futures
    all_tickers = set(['SPY'])
    for key in rotation.all_theme_keys():
        cfg = rotation._basket_cfg(key)
        if cfg['etf']:
            all_tickers.add(cfg['etf'])
        all_tickers.update(cfg['tickers'])

    closes_by_ticker = {}
    def _fetch(t):
        try: return t, get_price_closes(t, '6mo')  # 6mo faster than 1y, enough for momentum
        except Exception: return t, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        futs = {pool.submit(_fetch, t): t for t in all_tickers}
        for f in concurrent.futures.as_completed(futs, timeout=60):
            try:
                t, closes = f.result(timeout=10)
                if closes: closes_by_ticker[t] = closes
            except Exception: pass
    print(f"[rotation] Fetched {len(closes_by_ticker)}/{len(all_tickers)} tickers")

    spy_closes = closes_by_ticker.get('SPY')
    regime_label = 'mid_cycle'
    try:
        rie_snap = cache.get('rie:snapshot') or compute_regime_snapshot()
        pillar_scores = rie_snap.get('pillar_scores', {})
        if pillar_scores:
            regime_label = rotation.cycle_phase_from_pillars(pillar_scores)
    except Exception as e:
        print(f'[ROTATION] regime context error: {e}')

    snapshots = {}
    for key in rotation.all_theme_keys():
        history = _rotation_history(key)
        snap = rotation.build_theme_snapshot(
            key, closes_by_ticker, spy_closes, regime_label,
            news_sentiment=None, history=history,
        )
        if snap:
            snapshots[key] = snap

    rotation.rank_and_score(snapshots)
    _rotation_save_history(snapshots)

    result = {
        'regime_label': regime_label,
        'generated': int(time.time()),
        'themes':  [s for k, s in snapshots.items() if k in rotation.THEMES],
        'sectors': [s for k, s in snapshots.items() if k in rotation.SECTORS],
    }
    for grp in (result['themes'], result['sectors']):
        grp.sort(key=lambda s: s['rank_now'])
    return result, closes_by_ticker


@app.route('/api/version')
def get_version():
    """Quick health check — confirms which features are live on this deploy."""
    return ok({
        'rotation_available': ROTATION_AVAILABLE,
        'routes': ['/api/rotation', '/api/rotation/<theme>', '/api/cot/backfill',
                   '/api/positioning', '/api/consensus', '/api/trades'],
        'cot_markets': list(COT_MARKETS.keys()),
        'build': 'jun23-2026',
    })


@app.route('/api/rotation/refresh')
def refresh_rotation():
    """Force rebuild the rotation snapshot synchronously."""
    if not ROTATION_AVAILABLE:
        return jsonify({'ok': False, 'error': 'rotation module unavailable'})
    import json, traceback as _tb
    try:
        print("[rotation/refresh] Step 1: collecting ETF tickers...")
        etf_tickers = set(['SPY'])
        for key in rotation.all_theme_keys():
            cfg = rotation._basket_cfg(key)
            if cfg and cfg.get('etf'): etf_tickers.add(cfg['etf'])
        closes_by_ticker = {}
        import concurrent.futures as _cf
        def _fetch_one(t):
            try:
                c = get_price_closes(t, '6mo')
                return t, c
            except Exception as e:
                print(f"[rotation/refresh] {t} failed: {e}")
                return t, None
        print(f"[rotation/refresh] Step 2: fetching {len(etf_tickers)} ETFs in parallel...")
        with _cf.ThreadPoolExecutor(max_workers=25) as pool:
            for t, c in pool.map(_fetch_one, sorted(etf_tickers), timeout=90):
                if c: closes_by_ticker[t] = c
        print(f"[rotation/refresh] Step 3: got {len(closes_by_ticker)}/{len(etf_tickers)} ETFs, building snapshots...")
        spy_closes = closes_by_ticker.get('SPY')
        regime_label = 'mid_cycle'
        try:
            rie_snap = cache.get('rie:snapshot') or compute_regime_snapshot()
            pillar_scores = rie_snap.get('pillar_scores', {})
            if pillar_scores:
                regime_label = rotation.cycle_phase_from_pillars(pillar_scores)
        except Exception as e:
            print(f"[rotation/refresh] regime error: {e}")
        snapshots = {}
        for key in rotation.all_theme_keys():
            try:
                history = _rotation_history(key)
                snap = rotation.build_theme_snapshot(key, closes_by_ticker, spy_closes, regime_label, history=history)
                if snap: snapshots[key] = snap
            except Exception as e:
                print(f"[rotation/refresh] snapshot error for {key}: {e}")
        print(f"[rotation/refresh] Step 4: ranking {len(snapshots)} snapshots...")
        rotation.rank_and_score(snapshots)
        _rotation_save_history(snapshots)
        result = {
            'regime_label': regime_label,
            'generated': int(time.time()),
            'themes':  [s for k, s in snapshots.items() if k in rotation.THEMES],
            'sectors': [s for k, s in snapshots.items() if k in rotation.SECTORS],
        }
        for grp in (result['themes'], result['sectors']):
            grp.sort(key=lambda s: s.get('rank_now') or 999)
        result_safe = json.loads(json.dumps(result, default=lambda x: float(x) if hasattr(x, '__float__') else str(x)))
        cache.set('rotation:snapshot', result_safe, 1800)
        n = len(result_safe.get('themes', []))
        print(f"[rotation/refresh] Done — {n} themes cached")
        return jsonify({'ok': True, 'data': {'refreshed': True, 'themes': n, 'tickers_fetched': len(closes_by_ticker)}})
    except Exception as e:
        tb = _tb.format_exc()
        print(f"[rotation/refresh] CRASH: {e}\n{tb}")
        return jsonify({'ok': False, 'error': str(e), 'traceback': tb}), 200


@app.route('/api/rotation')
def get_rotation():
    """Theme Rotation Radar — serves from cache only. Background thread builds it."""
    if not ROTATION_AVAILABLE:
        return ok({'error': 'rotation module unavailable'})
    cached = cache.get('rotation:snapshot')
    if cached:
        return ok(cached, cached=True)
    # Not cached yet — background thread is building it, tell frontend to retry
    return ok({'warming': True, 'message': 'Rotation data is being built in the background — retry in 30 seconds.'})


@app.route('/api/rotation/<theme_key>')
def get_rotation_theme(theme_key):
    """Drilldown for a single theme/sector."""
    if not ROTATION_AVAILABLE:
        return ok({'error': 'rotation module unavailable'})
    cfg = rotation._basket_cfg(theme_key)
    if not cfg:
        return ok({'error': 'unknown theme', 'available': rotation.all_theme_keys()})

    snap_cache = cache.get('rotation:snapshot')
    snapshot = None
    closes_by_ticker = {}
    if snap_cache:
        for grp in (snap_cache.get('themes', []), snap_cache.get('sectors', [])):
            for s in grp:
                if s['theme_key'] == theme_key:
                    snapshot = s; break
    if snap_cache is None:
        result, closes_by_ticker = _compute_rotation_snapshot()
        cache.set('rotation:snapshot', result, 1800)
        for grp in (result.get('themes', []), result.get('sectors', [])):
            for s in grp:
                if s['theme_key'] == theme_key:
                    snapshot = s; break

    constituents = []
    tickers = ([cfg['etf']] if cfg['etf'] else []) + cfg['tickers']
    for t in tickers:
        if not t: continue
        try:
            closes = closes_by_ticker.get(t) or get_price_closes(t, '1y')
            if not closes: continue
            ma = get_moving_averages(t)
            mom_1m = rotation.pct_change([{'value': c} for c in closes], 21)
            mom_3m = rotation.pct_change([{'value': c} for c in closes], 63)
            constituents.append({
                'ticker': t, 'is_etf': (t == cfg['etf']),
                'price': round(closes[-1], 2),
                'momentum_1m': round(mom_1m, 2) if mom_1m is not None else None,
                'momentum_3m': round(mom_3m, 2) if mom_3m is not None else None,
                'pct_from_50ma': ma.get('pct_from_50') if ma else None,
                'pct_from_200ma': ma.get('pct_from_200') if ma else None,
            })
        except Exception as e:
            print(f'[ROTATION] constituent {t} error: {e}')
    constituents.sort(key=lambda c: (c['momentum_3m'] if c['momentum_3m'] is not None else -999), reverse=True)
    return ok({
        'theme_key': theme_key, 'name': cfg['name'], 'snapshot': snapshot,
        'best_performers':  constituents[:3],
        'worst_performers': constituents[-3:][::-1] if len(constituents) > 3 else [],
        'constituents': constituents,
        'note': ('This identifies where capital has recently moved — description of current '
                 'positioning, not a prediction.'),
    })


@app.route('/api/regime/history')
def get_regime_history():
    """Historical regime scores — up to 95 days from the durable store."""
    cached = cache.get('rie:history')
    if cached:
        return ok({'points': cached, 'count': len(cached)}, cached=True)
    if not store:
        return ok({'points': [], 'count': 0, 'note': 'store unavailable'})
    try:
        since = int(time.time()) - 95 * 86400
        hist = store.get_snapshots(since_ts=since)
        if hist:
            cache.set('rie:history', hist, 3600)
        return ok({'points': hist or [], 'count': len(hist or [])})
    except Exception as e:
        return ok({'points': [], 'count': 0, 'error': str(e)})


@app.route('/api/debug/backtest')
def debug_backtest():
    """Debug: check price history and stored scores for the backtest engine."""
    import datetime as _dt
    test_tickers = ['SPY', 'TLT', 'GLD', 'UUP', 'USO']
    price_results = {}
    for t in test_tickers:
        pairs = get_price_closes_with_dates(t, '1y')
        if pairs:
            price_results[t] = {
                'count': len(pairs),
                'first_date': _dt.datetime.utcfromtimestamp(pairs[0][0]).strftime('%Y-%m-%d'),
                'last_date':  _dt.datetime.utcfromtimestamp(pairs[-1][0]).strftime('%Y-%m-%d'),
            }
        else:
            price_results[t] = {'error': 'no data'}

    stored_scores = {}
    if store:
        try:
            for t in test_tickers:
                series = store.get_series(f'score_{t}', window_days=30, max_points=30)
                stored_scores[t] = {
                    'count': len(series),
                    'latest': {'ts': series[-1][0], 'score': series[-1][1],
                               'date': _dt.datetime.utcfromtimestamp(series[-1][0]).strftime('%Y-%m-%d')}
                    if series else None,
                }
        except Exception as e:
            stored_scores['error'] = str(e)

    return ok({'price_history': price_results, 'stored_scores': stored_scores})


@app.route('/api/backtest/regime')
def get_regime_backtest():
    """
    Regime self-backtest: for every day we have stored scorecard data,
    check whether the per-ticker composite signal (≥57 Bullish / ≤43 Bearish)
    predicted price direction correctly over 5, 10, and 20 trading days.

    Data source: 'score_{ticker}' indicator series stored by build_setups_matrix
    on every fresh matrix build. Covers all 40 SCORECARD_ASSETS tickers.
    Cached 6hr.
    """
    cached = cache.get('backtest:regime')
    if cached:
        return ok(cached, cached=True)

    if not store:
        return ok({'error': 'store unavailable', 'signals': []})

    try:
        import datetime as _dt
        from collections import OrderedDict

        BULL = 57
        BEAR = 43
        WINDOWS = [5, 10, 20]

        # 1. Pull stored per-ticker scores for all 40 assets
        since = int(time.time()) - 95 * 86400
        ticker_series = {}
        for ticker in SCORECARD_ASSETS:
            try:
                series = store.get_series(f'score_{ticker}', window_days=95, max_points=200)
                if series:
                    ticker_series[ticker] = series  # [(ts, score), ...]
            except Exception:
                pass

        if not ticker_series:
            return ok({
                'error': 'No stored scores yet — scores are saved on each setups matrix build. '
                         'Visit the Top Setups page to trigger the first save, then check back tomorrow.',
                'signals': [], 'count': 0,
            })

        # 2. Fetch 1yr dated price history for tickers that have stored scores
        tickers_with_scores = list(ticker_series.keys())
        price_history = {}

        import concurrent.futures
        def _fetch_dated(t):
            try: return t, get_price_closes_with_dates(t, '1y')
            except Exception: return t, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_dated, t): t for t in tickers_with_scores}
            for f in concurrent.futures.as_completed(futs, timeout=90):
                try:
                    t, pairs = f.result()
                    if pairs: price_history[t] = pairs
                except Exception:
                    pass

        # 3. Build date-indexed price lookup
        def _ts_to_date(ts):
            return _dt.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')

        price_by_date = {}
        price_sorted  = {}
        for tk, pairs in price_history.items():
            arr = [(_ts_to_date(ts), c) for ts, c in pairs]
            price_by_date[tk] = {d: (i, c) for i, (d, c) in enumerate(arr)}
            price_sorted[tk] = arr

        def price_at_signal(ticker, signal_ts):
            arr = price_sorted.get(ticker)
            if not arr: return None
            target = _ts_to_date(signal_ts)
            for d, c in arr:
                if d >= target: return c
            return None

        def price_n_days_after(ticker, signal_ts, n):
            arr = price_sorted.get(ticker)
            if not arr: return None
            target = _ts_to_date(signal_ts)
            sig_idx = next((i for i, (d, c) in enumerate(arr) if d >= target), None)
            if sig_idx is None: return None
            target_idx = sig_idx + n
            return arr[target_idx][1] if target_idx < len(arr) else None

        # 4. Build signal records from stored score series
        # Also get regime label for each timestamp from stored regime snapshots
        regime_hist = store.get_snapshots(since_ts=since) or []
        regime_by_day = {}
        for snap in regime_hist:
            d = _ts_to_date(snap['ts'])
            regime_by_day[d] = {'label': snap.get('label','Neutral'), 'score': snap.get('score',50)}

        signals = []
        for ticker, series in ticker_series.items():
            asset_info = SCORECARD_ASSETS.get(ticker, {})
            # Deduplicate by calendar day (keep last score per day)
            by_day = OrderedDict()
            for ts, score in sorted(series, key=lambda x: x[0]):
                by_day[_ts_to_date(ts)] = (ts, score)

            for day, (ts, score) in by_day.items():
                composite = int(round(score))
                if BEAR < composite < BULL: continue
                direction = 'Bullish' if composite >= BULL else 'Bearish'
                sig_price = price_at_signal(ticker, ts)
                if sig_price is None: continue

                reg = regime_by_day.get(day, {})
                outcomes = {}
                for w in WINDOWS:
                    fp = price_n_days_after(ticker, ts, w)
                    if fp is None:
                        outcomes[f'd{w}_ret'] = None
                        outcomes[f'd{w}_correct'] = None
                    else:
                        ret = (fp - sig_price) / sig_price * 100
                        outcomes[f'd{w}_ret'] = round(ret, 2)
                        outcomes[f'd{w}_correct'] = (ret > 0) if direction == 'Bullish' else (ret < 0)

                if outcomes.get('d5_correct') is None:
                    continue  # too recent

                signals.append({
                    'ts':           ts,
                    'date':         day,
                    'regime_label': reg.get('label', 'Unknown'),
                    'regime_score': reg.get('score'),
                    'ticker':       ticker,
                    'name':         asset_info.get('n', ticker),
                    'type':         asset_info.get('type', ''),
                    'composite':    composite,
                    'direction':    direction,
                    **outcomes,
                })

        if not signals:
            return ok({
                'error': 'No completed signals yet — signals need 5+ trading days to resolve.',
                'signals': [], 'count': 0,
                'tickers_with_stored_scores': len(ticker_series),
                'note': 'Scores are now being stored daily. First results appear after 5 trading days.',
            })

        # 5. Aggregate stats
        def agg(sigs, w):
            done = [s for s in sigs if s.get(f'd{w}_correct') is not None]
            if not done: return None
            correct = sum(1 for s in done if s[f'd{w}_correct'])
            bull_rets = [s[f'd{w}_ret'] for s in done if s['direction']=='Bullish' and s[f'd{w}_ret'] is not None]
            bear_rets = [s[f'd{w}_ret'] for s in done if s['direction']=='Bearish' and s[f'd{w}_ret'] is not None]
            return {
                'total': len(done),
                'correct': correct,
                'accuracy': round(correct / len(done) * 100, 1),
                'avg_ret_bullish': round(sum(bull_rets)/len(bull_rets), 2) if bull_rets else None,
                'avg_ret_bearish': round(sum(bear_rets)/len(bear_rets), 2) if bear_rets else None,
            }

        def agg_group(sigs, w, key):
            groups = {}
            for s in sigs:
                groups.setdefault(s.get(key, 'other'), []).append(s)
            return {g: agg(ss, w) for g, ss in groups.items() if agg(ss, w)}

        # Per-ticker accuracy (min 3 signals)
        by_ticker = {}
        for s in signals:
            by_ticker.setdefault(s['ticker'], []).append(s)
        ticker_acc = []
        for tk, ss in by_ticker.items():
            a = agg(ss, 10) or agg(ss, 5)
            if a and a['total'] >= 3:
                ticker_acc.append({'ticker': tk, 'name': ss[0]['name'], 'type': ss[0]['type'], **a})
        ticker_acc.sort(key=lambda x: x['accuracy'], reverse=True)

        all_dates = sorted({s['date'] for s in signals})
        result = {
            'tickers_tracked': len(ticker_series),
            'signals_total': len(signals),
            'date_range': {
                'from_date': all_dates[0] if all_dates else None,
                'to_date':   all_dates[-1] if all_dates else None,
            },
            'overall':    {f'd{w}': agg(signals, w) for w in WINDOWS},
            'by_class':   {f'd{w}': agg_group(signals, w, 'type') for w in WINDOWS},
            'by_regime':  {f'd{w}': agg_group(signals, w, 'regime_label') for w in WINDOWS},
            'best_assets':  ticker_acc[:5],
            'worst_assets': ticker_acc[-5:][::-1] if len(ticker_acc) >= 5 else [],
            'recent_signals': sorted(signals, key=lambda x: x['ts'], reverse=True)[:50],
            'note': ('Accuracy = % of directional signals that were correct. '
                     'Bullish ≥57, Bearish ≤43. '
                     'Results grow more reliable with more history (target: 60+ days).'),
        }

        cache.set('backtest:regime', result, 21600)
        return ok(result)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f'[BACKTEST] error: {e}\n{tb}')
        return ok({'error': f'{type(e).__name__}: {e}', 'traceback': tb, 'signals': []})


# ══════════════════════════════════════════════════════════════════
# ◈ STORE — persistence status + FRED history backfill
# Backfill seeds the indicator table with decades of FRED history so
# percentile / z-score normalisation works on day one. Only the raw-level
# (untransformed) series are backfilled, so the seeded history matches the
# live values exactly — YoY/MoM-transformed series accrue live instead.
# ══════════════════════════════════════════════════════════════════
BACKFILL_SERIES = {
    'macro_gdp':           'A191RL1Q225SBEA',
    'macro_unemp':         'UNRATE',
    'macro_jobless':       'ICSA',
    'macro_jolts':         'JTSJOL',
    'macro_real_yield':    'DFII10',
    'macro_reverse_repo':  'RRPONTSYD',
    'macro_tga':           'WTREGEN',
    'macro_yield_curve':   'T10Y2Y',
    'macro_consumer_sent': 'UMCSENT',
}


@app.route('/api/debug/fred')
def debug_fred():
    """Call FRED exactly like the app does and report the raw outcome."""
    series = request.args.get('series', 'CPIAUCNS')
    key = FRED_KEY or ''
    out = {'series': series, 'fred_base': FRED_BASE, 'key_set': bool(key),
           'key_len': len(key), 'key_tail': key[-4:] if key else None}
    try:
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        r = requests.get(FRED_BASE, params={
            'series_id': series, 'observation_start': start,
            'file_type': 'json', 'sort_order': 'asc', 'api_key': key}, timeout=15)
        out['http_status'] = r.status_code
        out['body_snippet'] = r.text[:500]
    except Exception as e:
        out['request_error'] = f'{type(e).__name__}: {e}'
    try:
        pts = get_fred_series(series, years=1)
        out['get_fred_series_points'] = (len(pts) if pts else 0)
    except Exception as e:
        out['get_fred_series_error'] = f'{type(e).__name__}: {e}'
    return ok(out)


@app.route('/api/store/status')
def store_status():
    if not store:
        return ok({'available': False, 'backend': 'none', 'note': 'store module not loaded'})
    return ok(store.status())


# Series available for trend charts: key -> (store name, label, unit)
CHARTABLE = {
    'cpi':          ('macro_cpi',          'CPI YoY',                '%'),
    'core_cpi':     ('macro_core_cpi',     'Core CPI YoY',           '%'),
    'ppi':          ('macro_ppi',          'PPI YoY',                '%'),
    'real_yield':   ('macro_real_yield',   '10Y Real Yield',         '%'),
    'gdp':          ('macro_gdp',          'GDP Growth',             '%'),
    'unemp':        ('macro_unemp',        'Unemployment Rate',      '%'),
    'jobless':      ('macro_jobless',      'Initial Jobless Claims', 'K'),
    'jolts':        ('macro_jolts',        'JOLTS Job Openings',     'K'),
    'reverse_repo': ('macro_reverse_repo', 'Reverse Repo',           'B'),
    'tga':          ('macro_tga',          'Treasury General Acct',  'B'),
    'yield_curve':  ('macro_yield_curve',  '10Y-2Y Spread',          '%'),
    'consumer_sent':('macro_consumer_sent','Consumer Sentiment',     'idx'),
}


@app.route('/api/history')
def history_list():
    """List chartable series with how many points each has stored."""
    out = []
    for key, (sname, label, unit) in CHARTABLE.items():
        n = len(store._series(sname)) if store else 0
        out.append({'key': key, 'label': label, 'unit': unit, 'count': n})
    return ok({'series': out})


@app.route('/api/history/<key>')
def history_series(key):
    if not store:
        return ok({'error': 'store not loaded'})
    meta = CHARTABLE.get(key)
    if not meta:
        return ok({'error': 'unknown series', 'available': list(CHARTABLE.keys())})
    sname, label, unit = meta
    days = request.args.get('days', type=int)  # omit = full history
    pts  = store.get_series(sname, window_days=days, max_points=500)
    series = [{'ts': ts, 'v': round(v, 3)} for ts, v in pts]
    latest = series[-1]['v'] if series else None
    pctl = store.percentile_rank(sname, latest) if latest is not None else None
    return ok({'key': key, 'label': label, 'unit': unit, 'points': series,
               'count': len(series), 'latest': latest, 'percentile': pctl})


@app.route('/api/debug/inflation')
def debug_inflation():
    """Show exactly what each inflation YoY is computed from: the series ID actually in use
    (pulled from US_INDICATORS, so it reflects what's deployed), the latest and 12-months-prior
    index points (date + value), and the resulting YoY — to settle SA/NSA and data-vintage questions."""
    out = []
    infl = [(k, lbl, series) for (k, lbl, cat, series, *_rest) in US_INDICATORS if cat == 'Inflation']
    for key, label, series in infl:
        row = {'key': key, 'label': label, 'series_in_use': series}
        try:
            pts = get_fred_series(series, years=3)
            n = len(pts) if pts else 0
            row['n_points'] = n
            if n >= 14:
                curr, yr_ago = pts[-1], pts[-13]
                row['latest']        = {'date': curr['date'], 'value': curr['value']}
                row['twelve_mo_ago'] = {'date': yr_ago['date'], 'value': yr_ago['value']}
                row['computed_yoy']  = round((curr['value'] - yr_ago['value']) / yr_ago['value'] * 100, 2) if yr_ago['value'] else None
            else:
                row['error'] = f'need >=14 monthly points, got {n}'
        except Exception as e:
            row['error'] = f'{type(e).__name__}: {e}'
        out.append(row)
    return ok({'inflation': out,
               'note': 'series_in_use is what get_us_heatmap divides; YoY=(latest-twelve_mo_ago)/twelve_mo_ago*100'})


@app.route('/api/debug/bias')
def debug_bias():
    """Line-by-line breakdown of the overall USD/Stocks bias score — every indicator's
    category, weight, impact and contribution, then the weighted totals. Reconciles exactly
    to the heatmap's usd_pct/stocks_pct, and makes the inflation-dedup weighting visible."""
    cached = cache.get('heatmap:us')
    if not cached:
        try:
            get_us_heatmap()
        except Exception:
            pass
        cached = cache.get('heatmap:us')
    rows = (cached or {}).get('rows', [])

    lines = []
    ub = ued = sb = sed = 0.0
    for r in rows:
        cat = r.get('category')
        w   = _indicator_weight(cat)
        ui, si = r.get('usd_impact'), r.get('stocks_impact')
        if ui == 'Bullish': ub += w
        elif ui == 'Bearish': ued += w
        if si == 'Bullish': sb += w
        elif si == 'Bearish': sed += w
        lines.append({
            'indicator':           r.get('label'),
            'category':            cat,
            'weight':              round(w, 4),
            'actual':              r.get('actual_fmt'),
            'forecast':            r.get('forecast_fmt'),
            'surprise':            r.get('surprise'),
            'usd_impact':          ui,
            'usd_contribution':    round(w if ui == 'Bullish' else -w if ui == 'Bearish' else 0, 4),
            'stocks_impact':       si,
            'stocks_contribution': round(w if si == 'Bullish' else -w if si == 'Bearish' else 0, 4),
        })

    ut, st = ub + ued, sb + sed
    return ok({
        'indicators':       lines,
        'category_weights': HEATMAP_CATEGORY_WEIGHTS,
        'totals': {
            'usd_bull_weight':    round(ub, 4),  'usd_bear_weight':    round(ued, 4),
            'usd_score':          round(ub / ut * 100) if ut else 50,
            'stocks_bull_weight': round(sb, 4),  'stocks_bear_weight': round(sed, 4),
            'stocks_score':       round(sb / st * 100) if st else 50,
        },
        'note': 'weight = category weight ÷ members. The 4 inflation rows share Inflation (0.25), '
                'so each ≈ 0.0625 — no longer 4 full votes. Totals reconcile to the heatmap score.',
    })


@app.route('/api/debug/internals')
def debug_internals():
    """Show the raw ETF data feeding each internals sub-signal — the definitive test of
    whether a 0 is a real floor reading or missing data."""
    tickers = ['spy', 'qqq', 'iwm', 'rsp', 'xly', 'xlp', 'sphb', 'splv', 'smh']
    prices = {}
    for t in tickers:
        try:
            p = get_live_price(t.upper())
            prices[t] = {
                'present': bool(p),
                'price': p.get('price') if p else None,
                'changePct': p.get('changePct') if p else None,
            }
        except Exception as e:
            prices[t] = {'present': False, 'error': str(e)}

    def chg(t):
        return prices.get(t, {}).get('changePct') or 0

    def normalise(x, hi, lo):
        if hi == lo:
            return 50
        return max(0, min(100, round((x - lo) / (hi - lo) * 100)))

    signals = []

    # Small vs Large
    if prices.get('spy', {}).get('present') and prices.get('iwm', {}).get('present'):
        diff = chg('iwm') - chg('spy')
        score = normalise(diff, 0.5, -0.5)
        signals.append({'name': 'Small vs Large', 'lead': 'IWM', 'lag': 'SPY',
                        'lead_chg': chg('iwm'), 'lag_chg': chg('spy'), 'diff': round(diff, 3),
                        'bounds': '[-0.5, +0.5]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Small vs Large', 'status': 'MISSING', 'reason': 'IWM or SPY absent'})

    # Breadth
    if prices.get('spy', {}).get('present') and prices.get('rsp', {}).get('present'):
        diff = chg('rsp') - chg('spy')
        score = normalise(diff, 0.3, -0.3)
        signals.append({'name': 'Breadth · RSP', 'lead': 'RSP', 'lag': 'SPY',
                        'lead_chg': chg('rsp'), 'lag_chg': chg('spy'), 'diff': round(diff, 3),
                        'bounds': '[-0.3, +0.3]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Breadth · RSP', 'status': 'MISSING'})

    # Offense / Defense
    if prices.get('xly', {}).get('present') and prices.get('xlp', {}).get('present'):
        diff = chg('xly') - chg('xlp')
        score = normalise(diff, 0.3, -0.3)
        signals.append({'name': 'Offense / Defense', 'lead': 'XLY', 'lag': 'XLP',
                        'lead_chg': chg('xly'), 'lag_chg': chg('xlp'), 'diff': round(diff, 3),
                        'bounds': '[-0.3, +0.3]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Offense / Defense', 'status': 'MISSING'})

    # Risk Appetite
    if prices.get('sphb', {}).get('present') and prices.get('splv', {}).get('present'):
        diff = chg('sphb') - chg('splv')
        score = normalise(diff, 0.4, -0.4)
        signals.append({'name': 'Risk Appetite', 'lead': 'SPHB', 'lag': 'SPLV',
                        'lead_chg': chg('sphb'), 'lag_chg': chg('splv'), 'diff': round(diff, 3),
                        'bounds': '[-0.4, +0.4]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Risk Appetite', 'status': 'MISSING'})

    # Semis Leadership
    if prices.get('smh', {}).get('present') and prices.get('spy', {}).get('present'):
        diff = chg('smh') - chg('spy')
        score = normalise(diff, 0.5, -0.5)
        signals.append({'name': 'Semis Leadership', 'lead': 'SMH', 'lag': 'SPY',
                        'lead_chg': chg('smh'), 'lag_chg': chg('spy'), 'diff': round(diff, 3),
                        'bounds': '[-0.5, +0.5]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Semis Leadership', 'status': 'MISSING'})

    # Tech Leadership
    if prices.get('spy', {}).get('present') and prices.get('qqq', {}).get('present'):
        diff = chg('qqq') - chg('spy')
        score = normalise(diff, 0.5, -0.8)
        signals.append({'name': 'Tech Leadership', 'lead': 'QQQ', 'lag': 'SPY',
                        'lead_chg': chg('qqq'), 'lag_chg': chg('spy'), 'diff': round(diff, 3),
                        'bounds': '[-0.8, +0.5]', 'score': score, 'status': 'LIVE'})
    else:
        signals.append({'name': 'Tech Leadership', 'status': 'MISSING'})

    return ok({
        'etf_prices': prices,
        'signals': signals,
        'note': 'If status=LIVE and score=0, the ETF data is present and the rotation genuinely '
                'floors at 0 (the lead underperforms the lag beyond the normalise threshold). '
                'If status=MISSING, the ETF failed to fetch — that would be a real data gap.',
    })


@app.route('/api/debug/pctl')
def debug_pctl():
    if not store:
        return ok({'error': 'store not loaded'})
    macro = get_scorecard_macro()
    out = {'_pctl': macro.get('_pctl'), 'series': {}}
    for fac, sname in [('cpi', 'macro_cpi'), ('core_cpi', 'macro_core_cpi'),
                       ('ppi', 'macro_ppi'), ('real_yield', 'macro_real_yield')]:
        d = macro.get(fac) or {}
        cur = d.get('current')
        try:    samples = len(store._series(sname))
        except: samples = 'err'
        out['series'][fac] = {
            'store_name': sname,
            'current_value': cur,
            'present_in_macro': cur is not None,
            'sample_count': samples,
            'percentile': (store.percentile_rank(sname, cur) if cur is not None else None),
        }
    return ok(out)


@app.route('/api/store/backfill')
def store_backfill():
    """Seed indicator history from FRED (raw-level series). years= query param (default 20)."""
    if not store:
        return service_error('store module not loaded')
    import datetime
    years = min(int(request.args.get('years', 20)), 40)
    results = {}
    for name, series in BACKFILL_SERIES.items():
        pts = get_fred_series(series, years=years)
        if not pts:
            results[name] = 0
            continue
        rows = []
        for o in pts:
            try:
                ts = int(datetime.datetime.strptime(o['date'], '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp())
                rows.append((ts, o['value']))
            except Exception:
                continue
        results[name] = store.record_indicators_bulk(name, rows)

    # ── Scoring-factor series: stored as TRANSFORMS (YoY %) so percentiles are meaningful ──
    # (raw CPI/PPI indices only ever rise, so percentile-of-index is useless; we store YoY)
    yoy_series = {'macro_cpi': 'CPIAUCNS', 'macro_core_cpi': 'CPILFENS', 'macro_ppi': 'PPIFID'}
    for name, series in yoy_series.items():
        pts = get_fred_series(series, years=years + 1)  # +1y so the earliest YoY has a base
        if not pts or len(pts) < 13:
            results[name] = 0
            continue
        rows = []
        for i in range(12, len(pts)):
            try:
                base = pts[i - 12]['value']
                if not base:
                    continue
                yoy = (pts[i]['value'] / base - 1) * 100
                ts = int(datetime.datetime.strptime(pts[i]['date'], '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp())
                rows.append((ts, round(yoy, 3)))
            except Exception:
                continue
        results[name] = store.record_indicators_bulk(name, rows)

    return ok({'backfilled': results, 'total_points': sum(results.values())})

@app.route('/api/crypto')
def get_crypto():
    """
    Crypto Layer — regime, asset scorecards, DCA zones, warnings.
    Cached 10 min. Fetches BTC/ETH/SOL/XRP + ETH-BTC from Yahoo.
    """
    if not CRYPTO_AVAILABLE:
        return ok({'error': 'crypto module unavailable'})

    cached = cache.get('crypto:snapshot')
    if cached:
        return ok(cached, cached=True)

    try:
        # 1. Fetch crypto price history in parallel
        CRYPTO_TICKERS = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD', 'ETH-BTC']
        crypto_prices = {}
        import concurrent.futures

        def _fetch_crypto(t):
            try:
                closes = get_price_closes(t, '1y')
                dated  = get_price_closes_with_dates(t, '1y')
                live   = get_live_price(t) or {}
                return t, {
                    'closes':    closes or [],
                    'price':     live.get('price', closes[-1] if closes else 0),
                    'changePct': live.get('changePct', 0),
                    'rangePos':  live.get('rangePos', 50),
                    'week52High': live.get('week52High'),
                    'week52Low':  live.get('week52Low'),
                }
            except Exception as e:
                print(f'[CRYPTO] {t} error: {e}')
                return t, {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futs = {pool.submit(_fetch_crypto, t): t for t in CRYPTO_TICKERS}
            for f in concurrent.futures.as_completed(futs, timeout=30):
                try:
                    t, data = f.result()
                    if data: crypto_prices[t] = data
                except Exception:
                    pass

        # 2. Get macro context from existing RIE
        macro_snap = cache.get('rie:snapshot') or {}

        # Expose raw readings for crypto module
        # Derive from pillar scores if not directly available
        ps = macro_snap.get('pillar_scores', {})
        if 'raw_readings' not in macro_snap:
            macro_snap['raw_readings'] = {
                'ry':  macro_snap.get('real_yield_raw', 91),   # from scorecard
                'usd': macro_snap.get('usd_raw', 57),
            }

        # 3. Compute crypto regime
        regime = crypto_engine.compute_crypto_regime(macro_snap, crypto_prices)

        # 4. Score each asset
        asset_scores = []
        for ticker in ['BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD']:
            closes = crypto_prices.get(ticker, {}).get('closes', [])
            score = crypto_engine.score_crypto_asset(ticker, closes, regime['score'], macro_snap)
            if score:
                # Add live price data
                live = crypto_prices.get(ticker, {})
                score['price']     = live.get('price', score.get('price'))
                score['changePct'] = live.get('changePct', 0)
                asset_scores.append(score)

        # 5. DCA zones
        dca_zones = {}
        for ticker in ['BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD']:
            closes = crypto_prices.get(ticker, {}).get('closes', [])
            zones = crypto_engine.compute_dca_zones(ticker, closes, regime['score'])
            if zones:
                dca_zones[ticker] = zones

        # 6. Risk warnings
        warnings = crypto_engine.get_crypto_warnings(asset_scores, regime, macro_snap)

        # 7. ETH/BTC trend
        eth_btc = crypto_prices.get('ETH-BTC', {})
        eth_btc_closes = eth_btc.get('closes', [])
        eth_btc_trend = None
        if eth_btc_closes and len(eth_btc_closes) >= 30:
            cur  = eth_btc_closes[-1]
            prev = eth_btc_closes[-30]
            eth_btc_trend = {
                'current':   round(cur, 5),
                'change_30d': round((cur - prev) / prev * 100, 2) if prev else 0,
                'direction': 'Rising' if cur > prev else 'Falling',
                'implication': ('Altcoin season expanding — ETH and alts outperforming BTC'
                                if cur > prev else
                                'BTC dominance rising — capital rotating to BTC'),
            }

        result = {
            'regime':       regime,
            'assets':       sorted(asset_scores, key=lambda x: x['composite'], reverse=True),
            'dca_zones':    dca_zones,
            'warnings':     warnings,
            'eth_btc':      eth_btc_trend,
            'generated':    int(time.time()),
            'note': ('Crypto scores are based on macro regime alignment, price trend, '
                     'momentum, and volatility. Not financial advice.'),
        }

        cache.set('crypto:snapshot', result, 600)  # 10 min cache
        return ok(result)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f'[CRYPTO] error: {e}\n{tb}')
        return ok({'error': f'{type(e).__name__}: {e}', 'traceback': tb})


@app.route('/api/crypto/refresh')
def refresh_crypto():
    cache.delete('crypto:snapshot')
    return ok({'cleared': True})


# ══════════════════════════════════════════════════════════════════
# ◈ TRADE LOG — persistent trade journal backed by Postgres
# ══════════════════════════════════════════════════════════════════

def _trade_db():
    """Get a Postgres connection for trade log ops. Returns None if unavailable."""
    try:
        import psycopg2, os
        url = os.environ.get('DATABASE_URL')
        if not url: return None
        conn = psycopg2.connect(url, sslmode='require')
        return conn
    except Exception as e:
        print(f'[TRADE_LOG] db connect error: {e}')
        return None


def _ensure_trade_table():
    """Create trade_log table if it doesn't exist."""
    conn = _trade_db()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    id          SERIAL PRIMARY KEY,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    ticker      VARCHAR(20)  NOT NULL,
                    direction   VARCHAR(5)   NOT NULL CHECK (direction IN ('LONG','SHORT')),
                    entry_price FLOAT        NOT NULL,
                    stop_price  FLOAT,
                    target_price FLOAT,
                    size        VARCHAR(50),
                    status      VARCHAR(10)  DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','CANCELLED')),
                    exit_price  FLOAT,
                    pnl_pct     FLOAT,
                    regime_score INT,
                    regime_label VARCHAR(50),
                    notes       TEXT,
                    closed_at   TIMESTAMPTZ
                )
            """)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[TRADE_LOG] table create error: {e}')
        try: conn.close()
        except: pass
        return False


@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Get all trades, newest first."""
    conn = _trade_db()
    if not conn:
        return ok({'trades': [], 'error': 'database unavailable'})
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, created_at, ticker, direction, entry_price, stop_price,
                       target_price, size, status, exit_price, pnl_pct,
                       regime_score, regime_label, notes, closed_at
                FROM trade_log ORDER BY created_at DESC LIMIT 100
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            # Serialise datetimes
            for r in rows:
                for k in ('created_at', 'closed_at'):
                    if r.get(k): r[k] = r[k].isoformat()
        conn.close()
        return ok({'trades': rows, 'count': len(rows)})
    except Exception as e:
        try: conn.close()
        except: pass
        return ok({'trades': [], 'error': str(e)})


@app.route('/api/trades', methods=['POST'])
def add_trade():
    """Add a new trade."""
    _ensure_trade_table()
    data = request.get_json() or {}
    ticker    = (data.get('ticker') or '').upper().strip()
    direction = (data.get('direction') or '').upper().strip()
    entry     = data.get('entry_price')
    if not ticker or direction not in ('LONG','SHORT') or not entry:
        return ok({'error': 'ticker, direction (LONG/SHORT), and entry_price are required'}), 400

    # Attach current regime context automatically
    regime_score = None
    regime_label = None
    try:
        snap = cache.get('rie:snapshot') or {}
        regime_score = snap.get('regime_score')
        regime_label = snap.get('regime_label')
    except: pass

    conn = _trade_db()
    if not conn: return ok({'error': 'database unavailable'})
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_log
                  (ticker, direction, entry_price, stop_price, target_price,
                   size, notes, regime_score, regime_label)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, created_at
            """, (
                ticker, direction, float(entry),
                float(data['stop_price'])   if data.get('stop_price')   else None,
                float(data['target_price']) if data.get('target_price') else None,
                data.get('size'),
                data.get('notes'),
                regime_score, regime_label,
            ))
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return ok({'id': row[0], 'created_at': row[1].isoformat(), 'message': 'Trade logged'})
    except Exception as e:
        try: conn.close()
        except: pass
        return ok({'error': str(e)})


@app.route('/api/trades/<int:trade_id>', methods=['PATCH'])
def update_trade(trade_id):
    """Close a trade or update notes."""
    data = request.get_json() or {}
    conn = _trade_db()
    if not conn: return ok({'error': 'database unavailable'})
    try:
        updates = []
        params  = []
        if data.get('status'):
            updates.append('status=%s'); params.append(data['status'].upper())
        if data.get('exit_price') is not None:
            updates.append('exit_price=%s'); params.append(float(data['exit_price']))
            updates.append('closed_at=NOW()')
        if data.get('pnl_pct') is not None:
            updates.append('pnl_pct=%s'); params.append(float(data['pnl_pct']))
        if data.get('notes') is not None:
            updates.append('notes=%s'); params.append(data['notes'])
        if not updates:
            conn.close()
            return ok({'error': 'nothing to update'})
        params.append(trade_id)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE trade_log SET {', '.join(updates)} WHERE id=%s", params)
        conn.commit()
        conn.close()
        return ok({'updated': True, 'id': trade_id})
    except Exception as e:
        try: conn.close()
        except: pass
        return ok({'error': str(e)})


@app.route('/api/trades/<int:trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    """Delete a trade."""
    conn = _trade_db()
    if not conn: return ok({'error': 'database unavailable'})
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trade_log WHERE id=%s", (trade_id,))
        conn.commit()
        conn.close()
        return ok({'deleted': True, 'id': trade_id})
    except Exception as e:
        try: conn.close()
        except: pass
        return ok({'error': str(e)})


if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    print(f"\n◈ STOCKSENSE on port {port}\n")
    app.run(host='0.0.0.0',port=port,debug=False)
