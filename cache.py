"""
◈ STOCKSENSE — Unified Cache
Single store, namespaced keys, per-entry TTL.
Thread-safe via GIL for CPython.
"""
import time, threading

class Cache:
    """
    Unified key-value cache with per-entry TTL.
    Keys are namespaced: 'ns:key' e.g. 'stock:NVDA', 'macro:fred:CPIAUCSL'
    """
    def __init__(self):
        self._store = {}   # key → {'v': value, 'ts': timestamp, 'ttl': seconds}
        self._lock  = threading.Lock()

    def get(self, key):
        with self._lock:
            e = self._store.get(key)
            if e and (time.time() - e['ts']) < e['ttl']:
                return e['v']
            if e:
                del self._store[key]   # expired — evict immediately
            return None

    def set(self, key, value, ttl=600):
        with self._lock:
            self._store[key] = {'v': value, 'ts': time.time(), 'ttl': ttl}

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)

    def keys(self, prefix=''):
        with self._lock:
            now = time.time()
            return [k for k, e in self._store.items()
                    if k.startswith(prefix) and (now - e['ts']) < e['ttl']]

    def stats(self):
        with self._lock:
            now   = time.time()
            live  = sum(1 for e in self._store.values() if (now - e['ts']) < e['ttl'])
            total = len(self._store)
            return {'live': live, 'total': total, 'expired': total - live}

# TTLs — one place to tune them all
TTL = {
    'stock':     600,     # 10 min  — stock fundamentals
    'quote':     60,      # 1 min   — live prices
    'macro':     300,     # 5 min   — macro market data
    'fred':      21600,   # 6 hours — FRED economic series
    'wb':        21600,   # 6 hours — World Bank series
    'fx':        300,     # 5 min   — FX prices
    'sentiment': 300,     # 5 min   — options sentiment
    'scanner':   1800,    # 30 min  — scanner results (long — expensive to compute)
    'news':      900,     # 15 min  — news headlines
}

cache = Cache()
