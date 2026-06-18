"""
scoring.py — institutional-style weighted asset scoring.

Seven factors: Growth · Inflation · Real Yields · Liquidity · USD · Momentum · Fear.

Weights are theory-grounded. Revised 2026-06 after a full relationship audit:
  - Gold growth sign flipped +1→-1 (safe-haven: weak growth = gold bullish).
  - Forex growth sign flipped +1→-1 (strong US growth = USD strength = foreign weakness).
  - USD Long liquidity sign set to -1 (ample liquidity = weak dollar).
  - Fear/VIX factor added (gold's safe-haven function was unscored; bonds' risk-off demand too).
  - Weights rebalanced so each row sums to 100 with fear carved from lower-priority factors.
  - Layers A (scoring.py) and B (rie.py calc_asset_scores) now agree directionally on gold.

Pure stdlib, no app deps, so server.py and the validation harness share it.
"""

# ── Weight matrix (each row sums to 100) ──────────────────────────
ASSET_WEIGHTS = {
    'equities':  {'growth': 20, 'infl': 15, 'ry': 15, 'liq': 20, 'usd': 5,  'mom': 15, 'fear': 10},
    'gold':      {'growth': 10, 'infl': 20, 'ry': 15, 'liq': 10, 'usd': 20, 'mom': 5,  'fear': 20},
    'bonds':     {'growth': 15, 'infl': 20, 'ry': 25, 'liq': 15, 'usd': 0,  'mom': 10, 'fear': 15},
    'commodity': {'growth': 25, 'infl': 15, 'ry': 5,  'liq': 15, 'usd': 20, 'mom': 15, 'fear': 5},
    'forex':     {'growth': 10, 'infl': 0,  'ry': 15, 'liq': 10, 'usd': 50, 'mom': 5,  'fear': 10},
    # BTC: high-beta risk asset. Loves liquidity + weak dollar + weak real yields.
    # Liquidity (25%) is the dominant driver — global M2 expansion drives crypto.
    # Real yields (20%) — high real yields crush non-yielding assets hardest.
    # USD (15%) — strong dollar drains emerging/risk assets.
    'crypto':    {'growth': 10, 'infl': 10, 'ry': 20, 'liq': 25, 'usd': 15, 'mom': 15, 'fear': 5},
}

# Sign: does a HIGH reading help (+1) or hurt (-1) this class?
#   growth high = strong economy · infl high = hot inflation · ry high = high real yields
#   liq high = ample liquidity · usd high = strong dollar · mom high = strong trend
#   fear high = elevated VIX / risk-off
ASSET_SIGNS = {
    'equities':  {'growth': +1, 'infl': -1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1, 'fear': -1},
    'gold':      {'growth': -1, 'infl': +1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1, 'fear': +1},
    'bonds':     {'growth': -1, 'infl': -1, 'ry': -1, 'liq': +1, 'usd':  0, 'mom': +1, 'fear': +1},
    'commodity': {'growth': +1, 'infl': +1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1, 'fear': -1},
    'forex':     {'growth': -1, 'infl':  0, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1, 'fear': -1},
    # Crypto: high real yields and strong dollar are biggest headwinds.
    # Ample liquidity is biggest tailwind. Fear (VIX spike) = crypto selloff.
    'crypto':    {'growth': +1, 'infl': +1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1, 'fear': -1},
}
# Long-dollar instruments (UUP) invert the USD/real-yield/liquidity signs.
USD_LONG_SIGNS = {'growth': +1, 'infl': 0, 'ry': +1, 'liq': -1, 'usd': +1, 'mom': +1, 'fear': +1}
USD_LONG_TICKERS = {'UUP', 'USDU'}
GOLD_TICKERS = {'GLD', 'SLV', 'GDX', 'IAU', 'TIP', 'SLVP', 'SIVR'}

FACTOR_LABELS = {'growth': 'Growth', 'infl': 'Inflation', 'ry': 'Real Yields',
                 'liq': 'Liquidity', 'usd': 'USD', 'mom': 'Momentum', 'fear': 'Fear/VIX'}


def nz(val, bear, bull):
    """Map a raw reading to 0-100 strength. bear→0, bull→100, midpoint→50, clamped."""
    if val is None or bull == bear:
        return 50.0
    t = (val - bear) / (bull - bear)
    return max(0.0, min(100.0, t * 100.0))


def resolve_asset_class(asset_type, ticker):
    t = (ticker or '').upper()
    if asset_type == 'bond':      return 'bonds'
    if asset_type == 'commodity': return 'gold' if t in GOLD_TICKERS else 'commodity'
    if asset_type == 'forex':     return 'forex'
    if asset_type == 'crypto':    return 'crypto'
    return 'equities'


def _g(macro, key, field='current'):
    d = macro.get(key) or {}
    return d.get(field)


def compute_raw_readings(macro, chg_pct=0.0, range_pos=50.0, pctl=None):
    """
    Build the six asset-INDEPENDENT 0-100 readings from the macro dict.
    Missing inputs default to 50 (neutral) so partial data never fabricates signal.
    macro keys used: gdp/nfp/unemp/retail, cpi/ppi, real_yield, liquidity_pillar, uup.

    pctl: optional dict of {series_name: percentile_0_100} from the durable store.
    When present (>=30 samples of history exist), a factor reads off its OWN 20-year
    distribution instead of hand-set thresholds. Same orientation (high = hot/high),
    so it's a drop-in. Absent series fall back to the frozen nz() thresholds — so this
    is purely additive and never changes behaviour where history is thin.
    """
    pctl = pctl or {}
    def _pc(name):
        v = pctl.get(name)
        return float(v) if v is not None else None

    # Growth: GDP, payrolls, unemployment direction, retail
    gs = []
    if _g(macro, 'gdp')          is not None: gs.append(nz(_g(macro, 'gdp'),           0.0,  3.0))
    if _g(macro, 'nfp', 'change')is not None: gs.append(nz(_g(macro, 'nfp', 'change'), -50,  250))
    if _g(macro, 'unemp','change')is not None: gs.append(nz(_g(macro, 'unemp','change'), 0.2, -0.2))  # falling=strong
    if _g(macro, 'retail','change')is not None: gs.append(nz(_g(macro, 'retail','change'), -0.3, 0.6))
    growth = sum(gs) / len(gs) if gs else 50.0

    # Inflation hotness: percentile vs 20y history when available, else frozen YoY-level thresholds
    is_ = []
    if _g(macro, 'cpi')      is not None: is_.append(_pc('cpi')      if _pc('cpi')      is not None else nz(_g(macro, 'cpi'),      2.0, 5.0))
    if _g(macro, 'core_cpi') is not None: is_.append(_pc('core_cpi') if _pc('core_cpi') is not None else nz(_g(macro, 'core_cpi'), 2.0, 4.5))
    if _g(macro, 'ppi')      is not None: is_.append(_pc('ppi')      if _pc('ppi')      is not None else nz(_g(macro, 'ppi'),      0.0, 8.0))
    infl = sum(is_) / len(is_) if is_ else 50.0

    # Real yields: percentile vs 20y history when available, else frozen level thresholds
    if _pc('real_yield') is not None:
        ry = _pc('real_yield')
    else:
        ry = nz(_g(macro, 'real_yield'), 0.0, 2.5) if _g(macro, 'real_yield') is not None else 50.0

    # Liquidity — already a 0-100 favourability from the regime engine
    lp = macro.get('liquidity_pillar')
    liq = float(lp) if lp is not None else 50.0

    # USD strength — blend daily direction with 52-week range position.
    # Daily changePct alone let a single weak day override the structural dollar
    # signal. The 52w range position anchors the reading to where UUP sits
    # structurally. Blend: 30% daily direction, 70% 52w range position.
    # Note: get_live_price returns week52High/Low but not rangePos directly,
    # so we compute rangePos here from the high/low/price.
    uup_data   = macro.get('uup') or {}
    uup_chg    = uup_data.get('changePct')
    uup_price  = uup_data.get('price')
    uup_hi     = uup_data.get('week52High')
    uup_lo     = uup_data.get('week52Low')
    uup_rng    = uup_data.get('rangePos')   # pre-computed if available
    if uup_rng is None and uup_price and uup_hi and uup_lo and uup_hi > uup_lo:
        uup_rng = (uup_price - uup_lo) / (uup_hi - uup_lo) * 100.0
    usd_daily  = nz(uup_chg, -0.5, 0.5) if uup_chg is not None else None
    usd_struct = float(uup_rng)            if uup_rng is not None else None
    if usd_daily is not None and usd_struct is not None:
        usd = 0.30 * usd_daily + 0.70 * usd_struct
    elif usd_struct is not None:
        usd = usd_struct
    elif usd_daily is not None:
        usd = usd_daily
    else:
        usd = 50.0

    # Fear / VIX level — high VIX = high fear reading.
    # VIX 12 = calm (0), VIX 35 = extreme fear (100). ~20 = moderate (~35 reading).
    # This gives gold/bonds a safe-haven signal and equities a drag in risk-off —
    # the missing component that made scoring.py contradict rie.py on gold.
    vix_price = (macro.get('vix') or {}).get('price')
    fear = nz(vix_price, 12.0, 35.0) if vix_price is not None else 50.0

    # Momentum — per-asset: price change + 52w range position
    mom = (nz(chg_pct, -2.0, 2.0) + range_pos) / 2.0

    return {k: round(v, 1) for k, v in
            {'growth': growth, 'infl': infl, 'ry': ry, 'liq': liq, 'usd': usd, 'mom': mom, 'fear': fear}.items()}


def score_asset(asset_type, ticker, raw):
    """
    Weighted 0-100 composite + per-factor decomposition.
    Returns (composite, overall_label, asset_class, breakdown).
    breakdown[factor] = {weight, favour(0-100), points} so every score is auditable.
    """
    cls = resolve_asset_class(asset_type, ticker)
    W = ASSET_WEIGHTS[cls]
    S = ASSET_SIGNS[cls]
    if cls == 'forex' and (ticker or '').upper() in USD_LONG_TICKERS:
        S = USD_LONG_SIGNS

    breakdown = {}
    total = 0.0
    for f, w in W.items():
        sign = S.get(f, 0)
        fav = raw[f] if sign > 0 else (100.0 - raw[f]) if sign < 0 else 50.0
        pts = (w / 100.0) * fav
        if w > 0:
            breakdown[f] = {'weight': w, 'favour': round(fav), 'points': round(pts, 1), 'sign': sign}
        total += pts

    comp = round(total)
    overall = ('Very Bullish' if comp >= 68 else 'Bullish' if comp >= 57 else
               'Neutral'      if comp >= 44 else 'Bearish' if comp >= 33 else 'Very Bearish')
    return comp, overall, cls, breakdown
