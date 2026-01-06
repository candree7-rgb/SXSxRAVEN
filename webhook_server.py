"""
Simple webhook server for testing signals manually.
Run this to accept webhook POSTs on /webhook endpoint.
"""

import sys
import logging
from flask import Flask, request, jsonify

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY,
    STATE_FILE, DRY_RUN, LOG_LEVEL,
    TP_SPLITS, DCA_QTY_MULTS, INITIAL_SL_PCT,
    USE_QUANTUM_ENTRY, FOLLOW_TP_ENABLED, MAX_SL_DISTANCE_PCT
)
from bybit_v5 import BybitV5
from signal_parser_raven import parse_signal, signal_hash
from state import load_state, save_state
from trade_engine import TradeEngine
import db_export
import time


def setup_logger() -> logging.Logger:
    log = logging.getLogger("webhook")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log


# Initialize components
log = setup_logger()
st = load_state(STATE_FILE)
bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
engine = TradeEngine(bybit, st, log)

# Initialize database if enabled
if db_export.is_enabled():
    log.info("📊 Initializing database...")
    if db_export.init_database():
        log.info("✅ Database ready")

app = Flask(__name__)


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Webhook endpoint for receiving signals.

    Accepts JSON with either:
    - {"text": "DUSK/USDT 📈 BUY\n🔹Entry zone: ..."}
    OR
    - Direct signal text as raw body
    """
    try:
        # Get signal text
        if request.is_json:
            data = request.get_json()
            text = data.get('text', '') or data.get('signal', '')
        else:
            text = request.data.decode('utf-8')

        if not text:
            return jsonify({"error": "No signal text provided"}), 400

        log.info(f"📨 Webhook received: {text[:100]}...")

        # Parse signal
        sig = parse_signal(text, quote=QUOTE)

        if not sig:
            log.warning("❌ Failed to parse signal")
            return jsonify({"error": "Invalid signal format"}), 400

        log.info(f"✅ Parsed signal: {sig['symbol']} {sig['side'].upper()} (zone: {sig['entry_zone_low']} - {sig['entry_zone_high']})")

        # Check if already seen
        sh = signal_hash(sig)
        seen = set(st.get("seen_signal_hashes", []))
        if sh in seen:
            log.info(f"⏭️  Signal already processed")
            return jsonify({"status": "skipped", "reason": "already_seen"}), 200

        # Mark as seen
        seen.add(sh)
        st["seen_signal_hashes"] = list(seen)[-500:]

        # Check concurrent trades limit
        active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
        if len(active) >= MAX_CONCURRENT_TRADES:
            log.info(f"⏭️  Max concurrent trades reached ({len(active)}/{MAX_CONCURRENT_TRADES})")
            return jsonify({"status": "skipped", "reason": "max_concurrent"}), 200

        # Place entry order
        trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time())}"

        if USE_QUANTUM_ENTRY:
            oid = engine.place_zone_entry(sig, trade_id)
        else:
            sig['trigger'] = sig['entry_zone_mid']
            oid = engine.place_conditional_entry(sig, trade_id)

        if not oid:
            log.warning(f"❌ Entry order failed for {sig['symbol']}")
            return jsonify({"status": "failed", "reason": "entry_failed"}), 500

        # Store trade
        st.setdefault("open_trades", {})[trade_id] = {
            "id": trade_id,
            "symbol": sig["symbol"],
            "order_side": "Sell" if sig["side"] == "sell" else "Buy",
            "pos_side": "Short" if sig["side"] == "sell" else "Long",
            "trigger": sig["entry_zone_mid"],
            "entry_zone_low": sig["entry_zone_low"],
            "entry_zone_high": sig["entry_zone_high"],
            "tp_prices": sig.get("tp_prices") or [],
            "tp_splits": None,
            "dca_prices": [],
            "sl_price": sig.get("sl_price"),
            "entry_order_id": oid,
            "status": "pending",
            "placed_ts": time.time(),
            "base_qty": engine.calc_base_qty(sig["symbol"], sig["entry_zone_mid"]),
            "raw": sig.get("raw", ""),
            "webhook_msg": True,  # Mark as webhook source
        }

        save_state(STATE_FILE, st)

        log.info(f"🟡 ENTRY PLACED {sig['symbol']} {sig['side'].upper()} zone={sig['entry_zone_low']}-{sig['entry_zone_high']} (id={trade_id})")

        return jsonify({
            "status": "success",
            "trade_id": trade_id,
            "symbol": sig["symbol"],
            "side": sig["side"],
            "entry_zone": [sig["entry_zone_low"], sig["entry_zone_high"]],
            "order_id": oid
        }), 200

    except Exception as e:
        log.exception(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
    return jsonify({
        "status": "ok",
        "active_trades": len(active),
        "dry_run": DRY_RUN
    }), 200


if __name__ == '__main__':
    log.info("="*60)
    log.info("Webhook Server (Raven Pro)")
    log.info("="*60)
    log.info(f"DRY_RUN: {DRY_RUN}")
    log.info(f"Listening on: http://0.0.0.0:8080/webhook")
    log.info("="*60)

    app.run(host='0.0.0.0', port=8080, debug=False)
