"""
crypto.py — Crypto Layer for StockSense

Phase 1: Crypto Macro Regime + Asset Scorecards + DCA Zones
Data: Yahoo Finance (BTC-USD, ETH-USD, SOL-USD, XRP-USD, ETH-BTC)
      + existing macro pillars from rie.py
      + FRED (real yields, DXY proxy via UUP)

No paid APIs required. Builds on existing infrastructure.

Architecture:
  compute_crypto_regime(macro_snapshot)  → regime dict
  score_crypto_asset(ticker, closes, regime, macro) → scorecard dict
  compute_dca_zones(ticker, closes, ath)  → zones dict
  get_crypto_warnings(assets, regime)    → warnings list
"""

# ── Asset definitions ────────────────────────────────────────────
CRYPTO_ASSETS = {
    'BTC-USD': {'name': 'Bitcoin',  'symbol': 'BTC', 'tier': 1, 'category': 'L1'},
    'ETH-USD': {'name': 'Ethereum', 'symbol': 'ETH', 'tier': 1, 'category': 'L1'},
    'SOL-USD': {'name': 'Solana',   'symbol': 'SOL', 'tier': 2, 'category': 'L1'},
    'XRP-USD': {'name': 'XRP',      'symbol': 'XRP', 'tier': 2, 'category': 'Payment'},
}

# Cycle ATHs (approximate, for drawdown calculations)
# Updated manually — or computed dynamically from 2yr max of closes
KNOWN_ATHS = {
    'BTC-USD': 109000,
    'ETH-USD': 4900,
    'SOL-USD': 295,
    'XRP-USD': 3.40,
}

# ── Regime scoring ───────────────────────────────────────────────

CRYPTO_REGIME_LABELS = [
    (75, 'Strong Crypto Bullish', '#48d597', '#0a2a1a'),
    (60, 'Crypto Bullish',        '#60e8d0', '#0a1f1a'),
    (45, 'Neutral / Choppy',      '#f6c90e', '#1a1500'),
    (30, 'Crypto Bearish',        '#f6a93e', '#1a0f00'),
    (0,  'Strong Crypto Bearish', '#f56565', '#1a0000'),
]


def regime_label(score):
    for threshold, label, col, bg in CRYPTO_REGIME_LABELS:
        if score >= threshold:
            return label, col, bg
    return 'Strong Crypto Bearish', '#f56565', '#1a0000'


def normalise(val, bear, bull):
    """Map raw value to 0-100. bear→0, bull→100, clamped."""
    if val is None or bull == bear:
        return 50.0
    t = (val - bear) / (bull - bear)
    return max(0.0, min(100.0, t * 100.0))


def compute_crypto_regime(macro_snapshot, crypto_prices):
    """
    Score the crypto macro environment using existing macro pillars
    + crypto-specific inputs.

    macro_snapshot: output of compute_regime_snapshot() from rie.py
    crypto_prices:  {ticker: {'price', 'changePct', 'rangePos', 'week52High',
                               'week52Low', 'closes': [floats...]}}

    Returns: {score, label, color, bg, pillars, bulls, bears, summary}

    Scoring pillars (each 0-100, weighted):
      Macro Liquidity     20%  — existing liq pillar
      Risk Appetite       20%  — existing internals pillar
      Trend/Momentum      20%  — BTC trend vs 200DMA + 52w range
      Dollar/Rates        20%  — real yields + USD direction
      Crypto Internals    20%  — ETH/BTC trend, BTC dominance proxy,
                                  Fear & Greed proxy from BTC momentum
    """
    pillars = {}
    bulls = []
    bears = []

    ps = macro_snapshot.get('pillar_scores', {}) if macro_snapshot else {}

    # ── 1. Macro Liquidity (20%) ─────────────────────────────────
    liq = ps.get('liquidity', 50)
    pillars['macro_liquidity'] = liq
    if liq >= 60:
        bulls.append({'factor': 'Macro Liquidity', 'detail': f'Liquidity pillar {liq}/100 — ample conditions support risk assets', 'pts': round((liq-50)*0.20, 1)})
    elif liq <= 40:
        bears.append({'factor': 'Macro Liquidity', 'detail': f'Liquidity pillar {liq}/100 — tighter conditions weigh on crypto', 'pts': round((50-liq)*-0.20, 1)})

    # ── 2. Risk Appetite (20%) ───────────────────────────────────
    internals = ps.get('internals', 50)
    pillars['risk_appetite'] = internals
    if internals >= 65:
        bulls.append({'factor': 'Risk Appetite', 'detail': f'Market internals {internals}/100 — strong risk-on, supportive for crypto', 'pts': round((internals-50)*0.20, 1)})
    elif internals <= 40:
        bears.append({'factor': 'Risk Appetite', 'detail': f'Market internals {internals}/100 — risk-off, crypto typically underperforms', 'pts': round((50-internals)*-0.20, 1)})

    # ── 3. BTC Trend & Momentum (20%) ───────────────────────────
    btc = crypto_prices.get('BTC-USD', {})
    btc_closes = btc.get('closes', [])
    btc_score = 50.0
    btc_detail = 'BTC data unavailable'
    if btc_closes and len(btc_closes) >= 200:
        price = btc_closes[-1]
        ma200 = sum(btc_closes[-200:]) / 200
        ma50  = sum(btc_closes[-50:])  / 50
        range_pos = btc.get('rangePos', 50)
        above_200 = price > ma200
        above_50  = price > ma50
        pct_from_200 = (price - ma200) / ma200 * 100
        # 0-100 score: above 200MA and 50MA + range position
        trend_score = (
            (70 if above_200 else 30) * 0.4 +
            (65 if above_50  else 35) * 0.3 +
            range_pos * 0.3
        )
        btc_score = round(trend_score, 1)
        btc_detail = (f'BTC {"above" if above_200 else "below"} 200DMA '
                      f'({pct_from_200:+.1f}%), 52w range {range_pos:.0f}%')
        if above_200 and above_50:
            bulls.append({'factor': 'BTC Trend', 'detail': btc_detail, 'pts': round((btc_score-50)*0.20, 1)})
        elif not above_200:
            bears.append({'factor': 'BTC Trend', 'detail': btc_detail, 'pts': round((50-btc_score)*-0.20, 1)})
    elif btc_closes and len(btc_closes) >= 50:
        price = btc_closes[-1]
        ma50  = sum(btc_closes[-50:]) / 50
        range_pos = btc.get('rangePos', 50)
        btc_score = (65 if price > ma50 else 35) * 0.5 + range_pos * 0.5
        btc_detail = f'BTC {"above" if price > ma50 else "below"} 50DMA, range {range_pos:.0f}%'
    pillars['btc_trend'] = round(btc_score)

    # ── 4. Dollar & Real Yields (20%) ───────────────────────────
    # High real yields = bearish crypto (opportunity cost rises)
    # Strong DXY = bearish crypto (dollar strength drains risk assets)
    ry_raw   = macro_snapshot.get('raw_readings', {}).get('ry', 50) if macro_snapshot else 50
    usd_raw  = macro_snapshot.get('raw_readings', {}).get('usd', 50) if macro_snapshot else 50
    # For crypto: high real yields = bearish, so invert
    ry_crypto  = 100 - ry_raw     # high ry → bearish → low score
    usd_crypto = 100 - usd_raw    # strong USD → bearish → low score
    dr_score = round(ry_crypto * 0.6 + usd_crypto * 0.4)
    pillars['dollar_rates'] = dr_score
    if dr_score >= 60:
        bulls.append({'factor': 'Dollar & Rates', 'detail': f'Falling real yields and/or weak USD — supportive for crypto', 'pts': round((dr_score-50)*0.20, 1)})
    elif dr_score <= 40:
        bears.append({'factor': 'Dollar & Rates', 'detail': f'High real yields ({ry_raw:.0f}/100) and strong USD — headwind for crypto', 'pts': round((50-dr_score)*-0.20, 1)})

    # ── 5. Crypto Internals (20%) ────────────────────────────────
    # ETH/BTC trend: rising = altcoin season expanding, bullish breadth
    # BTC 30d momentum proxy for Fear & Greed
    eth_btc = crypto_prices.get('ETH-BTC', {})
    eth_btc_closes = eth_btc.get('closes', [])
    eth_btc_score = 50.0

    if eth_btc_closes and len(eth_btc_closes) >= 30:
        cur  = eth_btc_closes[-1]
        prev = eth_btc_closes[-30]
        eth_btc_mom = (cur - prev) / prev * 100 if prev else 0
        eth_btc_score = normalise(eth_btc_mom, -15, 15)

    # BTC 30d momentum as sentiment proxy
    btc_sentiment = 50.0
    if btc_closes and len(btc_closes) >= 30:
        cur30  = btc_closes[-1]
        prev30 = btc_closes[-30]
        btc_mom_30 = (cur30 - prev30) / prev30 * 100 if prev30 else 0
        btc_sentiment = normalise(btc_mom_30, -30, 30)

    crypto_internals = round(eth_btc_score * 0.5 + btc_sentiment * 0.5)
    pillars['crypto_internals'] = crypto_internals
    if crypto_internals >= 60:
        bulls.append({'factor': 'Crypto Internals', 'detail': 'ETH/BTC trending up and BTC momentum positive — broad crypto strength', 'pts': round((crypto_internals-50)*0.20, 1)})
    elif crypto_internals <= 40:
        bears.append({'factor': 'Crypto Internals', 'detail': 'ETH/BTC weak or BTC momentum negative — risk of broad crypto weakness', 'pts': round((50-crypto_internals)*-0.20, 1)})

    # ── Final score ──────────────────────────────────────────────
    weights = {
        'macro_liquidity':  0.20,
        'risk_appetite':    0.20,
        'btc_trend':        0.20,
        'dollar_rates':     0.20,
        'crypto_internals': 0.20,
    }
    score = sum(pillars[k] * w for k, w in weights.items())
    score = round(score)
    label, col, bg = regime_label(score)

    # Summary
    top_bull = bulls[0]['factor'] if bulls else None
    top_bear = bears[0]['factor'] if bears else None
    if score >= 60:
        summary = f'Crypto regime is {label.lower()}. {top_bull or "Macro conditions"} is the primary driver.'
        if top_bear:
            summary += f' {top_bear} is the main headwind.'
    elif score <= 40:
        summary = f'Crypto regime is {label.lower()}. {top_bear or "Macro conditions"} is weighing on the space.'
        if top_bull:
            summary += f' {top_bull} offers some support.'
    else:
        summary = f'Mixed signals — crypto in neutral/choppy territory. No strong directional edge.'

    return {
        'score': score,
        'label': label,
        'color': col,
        'bg':    bg,
        'pillars': pillars,
        'bulls':   sorted(bulls, key=lambda x: x.get('pts', 0), reverse=True),
        'bears':   sorted(bears, key=lambda x: x.get('pts', 0)),
        'summary': summary,
    }


# ── Asset scoring ────────────────────────────────────────────────

ASSET_WEIGHTS = {
    'macro_alignment': 25,  # regime score alignment
    'trend':           25,  # price vs 50/200 DMA
    'momentum':        20,  # 1m/3m/6m momentum blended
    'range_position':  15,  # 52w range position
    'volatility':      15,  # lower vol = higher score at same level (stability)
}

SCORE_LABELS = [
    (75, 'Strong Accumulation', '#48d597'),
    (60, 'Accumulation',        '#60e8d0'),
    (45, 'Watchlist',           '#f6c90e'),
    (30, 'Avoid',               '#f6a93e'),
    (0,  'High Risk / Spec',    '#f56565'),
]


def asset_score_label(score):
    for threshold, label, col in SCORE_LABELS:
        if score >= threshold:
            return label, col
    return 'High Risk / Spec', '#f56565'


def score_crypto_asset(ticker, closes, regime_score, macro_snapshot=None):
    """
    Score a single crypto asset 0-100.
    closes: list of daily closes, oldest first.
    Returns full scorecard dict.
    """
    if not closes or len(closes) < 10:
        return None

    price = closes[-1]
    info  = CRYPTO_ASSETS.get(ticker, {})

    # ── Macro alignment (25%) ─────────────────────────────────
    # How well does this asset benefit from the current crypto regime?
    # Tier 1 (BTC/ETH) track regime closely; Tier 2 are more volatile
    tier = info.get('tier', 2)
    regime_factor = normalise(regime_score, 20, 80)
    if tier == 2:
        # Alt amplification: alts outperform in strong bull, underperform in bear
        regime_factor = normalise(regime_score, 30, 80)
    macro_score = round(regime_factor)

    # ── Trend vs MAs (25%) ───────────────────────────────────
    trend_score = 50
    trend_detail = []
    if len(closes) >= 200:
        ma200 = sum(closes[-200:]) / 200
        ma50  = sum(closes[-50:])  / 50
        pct200 = (price - ma200) / ma200 * 100
        pct50  = (price - ma50)  / ma50  * 100
        # Score: above both MAs = strong; golden cross structure
        if price > ma200 and price > ma50:
            trend_score = min(85, 60 + pct50 * 0.5)
        elif price > ma200 and price <= ma50:
            trend_score = 52
        elif price <= ma200 and price > ma50:
            trend_score = 45
        else:
            trend_score = max(15, 40 + pct200 * 0.5)
        trend_detail = [
            f'vs 200DMA: {pct200:+.1f}%',
            f'vs 50DMA: {pct50:+.1f}%',
        ]
    elif len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        pct50 = (price - ma50) / ma50 * 100
        trend_score = min(80, max(20, 50 + pct50 * 0.8))
        trend_detail = [f'vs 50DMA: {pct50:+.1f}%']
    trend_score = round(max(0, min(100, trend_score)))

    # ── Momentum 1m/3m/6m (20%) ─────────────────────────────
    def pct_chg(n):
        if len(closes) > n and closes[-1-n]:
            return (closes[-1] - closes[-1-n]) / closes[-1-n] * 100
        return None

    m1m = pct_chg(21)
    m3m = pct_chg(63)
    m6m = pct_chg(126)

    # Calibration: crypto moves faster than equities
    n1 = normalise(m1m, -30, 30) if m1m is not None else 50
    n3 = normalise(m3m, -50, 50) if m3m is not None else 50
    n6 = normalise(m6m, -70, 70) if m6m is not None else 50
    mom_score = round(n1 * 0.5 + n3 * 0.3 + n6 * 0.2)

    # ── 52w Range Position (15%) ──────────────────────────────
    hi = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    range_pos = round((price - lo) / (hi - lo) * 100) if hi > lo else 50

    # ── Volatility adjustment (15%) ──────────────────────────
    # Lower 30d vol relative to 90d = stabilising = higher score
    if len(closes) >= 90:
        import statistics
        rets = [((closes[i]-closes[i-1])/closes[i-1])*100 for i in range(1, len(closes))]
        vol30 = statistics.stdev(rets[-30:]) if len(rets) >= 30 else 3
        vol90 = statistics.stdev(rets[-90:]) if len(rets) >= 90 else 3
        # Falling volatility = consolidating = slightly bullish
        vol_ratio = vol30 / vol90 if vol90 > 0 else 1.0
        vol_score = round(normalise(vol_ratio, 1.5, 0.5))  # low ratio = high score
    else:
        vol_score = 50

    # ── Composite ────────────────────────────────────────────
    composite = round(
        macro_score  * 0.25 +
        trend_score  * 0.25 +
        mom_score    * 0.20 +
        range_pos    * 0.15 +
        vol_score    * 0.15
    )

    status, status_col = asset_score_label(composite)

    # ── DCA status ────────────────────────────────────────────
    if composite >= 68 and regime_score >= 55:
        dca_status = 'Normal DCA'
        dca_col = '#48d597'
    elif composite >= 55 and regime_score >= 45:
        dca_status = 'Light DCA'
        dca_col = '#60e8d0'
    elif composite >= 45 and range_pos <= 30:
        dca_status = 'Reduced DCA'
        dca_col = '#f6c90e'
    elif regime_score >= 60 and range_pos <= 25:
        dca_status = 'Deep Value DCA'
        dca_col = '#60a8ff'
    else:
        dca_status = 'Pause DCA'
        dca_col = '#f56565'

    return {
        'ticker':     ticker,
        'name':       info.get('name', ticker),
        'symbol':     info.get('symbol', ticker),
        'category':   info.get('category', ''),
        'tier':       tier,
        'price':      round(price, 4) if price < 10 else round(price, 2),
        'composite':  composite,
        'status':     status,
        'status_col': status_col,
        'dca_status': dca_status,
        'dca_col':    dca_col,
        'factors': {
            'macro_alignment': {'score': macro_score,  'weight': 25, 'label': 'Macro Alignment'},
            'trend':           {'score': trend_score,  'weight': 25, 'label': 'Trend vs MAs', 'detail': trend_detail},
            'momentum':        {'score': mom_score,    'weight': 20, 'label': 'Momentum',
                                'detail': [f'1M: {m1m:+.1f}%' if m1m else '1M: —',
                                           f'3M: {m3m:+.1f}%' if m3m else '3M: —',
                                           f'6M: {m6m:+.1f}%' if m6m else '6M: —']},
            'range_position':  {'score': range_pos,   'weight': 15, 'label': '52W Range',
                                'detail': [f'{range_pos}% of 52w range']},
            'volatility':      {'score': vol_score,   'weight': 15, 'label': 'Volatility'},
        },
        'momentum_1m': round(m1m, 1) if m1m is not None else None,
        'momentum_3m': round(m3m, 1) if m3m is not None else None,
        'momentum_6m': round(m6m, 1) if m6m is not None else None,
        'range_pos':  range_pos,
    }


# ── DCA Zones ────────────────────────────────────────────────────

def compute_dca_zones(ticker, closes, regime_score):
    """
    Calculate 3 DCA zones based on technical levels + regime context.
    Returns list of zone dicts sorted from current price downward.
    """
    if not closes or len(closes) < 50:
        return []

    price = closes[-1]
    hi52  = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo52  = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    ath   = KNOWN_ATHS.get(ticker, hi52 * 1.1)

    ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
    ma50  = sum(closes[-50:])  / 50

    # Drawdown from ATH
    dd_from_ath = (ath - price) / ath * 100

    # Fibonacci retracements from ATH to 52w low
    swing_high = ath
    swing_low  = lo52
    fib_range  = swing_high - swing_low

    fib_618 = swing_high - fib_range * 0.618
    fib_786 = swing_high - fib_range * 0.786
    fib_500 = swing_high - fib_range * 0.500

    # Zone definitions
    zones = []

    # Zone 1: Light accumulation — pullback to 50DMA or Fib 38.2%
    fib_382 = swing_high - fib_range * 0.382
    z1_price = max(fib_382, ma50 * 0.97) if ma50 else fib_382
    z1_price = round(z1_price, 2) if z1_price > 10 else round(z1_price, 4)

    zones.append({
        'zone': 1,
        'label': 'Zone 1 — Light Accumulation',
        'color': '#60e8d0',
        'price_level': z1_price,
        'drawdown_from_ath': round((ath - z1_price) / ath * 100, 1),
        'intensity': 'Light (25% of normal allocation)',
        'technical': f'Near 50DMA / Fib 38.2% retracement',
        'macro_condition': 'Valid when regime ≥ 45',
        'active': regime_score >= 45 and price > z1_price * 0.95,
        'valid': regime_score >= 45,
        'invalidated_if': '50DMA breaks down with regime turning bearish',
    })

    # Zone 2: Strong accumulation — 200DMA or Fib 61.8%
    z2_price = max(fib_618, (ma200 * 0.98) if ma200 else fib_618)
    z2_price = round(z2_price, 2) if z2_price > 10 else round(z2_price, 4)

    zones.append({
        'zone': 2,
        'label': 'Zone 2 — Strong Accumulation',
        'color': '#48d597',
        'price_level': z2_price,
        'drawdown_from_ath': round((ath - z2_price) / ath * 100, 1),
        'intensity': 'Strong (50% of normal allocation)',
        'technical': f'Near 200DMA / Fib 61.8% — historically strong support',
        'macro_condition': 'Valid in any regime — long-term accumulation zone',
        'active': price > z2_price * 0.95,
        'valid': True,
        'invalidated_if': 'Macro regime goes Strong Bearish — wait for stabilisation',
    })

    # Zone 3: Maximum opportunity — deep capitulation zone
    z3_price = min(fib_786, lo52 * 1.05)
    z3_price = round(z3_price, 2) if z3_price > 10 else round(z3_price, 4)

    zones.append({
        'zone': 3,
        'label': 'Zone 3 — Maximum Opportunity',
        'color': '#60a8ff',
        'price_level': z3_price,
        'drawdown_from_ath': round((ath - z3_price) / ath * 100, 1),
        'intensity': 'Aggressive (100% of normal allocation)',
        'technical': f'Fib 78.6% / near cycle lows — capitulation zone',
        'macro_condition': 'Best when macro regime starts recovering from bearish',
        'active': price <= z3_price * 1.10,
        'valid': True,
        'invalidated_if': 'Complete macro breakdown — deploy in tranches only',
    })

    # Invalid zone (do not DCA)
    zones.append({
        'zone': 0,
        'label': '⚠ Do Not DCA Zone',
        'color': '#f56565',
        'price_level': None,
        'condition': 'Active when: regime score < 30 AND price below all MAs AND 52w range < 15%',
        'trigger': regime_score < 30,
        'active': regime_score < 30,
        'reason': 'Strong Bearish regime — wait for macro stabilisation before accumulating',
    })

    # Current zone assessment
    current_zone = None
    if price >= z1_price * 0.97:
        current_zone = 'Above Zone 1 — monitor for pullback'
    elif price >= z2_price * 0.97:
        current_zone = 'Zone 1 active'
    elif price >= z3_price * 0.97:
        current_zone = 'Zone 2 active — good accumulation level'
    else:
        current_zone = 'Zone 3 active — maximum opportunity'

    return {
        'ticker':       ticker,
        'current_price': round(price, 2) if price > 10 else round(price, 4),
        'ath':          ath,
        'dd_from_ath':  round(dd_from_ath, 1),
        'ma200':        round(ma200, 2) if ma200 and ma200 > 10 else (round(ma200, 4) if ma200 else None),
        'ma50':         round(ma50, 2) if ma50 > 10 else round(ma50, 4),
        'current_zone': current_zone,
        'zones':        zones,
    }


# ── Risk Warnings ────────────────────────────────────────────────

def get_crypto_warnings(asset_scores, regime, macro_snapshot=None):
    """
    Generate risk warnings based on current conditions.
    Returns list of {type, severity, message, action}.
    """
    warnings = []
    regime_score = regime.get('score', 50)
    ps = macro_snapshot.get('pillar_scores', {}) if macro_snapshot else {}

    # Macro headwinds
    ry_raw  = macro_snapshot.get('raw_readings', {}).get('ry', 50) if macro_snapshot else 50
    usd_raw = macro_snapshot.get('raw_readings', {}).get('usd', 50) if macro_snapshot else 50

    if ry_raw >= 75:
        warnings.append({
            'type': 'MACRO',
            'severity': 'HIGH',
            'icon': '📈',
            'title': 'Very High Real Yields',
            'message': f'10Y real yield at {ry_raw:.0f}/100 — elevated opportunity cost for non-yielding assets. Historically bearish for crypto.',
            'action': 'Reduce allocation size. Prioritise BTC over alts.',
        })

    if usd_raw >= 70:
        warnings.append({
            'type': 'MACRO',
            'severity': 'MEDIUM',
            'icon': '💵',
            'title': 'Strong Dollar Pressure',
            'message': 'USD strength is a headwind for crypto — dollar strength typically accompanies risk-off conditions.',
            'action': 'Wait for DXY to peak/roll over before increasing crypto exposure.',
        })

    # Regime warnings
    if regime_score <= 35:
        warnings.append({
            'type': 'REGIME',
            'severity': 'HIGH',
            'icon': '🔴',
            'title': 'Bearish Crypto Regime',
            'message': f'Crypto macro regime score {regime_score}/100 — conditions do not favour new positions.',
            'action': 'Pause DCA. Hold stablecoins. Wait for regime to recover above 45.',
        })

    if regime_score >= 75:
        warnings.append({
            'type': 'REGIME',
            'severity': 'LOW',
            'icon': '⚠',
            'title': 'Strong Bullish — Watch for Overextension',
            'message': 'Strong crypto regime can precede sentiment extremes. Check if price is extended from key MAs.',
            'action': 'Normal DCA acceptable but avoid chasing breakouts. Take partial profits at resistance.',
        })

    # Asset-specific
    btc_score = next((a['composite'] for a in asset_scores if a.get('ticker') == 'BTC-USD'), None)
    eth_score = next((a['composite'] for a in asset_scores if a.get('ticker') == 'ETH-USD'), None)

    if btc_score and eth_score and eth_score < btc_score - 20:
        warnings.append({
            'type': 'ROTATION',
            'severity': 'MEDIUM',
            'icon': '🔄',
            'title': 'BTC Dominance Likely Rising',
            'message': f'ETH scoring {eth_score} vs BTC {btc_score} — ETH/BTC diverging. Capital may be rotating back to BTC dominance.',
            'action': 'Reduce ETH and alt exposure. Favour BTC until ETH/BTC trend stabilises.',
        })

    # Range position warnings (overextended)
    for a in asset_scores:
        if a.get('range_pos', 0) >= 90 and a.get('momentum_3m', 0) and a['momentum_3m'] > 50:
            warnings.append({
                'type': 'OVEREXTENDED',
                'severity': 'MEDIUM',
                'icon': '🚀',
                'title': f'{a["symbol"]} Overextended',
                'message': f'{a["symbol"]} at {a["range_pos"]}% of 52w range with +{a["momentum_3m"]:.0f}% 3M momentum — high risk of pullback.',
                'action': 'Do not add at these levels. Wait for pullback to DCA zones.',
            })

    # Deep drawdown opportunity
    for a in asset_scores:
        if a.get('range_pos', 100) <= 20 and regime_score >= 45:
            warnings.append({
                'type': 'OPPORTUNITY',
                'severity': 'LOW',
                'icon': '💎',
                'title': f'{a["symbol"]} Near Cycle Lows',
                'message': f'{a["symbol"]} at only {a["range_pos"]}% of its 52w range — potential deep value zone.',
                'action': 'Consider light accumulation if macro regime confirms. Check DCA zones.',
            })

    # Sort by severity
    sev_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    warnings.sort(key=lambda w: sev_order.get(w['severity'], 3))
    return warnings
