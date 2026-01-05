import re
import hashlib
from typing import Any, Dict, Optional, List

NUM = r"([0-9]+(?:\.[0-9]+)?)"

# ============================================================
# RAVEN PRO FORMAT
# ============================================================
# Example Signal:
# #AUCTION/USDT 📉 SELL
# 🔹Entry zone: 5.9200 - 6.1000
# 💰TP1 5.8700
# 💰TP2 5.8200
# 💰TP3 5.7600
# 💰TP4 5.7100
# 💰TP5 5.6600
# 💰TP6 5.5920
# 🚫SL 6.2100
# 〽️Leverage cross 10x
# ============================================================

# Symbol and Side: "#AUCTION/USDT 📉 SELL" or "ALGO/USDT 📈 BUY"
RE_SYMBOL_SIDE = re.compile(
    r"#?([A-Z0-9]+)/USDT\s*📈\s*BUY|#?([A-Z0-9]+)/USDT\s*📉\s*SELL",
    re.I
)

# Entry zone: "🔹Entry zone: 0.1170 - 0.1209"
RE_ENTRY_ZONE = re.compile(
    r"Entry\s+zone\s*:\s*" + NUM + r"\s*-\s*" + NUM,
    re.I
)

# TP: "💰TP1 5.8700" or "💰TP1: 5.8700"
RE_TP = re.compile(
    r"TP(\d+)\s*:?\s*" + NUM,
    re.I
)

# SL: "🚫SL 6.2100" or "🚫SL: 6.2100"
RE_SL = re.compile(
    r"SL\s*:?\s*" + NUM,
    re.I
)

# Leverage: "〽️Leverage cross 10x" or "Leverage cross 10x"
RE_LEVERAGE = re.compile(
    r"Leverage\s+(?:cross\s+)?(\d+)x",
    re.I
)

# Status patterns to detect if trade is still valid
RE_CLOSED = re.compile(
    r"CLOSED|CANCELLED|STOP\s*LOSS\s*HIT|ALL\s*TPS?\s*HIT",
    re.I
)


def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    """
    Parse Raven Pro signal format.

    Returns None if:
    - Not a signal (no symbol/side match)
    - Trade is already CLOSED/CANCELLED
    - Cannot parse entry zone
    """
    # Skip already closed trades
    if RE_CLOSED.search(text):
        return None

    # Parse symbol and side
    ms = RE_SYMBOL_SIDE.search(text)
    if not ms:
        return None

    # Extract symbol (either group 1 for BUY or group 2 for SELL)
    symbol_buy = ms.group(1)
    symbol_sell = ms.group(2)

    if symbol_buy:
        side = "buy"
        base = symbol_buy.upper()
    elif symbol_sell:
        side = "sell"
        base = symbol_sell.upper()
    else:
        return None

    symbol = f"{base}USDT"

    # Parse entry zone (REQUIRED for Raven Pro)
    mzone = RE_ENTRY_ZONE.search(text)
    if not mzone:
        return None

    zone_low = float(mzone.group(1))
    zone_high = float(mzone.group(2))

    # Sanity check: zone_low should be lower than zone_high
    if zone_low > zone_high:
        zone_low, zone_high = zone_high, zone_low

    # Parse TP prices (up to 6 TPs)
    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        # Keep in order
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    tps = [p for p in tps if p > 0]

    # Parse Stop Loss
    sl = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    # Parse leverage (optional)
    leverage = None
    mlev = RE_LEVERAGE.search(text)
    if mlev:
        leverage = int(mlev.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,          # buy / sell
        "entry_zone_low": zone_low,
        "entry_zone_high": zone_high,
        "entry_zone_mid": (zone_low + zone_high) / 2,
        "tp_prices": tps,
        "dca_prices": [],      # Raven Pro doesn't use DCAs
        "sl_price": sl,
        "leverage": leverage,
        "raw": text,
    }


def parse_signal_update(text: str) -> Dict[str, Any]:
    """
    Parse signal for SL/TP updates only.

    Unlike parse_signal(), this does NOT require entry zone.
    Used for checking if an existing signal was updated.

    Returns dict with sl_price and tp_prices (may be None/empty if not found).
    """
    result = {
        "sl_price": None,
        "tp_prices": [],
    }

    # Parse Stop Loss
    msl = RE_SL.search(text)
    if msl:
        result["sl_price"] = float(msl.group(1))

    # Parse TP prices
    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    result["tp_prices"] = [p for p in tps if p > 0]

    return result


def signal_hash(sig: Dict[str, Any]) -> str:
    """Generate unique hash for signal deduplication."""
    core = f"{sig.get('symbol')}|{sig.get('side')}|{sig.get('entry_zone_low')}|{sig.get('entry_zone_high')}|{sig.get('tp_prices')}|{sig.get('sl_price')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()
