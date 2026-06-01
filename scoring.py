"""
scoring.py — institutional-style weighted asset scoring (FROZEN matrix).

Replaces the old equal-weighted -1/0/+1 counting with a weighted blend of
continuous 0-100 factor readings, where each asset CLASS weights factors by how
much they actually drive it. Composite is 0-100; conviction is distance from 50.

Six factors: Growth · Inflation · Real Yields · Liquidity · USD · Momentum.

Weights are theory-grounded and FROZEN (approved 2026-06). Do not tune to make
rankings match expectations — refine only on backtest evidence.

Pure stdlib, no app deps, so server.py and the validation harness share it.
"""

# ── FROZEN weight matrix (each row sums to 100) ──────────────────
# Equities row reflects the approved tweak: 5% moved Inflation→Real Yields.
ASSET_WEIGHTS = {
    'equities':  {'growth': 20, 'infl': 15, 'ry': 15, 'liq': 25, 'usd': 5,  'mom': 20},
    'gold':      {'growth': 5,  'infl': 30, 'ry': 20, 'liq': 20, 'usd': 20, 'mom': 5},
    'bonds':     {'growth': 20, 'infl': 25, 'ry': 25, 'liq': 20, 'usd': 0,  'mom': 10},
    'commodity': {'growth': 30, 'infl': 15, 'ry': 5,  'liq': 15, 'usd': 20, 'mom': 15},
    'forex':     {'growth': 15, 'infl': 0,  'ry': 20, 'liq': 10, 'usd': 50, 'mom': 5},
}

# Sign of each factor: does a HIGH reading help (+1) or hurt (-1) this class?
#   growth high = strong economy · infl high = hot inflation · ry high = high real yields
#   liq high = ample liquidity · usd high = strong dollar · mom high = strong trend
ASSET_SIGNS = {
    'equities':  {'growth': +1, 'infl': -1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1},
    'gold':      {'growth': +1, 'infl': +1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1},
    'bonds':     {'growth': -1, 'infl': -1, 'ry': -1, 'liq': +1, 'usd':  0, 'mom': +1},
    'commodity': {'growth': +1, 'infl': +1, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1},
    'forex':     {'growth': +1, 'infl':  0, 'ry': -1, 'liq': +1, 'usd': -1, 'mom': +1},  # foreign-ccy default
}
# Long-dollar instruments (UUP) invert the USD/real-yield signs.
USD_LONG_SIGNS = {'growth': +1, 'infl': 0, 'ry': +1, 'liq': 0, 'usd': +1, 'mom': +1}
USD_LONG_TICKERS = {'UUP', 'USDU'}
GOLD_TICKERS = {'GLD', 'SLV', 'GDX', 'IAU', 'TIP', 'SLVP', 'SIVR'}

FACTOR_LABELS = {'growth': 'Growth', 'infl': 'Inflation', 'ry': 'Real Yields',
                 'liq': 'Liquidity', 'usd': 'USD', 'mom': 'Momentum'}


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
    return 'equities'


def _g(macro, key, field='current'):
    d = macro.get(key) or {}
    return d.get(field)


def compute_raw_readings(macro, chg_pct=0.0, range_pos=50.0):
    """
    Build the six asset-INDEPENDENT 0-100 readings from the macro dict.
    Missing inputs default to 50 (neutral) so partial data never fabricates signal.
    macro keys used: gdp/nfp/unemp/retail, cpi/ppi, real_yield, liquidity_pillar, uup.
    """
    # Growth: GDP, payrolls, unemployment direction, retail
    gs = []
    if _g(macro, 'gdp')          is not None: gs.append(nz(_g(macro, 'gdp'),           0.0,  3.0))
    if _g(macro, 'nfp', 'change')is not None: gs.append(nz(_g(macro, 'nfp', 'change'), -50,  250))
    if _g(macro, 'unemp','change')is not None: gs.append(nz(_g(macro, 'unemp','change'), 0.2, -0.2))  # falling=strong
    if _g(macro, 'retail','change')is not None: gs.append(nz(_g(macro, 'retail','change'), -0.3, 0.6))
    growth = sum(gs) / len(gs) if gs else 50.0

    # Inflation hotness: driven by YoY LEVELS (CPI/core/PPI), graded not saturated
    is_ = []
    if _g(macro, 'cpi')      is not None: is_.append(nz(_g(macro, 'cpi'),      2.0, 5.0))
    if _g(macro, 'core_cpi') is not None: is_.append(nz(_g(macro, 'core_cpi'), 2.0, 4.5))
    if _g(macro, 'ppi')      is not None: is_.append(nz(_g(macro, 'ppi'),      0.0, 8.0))
    infl = sum(is_) / len(is_) if is_ else 50.0

    # Real yields (level)
    ry = nz(_g(macro, 'real_yield'), 0.0, 2.5) if _g(macro, 'real_yield') is not None else 50.0

    # Liquidity — already a 0-100 favourability from the regime engine
    lp = macro.get('liquidity_pillar')
    liq = float(lp) if lp is not None else 50.0

    # USD strength (daily dollar direction)
    uup = (macro.get('uup') or {}).get('changePct')
    usd = nz(uup, -0.5, 0.5) if uup is not None else 50.0

    # Momentum — per-asset: price change + 52w range position
    mom = (nz(chg_pct, -2.0, 2.0) + range_pos) / 2.0

    return {k: round(v, 1) for k, v in
            {'growth': growth, 'infl': infl, 'ry': ry, 'liq': liq, 'usd': usd, 'mom': mom}.items()}


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
