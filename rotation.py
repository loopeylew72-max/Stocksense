"""
rotation.py — Theme Rotation Radar / Capital Flow Detection Engine.

Three levels:
  1. Sector Rotation  — the 11 GICS sector ETFs
  2. Theme Rotation   — ~20 investable theme baskets (AI, nuclear, defence, etc.)
  3. Capital Flow     — momentum, relative-strength delta, breadth, macro fit,
                        news sentiment, crowding — blended into a single
                        Theme Rotation Score with a status label.

This module is intentionally self-contained: it takes price/MA data and a
macro regime label as inputs (fetched by server.py via the existing
get_live_price / get_moving_averages / rie helpers) and returns ranked,
delta-aware theme snapshots. Persistence (for rank-delta-over-time) goes
through store.record_indicators_bulk / store.get_series — the same generic
time-series API already used for COT data — so no new DB schema is required.

Design discipline (matches the rest of StockSense):
  - Never fabricate. If a component lacks data, it's None and excluded from
    the weighted blend (renormalised over what's present), and surfaced via
    'data_coverage'.
  - Smoothing: rank deltas use a short rolling average to avoid single-day
    whipsaw, computed from stored history.
  - Status labels require CONFIRMATION across multiple signals, not a single
    metric spike (see classify_status).
"""

import time

# ══════════════════════════════════════════════════════════════════
# 1 · SECTOR ROTATION — the 11 GICS sector ETFs
# ══════════════════════════════════════════════════════════════════
SECTORS = {
    'technology':            {'name': 'Technology',             'etf': 'XLK'},
    'financials':            {'name': 'Financials',             'etf': 'XLF'},
    'energy':                {'name': 'Energy',                 'etf': 'XLE'},
    'industrials':           {'name': 'Industrials',            'etf': 'XLI'},
    'consumer_discretionary':{'name': 'Consumer Discretionary', 'etf': 'XLY'},
    'consumer_staples':      {'name': 'Consumer Staples',       'etf': 'XLP'},
    'healthcare':            {'name': 'Healthcare',             'etf': 'XLV'},
    'utilities':             {'name': 'Utilities',              'etf': 'XLU'},
    'materials':             {'name': 'Materials',              'etf': 'XLB'},
    'real_estate':           {'name': 'Real Estate',            'etf': 'XLRE'},
    'communication_services':{'name': 'Communication Services','etf': 'XLC'},
}

# ══════════════════════════════════════════════════════════════════
# 2 · THEME ROTATION — investable theme baskets
# Each theme: a representative ETF (fast momentum/RS proxy, may be None if
# no clean single-ETF wrapper exists) + 5-9 constituent stocks (for breadth).
# Tickers chosen for liquidity and theme-purity, not exhaustiveness.
# ══════════════════════════════════════════════════════════════════
THEMES = {
    'ai_software': {
        'name': 'AI / Software', 'category': 'tech', 'etf': None,
        'tickers': ['MSFT', 'GOOGL', 'META', 'ORCL', 'PLTR', 'CRM', 'NOW', 'SNOW'],
    },
    'semiconductors': {
        'name': 'Semiconductors', 'category': 'tech', 'etf': 'SMH',
        'tickers': ['NVDA', 'AMD', 'AVGO', 'TSM', 'ASML', 'MU', 'LRCX', 'KLAC'],
    },
    'data_centres': {
        'name': 'Data Centres', 'category': 'tech', 'etf': None,
        'tickers': ['VRT', 'DLR', 'EQIX', 'ETN', 'CEG', 'VST', 'MOD'],
    },
    'power_grid': {
        'name': 'Power Grid / Electrification', 'category': 'energy', 'etf': 'GRID',
        'tickers': ['ETN', 'GEV', 'PWR', 'HUBB', 'POWL', 'NVT'],
    },
    'nuclear_uranium': {
        'name': 'Nuclear / Uranium', 'category': 'energy', 'etf': 'URA',
        'tickers': ['CCJ', 'UEC', 'SMR', 'OKLO', 'CEG', 'VST', 'LEU'],
    },
    'defence': {
        'name': 'Defence', 'category': 'industrials', 'etf': 'ITA',
        'tickers': ['LMT', 'RTX', 'NOC', 'GD', 'LHX', 'HII', 'TDG'],
    },
    'cybersecurity': {
        'name': 'Cybersecurity', 'category': 'tech', 'etf': 'CIBR',
        'tickers': ['CRWD', 'PANW', 'FTNT', 'ZS', 'NET', 'S'],
    },
    'robotics_automation': {
        'name': 'Robotics / Automation', 'category': 'industrials', 'etf': 'BOTZ',
        'tickers': ['ISRG', 'ROK', 'ABB', 'FANUY', 'TER', 'PATH'],
    },
    'quantum_computing': {
        'name': 'Quantum Computing', 'category': 'tech', 'etf': 'QTUM',
        'tickers': ['IONQ', 'RGTI', 'QBTS', 'IBM', 'NVDA'],
    },
    'biotech': {
        'name': 'Biotech', 'category': 'healthcare', 'etf': 'XBI',
        'tickers': ['VRTX', 'REGN', 'AMGN', 'GILD', 'BIIB', 'MRNA'],
    },
    'glp1_weightloss': {
        'name': 'GLP-1 / Weight Loss', 'category': 'healthcare', 'etf': None,
        'tickers': ['LLY', 'NVO', 'AMGN', 'VKTX', 'RHHBY'],
    },
    'gold_miners': {
        'name': 'Gold Miners', 'category': 'materials', 'etf': 'GDX',
        'tickers': ['NEM', 'GOLD', 'AEM', 'KGC', 'AU', 'WPM'],
    },
    'copper_critical_metals': {
        'name': 'Copper / Critical Metals', 'category': 'materials', 'etf': 'COPX',
        'tickers': ['FCX', 'SCCO', 'TECK', 'ALB', 'MP'],
    },
    'oil_gas': {
        'name': 'Oil & Gas', 'category': 'energy', 'etf': 'XOP',
        'tickers': ['XOM', 'CVX', 'COP', 'EOG', 'SLB', 'OXY'],
    },
    'banks': {
        'name': 'Banks', 'category': 'financials', 'etf': 'KBE',
        'tickers': ['JPM', 'BAC', 'WFC', 'GS', 'MS', 'C'],
    },
    'small_caps': {
        'name': 'Small Caps', 'category': 'broad', 'etf': 'IWM',
        'tickers': [],  # breadth not meaningful for a 2000-name index; ETF-only
    },
    'homebuilders': {
        'name': 'Homebuilders', 'category': 'consumer_discretionary', 'etf': 'XHB',
        'tickers': ['DHI', 'LEN', 'PHM', 'NVR', 'TOL', 'BLDR'],
    },
    'crypto_infrastructure': {
        'name': 'Crypto Infrastructure', 'category': 'tech', 'etf': None,
        'tickers': ['COIN', 'MSTR', 'MARA', 'RIOT', 'HOOD'],
    },
    'cloud_software': {
        'name': 'Cloud Software', 'category': 'tech', 'etf': 'WCLD',
        'tickers': ['CRM', 'NOW', 'SNOW', 'WDAY', 'DDOG', 'HUBS'],
    },
    'infrastructure': {
        'name': 'Infrastructure', 'category': 'industrials', 'etf': 'PAVE',
        'tickers': ['CAT', 'DE', 'VMC', 'MLM', 'URI', 'NUE'],
    },
    'clean_energy': {
        'name': 'Clean Energy', 'category': 'energy', 'etf': 'ICLN',
        'tickers': ['FSLR', 'ENPH', 'NEE', 'RUN', 'BE'],
    },
}

# ══════════════════════════════════════════════════════════════════
# 3 · MACRO REGIME ALIGNMENT MAP
# Derived from the 11-regime framework. Scores are 0-100 favourability
# for each theme under each regime label. Where a theme has no strong
# historical regime relationship, it's omitted and macro_alignment falls
# back to neutral (50) for that theme.
#
# Regime labels match rie.py's overall regime classification:
#   'early_cycle', 'mid_cycle', 'late_cycle', 'stagflation', 'recession',
#   'goldilocks' (low inflation + strong growth), 'reflation'
# ══════════════════════════════════════════════════════════════════
THEME_REGIME_FIT = {
    'nuclear_uranium':       {'late_cycle': 80, 'stagflation': 70, 'reflation': 65, 'early_cycle': 40, 'recession': 30, 'mid_cycle': 60, 'goldilocks': 55},
    'small_caps':            {'early_cycle': 90, 'mid_cycle': 70, 'goldilocks': 75, 'late_cycle': 30, 'recession': 10, 'stagflation': 25, 'reflation': 60},
    'gold_miners':           {'stagflation': 90, 'recession': 70, 'reflation': 60, 'early_cycle': 30, 'late_cycle': 60, 'mid_cycle': 40, 'goldilocks': 25},
    'banks':                 {'early_cycle': 85, 'mid_cycle': 70, 'goldilocks': 75, 'late_cycle': 40, 'recession': 20, 'stagflation': 30, 'reflation': 55},
    'defence':               {'late_cycle': 70, 'stagflation': 60, 'recession': 50, 'early_cycle': 40, 'mid_cycle': 55, 'goldilocks': 45, 'reflation': 55},
    'homebuilders':          {'early_cycle': 85, 'mid_cycle': 65, 'goldilocks': 70, 'late_cycle': 25, 'recession': 15, 'stagflation': 20, 'reflation': 50},
    'semiconductors':        {'early_cycle': 75, 'mid_cycle': 80, 'goldilocks': 85, 'late_cycle': 50, 'recession': 25, 'stagflation': 35, 'reflation': 55},
    'ai_software':           {'early_cycle': 70, 'mid_cycle': 80, 'goldilocks': 85, 'late_cycle': 55, 'recession': 30, 'stagflation': 35, 'reflation': 50},
    'oil_gas':               {'reflation': 85, 'stagflation': 75, 'late_cycle': 65, 'mid_cycle': 55, 'early_cycle': 40, 'recession': 25, 'goldilocks': 35},
    'copper_critical_metals':{'reflation': 85, 'early_cycle': 70, 'mid_cycle': 65, 'stagflation': 55, 'late_cycle': 50, 'recession': 20, 'goldilocks': 50},
    'clean_energy':          {'early_cycle': 65, 'goldilocks': 65, 'mid_cycle': 55, 'late_cycle': 35, 'recession': 25, 'stagflation': 30, 'reflation': 45},
    'biotech':                {'recession': 65, 'late_cycle': 60, 'mid_cycle': 55, 'early_cycle': 45, 'stagflation': 45, 'goldilocks': 55, 'reflation': 50},
    'consumer_staples':       {'recession': 75, 'stagflation': 65, 'late_cycle': 60, 'early_cycle': 30, 'mid_cycle': 40, 'goldilocks': 35, 'reflation': 45},
    'utilities':              {'recession': 70, 'stagflation': 60, 'late_cycle': 65, 'early_cycle': 30, 'mid_cycle': 40, 'goldilocks': 30, 'reflation': 40},
}

_REGIME_LABELS = ('early_cycle', 'mid_cycle', 'late_cycle', 'stagflation',
                  'recession', 'goldilocks', 'reflation')


def score_macro_fit(theme_key, regime_label):
    """0-100 favourability of `theme_key` under `regime_label`. Neutral (50)
    if no historical relationship is mapped, or if regime_label unrecognised."""
    fit = THEME_REGIME_FIT.get(theme_key)
    if not fit or regime_label not in fit:
        return 50.0
    return float(fit[regime_label])


def cycle_phase_from_pillars(pillar_scores):
    """Heuristic mapping from rie.py's 5 pillar scores (0-100, 'economic',
    'liquidity', 'internals', 'price', 'sentiment') to one of the 7
    THEME_REGIME_FIT cycle-phase labels. This is an APPROXIMATION — the
    pillars measure current conditions, not the textbook business-cycle
    phase, so treat this as a directional input to macro_alignment, not a
    precise regime call. economic pillar stands in for growth momentum;
    liquidity pillar stands in for the rates/liquidity backdrop.

      economic high + liquidity high  -> goldilocks (growth ok, easy policy)
      economic high + liquidity low   -> early_cycle / reflation (growth
                                          picking up against tightening)
      economic low  + liquidity low   -> stagflation (weak growth, tight policy)
      economic low  + liquidity high  -> late_cycle / recession risk (policy
                                          already easing in response to weakness)
    Mid-range readings on both -> mid_cycle.
    """
    eco = pillar_scores.get('economic', 50)
    liq = pillar_scores.get('liquidity', 50)

    eco_hi, eco_lo = eco >= 60, eco <= 40
    liq_hi, liq_lo = liq >= 60, liq <= 40

    if eco_hi and liq_hi:
        return 'goldilocks'
    if eco_hi and liq_lo:
        return 'reflation'
    if eco_lo and liq_lo:
        return 'stagflation'
    if eco_lo and liq_hi:
        return 'late_cycle' if eco <= 30 else 'recession'
    return 'mid_cycle'


# ══════════════════════════════════════════════════════════════════
# Helpers — pure functions, no I/O. server.py supplies the data.
# ══════════════════════════════════════════════════════════════════

def pct_change(series, lookback_days):
    """% change between the last point and the point `lookback_days` of
    trading days back. `series` is a list of {'date','value'} dicts, oldest
    first (same shape as get_fred_series). Returns None if insufficient data."""
    if not series or len(series) < 2:
        return None
    lb = min(lookback_days, len(series) - 1)
    cur = series[-1]['value']
    past = series[-1 - lb]['value']
    if not past:
        return None
    return (cur - past) / past * 100.0


def normalise(val, bear, bull):
    """Map a raw reading to 0-100. bear→0, bull→100, clamped. Same convention
    as scoring.py's nz()."""
    if val is None or bull == bear:
        return 50.0
    t = (val - bear) / (bull - bear)
    return max(0.0, min(100.0, t * 100.0))


def blend(parts, weights):
    """Weighted average over (value, weight) pairs, skipping None values and
    renormalising over what's present. Returns (blended_value_or_None,
    n_present, n_total)."""
    n_total = len(parts)
    present = [(v, w) for v, w in zip(parts, weights) if v is not None]
    if not present:
        return None, 0, n_total
    tw = sum(w for _, w in present)
    if tw == 0:
        return None, len(present), n_total
    val = sum(v * w for v, w in present) / tw
    return val, len(present), n_total


def compute_momentum_score(mom_1m, mom_3m, mom_6m):
    """Blend 1m/3m/6m % momentum into a 0-100 score. Recent weighted higher.
    Calibration: +/-15% over the relevant window maps to 0/100 (asset-class-
    agnostic; sector/theme ETFs commonly move 10-20% over a quarter)."""
    norm = lambda m: normalise(m, -15.0, 15.0) if m is not None else None
    val, n, total = blend(
        [norm(mom_1m), norm(mom_3m), norm(mom_6m)],
        [0.5, 0.3, 0.2],
    )
    return val, n, total


def compute_breadth_score(b20, b50, b200, new_highs_pct):
    """Blend % of basket above 20/50/200 DMA + % making new 20d highs into a
    single 0-100 breadth score. All inputs already 0-100 percentages."""
    val, n, total = blend(
        [b20, b50, b200, new_highs_pct],
        [0.30, 0.30, 0.20, 0.20],
    )
    return val, n, total


def compute_relative_strength(theme_mom_3m, spy_mom_3m):
    """RS = theme 3m momentum minus SPY 3m momentum, in percentage points.
    Positive = outperforming. Returns the raw pp figure (not 0-100) — used
    both as a display figure and as an input to flow acceleration."""
    if theme_mom_3m is None or spy_mom_3m is None:
        return None
    return theme_mom_3m - spy_mom_3m


def compute_flow_acceleration(rs_now, rs_4w_ago, rank_now, rank_4w_ago):
    """0-100 score capturing whether capital flow into this theme is
    ACCELERATING (early-detection signal). Two components, averaged:
      - RS delta: is relative strength vs SPY improving? (+/-10pp -> 0/100)
      - Rank delta: has the theme's rank improved over 4 weeks?
        (rank_4w_ago - rank_now), positive = climbing. +/-10 ranks -> 0/100.
    Either component may be None if history is unavailable (engine just
    started collecting snapshots); renormalises over what's present."""
    rs_delta = None
    if rs_now is not None and rs_4w_ago is not None:
        rs_delta = normalise(rs_now - rs_4w_ago, -10.0, 10.0)

    rank_delta_score = None
    if rank_now is not None and rank_4w_ago is not None:
        rank_delta = rank_4w_ago - rank_now  # positive = climbing (better rank = lower number)
        rank_delta_score = normalise(rank_delta, -10.0, 10.0)

    val, n, total = blend([rs_delta, rank_delta_score], [0.5, 0.5])
    return val, n, total


def compute_crowding_score(rs_now, rs_percentile_2y):
    """0-100 crowding penalty. Higher = more extended/crowded.
    Primarily driven by where current RS sits in its own 2-year percentile
    distribution (requires stored history — None if unavailable, in which
    case crowding defaults to a neutral 30 so it doesn't unfairly penalise
    themes the engine has only just started tracking)."""
    if rs_percentile_2y is None:
        return 30.0, False  # (score, has_data)
    return float(rs_percentile_2y), True


# ══════════════════════════════════════════════════════════════════
# Status classification — requires CONFIRMATION across signals, not a
# single metric spike. See framework doc for the full decision table.
# ══════════════════════════════════════════════════════════════════

STATUS_LABELS = (
    'Dominant Leader', 'Mature Leader', 'Emerging Rotation',
    'Early Accumulation', 'Confirmed Rotation', 'Crowded / Extended',
    'Losing Momentum', 'Regime Divergent', 'Stable', 'Avoid / Weak',
    'Insufficient Data',
)


def classify_status(snap):
    """snap is a dict with keys: theme_score, momentum_score, flow_accel_score,
    breadth_score, macro_alignment, crowding_score, rank_now, rank_delta_4w
    (positive = improving rank), momentum_1m, momentum_3m.
    All scores 0-100 or None. Returns one of STATUS_LABELS."""
    score   = snap.get('theme_score')
    if score is None:
        return 'Insufficient Data'

    flow    = snap.get('flow_accel_score')
    breadth = snap.get('breadth_score')
    macro   = snap.get('macro_alignment')
    crowd   = snap.get('crowding_score')
    delta4w = snap.get('rank_delta_4w')
    mom1m   = snap.get('momentum_1m')
    mom3m   = snap.get('momentum_3m')

    def ge(v, t): return v is not None and v >= t
    def le(v, t): return v is not None and v <= t

    # Crowding overrides leadership claims when extreme, regardless of score.
    if ge(crowd, 80) and ge(score, 55):
        return 'Crowded / Extended'

    # Strong score + confirming flow + confirming breadth = genuinely dominant.
    if ge(score, 70) and ge(flow, 55) and ge(breadth, 55):
        return 'Dominant Leader'

    # High score but breadth/flow not confirming -> ageing leadership.
    if ge(score, 70):
        if delta4w is not None and delta4w < 0:
            return 'Losing Momentum'
        return 'Mature Leader'

    # Rank climbing fast + flow confirming = the headline "early detection" case.
    if delta4w is not None and delta4w >= 5 and ge(flow, 50):
        if ge(score, 60):
            return 'Confirmed Rotation'
        return 'Emerging Rotation'

    # Rank climbing, momentum just turning (1m stronger than 3m) but breadth
    # not yet confirming -> very early stage.
    if (delta4w is not None and delta4w >= 3 and mom1m is not None
            and mom3m is not None and mom1m > mom3m and le(breadth, 50)):
        return 'Early Accumulation'

    # Was doing fine, now sliding.
    if delta4w is not None and delta4w <= -5 and ge(score, 50):
        return 'Losing Momentum'

    # Tape says yes but the macro backdrop disagrees — flag, don't suppress.
    if le(macro, 30) and ge(score, 50):
        return 'Regime Divergent'

    # Moderate-to-decent score with no confirming/deteriorating rotation
    # signal — neither leading nor weak, just holding position.
    if ge(score, 45):
        return 'Stable'

    return 'Avoid / Weak'


# ══════════════════════════════════════════════════════════════════
# Persistence keys — stored via store.record_indicators_bulk /
# store.get_series, same generic API as COT data. One series per
# (theme_key, metric).
# ══════════════════════════════════════════════════════════════════

def series_key(theme_key, metric):
    """e.g. 'rotation_nuclear_uranium_rank' / 'rotation_nuclear_uranium_rs'."""
    return f'rotation_{theme_key}_{metric}'


def all_theme_keys():
    return list(THEMES.keys()) + list(SECTORS.keys())


def _basket_cfg(key):
    """Look up a theme or sector config by key, normalised to a common shape:
    {name, etf, tickers, category}."""
    if key in THEMES:
        c = THEMES[key]
        return {'name': c['name'], 'etf': c.get('etf'), 'tickers': c.get('tickers', []),
                'category': c.get('category', 'theme')}
    if key in SECTORS:
        c = SECTORS[key]
        return {'name': c['name'], 'etf': c.get('etf'), 'tickers': [], 'category': 'sector'}
    return None


def compute_theme_momentum(closes_by_ticker, cfg):
    """Given a {ticker: [closes...]} dict for this theme's etf + constituents,
    compute basket-average 1m/3m/6m momentum. Uses the ETF if available
    (cleaner single-instrument momentum), else the constituent average."""
    series = []
    if cfg['etf'] and closes_by_ticker.get(cfg['etf']):
        series = [closes_by_ticker[cfg['etf']]]
    else:
        series = [closes_by_ticker[t] for t in cfg['tickers'] if closes_by_ticker.get(t)]
    if not series:
        return None, None, None

    def avg_pct_change(lb):
        vals = []
        for closes in series:
            if len(closes) > lb:
                cur, past = closes[-1], closes[-1 - lb]
                if past:
                    vals.append((cur - past) / past * 100.0)
        return sum(vals) / len(vals) if vals else None

    return avg_pct_change(21), avg_pct_change(63), avg_pct_change(126)


def compute_theme_breadth(closes_by_ticker, cfg):
    """% of constituent tickers above their 20/50/200-day SMA, and % making a
    new 20-day high. Returns (b20, b50, b200, new_highs_pct) each 0-100, or
    (None,None,None,None) if no constituents have data (e.g. small_caps,
    which is ETF-only by design)."""
    tickers = [t for t in cfg['tickers'] if closes_by_ticker.get(t)]
    if not tickers:
        return None, None, None, None

    above20 = above50 = above200 = new_hi = 0
    n200 = 0  # only count toward the 200d stat if enough history exists
    for t in tickers:
        closes = closes_by_ticker[t]
        price = closes[-1]
        if len(closes) >= 20:
            if price > sum(closes[-20:]) / 20:
                above20 += 1
            if price >= max(closes[-20:]):
                new_hi += 1
        if len(closes) >= 50:
            if price > sum(closes[-50:]) / 50:
                above50 += 1
        if len(closes) >= 200:
            n200 += 1
            if price > sum(closes[-200:]) / 200:
                above200 += 1

    n = len(tickers)
    b20  = above20 / n * 100.0 if n else None
    b50  = above50 / n * 100.0 if n else None
    b200 = above200 / n200 * 100.0 if n200 else None
    new_highs_pct = new_hi / n * 100.0 if n else None
    return b20, b50, b200, new_highs_pct


def build_theme_snapshot(theme_key, closes_by_ticker, spy_closes, regime_label,
                          news_sentiment=None, history=None):
    """Build one theme's snapshot for TODAY. `history` is an optional dict
    {'rank_1w_ago': int|None, 'rank_4w_ago': int|None, 'rs_4w_ago': float|None,
     'rs_percentile_2y': float|None} sourced from store.get_series /
     percentile_rank by server.py — kept out of this module to avoid a hard
     dependency on store.

    Returns a snapshot dict WITHOUT rank_now / theme_score (those require
    cross-theme comparison — see rank_and_score below). Includes
    'data_coverage' as 'X of Y signals live'.
    """
    cfg = _basket_cfg(theme_key)
    if cfg is None:
        return None
    history = history or {}

    mom_1m, mom_3m, mom_6m = compute_theme_momentum(closes_by_ticker, cfg)
    momentum_score, mom_n, mom_total = compute_momentum_score(mom_1m, mom_3m, mom_6m)

    b20, b50, b200, new_hi = compute_theme_breadth(closes_by_ticker, cfg)
    breadth_score, b_n, b_total = compute_breadth_score(b20, b50, b200, new_hi)

    spy_mom_3m = None
    if spy_closes and len(spy_closes) > 63 and spy_closes[-64]:
        spy_mom_3m = (spy_closes[-1] - spy_closes[-64]) / spy_closes[-64] * 100.0
    rs_now = compute_relative_strength(mom_3m, spy_mom_3m)

    macro_alignment = score_macro_fit(theme_key, regime_label)

    # Flow acceleration needs prior rank + prior RS — both come from history.
    rank_4w_ago = history.get('rank_4w_ago')
    rs_4w_ago = history.get('rs_4w_ago')
    flow_accel_score, flow_n, flow_total = compute_flow_acceleration(
        rs_now, rs_4w_ago, rank_now=None, rank_4w_ago=rank_4w_ago)
    # rank_now isn't known yet at this stage — flow_accel here only reflects
    # the RS-delta half; rank-delta half is folded in during rank_and_score.

    crowding_score, has_crowd_data = compute_crowding_score(rs_now, history.get('rs_percentile_2y'))

    # Component presence count for data_coverage (6 components total)
    components = [momentum_score, flow_accel_score, breadth_score,
                   macro_alignment, news_sentiment,
                   crowding_score if has_crowd_data else None]
    n_live = sum(1 for c in components if c is not None)

    return {
        'theme_key': theme_key,
        'name': cfg['name'],
        'category': cfg['category'],
        'momentum_1m': round(mom_1m, 2) if mom_1m is not None else None,
        'momentum_3m': round(mom_3m, 2) if mom_3m is not None else None,
        'momentum_6m': round(mom_6m, 2) if mom_6m is not None else None,
        'momentum_score': round(momentum_score) if momentum_score is not None else None,
        'rs_vs_spy': round(rs_now, 2) if rs_now is not None else None,
        'breadth_20d': round(b20, 1) if b20 is not None else None,
        'breadth_50d': round(b50, 1) if b50 is not None else None,
        'breadth_200d': round(b200, 1) if b200 is not None else None,
        'new_highs_pct': round(new_hi, 1) if new_hi is not None else None,
        'breadth_score': round(breadth_score) if breadth_score is not None else None,
        'macro_alignment': round(macro_alignment) if macro_alignment is not None else None,
        'news_sentiment': round(news_sentiment) if news_sentiment is not None else None,
        'flow_accel_score': round(flow_accel_score) if flow_accel_score is not None else None,
        'crowding_score': round(crowding_score) if has_crowd_data and crowding_score is not None else None,
        'rank_1w_ago': history.get('rank_1w_ago'),
        'rank_4w_ago': history.get('rank_4w_ago'),
        'data_coverage': f'{n_live} of 6 signals live',
    }


def rank_and_score(snapshots):
    """Second pass over a {theme_key: snapshot} dict (from build_theme_snapshot):
      1. Rank by momentum_score (provisional, for rank-delta-vs-flow purposes
         this isn't needed — final ranking is by theme_score below).
      2. Fold the rank-delta half of flow_acceleration in now that rank_4w_ago
         is known per-snapshot but rank_now requires this cross-theme pass.
      3. Compute theme_score (weighted blend) and final rank_now.
      4. Compute rank_delta_4w / rank_delta_1w.
      5. Classify status.
    Mutates and returns `snapshots`."""
    # Provisional theme_score using flow_accel as computed so far (RS-delta
    # component only), so we can establish a first-pass rank to compute the
    # rank-delta component of flow_acceleration.
    def provisional_score(s):
        val, _, _ = blend(
            [s['momentum_score'], s['flow_accel_score'], s['breadth_score'],
             s['macro_alignment'], s['news_sentiment'], s['crowding_score']],
            [0.25, 0.25, 0.20, 0.15, 0.10, -0.05],
        )
        return val if val is not None else 0.0

    provisional = sorted(snapshots.items(), key=lambda kv: provisional_score(kv[1]), reverse=True)
    prov_rank = {k: i + 1 for i, (k, _) in enumerate(provisional)}

    # Re-run flow_acceleration including the rank-delta component now that
    # provisional rank_now is known.
    for key, s in snapshots.items():
        rs_now = s['rs_vs_spy']
        rs_4w_ago = None  # already folded into s['flow_accel_score']'s RS component;
        # re-deriving rs_4w_ago isn't possible here without re-fetching history,
        # so instead blend the existing (RS-only) flow_accel with the new
        # rank-delta component directly.
        rank_now = prov_rank[key]
        rank_4w_ago = s.get('rank_4w_ago')
        rank_delta_score = None
        if rank_4w_ago is not None:
            rank_delta_score = normalise(rank_4w_ago - rank_now, -10.0, 10.0)
        combined, n, total = blend([s['flow_accel_score'], rank_delta_score], [0.5, 0.5])
        s['flow_accel_score'] = round(combined) if combined is not None else None

    # Final theme_score with the fully-formed flow_accel_score.
    # Require at least 3 of the 6 components live — renormalising over 1-2
    # components (e.g. just macro_alignment + crowding, both of which default
    # to neutral-ish values even with zero price data) produces a misleadingly
    # confident score. Below that threshold, theme_score is None and status
    # falls through to a dedicated low-data label.
    final_scores = {}
    final_n_live = {}
    for key, s in snapshots.items():
        components = [s['momentum_score'], s['flow_accel_score'], s['breadth_score'],
                       s['macro_alignment'], s['news_sentiment'], s['crowding_score']]
        n_live = sum(1 for c in components if c is not None)
        final_n_live[key] = n_live
        if n_live < 3:
            final_scores[key] = None
            continue
        val, _, _ = blend(components, [0.25, 0.25, 0.20, 0.15, 0.10, -0.05])
        final_scores[key] = val

    final_ranked = sorted(snapshots.keys(), key=lambda k: (final_scores[k] if final_scores[k] is not None else -1), reverse=True)
    rank_now = {k: i + 1 for i, k in enumerate(final_ranked)}

    for key, s in snapshots.items():
        s['theme_score'] = round(final_scores[key]) if final_scores[key] is not None else None
        s['rank_now'] = rank_now[key]
        rank_4w_ago = s.get('rank_4w_ago')
        rank_1w_ago = s.get('rank_1w_ago')
        s['rank_delta_4w'] = (rank_4w_ago - s['rank_now']) if rank_4w_ago is not None else None
        s['rank_delta_1w'] = (rank_1w_ago - s['rank_now']) if rank_1w_ago is not None else None

        # Confidence: based on data coverage + history availability.
        try:
            n_live = int(s['data_coverage'].split(' of ')[0])
        except Exception:
            n_live = 0
        has_history = rank_4w_ago is not None
        confidence = round(n_live / 6 * 70 + (30 if has_history else 0))
        s['confidence'] = min(confidence, 100)

        s['status'] = classify_status(s)

    return snapshots
