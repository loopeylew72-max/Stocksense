"""
◈ REGIME INTELLIGENCE ENGINE (RIE)
The central intelligence layer powering all of StockSense.

Architecture:
  5 Pillars → Weighted Composite → Regime Score → Asset Scores → Confidence
  
Weights:
  Economic:  20%
  Liquidity: 25%
  Internals: 20%
  Price:     25%
  Sentiment: 10%
"""
import time, math
from cache import cache

# ── Regime thresholds ─────────────────────────────────────────────
REGIME_LABELS = [
    (82, 'Strong Bullish',     '#22c55e', '#0a2010'),
    (68, 'Bullish',            '#48d597', '#0d2820'),
    (57, 'Cautiously Bullish', '#7ed4a4', '#0d2018'),
    (44, 'Neutral',            '#f6c90e', '#1a1500'),
    (33, 'Cautious',           '#f6a05a', '#2a1500'),
    (20, 'Bearish',            '#f56565', '#2a0a0a'),
    ( 0, 'Strong Bearish',     '#c53030', '#3a0000'),
]

def regime_label(score):
    for threshold, label, col, bg in REGIME_LABELS:
        if score >= threshold:
            return label, col, bg
    return 'Strong Bearish', '#c53030', '#3a0000'


# ── Normaliser: raw signal → 0-100 ───────────────────────────────
def normalise(value, bull_threshold, bear_threshold, invert=False):
    """Convert a raw value to a 0-100 score."""
    if value is None:
        return 50  # neutral when no data
    if invert:
        value = -value
    if value >= bull_threshold:
        # Scale from 60-100 based on how far above threshold
        excess = min(value - bull_threshold, bull_threshold)
        return round(60 + (excess / bull_threshold) * 40)
    if value <= bear_threshold:
        deficit = min(bear_threshold - value, abs(bear_threshold))
        return round(40 - (deficit / max(abs(bear_threshold), 0.01)) * 40)
    # Between thresholds → neutral zone 40-60
    rng = bull_threshold - bear_threshold
    pos = (value - bear_threshold) / rng if rng else 0.5
    return round(40 + pos * 20)


# ══════════════════════════════════════════════════════════════════
# PILLAR 1: ECONOMIC DATA (20%)
# Backbone: US Economic Heatmap
# ══════════════════════════════════════════════════════════════════
def score_economic(fred_data):
    """
    Score economic pillar from FRED data.
    Returns: {score, sub_scores, bull_factors, bear_factors}
    """
    subs  = {}
    bulls = []
    bears = []

    # ── Growth sub-score ────────────────────────────────────────
    growth_scores = []
    gdp = fred_data.get('gdp', {})
    if gdp.get('actual') is not None:
        v = normalise(gdp['actual'], 2.5, 0.5)
        growth_scores.append(v)
        if v >= 60: bulls.append({'factor': 'GDP Growth', 'detail': f"{gdp['actual']:.1f}% QoQ", 'pillar': 'Economic'})
        elif v <= 40: bears.append({'factor': 'GDP Growth', 'detail': f"{gdp['actual']:.1f}% — slowing", 'pillar': 'Economic'})

    retail = fred_data.get('retail', {})
    if retail.get('change') is not None:
        v = normalise(retail['change'], 0.5, -0.3)
        growth_scores.append(v)
        if v >= 60: bulls.append({'factor': 'Retail Sales', 'detail': f"{retail['change']:+.1f}% MoM beat", 'pillar': 'Economic'})
        elif v <= 40: bears.append({'factor': 'Retail Sales', 'detail': f"{retail['change']:+.1f}% MoM miss", 'pillar': 'Economic'})

    growth_score = sum(growth_scores) / len(growth_scores) if growth_scores else 50

    # ── Inflation sub-score ─────────────────────────────────────
    infl_scores = []
    cpi = fred_data.get('cpi', {})
    if cpi.get('actual') is not None:
        # For equities: falling CPI is bullish (Fed can cut)
        v = normalise(cpi['actual'], 2.5, 4.5, invert=True)
        infl_scores.append(v)
        if v >= 60: bulls.append({'factor': 'CPI YoY', 'detail': f"{cpi['actual']:.1f}% — contained", 'pillar': 'Economic'})
        elif v <= 40: bears.append({'factor': 'CPI YoY', 'detail': f"{cpi['actual']:.1f}% — elevated inflation", 'pillar': 'Economic'})

    ppi = fred_data.get('ppi', {})
    if ppi.get('change') is not None:
        v = normalise(ppi['change'], -0.2, 0.5, invert=True)
        infl_scores.append(v)
        if v <= 40: bears.append({'factor': 'PPI', 'detail': f"PPI rising {ppi['change']:+.2f} — pipeline pressure", 'pillar': 'Economic'})

    infl_score = sum(infl_scores) / len(infl_scores) if infl_scores else 50

    # ── Employment sub-score ────────────────────────────────────
    emp_scores = []
    nfp = fred_data.get('nfp', {})
    if nfp.get('actual') is not None:
        v = normalise(nfp['actual'], 150, 50)  # 150K+ = bull, <50K = bear
        emp_scores.append(v)
        if v >= 60: bulls.append({'factor': 'Non-Farm Payrolls', 'detail': f"{nfp['actual']:.0f}K jobs added", 'pillar': 'Economic'})
        elif v <= 40: bears.append({'factor': 'Non-Farm Payrolls', 'detail': f"Only {nfp['actual']:.0f}K jobs — labour softening", 'pillar': 'Economic'})

    unemp = fred_data.get('unemp', {})
    if unemp.get('actual') is not None:
        v = normalise(unemp['actual'], 3.5, 4.5, invert=True)
        emp_scores.append(v)
        if v <= 40: bears.append({'factor': 'Unemployment', 'detail': f"{unemp['actual']:.1f}% — rising", 'pillar': 'Economic'})

    emp_score = sum(emp_scores) / len(emp_scores) if emp_scores else 50

    # Composite: Growth 40%, Inflation 35%, Employment 25%
    eco_score = round(growth_score * 0.40 + infl_score * 0.35 + emp_score * 0.25)

    subs['growth']     = round(growth_score)
    subs['inflation']  = round(infl_score)
    subs['employment'] = round(emp_score)

    return {
        'score':       eco_score,
        'sub_scores':  subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(growth_scores) + len(infl_scores) + len(emp_scores),
    }


# ══════════════════════════════════════════════════════════════════
# PILLAR 2: LIQUIDITY (25%)
# Net liquidity (Fed BS − RRP − TGA), real yields, M2, credit, curve.
# Liquidity leads markets by 3-6 months — the highest-weighted pillar.
# ══════════════════════════════════════════════════════════════════
def score_liquidity(fred_data, price_data):
    """
    Score liquidity conditions across six sub-signals:
      net_liquidity · real_yields · m2 · credit · yield_curve · bonds
    Each sub-score is 0-100; the pillar is a weighted blend.
    """
    subs  = {}
    bulls = []
    bears = []
    weighted = []   # (sub_score, weight)

    def add(key, score, weight):
        subs[key] = round(score)
        weighted.append((score, weight))

    # ── NET LIQUIDITY (the prime driver) ────────────────────────
    # Net Liq ≈ Fed Balance Sheet − Reverse Repo − Treasury Gen. Account.
    # Scored directionally so unit/frequency mismatches can't corrupt it:
    #   Fed BS expanding   → +   (QE / balance-sheet growth)
    #   RRP draining       → +   (cash leaving the Fed, into markets)
    #   TGA draining       → +   (Treasury spending money into the system)
    nl_parts = []
    fed_bs = fred_data.get('fed_balance', {})
    if fed_bs.get('change') is not None:
        # fed_balance change is % MoM — expansion (>0) is bullish
        nl_parts.append(normalise(fed_bs['change'], 0.2, -0.2))
    rrp = fred_data.get('reverse_repo', {})
    if rrp.get('change') is not None:
        # RRP change is absolute $B — falling (negative) drains the facility = bullish
        nl_parts.append(normalise(rrp['change'], 20, -20, invert=True))
    tga = fred_data.get('tga', {})
    if tga.get('change') is not None:
        # TGA change is absolute $B — falling (Treasury spending) = liquidity in = bullish
        nl_parts.append(normalise(tga['change'], 20, -20, invert=True))
    if nl_parts:
        nl = sum(nl_parts) / len(nl_parts)
        add('net_liquidity', nl, 0.30)
        if nl >= 60:
            bulls.append({'factor': 'Net Liquidity', 'detail': 'Fed balance sheet / RRP / TGA flows adding liquidity', 'pillar': 'Liquidity'})
        elif nl <= 40:
            bears.append({'factor': 'Net Liquidity', 'detail': 'Net liquidity draining — Fed/RRP/TGA pulling cash out', 'pillar': 'Liquidity'})

    # ── Real yields (DFII10 — 10Y TIPS) ─────────────────────────
    real_yield = fred_data.get('real_yield', {})
    if real_yield.get('actual') is not None:
        ry = real_yield['actual']
        v = normalise(ry, 0.5, 2.0, invert=True)  # low/neg real yields = loose = bullish
        add('real_yields', v, 0.22)
        if v >= 60: bulls.append({'factor': 'Real Yields', 'detail': f"Real yield {ry:.2f}% — supportive for equities", 'pillar': 'Liquidity'})
        elif v <= 40: bears.append({'factor': 'Real Yields', 'detail': f"Real yield {ry:.2f}% — restrictive financial conditions", 'pillar': 'Liquidity'})

    # ── Yield curve (T10Y2Y spread) ─────────────────────────────
    yc = fred_data.get('yield_curve', {})
    if yc.get('actual') is not None:
        spread = yc['actual']
        # Positive/steepening curve = healthy; inversion = late-cycle warning
        v = normalise(spread, 0.5, -0.5)
        add('yield_curve', v, 0.16)
        if spread < 0:
            bears.append({'factor': 'Yield Curve', 'detail': f"2s10s inverted ({spread:+.2f}%) — recession signal active", 'pillar': 'Liquidity'})
        elif v >= 60:
            bulls.append({'factor': 'Yield Curve', 'detail': f"2s10s positive ({spread:+.2f}%) — curve normalising", 'pillar': 'Liquidity'})

    # ── M2 Money Supply trend ───────────────────────────────────
    m2 = fred_data.get('m2', {})
    if m2.get('change') is not None:
        v = normalise(m2['change'], 0.3, -0.3)
        add('m2', v, 0.12)
        if v >= 60: bulls.append({'factor': 'M2 Money Supply', 'detail': 'Money supply expanding — liquidity supportive', 'pillar': 'Liquidity'})
        elif v <= 40: bears.append({'factor': 'M2 Money Supply', 'detail': 'M2 contracting — liquidity tightening', 'pillar': 'Liquidity'})

    # ── Credit conditions proxy (HYG) ───────────────────────────
    hyg = price_data.get('hyg', {})
    if hyg:
        hyg_chg = hyg.get('changePct', 0)
        v = normalise(hyg_chg, 0.3, -0.3)
        add('credit', v, 0.12)
        if v >= 60: bulls.append({'factor': 'Credit Spreads', 'detail': 'HYG rising — spreads tightening, credit healthy', 'pillar': 'Liquidity'})
        elif v <= 40: bears.append({'factor': 'Credit Spreads', 'detail': 'HYG falling — spreads widening, credit stress', 'pillar': 'Liquidity'})

    # ── Bond market direction (TLT) ─────────────────────────────
    tlt = price_data.get('tlt', {})
    if tlt:
        v = normalise(tlt.get('changePct', 0), 0.3, -0.3)
        add('bonds', v, 0.08)

    # Weighted blend (renormalised over whatever data is present)
    if weighted:
        tw = sum(w for _, w in weighted)
        liq_score = round(sum(s * w for s, w in weighted) / tw) if tw else 50
    else:
        liq_score = 50

    return {
        'score':        liq_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(weighted),
    }


# ══════════════════════════════════════════════════════════════════
# PILLAR 3: MARKET INTERNALS (20%)
# Breadth + rotation = is the move broad-based and risk-on, or narrow?
# All signals use liquid ETF proxies (no paid breadth feed required).
# ══════════════════════════════════════════════════════════════════
def score_internals(price_data):
    """
    Score market internals across breadth and rotation:
      small_large · breadth · trend_health · offense_defense ·
      risk_appetite · semis_leadership · tech_leadership
    """
    subs  = {}
    bulls = []
    bears = []
    scores = []

    def chg(key):
        p = price_data.get(key) or {}
        return p.get('changePct', 0)

    spy_chg = chg('spy')
    qqq_chg = chg('qqq')
    iwm_chg = chg('iwm')
    rsp_chg = chg('rsp')

    def rotation(leader_key, laggard_key, bull_thr, bear_thr):
        """Relative strength of leader vs laggard (both must have data)."""
        lead = price_data.get(leader_key)
        lag  = price_data.get(laggard_key)
        if not lead or not lag:
            return None, 0
        diff = lead.get('changePct', 0) - lag.get('changePct', 0)
        return normalise(diff, bull_thr, bear_thr), diff

    # ── Small vs Large cap (IWM vs SPY) ─────────────────────────
    if price_data.get('spy') and price_data.get('iwm'):
        small_large = iwm_chg - spy_chg
        v = normalise(small_large, 0.5, -0.5)
        scores.append(v); subs['small_large'] = round(v)
        if v >= 60: bulls.append({'factor': 'Small Cap Leadership', 'detail': f"IWM beating SPY by {small_large:+.1f}% — broad participation", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Large Cap Concentration', 'detail': 'Small caps lagging — narrow leadership', 'pillar': 'Internals'})

    # ── Equal weight vs cap weight (RSP vs SPY) ─────────────────
    if price_data.get('spy') and price_data.get('rsp'):
        breadth = rsp_chg - spy_chg
        v = normalise(breadth, 0.3, -0.3)
        scores.append(v); subs['breadth'] = round(v)
        if v >= 60: bulls.append({'factor': 'Market Breadth', 'detail': 'Equal-weight outperforming — breadth healthy', 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Market Breadth', 'detail': 'Cap-weight dominating — few names driving gains', 'pillar': 'Internals'})

    # ── 52-week range position (trend health) ───────────────────
    spy = price_data.get('spy') or {}
    spy_hi, spy_lo, spy_px = spy.get('week52High', 0), spy.get('week52Low', 0), spy.get('price', 0)
    if spy_hi > spy_lo > 0:
        rng_pos = (spy_px - spy_lo) / (spy_hi - spy_lo) * 100
        v = normalise(rng_pos, 70, 30)
        scores.append(v); subs['trend_health'] = round(v)
        if v >= 60: bulls.append({'factor': 'SPY Trend', 'detail': f"SPY at {rng_pos:.0f}% of 52w range — uptrend intact", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'SPY Trend', 'detail': f"SPY at {rng_pos:.0f}% of 52w range — downtrend", 'pillar': 'Internals'})

    # ── Offense vs Defense (XLY discretionary vs XLP staples) ───
    v, diff = rotation('xly', 'xlp', 0.3, -0.3)
    if v is not None:
        scores.append(v); subs['offense_defense'] = round(v)
        if v >= 60: bulls.append({'factor': 'Risk-On Rotation', 'detail': f"Discretionary leading staples ({diff:+.1f}%) — offense bid", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Defensive Rotation', 'detail': f"Staples leading discretionary ({diff:+.1f}%) — defensive posture", 'pillar': 'Internals'})

    # ── Risk appetite (SPHB high-beta vs SPLV low-vol) ──────────
    v, diff = rotation('sphb', 'splv', 0.4, -0.4)
    if v is not None:
        scores.append(v); subs['risk_appetite'] = round(v)
        if v >= 60: bulls.append({'factor': 'High-Beta Bid', 'detail': f"High-beta beating low-vol ({diff:+.1f}%) — risk appetite strong", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Defensive Bid', 'detail': f"Low-vol beating high-beta ({diff:+.1f}%) — risk-off undertone", 'pillar': 'Internals'})

    # ── Semiconductor leadership (SMH vs SPY) ───────────────────
    v, diff = rotation('smh', 'spy', 0.5, -0.5)
    if v is not None:
        scores.append(v); subs['semis_leadership'] = round(v)
        if v >= 60: bulls.append({'factor': 'Semis Leadership', 'detail': f"Semis leading market ({diff:+.1f}%) — cyclical/tech strength", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Semis Weakness', 'detail': f"Semis lagging ({diff:+.1f}%) — leadership group faltering", 'pillar': 'Internals'})

    # ── Tech leadership (QQQ vs SPY) — context sub-score ────────
    if price_data.get('spy') and price_data.get('qqq'):
        v = normalise(qqq_chg - spy_chg, 0.5, -0.8)
        scores.append(v); subs['tech_leadership'] = round(v)

    int_score = round(sum(scores) / len(scores)) if scores else 50

    return {
        'score':        int_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(scores),
    }


# ══════════════════════════════════════════════════════════════════
# PILLAR 4: PRICE ACTION (25%)
# The market's own vote — the most honest signal
# ══════════════════════════════════════════════════════════════════
def score_price_action(price_data):
    """
    Score price action across global indices.
    Trend + momentum + relative strength.
    """
    subs  = {}
    bulls = []
    bears = []
    scores = []

    assets = {
        'spy':  ('SPY (S&P 500)', 1.2),
        'qqq':  ('QQQ (Nasdaq)',  1.0),
        'iwm':  ('IWM (Russell)', 0.8),
        'dia':  ('DIA (Dow)',     0.7),
    }

    for key, (label, weight) in assets.items():
        p = price_data.get(key, {})
        if not p: continue
        chg = p.get('changePct', 0)
        hi  = p.get('week52High', 0)
        lo  = p.get('week52Low', 0)
        px  = p.get('price', 0)

        # Momentum score
        mom_v = normalise(chg, 0.8, -0.8)

        # Range position score
        rng_v = 50
        if hi > lo > 0:
            rng_pos = (px - lo) / (hi - lo) * 100
            rng_v = normalise(rng_pos, 65, 35)

        combined = round(mom_v * 0.5 + rng_v * 0.5)
        scores.append(combined * weight)
        subs[key] = combined

        if combined >= 65:
            bulls.append({'factor': f'{label} Momentum', 'detail': f"{chg:+.2f}% — bullish price action", 'pillar': 'Price Action'})
        elif combined <= 35:
            bears.append({'factor': f'{label} Momentum', 'detail': f"{chg:+.2f}% — bearish price action", 'pillar': 'Price Action'})

    # USD direction (inverse for risk assets)
    uup = price_data.get('uup', {})
    uup_chg = uup.get('changePct', 0) if uup else 0
    usd_v = normalise(uup_chg, 0.3, -0.3, invert=True)
    scores.append(usd_v * 0.8)
    subs['usd'] = round(usd_v)
    if usd_v >= 65: bulls.append({'factor': 'USD Weakness', 'detail': f"Dollar down {uup_chg:.2f}% — tailwind for risk assets", 'pillar': 'Price Action'})
    elif usd_v <= 35: bears.append({'factor': 'USD Strength', 'detail': f"Dollar up {uup_chg:.2f}% — headwind for risk assets", 'pillar': 'Price Action'})

    total_weight = sum(w for _, (_, w) in assets.items()) + 0.8
    px_score = round(sum(scores) / total_weight) if scores else 50

    return {
        'score':        px_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(scores),
    }


# ══════════════════════════════════════════════════════════════════
# PILLAR 5: SENTIMENT (10%)
# Contrarian positioning + survey gauges — extremes matter most.
# Weighted blend, renormalised over whatever data is present:
#   COT positioning 30% · Put/Call 25% · AAII 25% · Consumer 10% · VIX 10%
# Put/Call & AAII arrive via sentiment_data (manual/fed); missing → omitted.
# ══════════════════════════════════════════════════════════════════
def score_sentiment(price_data, sentiment_data=None):
    """
    Score sentiment from contrarian positioning + survey data.
    Fear/crowded-short = bullish; greed/crowded-long = caution.
    sentiment_data may contain: cot_spx {long,short,net}, put_call (float),
    aaii_spread (bull%-bear%), consumer_sent {actual,change}.
    """
    sd    = sentiment_data or {}
    subs  = {}
    bulls = []
    bears = []
    weighted = []   # (score, weight)

    def add(key, score, weight):
        subs[key] = round(score)
        weighted.append((score, weight))

    # ── COT positioning (S&P large specs — contrarian) — 30% ────
    cot = sd.get('cot_spx')
    if cot:
        denom = (cot.get('long', 0) + cot.get('short', 0)) or 1
        net_pct = cot.get('net', 0) / denom                 # -1..+1, net as % of spec OI
        v = normalise(net_pct, 0.30, -0.10, invert=True)     # crowded long → bearish
        add('cot_positioning', v, 0.30)
        if v >= 60:
            bulls.append({'factor': 'COT Positioning', 'detail': f"Large specs net {net_pct*100:.0f}% — light/short positioning, contrarian bullish", 'pillar': 'Sentiment'})
        elif v <= 40:
            bears.append({'factor': 'COT Positioning', 'detail': f"Large specs net +{net_pct*100:.0f}% long — crowded, contrarian caution", 'pillar': 'Sentiment'})

    # ── Put/Call ratio (contrarian) — 25% ───────────────────────
    pc = sd.get('put_call')
    if pc is not None:
        v = normalise(pc, 1.0, 0.7)                          # high P/C = fear = bullish
        add('put_call', v, 0.25)
        if v >= 60:
            bulls.append({'factor': 'Put/Call Ratio', 'detail': f"P/C {pc:.2f} — elevated hedging/fear, contrarian bullish", 'pillar': 'Sentiment'})
        elif v <= 40:
            bears.append({'factor': 'Put/Call Ratio', 'detail': f"P/C {pc:.2f} — call-heavy greed, contrarian caution", 'pillar': 'Sentiment'})

    # ── AAII bull-bear spread (contrarian) — 25% ────────────────
    aaii = sd.get('aaii_spread')
    if aaii is not None:
        v = normalise(aaii, 10, -15, invert=True)            # too bullish → bearish
        add('aaii', v, 0.25)
        if v >= 60:
            bulls.append({'factor': 'AAII Sentiment', 'detail': f"Bull-bear spread {aaii:+.0f}% — retail pessimism, contrarian bullish", 'pillar': 'Sentiment'})
        elif v <= 40:
            bears.append({'factor': 'AAII Sentiment', 'detail': f"Bull-bear spread {aaii:+.0f}% — retail euphoria, contrarian caution", 'pillar': 'Sentiment'})

    # ── Consumer sentiment (UMCSENT, pro-cyclical) — 10% ────────
    cons = sd.get('consumer_sent')
    if cons and cons.get('change') is not None:
        v = normalise(cons['change'], 2, -2)
        add('consumer', v, 0.10)
        if v >= 60: bulls.append({'factor': 'Consumer Sentiment', 'detail': 'Michigan sentiment rising — demand backdrop improving', 'pillar': 'Sentiment'})
        elif v <= 40: bears.append({'factor': 'Consumer Sentiment', 'detail': 'Michigan sentiment falling — demand cooling', 'pillar': 'Sentiment'})

    # ── VIX (live anchor) — 10% ─────────────────────────────────
    vix = price_data.get('vix', {})
    vix_level = vix.get('price', 18) if vix else 18
    if vix_level > 30:
        v = 75; bulls.append({'factor': 'VIX Spike', 'detail': f"VIX {vix_level:.1f} — extreme fear, contrarian buy signal", 'pillar': 'Sentiment'})
    elif vix_level > 22:
        v = 58
    elif vix_level < 13:
        v = 38; bears.append({'factor': 'VIX Complacency', 'detail': f"VIX {vix_level:.1f} — very low fear, market may be extended", 'pillar': 'Sentiment'})
    elif vix_level < 16:
        v = 48
    else:
        v = 54
    add('vix', v, 0.10)

    # Weighted blend, renormalised over present signals
    if weighted:
        tw = sum(w for _, w in weighted)
        sent_score = round(sum(s * w for s, w in weighted) / tw) if tw else 50
    else:
        sent_score = 50

    return {
        'score':        sent_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(weighted),
    }


# ══════════════════════════════════════════════════════════════════
# CONFIDENCE MODEL
# How much do the pillars agree with each other?
# ══════════════════════════════════════════════════════════════════
def calc_confidence(pillar_scores):
    """
    Confidence = inverse of inter-pillar disagreement.
    High std dev between pillars = low confidence (conflicting signals).
    Low std dev = high confidence (pillars agree).
    """
    scores = [v for v in pillar_scores.values() if v is not None]
    if len(scores) < 2:
        return 50
    mean    = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std_dev  = math.sqrt(variance)
    # Max possible std_dev for 0-100 = 50 (half all at 0, half at 100)
    confidence = round(100 - (std_dev / 50 * 100))
    return max(20, min(99, confidence))


# ══════════════════════════════════════════════════════════════════
# TIME HORIZON SCORES
# Different pillars dominate at different timeframes
# ══════════════════════════════════════════════════════════════════
def calc_horizons(pillar_scores):
    """
    Short:        Price(40%) + Sentiment(30%) + Internals(30%)
    Intermediate: Equal weighting (20% each)
    Long:         Liquidity(40%) + Economic(35%) + Internals(25%)
    """
    e  = pillar_scores.get('economic',  50)
    lq = pillar_scores.get('liquidity', 50)
    i  = pillar_scores.get('internals', 50)
    px = pillar_scores.get('price',     50)
    s  = pillar_scores.get('sentiment', 50)

    short_score = round(px*0.40 + s*0.30 + i*0.30)
    med_score   = round((e + lq + i + px + s) / 5)
    long_score  = round(lq*0.40 + e*0.35 + i*0.25)

    def label(sc):
        lbl, col, _ = regime_label(sc)
        return {'score': sc, 'label': lbl, 'color': col}

    return {
        'short':        label(short_score),
        'intermediate': label(med_score),
        'long':         label(long_score),
    }


# ══════════════════════════════════════════════════════════════════
# ASSET SCORES
# How does each asset class score in the current regime?
# ══════════════════════════════════════════════════════════════════
def _regime_fit(score):
    """Map an asset score to a regime-fit label."""
    if score >= 60: return 'Strong'
    if score >= 42: return 'Moderate'
    return 'Weak'


def _build_asset(name, ticker, components):
    """
    Build an asset score from weighted components and attribute every
    point to a named driver (relative to the 50 neutral line).

    components: list of (label, value_0_100, weight)
    The score is identical to the original weighted blend; drivers explain it.
    """
    score = round(sum(v * w for _, v, w in components))
    bulls, bears = [], []
    for label, v, w in components:
        pts = round((v - 50) * w)
        if pts > 0:   bulls.append({'label': label, 'pts': pts})
        elif pts < 0: bears.append({'label': label, 'pts': pts})
    bulls.sort(key=lambda d: -d['pts'])
    bears.sort(key=lambda d: d['pts'])
    conf = calc_confidence({label: v for label, v, _ in components})
    return {
        'name': name, 'ticker': ticker, 'score': score,
        'confidence': conf, 'regime_fit': _regime_fit(score),
        'drivers': {'bullish': bulls, 'bearish': bears},
    }


def _interpret(key, score, ctx):
    """Plain-English, advice-free interpretation per asset given regime context."""
    px, e, lq, s = ctx['price'], ctx['economic'], ctx['liquidity'], ctx['sentiment']
    risk_on = px >= 55
    if key == 'US_EQUITIES':
        if score >= 60:
            return 'Broadly supported by liquidity and price action — the regime favours risk assets.'
        if score >= 45:
            return 'Mixed support: constructive trend offset by softer economic or breadth signals. Review breadth before adding risk.'
        return 'Headwinds dominate — weak liquidity or deteriorating price action pressure equities.'
    if key == 'GOLD':
        if score >= 60 and risk_on:
            return 'Gold remains supported, but upside may be capped if equities continue risk-on.'
        if score >= 60:
            return 'Gold well-supported by inflation and safe-haven demand in a defensive regime.'
        return 'Gold support is muted — risk appetite is drawing flows away from defensives.'
    if key == 'BONDS':
        if score >= 60:
            return 'Falling yields and a risk-off tilt support duration — review long-duration exposure.'
        if score >= 45:
            return 'Bonds neutral — rate direction unclear, watch yields and the curve.'
        return 'Rising yields or risk-on conditions pressure bonds.'
    if key == 'USD':
        if score >= 60:
            return 'Dollar firm on strong relative growth or tight liquidity — a headwind for commodities and EM.'
        if score >= 42:
            return 'Dollar mixed — no strong directional driver right now.'
        return 'Dollar soft — supportive backdrop for commodities, gold and risk assets.'
    if key == 'OIL':
        if score >= 60:
            return 'Crude supported by risk-on demand and a softer dollar.'
        if score >= 45:
            return 'Crude balanced between demand signals and dollar strength.'
        return 'Crude pressured by weak demand signals or a firm dollar.'
    return 'Regime context applied.'


def calc_asset_scores(regime_score, pillar_scores, price_data):
    """
    Asset scores derived from regime + asset-specific adjustments, with every
    point attributed to a named driver and a plain-English interpretation.
    """
    e  = pillar_scores.get('economic',  50)
    lq = pillar_scores.get('liquidity', 50)
    i  = pillar_scores.get('internals', 50)
    px = pillar_scores.get('price',     50)
    s  = pillar_scores.get('sentiment', 50)
    ctx = {'economic': e, 'liquidity': lq, 'internals': i, 'price': px, 'sentiment': s}

    uup_chg = (price_data.get('uup') or {}).get('changePct', 0)
    tlt_chg = (price_data.get('tlt') or {}).get('changePct', 0)

    # Asset-specific adjustment factors (same math as before)
    usd_adj  = 60 if uup_chg < -0.2 else 40 if uup_chg > 0.2 else 50
    infl_adj = 65 if e < 45 else 45
    fear_adj = 70 if s < 45 else 45 if s > 65 else 52
    rate_adj = 65 if tlt_chg > 0.2 else 35 if tlt_chg < -0.2 else 50
    risk_adj = 65 if px < 45 else 35 if px > 65 else 50

    assets = {}
    assets['US_EQUITIES'] = _build_asset('US Equities', 'SPY', [
        ('Liquidity', lq, 0.25), ('Market internals', i, 0.25),
        ('Price action', px, 0.20), ('Economic data', e, 0.20),
        ('Sentiment', s, 0.10),
    ])
    assets['GOLD'] = _build_asset('Gold', 'GLD', [
        ('USD direction', usd_adj, 0.35), ('Inflation hedge', infl_adj, 0.35),
        ('Safe-haven demand', fear_adj, 0.30),
    ])
    assets['BONDS'] = _build_asset('US Bonds', 'TLT', [
        ('Falling yields', rate_adj, 0.50), ('Risk-off demand', risk_adj, 0.30),
        ('Weak growth', 100 - e, 0.20),
    ])
    assets['USD'] = _build_asset('US Dollar', 'UUP', [
        ('Relative growth', e, 0.40), ('Tight liquidity', 100 - lq, 0.30),
        ('Risk-off bid', 100 - px, 0.30),
    ])
    assets['OIL'] = _build_asset('Crude Oil', 'USO', [
        ('Risk-on demand', px, 0.30), ('Growth demand', e, 0.30),
        ('Weak USD', 100 - usd_adj, 0.25), ('Market internals', i, 0.15),
    ])

    for key, a in assets.items():
        a['interpretation'] = _interpret(key, a['score'], ctx)

    return assets


# ══════════════════════════════════════════════════════════════════
# DELTA — What changed since last snapshot?
# ══════════════════════════════════════════════════════════════════
def calc_delta(current_score, current_pillars):
    """Compare against previous cached snapshot."""
    prev = cache.get('rie:previous_snapshot')
    if not prev:
        return {'regime_change': 0, 'biggest_mover': None, 'narrative': 'First reading — establishing baseline'}

    prev_score   = prev.get('regime_score', current_score)
    prev_pillars = prev.get('pillar_scores', {})

    regime_change = current_score - prev_score

    # Find which pillar moved most
    pillar_deltas = {
        k: current_pillars.get(k, 50) - prev_pillars.get(k, 50)
        for k in current_pillars
    }
    biggest_mover = max(pillar_deltas, key=lambda k: abs(pillar_deltas[k])) if pillar_deltas else None
    biggest_delta = pillar_deltas.get(biggest_mover, 0) if biggest_mover else 0

    # Plain English narrative
    if abs(regime_change) < 2:
        narrative = 'Regime stable — no significant change from previous reading'
    elif biggest_mover and abs(biggest_delta) > 3:
        direction = 'improved' if biggest_delta > 0 else 'deteriorated'
        narrative = f"{biggest_mover.capitalize()} pillar {direction} ({biggest_delta:+.0f} pts) — driving regime shift"
    elif regime_change > 5:
        narrative = f'Regime strengthening — broad-based improvement across pillars'
    elif regime_change < -5:
        narrative = f'Regime weakening — conditions deteriorating'
    else:
        narrative = f'Minor regime shift ({regime_change:+.0f} pts)'

    return {
        'regime_change':  round(regime_change, 1),
        'biggest_mover':  biggest_mover,
        'biggest_delta':  round(biggest_delta, 1),
        'narrative':      narrative,
        'pillar_deltas':  {k: round(v, 1) for k, v in pillar_deltas.items()},
    }


# ══════════════════════════════════════════════════════════════════
# HISTORY & TREND — real time-series, not fabricated
# Snapshots are appended each run; trend/1w/1m derive from stored history.
# Until enough history accrues, trend fields return None (UI shows baseline).
# ══════════════════════════════════════════════════════════════════
TREND_THR = 3   # pts of change to call a trend Improving / Deteriorating

def _trend_label(change):
    if change is None: return None
    if change >= TREND_THR:  return 'Improving'
    if change <= -TREND_THR: return 'Deteriorating'
    return 'Stable'

def record_history(entry, max_days=95, max_len=600):
    """Append a minimal snapshot to the rolling history list in cache."""
    hist = cache.get('rie:history') or []
    hist.append(entry)
    cutoff = entry['ts'] - max_days * 86400
    hist = [h for h in hist if h.get('ts', 0) >= cutoff][-max_len:]
    cache.set('rie:history', hist, max_days * 86400)
    return hist

def get_historical(hist, now_ts, days, lo, hi):
    """
    Closest stored snapshot to (now - `days`), accepted only if its age is
    within [lo, hi] days. Returns the entry or None.
    """
    target = now_ts - days * 86400
    window = [h for h in hist
              if now_ts - hi * 86400 <= h.get('ts', 0) <= now_ts - lo * 86400]
    if not window:
        return None
    return min(window, key=lambda h: abs(h.get('ts', 0) - target))

def build_what_changed(curr_pillars, ref_pillars, weights, basis):
    """
    Attribute the regime-score change to each pillar:
    contribution = (pillar_now − pillar_ref) × pillar_weight.
    """
    if not ref_pillars:
        return {'basis': basis, 'available': False, 'items': []}
    items = []
    for k, w in weights.items():
        delta = curr_pillars.get(k, 50) - ref_pillars.get(k, 50)
        contrib = round(delta * w)
        if abs(contrib) >= 1:
            items.append({
                'pillar': k,
                'label': PILLAR_LABELS.get(k, k.capitalize()),
                'delta': round(delta, 1),
                'contribution': contrib,
            })
    items.sort(key=lambda d: -abs(d['contribution']))
    return {'basis': basis, 'available': True, 'items': items}

PILLAR_LABELS = {
    'economic': 'Economic Data', 'liquidity': 'Liquidity',
    'internals': 'Market Internals', 'price': 'Price Action',
    'sentiment': 'Sentiment',
}

def _dedup(seq, cap=6):
    return list(dict.fromkeys(seq))[:cap]

def build_environment(pillar_scores, asset_scores):
    """
    Map the current regime tilt to what it favours / pressures.
    Framed as favours/pressures/watch — never advice.
    """
    e  = pillar_scores.get('economic', 50)
    lq = pillar_scores.get('liquidity', 50)
    i  = pillar_scores.get('internals', 50)
    px = pillar_scores.get('price', 50)
    usd  = (asset_scores.get('USD')  or {}).get('score', 50)
    gold = (asset_scores.get('GOLD') or {}).get('score', 50)
    bonds= (asset_scores.get('BONDS')or {}).get('score', 50)
    oil  = (asset_scores.get('OIL')  or {}).get('score', 50)

    favours, pressures = [], []

    # Liquidity / rates
    if lq >= 55:
        favours += ['Long-duration growth assets', 'Falling-rate beneficiaries', 'Quality growth']
    elif lq <= 45:
        pressures += ['Long-duration growth assets', 'Highly indebted companies', 'Rate-sensitive sectors']

    # Internals / risk appetite
    if i >= 55:
        favours += ['Semiconductors', 'Cyclicals', 'High-beta equities']
    elif i <= 45:
        favours += ['Defensive sectors']
        pressures += ['Weak cyclicals', 'Small caps']

    # Price / risk-on
    if px >= 55:
        favours += ['Risk assets']
        pressures += ['Defensive havens']
    elif px <= 45:
        favours += ['Defensive havens']
        pressures += ['Risk assets']

    # Economy
    if e >= 55:
        favours += ['Cyclicals', 'Financials']
    elif e <= 42:
        pressures += ['Deep cyclicals']

    # Asset-score driven (keeps this consistent with the asset table)
    if usd >= 58:  pressures += ['Commodities', 'Crude oil', 'Emerging markets']
    elif usd <= 42: favours += ['Commodities', 'Emerging markets']; pressures += ['US dollar']
    if gold >= 58:  favours += ['Gold', 'Real assets']
    if bonds >= 58: favours += ['Long-duration bonds']
    if oil <= 45:   pressures += ['Crude oil']

    return {'favours': _dedup(favours), 'pressures': _dedup(pressures)}

def build_summary(regime_label, pillar_scores):
    """One-paragraph plain-English read of the regime."""
    strong = [PILLAR_LABELS[k] for k, v in pillar_scores.items() if v >= 56]
    weak   = [PILLAR_LABELS[k] for k, v in pillar_scores.items() if v <= 44]
    def join(xs):
        if not xs: return ''
        if len(xs) == 1: return xs[0]
        return ', '.join(xs[:-1]) + ' and ' + xs[-1]
    if strong and weak:
        body = f"{join(strong)} {'is' if len(strong)==1 else 'are'} supportive, while {join(weak).lower()} {'remains' if len(weak)==1 else 'remain'} weak."
    elif strong:
        body = f"{join(strong)} {'is' if len(strong)==1 else 'are'} supportive, with the other pillars broadly neutral."
    elif weak:
        body = f"{join(weak)} {'is' if len(weak)==1 else 'are'} weak, with the other pillars broadly neutral."
    else:
        body = "No pillar is strongly tilted — signals are broadly balanced."
    return f"{body} The environment is {regime_label.lower()}."


# ══════════════════════════════════════════════════════════════════
# MAIN ENGINE — Assemble everything
# ══════════════════════════════════════════════════════════════════
def run_rie(fred_data, price_data, sentiment_data=None):
    """
    Run the full Regime Intelligence Engine.
    
    Args:
        fred_data:  Dict of FRED series {key: {actual, previous, change, date}}
        price_data: Dict of live prices {ticker_lower: {price, changePct, ...}}
    
    Returns:
        Complete regime snapshot dict
    """
    # ── Score all 5 pillars ──────────────────────────────────────
    eco  = score_economic(fred_data)
    liq  = score_liquidity(fred_data, price_data)
    itn  = score_internals(price_data)
    px   = score_price_action(price_data)
    sent = score_sentiment(price_data, sentiment_data)

    pillar_scores = {
        'economic':  eco['score'],
        'liquidity': liq['score'],
        'internals': itn['score'],
        'price':     px['score'],
        'sentiment': sent['score'],
    }

    # ── Weighted composite ───────────────────────────────────────
    weights = {'economic': 0.20, 'liquidity': 0.25, 'internals': 0.20, 'price': 0.25, 'sentiment': 0.10}
    regime_score = round(sum(pillar_scores[k] * weights[k] for k in weights))

    # ── Confidence ───────────────────────────────────────────────
    confidence   = calc_confidence(pillar_scores)

    # ── Regime label ─────────────────────────────────────────────
    label, color, bg = regime_label(regime_score)

    # ── Time horizons ────────────────────────────────────────────
    horizons = calc_horizons(pillar_scores)

    # ── Asset scores ─────────────────────────────────────────────
    asset_scores = calc_asset_scores(regime_score, pillar_scores, price_data)

    # ── Bull / Bear factors (all pillars combined) ──────────────
    all_bulls = eco['bull_factors'] + liq['bull_factors'] + itn['bull_factors'] + px['bull_factors'] + sent['bull_factors']
    all_bears = eco['bear_factors'] + liq['bear_factors'] + itn['bear_factors'] + px['bear_factors'] + sent['bear_factors']

    # ── Delta vs previous reading (short-term) ──────────────────
    delta = calc_delta(regime_score, pillar_scores)

    # ── Time-series history → real weekly / monthly trend ───────
    now_ts = int(time.time())
    hist   = cache.get('rie:history') or []
    snap_1w = get_historical(hist, now_ts, 7,  3, 18)
    snap_1m = get_historical(hist, now_ts, 30, 18, 60)

    change_1w = round(regime_score - snap_1w['score'], 1) if snap_1w else None
    change_1m = round(regime_score - snap_1m['score'], 1) if snap_1m else None

    # Trend: prefer the real weekly change; fall back to previous-reading delta
    trend_basis = change_1w if change_1w is not None else (
        delta.get('regime_change') if delta.get('regime_change') is not None else None)
    regime_trend = _trend_label(trend_basis) or 'Stable'

    # ── Enrich each pillar: trend + top bull/bear driver ────────
    ref_pillars_1w = (snap_1w or {}).get('pillars')
    pillar_objs = {}
    raw = {'economic': eco, 'liquidity': liq, 'internals': itn, 'price': px, 'sentiment': sent}
    for k, obj in raw.items():
        if ref_pillars_1w and k in ref_pillars_1w:
            p_change = pillar_scores[k] - ref_pillars_1w[k]
        else:
            p_change = (delta.get('pillar_deltas') or {}).get(k)
        pillar_objs[k] = {
            **obj,
            'weight': weights[k],
            'label':  PILLAR_LABELS[k],
            'trend':  _trend_label(p_change) or 'Stable',
            'change': round(p_change, 1) if p_change is not None else None,
            'top_bull': (obj['bull_factors'][0] if obj['bull_factors'] else None),
            'top_bear': (obj['bear_factors'][0] if obj['bear_factors'] else None),
        }

    # ── Asset trend (vs ~1w ago) ────────────────────────────────
    ref_assets_1w = (snap_1w or {}).get('assets') or {}
    for akey, a in asset_scores.items():
        prev = ref_assets_1w.get(akey)
        a_change = round(a['score'] - prev, 1) if prev is not None else None
        a['trend']  = _trend_label(a_change) or 'Stable'
        a['change'] = a_change

    # ── What changed (pillar attribution of the score move) ─────
    if ref_pillars_1w:
        what_changed = build_what_changed(pillar_scores, ref_pillars_1w, weights, 'vs 1 week ago')
    else:
        what_changed = build_what_changed(pillar_scores, (delta.get('pillar_deltas') is not None) and
                                          {k: pillar_scores[k] - (delta['pillar_deltas'].get(k, 0)) for k in weights} or None,
                                          weights, 'vs previous reading')

    # ── Environment favours / pressures + plain-English summary ─
    environment = build_environment(pillar_scores, asset_scores)
    summary     = build_summary(label, pillar_scores)

    # ── Data quality ────────────────────────────────────────────
    total_dq = eco['data_quality'] + liq['data_quality'] + itn['data_quality'] + px['data_quality'] + sent['data_quality']
    data_quality = min(100, round(total_dq / 20 * 100))

    snapshot = {
        'regime_score':  regime_score,
        'regime_label':  label,
        'regime_color':  color,
        'regime_bg':     bg,
        'confidence':    confidence,
        'data_quality':  data_quality,

        'summary':       summary,
        'regime_trend':  regime_trend,
        'change_1w':     change_1w,
        'change_1m':     change_1m,

        'pillar_scores': pillar_scores,
        'pillars':       pillar_objs,

        'horizons':      horizons,
        'asset_scores':  asset_scores,
        'bull_factors':  all_bulls,
        'bear_factors':  all_bears,
        'delta':         delta,
        'what_changed':  what_changed,
        'environment':   environment,
        'timestamp':     now_ts,
    }

    # ── Persist: previous snapshot (short delta) + rolling history ──
    cache.set('rie:previous_snapshot', {
        'regime_score':  regime_score,
        'pillar_scores': pillar_scores,
    }, 86400)
    record_history({
        'ts':      now_ts,
        'score':   regime_score,
        'pillars': dict(pillar_scores),
        'assets':  {k: v['score'] for k, v in asset_scores.items()},
    })

    return snapshot
