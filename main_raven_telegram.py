import sys
import time
import random
import threading
import logging
import asyncio
from queue import Queue, Empty
from typing import Dict, Any
from flask import Flask, request, jsonify

from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHANNEL, TELEGRAM_SESSION_STRING,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW,
    CATEGORY, ACCOUNT_TYPE, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    POLL_SECONDS, POLL_JITTER_MAX, SIGNAL_UPDATE_INTERVAL_SEC,
    STATE_FILE, DRY_RUN, LOG_LEVEL,
    TP_SPLITS, TP_SPLITS_AUTO, DCA_QTY_MULTS, INITIAL_SL_PCT,
    USE_QUANTUM_ENTRY,
    FOLLOW_TP_ENABLED, MAX_SL_DISTANCE_PCT
)
from bybit_v5 import BybitV5
from telegram_reader import TelegramReader

# Import Raven Pro signal parser
from signal_parser_raven import parse_signal, parse_signal_update, signal_hash

from state import load_state, save_state, utc_day_key
from trade_engine import TradeEngine, get_risk_info
import db_export


def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log


# Flask app for webhook endpoint
app = Flask(__name__)
webhook_message_queue = None  # Will be set in main()
webhook_logger = None  # Will be set in main()


@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for receiving signals manually."""
    try:
        # Get signal text
        if request.is_json:
            data = request.get_json()
            text = data.get('text', '') or data.get('signal', '')
        else:
            text = request.data.decode('utf-8')

        if not text:
            return jsonify({"error": "No signal text provided"}), 400

        webhook_logger.info(f"📨 Webhook received: {text[:100]}...")

        # Parse signal to validate format
        sig = parse_signal(text, quote=QUOTE)
        if not sig:
            webhook_logger.warning("❌ Invalid signal format")
            return jsonify({"error": "Invalid signal format"}), 400

        # Add to message queue (same as Telegram messages)
        webhook_message_queue.put({
            'id': int(time.time() * 1000),  # Unique ID
            'text': text,
            'timestamp': time.time()
        })

        webhook_logger.info(f"✅ Webhook signal queued: {sig['symbol']} {sig['side'].upper()}")

        return jsonify({
            "status": "success",
            "symbol": sig["symbol"],
            "side": sig["side"],
            "entry_zone": [sig["entry_zone_low"], sig["entry_zone_high"]]
        }), 200

    except Exception as e:
        webhook_logger.exception(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "raven-pro-bot"}), 200


def main():
    log = setup_logger()

    # Basic env checks
    missing = []
    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 0:
        missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not TELEGRAM_CHANNEL:
        missing.append("TELEGRAM_CHANNEL")
    if not BYBIT_API_KEY:
        missing.append("BYBIT_API_KEY")
    if not BYBIT_API_SECRET:
        missing.append("BYBIT_API_SECRET")

    if missing:
        raise SystemExit(f"Missing ENV(s): {', '.join(missing)}")

    st = load_state(STATE_FILE)

    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
    engine = TradeEngine(bybit, st, log)

    log.info("="*58)
    mode_str = " | DRY_RUN" if DRY_RUN else ""
    mode_str += " | DEMO" if BYBIT_DEMO else ""
    mode_str += " | TESTNET" if BYBIT_TESTNET else ""
    log.info("Telegram → Bybit Bot (Raven Pro)" + mode_str)
    log.info("="*58)
    log.info(f"Config: SIGNAL_PARSER=RAVEN_PRO")
    log.info(f"Config: QUANTUM_ENTRY={USE_QUANTUM_ENTRY}")
    log.info(f"Config: CATEGORY={CATEGORY}, QUOTE={QUOTE}, LEVERAGE={LEVERAGE}x")
    log.info(f"Config: RISK_PCT={RISK_PCT}%, MAX_CONCURRENT={MAX_CONCURRENT_TRADES}, MAX_DAILY={MAX_TRADES_PER_DAY}")
    log.info(f"Config: POLL_SECONDS={POLL_SECONDS}, TC_MAX_LAG_SEC={TC_MAX_LAG_SEC}")
    log.info(f"Config: DRY_RUN={DRY_RUN}, LOG_LEVEL={LOG_LEVEL}")
    log.info(f"Config: TP_SPLITS={TP_SPLITS}, TP_SPLITS_AUTO={TP_SPLITS_AUTO}")
    log.info(f"Config: DCA_QTY_MULTS={DCA_QTY_MULTS}, INITIAL_SL_PCT={INITIAL_SL_PCT}%")
    log.info(f"Config: FOLLOW_TP={FOLLOW_TP_ENABLED}, MAX_SL_DISTANCE={MAX_SL_DISTANCE_PCT}%")
    log.info(f"Config: WEBHOOK_ENABLED=true (Telegram + HTTP webhook)")

    # Initialize database if enabled
    if db_export.is_enabled():
        log.info("📊 Initializing database...")
        if db_export.init_database():
            log.info("✅ Database ready")
        else:
            log.warning("⚠️ Database initialization failed (continuing without DB export)")

    # Startup sync - check for orphaned positions
    engine.startup_sync()

    # Message queue for Telegram events
    message_queue: Queue[Dict[str, Any]] = Queue()

    # Initialize Telegram reader
    telegram = TelegramReader(
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH,
        channels=TELEGRAM_CHANNEL,
        session_string=TELEGRAM_SESSION_STRING,
        logger=log
    )

    # Message handler: Add messages to queue
    def on_telegram_message(msg_id: int, text: str, timestamp: float):
        message_queue.put({
            'id': msg_id,
            'text': text,
            'timestamp': timestamp
        })

    telegram.set_message_handler(on_telegram_message)

    # Start Telegram in background thread
    def telegram_loop():
        try:
            log.info("🚀 Starting Telegram listener thread...")
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            telegram.start()
        except Exception as e:
            log.error(f"❌ Telegram thread error: {e}")

    telegram_thread = threading.Thread(target=telegram_loop, daemon=True)
    telegram_thread.start()

    # Set global variables for webhook
    global webhook_message_queue, webhook_logger
    webhook_message_queue = message_queue
    webhook_logger = log

    # Start Flask webhook server in background thread
    def flask_loop():
        try:
            import os
            port = int(os.environ.get('PORT', 8080))  # Railway sets PORT automatically
            log.info(f"🌐 Starting webhook server on port {port}...")
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        except Exception as e:
            log.error(f"❌ Flask server error: {e}")

    flask_thread = threading.Thread(target=flask_loop, daemon=True)
    flask_thread.start()

    # Give services time to start
    time.sleep(5)

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # Log heartbeat every 5 minutes

    # Signal update tracking
    last_signal_update_check = time.time() - (SIGNAL_UPDATE_INTERVAL_SEC - 5)

    # ----- Signal Update Checker -----
    def check_signal_updates():
        """Re-read Telegram messages for open/pending trades and apply SL/TP updates."""
        active_trades = [tr for tr in st.get("open_trades", {}).values()
                        if tr.get("status") in ("pending", "open") and tr.get("telegram_msg_id")]

        if not active_trades:
            return

        log.info(f"🔍 Checking {len(active_trades)} trade(s) for signal updates...")

        for tr in active_trades:
            try:
                msg_id = tr.get("telegram_msg_id")
                if not msg_id:
                    log.info(f"   {tr.get('symbol')}: No msg_id saved, skipping")
                    continue

                # Fetch single message by ID
                msg = telegram.fetch_message(int(msg_id))
                if not msg:
                    log.warning(f"   {tr.get('symbol')}: Could not fetch msg {msg_id}")
                    continue

                txt = msg.get('text', '')
                if not txt:
                    log.warning(f"   {tr.get('symbol')}: Empty message text")
                    continue

                # Check if trade was CANCELLED
                if "CLOSED" in txt.upper() or "CANCELLED" in txt.upper():
                    log.warning(f"❌ Signal CANCELLED for {tr['symbol']} - cancelling all orders")
                    trade_id = tr.get("id")

                    if tr.get("status") == "pending":
                        entry_oid = tr.get("entry_order_id")
                        if entry_oid and entry_oid not in ("DRY_RUN", "QUANTUM_ENTRY"):
                            try:
                                engine.cancel_entry(tr["symbol"], entry_oid)
                                log.info(f"🗑️ Cancelled entry order for {tr['symbol']}")
                            except Exception as e:
                                log.debug(f"Could not cancel entry: {e}")

                    if tr.get("status") == "open":
                        engine._cancel_all_trade_orders(tr)

                    tr["status"] = "cancelled"
                    tr["exit_reason"] = "signal_cancelled"
                    tr["closed_ts"] = time.time()
                    log.info(f"✅ Trade {tr['symbol']} marked as cancelled")
                    continue

                # Parse SL/TP updates
                sig = parse_signal_update(txt)

                new_sl = sig.get("sl_price")
                new_tps = sig.get("tp_prices") or []
                old_sl = tr.get("sl_price")
                old_tps = tr.get("tp_prices") or []

                log.info(f"   {tr['symbol']}: old SL={old_sl} → new SL={new_sl} | old TPs={old_tps} → new TPs={new_tps}")

                is_open = tr.get("status") == "open"

                # Update SL if changed
                if new_sl and new_sl != old_sl and not tr.get("sl_moved_to_be"):
                    log.info(f"🔄 Signal SL updated for {tr['symbol']}: {old_sl} → {new_sl}")
                    tr["sl_price"] = new_sl
                    if is_open:
                        if engine._move_sl(tr["symbol"], new_sl):
                            log.info(f"✅ SL updated on Bybit: {tr['symbol']} @ {new_sl}")
                    else:
                        log.info(f"📝 SL saved for {tr['symbol']} (will apply on entry fill)")

                # Update TPs if changed
                tps_changed = False
                if new_tps and len(new_tps) > 0:
                    if len(new_tps) != len(old_tps):
                        tps_changed = True
                    elif any(abs(float(new_tps[i]) - float(old_tps[i])) > 0.0000001 for i in range(len(new_tps))):
                        tps_changed = True

                if tps_changed:
                    log.info(f"🔄 Signal TPs changed for {tr['symbol']}: {old_tps} → {new_tps}")
                    if is_open and tr.get("post_orders_placed"):
                        engine.update_tp_orders(tr, new_tps)
                    else:
                        tr["tp_prices"] = new_tps
                        log.info(f"📝 TPs saved for {tr['symbol']} (will apply on entry fill)")

            except Exception as e:
                log.debug(f"Signal update check failed for {tr.get('symbol')}: {e}")

        save_state(STATE_FILE, st)

    # ----- WS thread -----
    ws_err = {"err": None}

    def on_execution(ev):
        try:
            engine.on_execution(ev)
        except Exception as e:
            log.warning(f"WS execution handler error: {e}")

    def on_order(ev):
        return

    def on_ws_error(err):
        ws_err["err"] = err
        log.debug(f"WS reconnecting: {err}")

    def ws_loop():
        while True:
            try:
                bybit.run_private_ws(on_execution=on_execution, on_order=on_order, on_error=on_ws_error)
            except Exception as e:
                on_ws_error(e)
            time.sleep(3)

    ws_thread = threading.Thread(target=ws_loop, daemon=True)
    ws_thread.start()

    # ----- helper: limits -----
    def trades_today() -> int:
        return int(st.get("daily_counts", {}).get(utc_day_key(), 0))

    def inc_trades_today():
        k = utc_day_key()
        st.setdefault("daily_counts", {})[k] = int(st.get("daily_counts", {}).get(k, 0)) + 1

    # ----- main loop -----
    while True:
        try:
            # Heartbeat log every 5 minutes
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
                log.info(f"💓 Heartbeat: {len(active)} active trade(s), {trades_today()} today")
                last_heartbeat = time.time()

            # Maintenance first
            engine.cancel_expired_entries()
            engine.cleanup_closed_trades()
            engine.check_tp_fills_fallback()
            engine.check_position_alerts()
            engine.log_daily_stats()

            # Check for signal updates
            if time.time() - last_signal_update_check > SIGNAL_UPDATE_INTERVAL_SEC:
                check_signal_updates()
                last_signal_update_check = time.time()

            # Entry-fill fallback (polling) and post-orders placement
            for tid, tr in list(st.get("open_trades", {}).items()):
                if tr.get("status") == "pending":
                    # Check if position opened via polling
                    sz, avg = engine.position_size_avg(tr["symbol"])
                    if sz > 0 and avg > 0:
                        tr["status"] = "open"
                        tr["entry_price"] = avg
                        tr["filled_ts"] = time.time()
                        log.info(f"✅ ENTRY (poll) {tr['symbol']} @ {avg}")
                if tr.get("status") == "open" and not tr.get("post_orders_placed"):
                    engine.place_post_entry_orders(tr)

            # Enforce concurrent trades
            active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
            if len(active) >= MAX_CONCURRENT_TRADES:
                log.debug(f"Active trades {len(active)}/{MAX_CONCURRENT_TRADES} → skip new signals")
            elif trades_today() >= MAX_TRADES_PER_DAY:
                log.debug(f"Trades today {trades_today()}/{MAX_TRADES_PER_DAY} → skip new signals")
            else:
                # Process messages from Telegram queue
                processed_count = 0
                while True:
                    try:
                        # Non-blocking get with 0.1s timeout
                        msg = message_queue.get(timeout=0.1)
                    except Empty:
                        break

                    mid = msg['id']
                    txt = msg['text']
                    ts = msg['timestamp']

                    # Ignore very old messages
                    age = time.time() - ts if ts else 0
                    if ts and age > TC_MAX_LAG_SEC:
                        log.debug(f"Skipping old message (age={age:.0f}s > {TC_MAX_LAG_SEC}s)")
                        continue

                    if not txt:
                        log.debug(f"Message {mid}: empty text, skipping")
                        continue

                    log.debug(f"Message {mid}: {txt[:200]}...")

                    # Parse Raven Pro signal
                    sig = parse_signal(txt, quote=QUOTE)
                    if not sig:
                        log.debug(f"Message {mid}: not a Raven Pro signal")
                        continue

                    log.info(f"📨 Raven Pro Signal: {sig['symbol']} {sig['side'].upper()} (zone: {sig['entry_zone_low']} - {sig['entry_zone_high']})")
                    log.info(f"   TPs: {sig.get('tp_prices', [])} | SL: {sig.get('sl_price')} | Leverage: {sig.get('leverage', '?')}x")

                    sh = signal_hash(sig)
                    seen = set(st.get("seen_signal_hashes", []))
                    if sh in seen:
                        log.debug(f"Signal {sig['symbol']} already seen, skipping")
                        continue

                    # Mark seen early
                    seen.add(sh)
                    st["seen_signal_hashes"] = list(seen)[-500:]

                    trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time())}"
                    log.info(f"🔄 Placing QUANTUM entry order for {sig['symbol']}...")

                    # Use Quantum Entry for zone-based signals
                    if USE_QUANTUM_ENTRY:
                        oid = engine.place_zone_entry(sig, trade_id)
                    else:
                        # Fallback: use zone_mid as fixed entry
                        sig['trigger'] = sig['entry_zone_mid']
                        oid = engine.place_conditional_entry(sig, trade_id)

                    if not oid:
                        log.warning(f"❌ Entry order failed for {sig['symbol']}")
                        continue

                    # Get current risk settings for tracking
                    risk_info = get_risk_info(engine.bybit, ACCOUNT_TYPE)

                    # Store trade with zone info + risk tracking
                    st.setdefault("open_trades", {})[trade_id] = {
                        "id": trade_id,
                        "symbol": sig["symbol"],
                        "order_side": "Sell" if sig["side"] == "sell" else "Buy",
                        "pos_side": "Short" if sig["side"] == "sell" else "Long",
                        "trigger": sig["entry_zone_mid"],  # For fallback/display
                        "entry_zone_low": sig["entry_zone_low"],
                        "entry_zone_high": sig["entry_zone_high"],
                        "tp_prices": sig.get("tp_prices") or [],
                        "tp_splits": None,  # engine uses config
                        "dca_prices": [],  # Raven Pro doesn't use DCAs
                        "sl_price": sig.get("sl_price"),
                        "entry_order_id": oid,
                        "status": "pending",
                        "placed_ts": time.time(),
                        "base_qty": engine.calc_base_qty(sig["symbol"], sig["entry_zone_mid"]),
                        "raw": sig.get("raw", ""),
                        "telegram_msg_id": mid,
                        # Risk & Leverage Tracking
                        **risk_info
                    }
                    inc_trades_today()
                    log.info(f"🟡 ENTRY PLACED {sig['symbol']} {sig['side'].upper()} zone={sig['entry_zone_low']}-{sig['entry_zone_high']} (id={trade_id})")

                    processed_count += 1

                    # Stop if we hit limits mid-batch
                    active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending","open")]
                    if len(active) >= MAX_CONCURRENT_TRADES or trades_today() >= MAX_TRADES_PER_DAY:
                        break

                if processed_count > 0:
                    log.debug(f"Processed {processed_count} message(s) from Telegram queue")

            save_state(STATE_FILE, st)

        except KeyboardInterrupt:
            log.info("Bye")
            telegram.stop()
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(3)

        time.sleep(max(1, POLL_SECONDS + random.uniform(0, max(0, POLL_JITTER_MAX))))


if __name__ == "__main__":
    main()
