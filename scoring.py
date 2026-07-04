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


# ── Plain-English factor explanations ─────────────────────────────
# One line per (asset_class, factor, direction). Derived from ASSET_SIGNS so the
# text can never contradict the maths: 'tail' = favour >= 57, 'head' = favour <= 43.
# Educational by design — this is what teaches a user WHY the score moves.
WHY_LINES = {
    'equities': {
        'growth': ("Economic momentum feeds earnings — growth is equity fuel.",
                   "Slowing growth threatens earnings, the core driver of equity returns."),
        'infl':   ("Cooling inflation eases margin pressure and lets the Fed loosen.",
                   "Hot inflation squeezes margins and keeps policy tight."),
        'ry':     ("Low real yields make future earnings worth more today.",
                   "High real yields discount future earnings harder and offer a risk-free alternative to stocks."),
        'liq':    ("Ample liquidity pushes cash out along the risk curve into equities.",
                   "Draining liquidity pulls cash out of risk assets first."),
        'usd':    ("A softer dollar flatters the overseas earnings of large caps.",
                   "A firm dollar shrinks overseas earnings when converted back to USD."),
        'mom':    ("Uptrend intact — price is confirming the bull case.",
                   "Downtrend — price itself is the warning sign."),
        'fear':   ("VIX subdued — risk appetite favours equities.",
                   "Elevated VIX — investors are paying up for protection, a classic risk-off tell."),
    },
    'gold': {
        'growth': ("Weak growth raises rate-cut odds — gold's favourite backdrop.",
                   "Firm growth reduces the need for cuts and the case for havens."),
        'infl':   ("Hot inflation erodes paper money — gold is the classic debasement hedge.",
                   "Cooling inflation weakens the case for holding an inflation hedge."),
        'ry':     ("Low or negative real yields remove gold's biggest competitor.",
                   "High real yields mean bonds pay a real return gold can't — its main structural headwind."),
        'liq':    ("Ample liquidity debases cash — supportive for hard assets.",
                   "Tight liquidity favours cash over non-yielding assets."),
        'usd':    ("Gold is priced in dollars — a weaker dollar lifts it for every non-US buyer.",
                   "A firm dollar makes gold dearer abroad, sapping physical and investment demand."),
        'mom':    ("Trend and positioning are with the metal.",
                   "The trend is against the metal — momentum sellers in control."),
        'fear':   ("Risk-off demand — fear is gold's tailwind.",
                   "Calm markets mute safe-haven demand for gold."),
    },
    'bonds': {
        'growth': ("Weak growth brings rate cuts closer — yields fall, bond prices rise.",
                   "Firm growth delays cuts and keeps yields elevated — pressure on prices."),
        'infl':   ("Cooling inflation protects the real value of a bond's fixed payments.",
                   "Hot inflation eats fixed coupons — the classic bond killer."),
        'ry':     ("Real yields have room to fall — price upside for duration.",
                   "Elevated real yields reflect tight policy and heavy supply — duration risk is high."),
        'liq':    ("Ample liquidity supports bids across fixed income.",
                   "Liquidity drain forces selling of the most liquid assets — Treasuries included."),
        'mom':    ("Bond uptrend — the market is already moving toward lower yields.",
                   "Bond downtrend — sellers demand ever-higher yields."),
        'fear':   ("Risk-off flight to safety bids Treasuries.",
                   "Risk-on mood — little haven demand for government paper."),
    },
    'commodity': {
        'growth': ("Strong growth means strong physical demand for raw materials.",
                   "Slowing growth cuts consumption of energy and materials directly."),
        'infl':   ("Commodities ARE the inflation trade — rising prices are the asset itself.",
                   "Disinflation removes the commodity bid."),
        'ry':     ("Low real yields cheapen the cost of holding real assets.",
                   "High real yields raise the cost of carry for non-yielding assets."),
        'liq':    ("Ample liquidity fuels speculative and industrial demand alike.",
                   "Tight liquidity cools the speculative bid for raw materials."),
        'usd':    ("Commodities are priced in dollars — a weak dollar lifts them globally.",
                   "A strong dollar makes commodities pricier for the rest of the world."),
        'mom':    ("Trend is up — physical tightness confirmed by price.",
                   "Trend is down — the market signals oversupply."),
        'fear':   ("Risk appetite supports cyclical demand.",
                   "Risk-off hits cyclical commodity demand first."),
    },
    'forex': {
        'growth': ("Soft US growth weakens the dollar — a lift for foreign currencies.",
                   "Strong US growth pulls capital toward the dollar, pressuring this currency."),
        'ry':     ("Falling US real yields narrow the dollar's rate advantage.",
                   "High US real yields attract flows to the dollar — pressure on foreign FX."),
        'liq':    ("Ample dollar liquidity softens the greenback against everything.",
                   "Dollar scarcity strengthens USD against all comers."),
        'usd':    ("This asset is effectively a short-dollar position — dollar weakness is the trade.",
                   "Dollar strength is the direct headwind — this asset is priced against it."),
        'mom':    ("The pair's trend is supportive.",
                   "The pair's trend is against it."),
        'fear':   ("Risk-on flows favour higher-beta currencies over the dollar.",
                   "Risk-off favours the dollar — the world's funding and refuge currency."),
    },
    'crypto': {
        'growth': ("Risk appetite rises with growth — crypto is a high-beta risk asset.",
                   "Growth scares hit the riskiest assets hardest."),
        'infl':   ("The debasement narrative — crypto trades as a hard-asset hedge.",
                   "Disinflation removes the monetary-debasement bid."),
        'ry':     ("Low real yields make non-yielding assets viable again.",
                   "High real yields crush non-yielding assets hardest — crypto included."),
        'liq':    ("Crypto is the purest liquidity trade — global money supply is its tide.",
                   "Liquidity drain hits crypto before anything else."),
        'usd':    ("A weak dollar pushes capital toward alternative stores of value.",
                   "A strong dollar drains speculative and emerging-asset flows."),
        'mom':    ("Momentum begets momentum in crypto — trend is the dominant factor.",
                   "Broken trend — in crypto, momentum losses compound fast."),
        'fear':   ("Calm markets let speculative appetite build.",
                   "VIX spikes hit crypto first and hardest."),
    },
    'usd_long': {
        'growth': ("US growth outperformance attracts global capital to the dollar.",
                   "Weak US data undercuts the dollar's yield and growth advantage."),
        'ry':     ("High US real yields pay dollar holders a real return.",
                   "Falling real yields erode the dollar's carry appeal."),
        'liq':    ("Liquidity drain means dollar scarcity — bullish USD.",
                   "Ample liquidity means plentiful dollars — bearish USD."),
        'usd':    ("This IS the dollar trade — structural USD strength scores directly.",
                   "Structural dollar weakness scores directly against this position."),
        'mom':    ("Dollar uptrend confirmed.",
                   "Dollar downtrend confirmed."),
        'fear':   ("The dollar is the world's risk-off refuge — fear is a bid.",
                   "Risk-on mood sends capital out of the dollar."),
    },
}


def get_why(asset_class, factor, favour):
    """Return the plain-English line explaining this factor's pull on this asset.
    Neutral favour (44-56) gets a neutral line; unsigned factors return ''."""
    lines = (WHY_LINES.get(asset_class) or {}).get(factor)
    if not lines:
        return ''
    if favour >= 57:
        return lines[0]
    if favour <= 43:
        return lines[1]
    return 'Currently near neutral — neither helping nor hurting this asset much.'
