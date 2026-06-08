"""
◈ STOCKSENSE SIGNAL ALERT SYSTEM
alerts.py

Runs every 4 hours via APScheduler on Railway.
Checks all scored assets for Very Bullish (≥68) or Very Bearish (≤32) signals.
Gates on VIX < 15 and regime alignment.
Sends instant Telegram alert with full signal detail.

Environment variables required:
  TELEGRAM_BOT_TOKEN  — from @BotFather on Telegram
  TELEGRAM_CHAT_ID    — your personal chat ID from @userinfobot

Optional (already in Railway env):
  FRED_KEY, AV_KEY, FMP_KEY — for live data feeds
"""

import os
import time
import requests
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Telegram config ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Signal thresholds ────────────────────────────────────────────────
VERY_BULLISH_MIN = 68    # composite score threshold for LONG signal
VERY_BEARISH_MAX = 32    # composite score threshold for SHORT signal
VIX_GATE         = 15.0  # maximum VIX for any signal (upgraded from 18)
MIN_CONFIDENCE   = 70    # minimum RIE confidence % to send alert

# ── Assets to monitor (matches Top Setups page) ─────────────────────
MONITORED_ASSETS = [
    # Equities
    {"sym": "SPY",  "name": "S&P 500",       "type": "equity",    "ticker": "SPY"},
    {"sym": "QQQ",  "name": "NASDAQ 100",     "type": "equity",    "ticker": "QQQ"},
    {"sym": "IWM",  "name": "Russell 2000",   "type": "equity",    "ticker": "QQQ"},
    {"sym": "DIA",  "name": "Dow Jones",      "type": "equity",    "ticker": "SPY"},
    # Commodities
    {"sym": "GLD",  "name": "Gold",           "type": "commodity", "ticker": "GLD"},
    {"sym": "SLV",  "name": "Silver",         "type": "commodity", "ticker": "SLV"},
    {"sym": "USO",  "name": "Crude Oil",      "type": "commodity", "ticker": "USO"},
    # FX ETFs
    {"sym": "UUP",  "name": "USD Index",      "type": "forex",     "ticker": "UUP"},
    {"sym": "FXE",  "name": "Euro",           "type": "forex",     "ticker": "EURUSD"},
    {"sym": "FXB",  "name": "British Pound",  "type": "forex",     "ticker": "GBPUSD"},
    {"sym": "FXY",  "name": "Japanese Yen",   "type": "forex",     "ticker": "USDJPY"},
    {"sym": "FXA",  "name": "Aussie Dollar",  "type": "forex",     "ticker": "AUDUSD"},
    {"sym": "FXC",  "name": "Canadian Dollar","type": "forex",     "ticker": "USDCAD"},
    {"sym": "FXF",  "name": "Swiss Franc",    "type": "forex",     "ticker": "USDCHF"},
    # Bonds
    {"sym": "TLT",  "name": "20Y Treasury",   "type": "bond",      "ticker": "TLT"},
    {"sym": "IEF",  "name": "10Y Treasury",   "type": "bond",      "ticker": "IEF"},
    {"sym": "HYG",  "name": "High Yield",     "type": "bond",      "ticker": "HYG"},
    # Sector ETFs
    {"sym": "SMH",  "name": "Semiconductors", "type": "equity",    "ticker": "QQQ"},
    {"sym": "XLF",  "name": "Financials",     "type": "equity",    "ticker": "SPY"},
    {"sym": "XLE",  "name": "Energy",         "type": "commodity", "ticker": "USO"},
]

# ── Alert deduplication ──────────────────────────────────────────────
# Tracks what we've already alerted on so we don't spam
# Format: { "SPY_LONG": timestamp_of_last_alert }
_alert_history = {}
ALERT_COOLDOWN_HOURS = 24  # don't re-alert same asset+direction within 24h


def _already_alerted(sym, direction):
    """True if we sent this exact signal within the cooldown window."""
    key = f"{sym}_{direction}"
    last = _alert_history.get(key, 0)
    return (time.time() - last) < (ALERT_COOLDOWN_HOURS * 3600)


def _mark_alerted(sym, direction):
    key = f"{sym}_{direction}"
    _alert_history[key] = time.time()


# ── Telegram sender ──────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            log.info("Telegram alert sent")
            return True
        else:
            log.error(f"Telegram error: {resp.text}")
            return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def send_test_message():
    """Send a test ping to verify Telegram is configured correctly."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"◈ <b>StockSense Alert System — Test</b>\n\n"
        f"✅ Telegram is configured correctly.\n"
        f"Signal alerts will be sent here when:\n"
        f"  • Asset scores ≥68 (Very Bullish) or ≤32 (Very Bearish)\n"
        f"  • VIX is below 15\n"
        f"  • RIE confidence ≥70%\n\n"
        f"<i>{now}</i>"
    )
    return send_telegram(msg)


# ── Signal formatter ─────────────────────────────────────────────────
def _format_factor_bar(score: int) -> str:
    """Turn a 0-100 score into a mini bar."""
    filled = round(score / 10)
    empty  = 10 - filled
    col    = "🟩" if score >= 60 else "🟨" if score >= 40 else "🟥"
    return col * filled + "⬜" * empty


def _regime_emoji(score: int) -> str:
    if score >= 68: return "🟢"
    if score >= 57: return "🟡"
    if score >= 44: return "🟠"
    if score >= 33: return "🔴"
    return "🔴"


def format_signal_alert(asset: dict, composite: int, label: str,
                         raw: dict, rie_result: dict, direction: str) -> str:
    """Format a full signal alert message for Telegram."""
    regime_score = rie_result.get("regime_score", 50)
    regime_label = rie_result.get("regime_label", "Unknown")
    confidence   = rie_result.get("confidence", 0)
    vix          = (rie_result.get("pillar_scores") or {})
    vix_val      = raw.get("fear", 50)
    # Convert fear score back to approximate VIX
    # fear = nz(vix, 12, 35) so vix ≈ 12 + (fear/100)*(35-12)
    approx_vix   = round(12 + (vix_val / 100) * 23, 1)

    dir_emoji  = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    score_bar  = _format_factor_bar(composite)
    reg_emoji  = _regime_emoji(regime_score)

    # Risk sizing recommendation based on score
    if composite >= 80 or composite <= 20:
        size_rec = "2.5% risk (max conviction)"
    elif composite >= 75 or composite <= 25:
        size_rec = "2.0% risk"
    else:
        size_rec = "1.5% risk"

    # Conviction level
    conv = "MAXIMUM" if abs(composite - 50) >= 33 else "HIGH" if abs(composite - 50) >= 18 else "STANDARD"

    now = datetime.now(timezone.utc).strftime("%a %d %b %H:%M UTC")

    lines = [
        f"◈ <b>StockSense Signal Alert</b>",
        f"",
        f"{dir_emoji} — <b>{asset['name']} ({asset['sym']})</b>",
        f"",
        f"<b>Composite Score:</b> {composite}/100 — {label}",
        f"{score_bar}",
        f"<b>Conviction:</b> {conv} | <b>Recommended size:</b> {size_rec}",
        f"",
        f"<b>Factor Breakdown:</b>",
        f"  Growth:    {round(raw.get('growth', 50)):>3}  {_format_factor_bar(round(raw.get('growth', 50)))}",
        f"  Inflation: {round(raw.get('infl', 50)):>3}  {_format_factor_bar(round(raw.get('infl', 50)))}",
        f"  RealYield: {round(raw.get('ry', 50)):>3}  {_format_factor_bar(round(raw.get('ry', 50)))}",
        f"  Liquidity: {round(raw.get('liq', 50)):>3}  {_format_factor_bar(round(raw.get('liq', 50)))}",
        f"  USD:       {round(raw.get('usd', 50)):>3}  {_format_factor_bar(round(raw.get('usd', 50)))}",
        f"  Momentum:  {round(raw.get('mom', 50)):>3}  {_format_factor_bar(round(raw.get('mom', 50)))}",
        f"  Fear/VIX:  {round(raw.get('fear', 50)):>3}  {_format_factor_bar(round(raw.get('fear', 50)))}",
        f"",
        f"{reg_emoji} <b>Market Regime:</b> {regime_score}/100 — {regime_label}",
        f"   Confidence: {confidence}% | VIX: ~{approx_vix} ✅",
        f"",
        f"<b>Action:</b>",
        f"  1. Open {asset['name']} daily chart",
        f"  2. Find nearest significant weekly level",
        f"  3. Wait for daily close break + retest",
        f"  4. Enter with {size_rec}, stop 1.5–2.0 ATR, target 3–5R",
        f"",
        f"🔗 <a href='https://web-production-72e6a.up.railway.app'>Open StockSense</a>",
        f"<i>{now}</i>",
    ]
    return "\n".join(lines)


# ── Core check function ──────────────────────────────────────────────
def check_signals(rie_result: dict, scoring_module, fred_data: dict, price_data: dict) -> list:
    """
    Check all monitored assets for signals.
    Called by the scheduler and the /api/alerts/check route.

    Args:
        rie_result:     Output of rie.run_rie() — already computed
        scoring_module: The imported scoring module
        fred_data:      Current FRED data dict
        price_data:     Current price data dict

    Returns:
        List of fired signals (dicts), whether or not alerts were sent
    """
    fired = []

    # Extract VIX from rie result
    sentiment_subs = (rie_result.get("pillars") or {}).get("sentiment") or {}
    vix_score = (sentiment_subs.get("sub_scores") or {}).get("vix", 54)
    # Convert VIX score back to approximate level
    # score 54 ≈ VIX 16–22, score 75 ≈ VIX >30, score 38 ≈ VIX <13
    # Approximate: vix_level = 12 + ((100-vix_score)/100)*(35-12) doesn't quite work
    # Better: use the raw VIX from price_data
    vix_level = (price_data.get("vix") or {}).get("price", 20.0)

    regime_score = rie_result.get("regime_score", 50)
    confidence   = rie_result.get("confidence", 0)
    pillar_scores = rie_result.get("pillar_scores") or {}
    liq_pillar    = pillar_scores.get("liquidity", 50)

    # VIX gate — hard stop
    vix_ok = vix_level < VIX_GATE

    # Confidence gate
    conf_ok = confidence >= MIN_CONFIDENCE

    for asset in MONITORED_ASSETS:
        try:
            # Build per-asset momentum from price_data
            sym_lower  = asset["sym"].lower()
            price_info = price_data.get(sym_lower) or {}
            chg_pct    = price_info.get("changePct", 0.0)

            # 52-week range position from price_data if available
            hi52 = price_info.get("week52High", 0)
            lo52 = price_info.get("week52Low",  0)
            px   = price_info.get("price", 0)
            if hi52 > lo52 > 0:
                range_pos = (px - lo52) / (hi52 - lo52) * 100
            else:
                range_pos = 50.0

            # Build macro dict for scoring.py
            macro = {
                "gdp":             fred_data.get("gdp",  {}),
                "nfp":             fred_data.get("nfp",  {}),
                "unemp":           fred_data.get("unemp",{}),
                "retail":          fred_data.get("retail",{}),
                "cpi":             fred_data.get("cpi",  {}),
                "core_cpi":        fred_data.get("core_cpi", {}),
                "ppi":             fred_data.get("ppi",  {}),
                "real_yield":      fred_data.get("real_yield", {}),
                "liquidity_pillar": liq_pillar,
                "uup":             price_data.get("uup", {}),
                "vix":             price_data.get("vix", {"price": vix_level}),
            }

            raw = scoring_module.compute_raw_readings(
                macro, chg_pct=chg_pct, range_pos=range_pos
            )
            composite, label, asset_class, breakdown = scoring_module.score_asset(
                asset["type"], asset["ticker"], raw
            )

            # Determine direction
            if label in ("Very Bullish",):
                direction = "LONG"
            elif label in ("Very Bearish",):
                direction = "SHORT"
            else:
                continue  # not a signal

            # Regime alignment check
            if direction == "LONG"  and regime_score < 40: continue  # no longs in clear risk-off
            if direction == "SHORT" and regime_score > 60: continue  # no shorts in clear risk-on

            signal = {
                "sym":       asset["sym"],
                "name":      asset["name"],
                "type":      asset["type"],
                "ticker":    asset["ticker"],
                "composite": composite,
                "label":     label,
                "direction": direction,
                "raw":       raw,
                "vix":       vix_level,
                "vix_ok":    vix_ok,
                "conf_ok":   conf_ok,
                "regime":    regime_score,
                "confidence":confidence,
                "alertable": vix_ok and conf_ok and not _already_alerted(asset["sym"], direction),
                "ts":        int(time.time()),
            }
            fired.append(signal)

            # Send alert if all gates pass
            if signal["alertable"]:
                msg = format_signal_alert(asset, composite, label, raw, rie_result, direction)
                sent = send_telegram(msg)
                if sent:
                    _mark_alerted(asset["sym"], direction)
                    signal["alert_sent"] = True
                    log.info(f"Alert sent: {asset['sym']} {direction} score={composite}")
                else:
                    signal["alert_sent"] = False
            else:
                signal["alert_sent"] = False
                if not vix_ok:
                    signal["blocked_by"] = f"VIX {vix_level:.1f} ≥ {VIX_GATE} gate"
                elif not conf_ok:
                    signal["blocked_by"] = f"Confidence {confidence}% < {MIN_CONFIDENCE}% minimum"
                elif _already_alerted(asset["sym"], direction):
                    signal["blocked_by"] = f"Already alerted within {ALERT_COOLDOWN_HOURS}h cooldown"

        except Exception as e:
            log.error(f"Error scoring {asset['sym']}: {e}")
            continue

    # Sort by conviction (distance from 50)
    fired.sort(key=lambda s: -abs(s["composite"] - 50))
    return fired


# ── Daily summary ────────────────────────────────────────────────────
def send_daily_summary(rie_result: dict, all_signals: list):
    """
    Send a morning summary every day at 08:00 UTC regardless of signals.
    Shows regime state and top 5 scores.
    """
    regime_score = rie_result.get("regime_score", 50)
    regime_label = rie_result.get("regime_label", "Unknown")
    confidence   = rie_result.get("confidence",   0)
    pillar_scores = rie_result.get("pillar_scores") or {}
    now = datetime.now(timezone.utc).strftime("%a %d %b %Y")

    # Top bullish and bearish
    bullish = [s for s in all_signals if s["direction"] == "LONG"][:3]
    bearish = [s for s in all_signals if s["direction"] == "SHORT"][:3]

    reg_emoji = _regime_emoji(regime_score)
    lines = [
        f"◈ <b>StockSense Morning Brief — {now}</b>",
        f"",
        f"{reg_emoji} <b>Regime: {regime_label} {regime_score}/100</b>",
        f"   Confidence: {confidence}%",
        f"   Economic: {pillar_scores.get('economic', 50)} | "
        f"Liquidity: {pillar_scores.get('liquidity', 50)} | "
        f"Internals: {pillar_scores.get('internals', 50)}",
        f"   Price: {pillar_scores.get('price', 50)} | "
        f"Sentiment: {pillar_scores.get('sentiment', 50)}",
        f"",
    ]

    if bullish:
        lines.append(f"<b>🟢 Strongest Bullish:</b>")
        for s in bullish:
            gate = "✅" if s["vix_ok"] else "❌ VIX"
            lines.append(f"  {s['sym']:6} {s['composite']:3}/100  {s['label'][:12]}  {gate}")
        lines.append("")

    if bearish:
        lines.append(f"<b>🔴 Strongest Bearish:</b>")
        for s in bearish:
            gate = "✅" if s["vix_ok"] else "❌ VIX"
            lines.append(f"  {s['sym']:6} {s['composite']:3}/100  {s['label'][:12]}  {gate}")
        lines.append("")

    if not bullish and not bearish:
        lines.append("No high-conviction signals. All assets in neutral zone.")
        lines.append("")

    lines.append(f"🔗 <a href='https://web-production-72e6a.up.railway.app'>Open StockSense</a>")
    return send_telegram("\n".join(lines))
