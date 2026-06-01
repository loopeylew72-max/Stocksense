"""
store.py — persistent time-series storage for StockSense.

Backs the regime history (so trend / weekly-monthly deltas survive restarts)
and an indicator time-series (so the engine can move from fixed thresholds to
historical percentile / z-score normalisation).

Backend is auto-detected:
  • If DATABASE_URL is set AND psycopg2 is installed → PostgreSQL (Railway's
    managed Postgres injects DATABASE_URL automatically). Durable, multi-instance
    safe — no volume needed.
  • Otherwise → SQLite at STORE_PATH (stdlib, zero deps). Durable on Railway only
    if STORE_PATH points at a mounted volume.

The public API is identical for both backends, so callers (server.py) never change.
Everything is wrapped so a storage failure NEVER breaks the app: callers get
None / [] / False and the app keeps running on its in-memory cache.

To enable Postgres: add `psycopg2-binary` to requirements.txt and attach a
Railway Postgres plugin (DATABASE_URL appears automatically).
"""
import os, json, time, threading, statistics

DATABASE_URL = (os.environ.get('DATABASE_URL') or '').strip()
STORE_PATH   = os.environ.get('STORE_PATH') or os.path.join(os.path.dirname(__file__), 'stocksense.db')
MIN_SAMPLES  = 30            # below this, percentile/zscore return None
DEFAULT_WINDOW_DAYS = 1825   # 5 years

_lock = threading.Lock()
_ready = False
_available = True
_BACKEND = None              # 'postgres' | 'sqlite'
_pg = None                   # psycopg2 module (if Postgres)
_pg_extras = None
_sqlite3 = None


def _detect_backend():
    global _BACKEND, _pg, _pg_extras, _sqlite3
    if DATABASE_URL:
        try:
            import psycopg2, psycopg2.extras
            _pg = psycopg2
            _pg_extras = psycopg2.extras
            _BACKEND = 'postgres'
            print('[STORE] backend: PostgreSQL (DATABASE_URL detected)')
            return
        except Exception as e:
            print(f'[STORE] DATABASE_URL set but psycopg2 unavailable ({e}); using SQLite')
    import sqlite3
    _sqlite3 = sqlite3
    _BACKEND = 'sqlite'
    print(f'[STORE] backend: SQLite ({STORE_PATH})')


def _connect():
    if _BACKEND == 'postgres':
        return _pg.connect(DATABASE_URL)
    return _sqlite3.connect(STORE_PATH, timeout=10)


def _ph(sql):
    """Convert ? placeholders to %s for Postgres."""
    return sql.replace('?', '%s') if _BACKEND == 'postgres' else sql


def _upsert(table, cols, conflict_cols):
    placeholders = ','.join(['?'] * len(cols))
    collist = ','.join(cols)
    if _BACKEND == 'postgres':
        conflict = ','.join(conflict_cols)
        updates = ','.join(f'{c}=EXCLUDED.{c}' for c in cols if c not in conflict_cols)
        return f'INSERT INTO {table}({collist}) VALUES({placeholders}) ON CONFLICT({conflict}) DO UPDATE SET {updates}'
    return f'INSERT OR REPLACE INTO {table}({collist}) VALUES({placeholders})'


def _run(sql, args=(), fetch=None, many=False):
    """Execute a single statement on a fresh connection. fetch: None|'one'|'all'."""
    conn = _connect()
    try:
        cur = conn.cursor()
        if many:
            cur.executemany(_ph(sql), args)
        else:
            cur.execute(_ph(sql), args)
        out = None
        if fetch == 'one': out = cur.fetchone()
        elif fetch == 'all': out = cur.fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def init():
    """Detect backend, create tables. Returns True if usable."""
    global _ready, _available
    if _ready:
        return True
    try:
        with _lock:
            if _BACKEND is None:
                _detect_backend()
            conn = _connect()
            try:
                cur = conn.cursor()
                if _BACKEND == 'postgres':
                    cur.execute('CREATE TABLE IF NOT EXISTS snapshots(ts BIGINT PRIMARY KEY, regime_score DOUBLE PRECISION, data TEXT)')
                    cur.execute('CREATE TABLE IF NOT EXISTS indicators(name TEXT NOT NULL, ts BIGINT NOT NULL, value DOUBLE PRECISION, PRIMARY KEY(name, ts))')
                else:
                    cur.execute('CREATE TABLE IF NOT EXISTS snapshots(ts INTEGER PRIMARY KEY, regime_score REAL, data TEXT)')
                    cur.execute('CREATE TABLE IF NOT EXISTS indicators(name TEXT NOT NULL, ts INTEGER NOT NULL, value REAL, PRIMARY KEY(name, ts))')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_ind_name_ts ON indicators(name, ts)')
                conn.commit()
            finally:
                conn.close()
        _ready = True
        _available = True
    except Exception as e:
        print(f'[STORE] init failed, persistence disabled: {e}')
        _available = False
    return _available


def available():
    return _available


# ── Regime snapshots ─────────────────────────────────────────────
def record_snapshot(ts, regime_score, payload, min_gap_s=21600):
    """Persist a snapshot; skip if the latest is newer than min_gap_s (default 6h)."""
    if not init():
        return False
    try:
        with _lock:
            last = _run('SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 1', fetch='one')
            if last and (int(ts) - int(last[0])) < min_gap_s:
                return False
            _run(_upsert('snapshots', ['ts', 'regime_score', 'data'], ['ts']),
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
        if since_ts:
            rows = _run('SELECT ts, regime_score, data FROM snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT ?',
                        (int(since_ts), int(limit)), fetch='all')
        else:
            rows = _run('SELECT ts, regime_score, data FROM snapshots ORDER BY ts ASC LIMIT ?',
                        (int(limit),), fetch='all')
        out = []
        for r in (rows or []):
            try:    payload = json.loads(r[2]) or {}
            except: payload = {}
            out.append({'ts': r[0], 'score': r[1], **payload})
        return out
    except Exception as e:
        print(f'[STORE] get_snapshots error: {e}')
        return []


# ── Indicator time-series ────────────────────────────────────────
def record_indicator(name, ts, value):
    if value is None or not init():
        return
    try:
        with _lock:
            _run(_upsert('indicators', ['name', 'ts', 'value'], ['name', 'ts']),
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
        with _lock:
            conn = _connect()
            try:
                cur = conn.cursor()
                if _BACKEND == 'postgres':
                    _pg_extras.execute_values(
                        cur,
                        'INSERT INTO indicators(name, ts, value) VALUES %s ON CONFLICT(name, ts) DO UPDATE SET value=EXCLUDED.value',
                        rows)
                else:
                    cur.executemany('INSERT OR REPLACE INTO indicators(name, ts, value) VALUES(?,?,?)', rows)
                conn.commit()
            finally:
                conn.close()
        return len(rows)
    except Exception as e:
        print(f'[STORE] bulk error: {e}')
        return 0


def _series(name, window_days=None, now=None):
    if not init():
        return []
    try:
        if window_days:
            now = now or time.time()
            rows = _run('SELECT value FROM indicators WHERE name = ? AND ts >= ? ORDER BY ts ASC',
                        (name, int(now - window_days * 86400)), fetch='all')
        else:
            rows = _run('SELECT value FROM indicators WHERE name = ? ORDER BY ts ASC', (name,), fetch='all')
        return [r[0] for r in (rows or [])]
    except Exception as e:
        print(f'[STORE] series error: {e}')
        return []


def percentile_rank(name, value, window_days=DEFAULT_WINDOW_DAYS):
    """Percentile of value within this indicator's trailing history (0–100). None if too little history."""
    if value is None:
        return None
    vals = _series(name, window_days)
    if len(vals) < MIN_SAMPLES:
        return None
    below = sum(1 for v in vals if v <= value)
    return round(below / len(vals) * 100, 1)


def zscore(name, value, window_days=DEFAULT_WINDOW_DAYS, winsor=3.0):
    """Z-score vs trailing mean/std, winsorised at ±winsor. None if too little history."""
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
        with _lock:
            snaps = _run('SELECT COUNT(*), MIN(ts), MAX(ts) FROM snapshots', fetch='one')
            inds  = _run('SELECT COUNT(DISTINCT name), COUNT(*) FROM indicators', fetch='one')
            per   = _run('SELECT name, COUNT(*), MIN(ts), MAX(ts) FROM indicators GROUP BY name ORDER BY name', fetch='all')
        return {
            'available': True,
            'backend': _BACKEND,
            'path': STORE_PATH if _BACKEND == 'sqlite' else 'postgres (DATABASE_URL)',
            'snapshots': {'count': snaps[0], 'from': snaps[1], 'to': snaps[2]},
            'indicators': {'series': inds[0], 'rows': inds[1],
                           'detail': [{'name': r[0], 'points': r[1], 'from': r[2], 'to': r[3]} for r in (per or [])]},
        }
    except Exception as e:
        return {'available': False, 'error': str(e)}
