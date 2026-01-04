import re
import hashlib
from typing import Any, Dict, Optional, List

NUM = r"([0-9]+(?:\.[0-9]+)?)"

# ============================================================
# AO TRADING V2 FORMAT (Plain text, no embeds)
# ============================================================
# Example Signal (fresh):
# <@&1428362286581551125> 📊 NEW SIGNAL • SAPIEN • Entry $0.13236
#
# BUY SAPIENUSDT Entry: 0.13236 CMP 25x LEVERAGE
#
# **SL:** `0.12500` ⏳ *Active*
#
# **TPs:**
# 🎯 **TP1:** `0.13501` **→ NEXT**
# ⏳ **TP2:** `0.13765` *Pending*
# ⏳ **TP3:** `0.14295` *Pending*
# ⏳ **TP4:** `0.15354` *Pending*
# ⏳ **TP5:** `0.17472` *Pending*
# ============================================================

# Header line: "📊 NEW SIGNAL • SAPIEN • Entry $0.13236"
RE_HEADER = re.compile(
    r"NEW\s+SIGNAL\s*[•·]\s*([A-Z0-9]+)\s*[•·]\s*Entry\s*\$?" + NUM,
    re.I
)

# Side and Symbol: "BUY SAPIENUSDT Entry: 0.13236 CMP 25x LEVERAGE"
# Also supports: "SELL LIGHTUSDT Entry: 1.16170 CMP 25x LEVERAGE"
RE_SIDE_SYMBOL = re.compile(
    r"(BUY|SELL)\s+([A-Z0-9]+)(USDT|USDC|BUSD)\s+Entry\s*:\s*" + NUM,
    re.I
)

# Entry price fallback (if not in side line)
RE_ENTRY = re.compile(
    r"Entry\s*:\s*`?\$?" + NUM + r"`?",
    re.I
)

# TP: "**TP1:** `0.13501`" or "✅ **TP1:** `0.13501` *HIT*"
RE_TP = re.compile(
    r"\*{0,2}TP(\d+)\s*:\s*\*{0,2}\s*`?\$?" + NUM + r"`?",
    re.I
)

# DCA (optional - usually not present in V2 signals):
# "**DCA1:** `0.12000`" or "⏳ **DCA1:** `0.12000` *Pending*"
RE_DCA = re.compile(
    r"\*{0,2}DCA\s*#?\s*(\d+)\s*:\s*\*{0,2}\s*`?\$?" + NUM + r"`?",
    re.I
)

# Stop Loss: "**SL:** `0.12500`"
RE_SL = re.compile(
    r"\*{0,2}SL\s*:\s*\*{0,2}\s*`?\$?" + NUM + r"`?",
    re.I
)

# Leverage: "25x LEVERAGE"
RE_LEVERAGE = re.compile(
    r"(\d+)x\s+LEVERAGE",
    re.I
)

# Status patterns to detect if trade is still valid for entry
RE_CLOSED = re.compile(
    r"TRADE\s+CLOSED|CLOSED\s+AT\s+BREAKEVEN|TRADE\s+CANCELLED|"
    r"Trade closed without entry|❌ TRADE CANCELLED",
    re.I
)


def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    """
    Parse AO Trading V2 signal format (plain text, no embeds).

    Returns None if:
    - Not a NEW SIGNAL message
    - Trade is already CLOSED/CANCELLED
    - Cannot parse symbol/side or entry price
    """
    # We only want fresh "NEW SIGNAL" entries
    if "NEW SIGNAL" not in text.upper():
        return None

    # Skip already closed/cancelled trades
    if RE_CLOSED.search(text):
        return None

    # Parse side and symbol from "BUY SAPIENUSDT Entry: 0.13236"
    ms = RE_SIDE_SYMBOL.search(text)
    if not ms:
        # Try header line as fallback
        mh = RE_HEADER.search(text)
        if not mh:
            return None
        # Can't determine side from header alone, need the BUY/SELL line
        return None

    side_word = ms.group(1).upper()
    base = ms.group(2).upper()
    quote_from_signal = ms.group(3).upper()
    trigger = float(ms.group(4))

    side = "sell" if side_word == "SELL" else "buy"
    symbol = f"{base}{quote_from_signal}"

    # Parse TP prices (dynamic: 3, 4, or 5 TPs)
    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        # Keep in order
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    tps = [p for p in tps if p > 0]

    # Parse DCA prices (optional - usually not present)
    dcas: List[float] = []
    for m in RE_DCA.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(dcas) < idx:
            dcas.append(0.0)
        dcas[idx-1] = price
    dcas = [p for p in dcas if p > 0]

    # Parse Stop Loss
    sl = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    # Parse leverage (optional, for logging)
    leverage = None
    mlev = RE_LEVERAGE.search(text)
    if mlev:
        leverage = int(mlev.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,          # buy / sell
        "trigger": trigger,
        "tp_prices": tps,
        "dca_prices": dcas,
        "sl_price": sl,
        "leverage": leverage,  # Optional: leverage from signal
        "raw": text,
    }


def parse_signal_update(text: str) -> Dict[str, Any]:
    """
    Parse signal for SL/DCA/TP updates only.

    Unlike parse_signal(), this does NOT require "NEW SIGNAL" in text.
    Used for checking if an existing signal was updated with new SL/DCA values.

    Returns dict with sl_price, dca_prices, and tp_prices (may be None/empty if not found).
    """
    result = {
        "sl_price": None,
        "dca_prices": [],
        "tp_prices": [],
    }

    # Parse Stop Loss
    msl = RE_SL.search(text)
    if msl:
        result["sl_price"] = float(msl.group(1))

    # Parse DCA prices (optional)
    dcas: List[float] = []
    for m in RE_DCA.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(dcas) < idx:
            dcas.append(0.0)
        dcas[idx-1] = price
    result["dca_prices"] = [p for p in dcas if p > 0]

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
    core = f"{sig.get('symbol')}|{sig.get('side')}|{sig.get('trigger')}|{sig.get('tp_prices')}|{sig.get('dca_prices')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()
