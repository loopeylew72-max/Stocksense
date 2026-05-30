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
# Tracks Fed balance sheet, M2, real yields, credit
# ══════════════════════════════════════════════════════════════════
def score_liquidity(fred_data, price_data):
    """
    Score liquidity conditions.
    Liquidity leads markets by 3-6 months.
    """
    subs  = {}
    bulls = []
    bears = []
    scores = []

    # ── Real yields (DFII10 — 10Y TIPS) ────────────────────────
    real_yield = fred_data.get('real_yield', {})
    if real_yield.get('actual') is not None:
        ry = real_yield['actual']
        # Negative/low real yields = loose financial conditions = bullish
        v = normalise(ry, 0.5, 2.0, invert=True)
        scores.append(v)
        subs['real_yields'] = round(v)
        if v >= 60: bulls.append({'factor': 'Real Yields', 'detail': f"Real yield {ry:.2f}% — supportive for equities", 'pillar': 'Liquidity'})
        elif v <= 40: bears.append({'factor': 'Real Yields', 'detail': f"Real yield {ry:.2f}% — restrictive financial conditions", 'pillar': 'Liquidity'})

    # ── M2 Money Supply trend ───────────────────────────────────
    m2 = fred_data.get('m2', {})
    if m2.get('change') is not None:
        m2_chg = m2['change']
        v = normalise(m2_chg, 0.3, -0.3)
        scores.append(v)
        subs['m2'] = round(v)
        if v >= 60: bulls.append({'factor': 'M2 Money Supply', 'detail': 'Money supply expanding — liquidity supportive', 'pillar': 'Liquidity'})
        elif v <= 40: bears.append({'factor': 'M2 Money Supply', 'detail': 'M2 contracting — liquidity tightening', 'pillar': 'Liquidity'})

    # ── Credit conditions proxy (HYG) ───────────────────────────
    hyg = price_data.get('hyg', {})
    hyg_chg = hyg.get('changePct', 0) if hyg else 0
    v = normalise(hyg_chg, 0.3, -0.3)
    scores.append(v)
    subs['credit'] = round(v)
    if v >= 60: bulls.append({'factor': 'Credit Spreads', 'detail': 'HYG rising — spreads tightening, credit healthy', 'pillar': 'Liquidity'})
    elif v <= 40: bears.append({'factor': 'Credit Spreads', 'detail': 'HYG falling — spreads widening, credit stress', 'pillar': 'Liquidity'})

    # ── TLT as bond market liquidity signal ─────────────────────
    tlt = price_data.get('tlt', {})
    tlt_chg = tlt.get('changePct', 0) if tlt else 0
    v = normalise(tlt_chg, 0.3, -0.3)
    scores.append(v)
    subs['bonds'] = round(v)
    if v >= 60: bulls.append({'factor': 'Bond Yields', 'detail': 'Yields falling — easier financial conditions', 'pillar': 'Liquidity'})
    elif v <= 40: bears.append({'factor': 'Bond Yields', 'detail': 'Yields rising — tightening conditions', 'pillar': 'Liquidity'})

    liq_score = round(sum(scores) / len(scores)) if scores else 50

    return {
        'score':        liq_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(scores),
    }


# ══════════════════════════════════════════════════════════════════
# PILLAR 3: MARKET INTERNALS (20%)
# Breadth = health of the rally
# ══════════════════════════════════════════════════════════════════
def score_internals(price_data):
    """
    Score market internals — is the move broad-based or narrow?
    Uses ETF proxies for breadth signals.
    """
    subs  = {}
    bulls = []
    bears = []
    scores = []

    spy = price_data.get('spy', {})
    qqq = price_data.get('qqq', {})
    iwm = price_data.get('iwm', {})  # Small caps
    rsp = price_data.get('rsp', {})  # Equal weight S&P

    spy_chg = spy.get('changePct', 0) if spy else 0
    qqq_chg = qqq.get('changePct', 0) if qqq else 0
    iwm_chg = iwm.get('changePct', 0) if iwm else 0
    rsp_chg = rsp.get('changePct', 0) if rsp else 0

    # ── Small vs Large cap (IWM vs SPY) ─────────────────────────
    if spy_chg != 0:
        small_large = iwm_chg - spy_chg
        v = normalise(small_large, 0.5, -0.5)
        scores.append(v)
        subs['small_large'] = round(v)
        if v >= 60: bulls.append({'factor': 'Small Cap Leadership', 'detail': f"IWM outperforming SPY by {small_large:.1f}% — broad rally", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Large Cap Concentration', 'detail': 'Small caps lagging — narrow market leadership', 'pillar': 'Internals'})

    # ── Equal weight vs cap weight (RSP vs SPY) ─────────────────
    if spy_chg != 0 and rsp_chg != 0:
        breadth = rsp_chg - spy_chg
        v = normalise(breadth, 0.3, -0.3)
        scores.append(v)
        subs['breadth'] = round(v)
        if v >= 60: bulls.append({'factor': 'Market Breadth', 'detail': 'Equal weight outperforming — breadth healthy', 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'Market Breadth', 'detail': 'Cap weight dominating — few stocks driving gains', 'pillar': 'Internals'})

    # ── 52-week range position (proxy for trend health) ─────────
    spy_hi  = spy.get('week52High', 0) if spy else 0
    spy_lo  = spy.get('week52Low', 0)  if spy else 0
    spy_px  = spy.get('price', 0)      if spy else 0
    if spy_hi > spy_lo > 0:
        rng_pos = (spy_px - spy_lo) / (spy_hi - spy_lo) * 100
        v = normalise(rng_pos, 70, 30)
        scores.append(v)
        subs['trend_health'] = round(v)
        if v >= 60: bulls.append({'factor': 'SPY Trend', 'detail': f"SPY at {rng_pos:.0f}% of 52w range — uptrend intact", 'pillar': 'Internals'})
        elif v <= 40: bears.append({'factor': 'SPY Trend', 'detail': f"SPY at {rng_pos:.0f}% of 52w range — downtrend", 'pillar': 'Internals'})

    # ── Tech leadership (QQQ vs SPY) ────────────────────────────
    if spy_chg != 0 and qqq_chg != 0:
        tech_lead = qqq_chg - spy_chg
        v = normalise(tech_lead, 0.5, -0.8)
        scores.append(v)
        subs['tech_leadership'] = round(v)

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
# Contrarian signal — extremes matter most
# ══════════════════════════════════════════════════════════════════
def score_sentiment(price_data):
    """
    Score sentiment — extremes are contrarian signals.
    Fear = buying opportunity. Greed = caution.
    """
    subs  = {}
    bulls = []
    bears = []
    scores = []

    # VIX — fear gauge
    vix = price_data.get('vix', {})
    vix_level = vix.get('price', 18) if vix else 18
    # Low VIX = complacency (slight negative) / Very high VIX = buying opportunity
    if vix_level > 30:
        v = 75  # Extreme fear = contrarian bullish
        bulls.append({'factor': 'VIX Spike', 'detail': f"VIX {vix_level:.1f} — extreme fear, contrarian buy signal", 'pillar': 'Sentiment'})
    elif vix_level > 22:
        v = 55
        bulls.append({'factor': 'VIX Elevated', 'detail': f"VIX {vix_level:.1f} — fear present, potential opportunity", 'pillar': 'Sentiment'})
    elif vix_level < 13:
        v = 38  # Extreme complacency = caution
        bears.append({'factor': 'VIX Complacency', 'detail': f"VIX {vix_level:.1f} — very low fear, market may be extended", 'pillar': 'Sentiment'})
    elif vix_level < 16:
        v = 48
    else:
        v = 55  # Moderate VIX = neutral-positive
    scores.append(v)
    subs['vix'] = round(v)

    # GLD as safe haven demand signal
    gld = price_data.get('gld', {})
    gld_chg = gld.get('changePct', 0) if gld else 0
    # Rising gold = fear / uncertainty (contrarian — depends on context)
    if gld_chg > 1.5:
        bears.append({'factor': 'Gold Surge', 'detail': f"Gold +{gld_chg:.1f}% — safe haven demand, risk-off signal", 'pillar': 'Sentiment'})
        scores.append(38)
        subs['gold_signal'] = 38
    elif gld_chg < -1:
        bulls.append({'factor': 'Gold Weakness', 'detail': 'Gold selling off — risk appetite healthy', 'pillar': 'Sentiment'})
        scores.append(65)
        subs['gold_signal'] = 65
    else:
        scores.append(52)
        subs['gold_signal'] = 52

    sent_score = round(sum(scores) / len(scores)) if scores else 50

    return {
        'score':        sent_score,
        'sub_scores':   subs,
        'bull_factors': bulls,
        'bear_factors': bears,
        'data_quality': len(scores),
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
def calc_asset_scores(regime_score, pillar_scores, price_data):
    """
    Asset scores derived from regime + asset-specific adjustments.
    Each asset has different sensitivity to each pillar.
    """
    e  = pillar_scores.get('economic',  50)
    lq = pillar_scores.get('liquidity', 50)
    i  = pillar_scores.get('internals', 50)
    px = pillar_scores.get('price',     50)
    s  = pillar_scores.get('sentiment', 50)

    uup_chg = (price_data.get('uup') or {}).get('changePct', 0)
    tlt_chg = (price_data.get('tlt') or {}).get('changePct', 0)
    gld_chg = (price_data.get('gld') or {}).get('changePct', 0)

    assets = {}

    # US Equities — benefits from all pillars when regime is good
    us_eq = round(e*0.20 + lq*0.25 + i*0.25 + px*0.20 + s*0.10)
    assets['US_EQUITIES'] = {
        'name': 'US Equities', 'score': us_eq,
        'confidence': calc_confidence({'eco': e, 'liq': lq, 'int': i, 'px': px}),
        'ticker': 'SPY',
    }

    # Gold — benefits from uncertainty, weak USD, high inflation
    usd_adj  = 60 if uup_chg < -0.2 else 40 if uup_chg > 0.2 else 50
    infl_adj = 65 if e < 45 else 45  # high inflation (low eco score) = gold bullish
    fear_adj = 70 if s < 45 else 45 if s > 65 else 52
    gld_score = round(usd_adj*0.35 + infl_adj*0.35 + fear_adj*0.30)
    assets['GOLD'] = {
        'name': 'Gold', 'score': gld_score,
        'confidence': calc_confidence({'usd': usd_adj, 'infl': infl_adj, 'fear': fear_adj}),
        'ticker': 'GLD',
    }

    # Bonds — inverse of equity environment (mostly)
    rate_adj  = 65 if tlt_chg > 0.2 else 35 if tlt_chg < -0.2 else 50
    risk_adj  = 65 if px < 45 else 35 if px > 65 else 50  # bonds do well in risk-off
    bond_score = round(rate_adj*0.50 + risk_adj*0.30 + (100-e)*0.20)
    assets['BONDS'] = {
        'name': 'US Bonds', 'score': bond_score,
        'confidence': calc_confidence({'rates': rate_adj, 'risk': risk_adj}),
        'ticker': 'TLT',
    }

    # USD — benefits from strong economy, rate differential, risk-off
    usd_eco   = round(e * 0.4 + (100 - lq) * 0.3 + (100 - px) * 0.3)
    assets['USD'] = {
        'name': 'US Dollar', 'score': usd_eco,
        'confidence': calc_confidence({'eco': e, 'liq': 100-lq}),
        'ticker': 'UUP',
    }

    # Oil — benefits from risk-on, growth, weak USD
    oil_score = round(px*0.30 + e*0.30 + (100-usd_adj)*0.25 + i*0.15)
    assets['OIL'] = {
        'name': 'Crude Oil', 'score': oil_score,
        'confidence': calc_confidence({'price': px, 'eco': e, 'usd': 100-usd_adj}),
        'ticker': 'USO',
    }

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
# MAIN ENGINE — Assemble everything
# ══════════════════════════════════════════════════════════════════
def run_rie(fred_data, price_data):
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
    sent = score_sentiment(price_data)

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

    # ── Bull / Bear factors (all pillars combined, ranked) ───────
    all_bulls = eco['bull_factors'] + liq['bull_factors'] + itn['bull_factors'] + px['bull_factors'] + sent['bull_factors']
    all_bears = eco['bear_factors'] + liq['bear_factors'] + itn['bear_factors'] + px['bear_factors'] + sent['bear_factors']

    # ── Delta (what changed) ─────────────────────────────────────
    delta = calc_delta(regime_score, pillar_scores)

    # ── Data quality score (how many real data points?) ──────────
    total_dq = eco['data_quality'] + liq['data_quality'] + itn['data_quality'] + px['data_quality'] + sent['data_quality']
    data_quality = min(100, round(total_dq / 20 * 100))

    snapshot = {
        'regime_score':  regime_score,
        'regime_label':  label,
        'regime_color':  color,
        'regime_bg':     bg,
        'confidence':    confidence,
        'data_quality':  data_quality,

        'pillar_scores': pillar_scores,
        'pillars': {
            'economic':  {**eco,  'weight': weights['economic'],  'label': 'Economic Data'},
            'liquidity': {**liq,  'weight': weights['liquidity'], 'label': 'Liquidity'},
            'internals': {**itn,  'weight': weights['internals'], 'label': 'Market Internals'},
            'price':     {**px,   'weight': weights['price'],     'label': 'Price Action'},
            'sentiment': {**sent, 'weight': weights['sentiment'], 'label': 'Sentiment'},
        },

        'horizons':     horizons,
        'asset_scores': asset_scores,
        'bull_factors': all_bulls,
        'bear_factors': all_bears,
        'delta':        delta,
        'timestamp':    int(time.time()),
    }

    # Store as previous for next delta calculation
    cache.set('rie:previous_snapshot', {
        'regime_score':  regime_score,
        'pillar_scores': pillar_scores,
    }, 86400)  # 24 hours

    return snapshot
