"""
◈ STOCKSENSE — API Utilities
Consistent response schema for all endpoints.
Every route returns: { ok, data, error, ts, cached }
"""
import time
from flask import jsonify

def ok(data, cached=False, meta=None):
    """Successful response."""
    resp = {'ok': True, 'data': data, 'ts': int(time.time()), 'cached': cached}
    if meta: resp['meta'] = meta
    return jsonify(resp)

def err(message, status=200, code=None):
    """
    Error response — always HTTP 200 so frontend can read the body.
    Use status=429 only for rate limits where you want browser-level retry.
    """
    resp = {'ok': False, 'error': message, 'ts': int(time.time())}
    if code: resp['code'] = code
    return jsonify(resp), status

def rate_limited():
    return err('Rate limited — please wait 60 seconds and try again.', code='RATE_LIMITED')

def not_found(ticker):
    return err(f'Ticker "{ticker}" not found. Check the symbol and try again.', code='NOT_FOUND')

def service_error(msg='Data service temporarily unavailable. Please try again.'):
    return err(msg, code='SERVICE_ERROR')
