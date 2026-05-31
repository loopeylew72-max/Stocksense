"""
store.py — persistent time-series storage for StockSense.

Backs the regime history (so trend / weekly-monthly deltas survive restarts)
and an indicator time-series (so the engine can move from fixed thresholds to
historical percentile / z-score normalisation).

Backend: SQLite by default (stdlib, zero deps). On Railway, point STORE_PATH at
a mounted volume (e.g. /data/stocksense.db) so it persists across redeploys —
the local app filesystem is ephemeral and will reset on each deploy otherwise.

Everything is wrapped so a storage failure NEVER breaks the app: callers get
None / [] and the app keeps running on its in-memory cache as before.

Scaling note: SQLite is single-writer and perfect for a single instance. If you
move to multiple instances, swap the connection layer for Postgres (via
DATABASE_URL) — the public API below stays identical, so callers don't change.
"""
import os, sqlite3, json, time, threading, statistics

STORE_PATH = os.environ.get('STORE_PATH') or os.path.join(os.path.dirname(__file__), 'stocksense.db')
MIN_SAMPLES = 30            # below this, percentile/zscore return None (not enough history)
DEFAULT_WINDOW_DAYS = 1825  # 5 years

_lock = threading.Lock()
_ready = False
_available = True


def _conn():
    c = sqlite3.connect(STORE_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    """Create tables if needed. Returns True if the store is usable."""
    global _ready, _available
    if _ready:
        return True
    try:
        with _lock, _conn() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS snapshots(
                ts INTEGER PRIMARY KEY,
                regime_score REAL,
                data TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS indicators(
                name TEXT NOT NULL,
                ts INTEGER NOT NULL,
                value REAL,
                PRIMARY KEY(name, ts))''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_ind_name_ts ON indicators(name, ts)')
        _ready = True
        _available = True
    except Exception as e:
        print(f'[STORE] init failed, running without persistence: {e}')
        _available = False
    return _available


def available():
    return _available


# ── Regime snapshots ─────────────────────────────────────────────
def record_snapshot(ts, regime_score, payload, min_gap_s=21600):
    """
    Persist a regime snapshot. Skips if the most recent snapshot is newer than
    min_gap_s (default 6h) to avoid bloat from the 15-min compute cadence.
    payload: {'pillars': {...}, 'assets': {...}, 'label': str}
    """
    if not init():
        return False
    try:
        with _lock, _conn() as c:
            last = c.execute('SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 1').fetchone()
            if last and (int(ts) - last['ts']) < min_gap_s:
                return False
            c.execute('INSERT OR REPLACE INTO snapshots(ts, regime_score, data) VALUES(?,?,?)',
                      (int(ts), float(regime_score), json.dumps(payload)))
        return True
    except Exception as e:
        print(f'[STORE] record_snapshot error: {e}')
        return False


def get_snapshots(since_ts=None, limit=2000):
    """Return snapshots oldest→newest as [{ts, score, ...payload}]."""
    if not init():
        return []
    try:
        q, args = 'SELECT ts, regime_score, data FROM snapshots', []
        if since_ts:
            q += ' WHERE ts >= ?'; args.append(int(since_ts))
        q += ' ORDER BY ts ASC LIMIT ?'; args.append(int(limit))
        with _lock, _conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            try:    payload = json.loads(r['data']) or {}
            except: payload = {}
            out.append({'ts': r['ts'], 'score': r['regime_score'], **payload})
        return out
    except Exception as e:
        print(f'[STORE] get_snapshots error: {e}')
        return []


# ── Indicator time-series (for percentile / z-score) ─────────────
def record_indicator(name, ts, value):
    if value is None or not init():
        return
    try:
        with _lock, _conn() as c:
            c.execute('INSERT OR REPLACE INTO indicators(name, ts, value) VALUES(?,?,?)',
                      (name, int(ts), float(value)))
    except Exception as e:
        print(f'[STORE] record_indicator error: {e}')


def record_indicators_bulk(name, points):
    """points: iterable of (ts, value). Used by FRED backfill."""
    if not init():
        return 0
    rows = [(name, int(t), float(v)) for t, v in points if v is not None]
    if not rows:
        return 0
    try:
        with _lock, _conn() as c:
            c.executemany('INSERT OR REPLACE INTO indicators(name, ts, value) VALUES(?,?,?)', rows)
        return len(rows)
    except Exception as e:
        print(f'[STORE] bulk error: {e}')
        return 0


def _series(name, window_days=None, now=None):
    if not init():
        return []
    try:
        q, args = 'SELECT value FROM indicators WHERE name = ?', [name]
        if window_days:
            now = now or time.time()
            q += ' AND ts >= ?'; args.append(int(now - window_days * 86400))
        q += ' ORDER BY ts ASC'
        with _lock, _conn() as c:
            return [r['value'] for r in c.execute(q, args).fetchall()]
    except Exception as e:
        print(f'[STORE] series error: {e}')
        return []


def percentile_rank(name, value, window_days=DEFAULT_WINDOW_DAYS):
    """
    Percentile of `value` within this indicator's trailing history (0–100).
    Returns None until MIN_SAMPLES points exist — caller falls back to fixed
    thresholds. Good for explainability ("hotter than 88% of the last 5y").
    """
    if value is None:
        return None
    vals = _series(name, window_days)
    if len(vals) < MIN_SAMPLES:
        return None
    below = sum(1 for v in vals if v <= value)
    return round(below / len(vals) * 100, 1)


def zscore(name, value, window_days=DEFAULT_WINDOW_DAYS, winsor=3.0):
    """
    Z-score of `value` vs trailing mean/std, winsorised at ±winsor.
    Keeps magnitude (unlike percentile). None until enough history.
    """
    if value is None:
        return None
    vals = _series(name, window_days)
    if len(vals) < MIN_SAMPLES:
        return None
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return 0.0
    z = (value - mu) / sd
    return round(max(-winsor, min(winsor, z)), 2)


def status():
    """Health + coverage summary for /api/store/status."""
    if not init():
        return {'available': False, 'backend': 'none'}
    try:
        with _lock, _conn() as c:
            snaps = c.execute('SELECT COUNT(*) n, MIN(ts) lo, MAX(ts) hi FROM snapshots').fetchone()
            inds  = c.execute('SELECT COUNT(DISTINCT name) names, COUNT(*) rows FROM indicators').fetchone()
            per   = c.execute('SELECT name, COUNT(*) n, MIN(ts) lo, MAX(ts) hi FROM indicators GROUP BY name ORDER BY name').fetchall()
        return {
            'available': True,
            'backend': 'sqlite',
            'path': STORE_PATH,
            'snapshots': {'count': snaps['n'], 'from': snaps['lo'], 'to': snaps['hi']},
            'indicators': {'series': inds['names'], 'rows': inds['rows'],
                           'detail': [{'name': r['name'], 'points': r['n'], 'from': r['lo'], 'to': r['hi']} for r in per]},
        }
    except Exception as e:
        return {'available': False, 'error': str(e)}
