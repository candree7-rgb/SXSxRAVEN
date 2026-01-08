"""
Quantum Entry Engine - Adaptive multi-layer entry system for zone-based signals

Features:
- Multi-layer limit orders across the entry zone
- Dynamic price tracking and order repositioning
- OrderBook liquidity analysis
- Momentum-based adjustments
- Guaranteed fill with progressive aggression
"""

import time
import math
from typing import Any, Dict, List, Optional, Tuple
from config import CATEGORY, DRY_RUN, QUANTUM_MAX_ENTRY_TIME, QUANTUM_TP1_PROXIMITY_EXIT


def calculate_optimal_timeouts(
    current_price: float,
    zone_low: float,
    zone_high: float,
    tp1_price: float,
    side: str,
    mode: str = "auto"
) -> Optional[Tuple[int, int, int]]:
    """
    Calculate optimal entry timeouts based on zone position and TP1 distance.

    Args:
        current_price: Current market price
        zone_low: Bottom of entry zone
        zone_high: Top of entry zone
        tp1_price: First take profit level
        side: "Buy" or "Sell"
        mode: "auto" (dynamic), "patient", "standard", "aggressive"

    Returns:
        Tuple of (phase1_timeout, phase2_timeout, total_timeout) in seconds
        None if signal should be skipped (price too far, TP1 too close, etc.)

    Strategy:
    - Bottom 25% of zone → Patient (90/90/180) - best entries!
    - Middle 50% of zone → Standard (60/60/120) - balance
    - Top 25% of zone → Aggressive (30/30/60) - catch breakout!
    - Outside zone → Still place orders, wait for retouch!

    NOTE: We no longer skip based on TP1 distance alone!
    Only skip if price ACTUALLY hit TP1 already (checked in execute()).
    """
    # Fixed modes
    if mode == "patient":
        return (90, 90, 180)
    elif mode == "standard":
        return (60, 60, 120)
    elif mode == "aggressive":
        return (30, 30, 60)

    # Auto mode - zone position based
    zone_range = zone_high - zone_low
    if zone_range <= 0:
        return (60, 60, 120)  # Fallback to standard

    # Calculate position in zone (0.0 = bottom, 1.0 = top)
    if side == "Buy":
        position_in_zone = (current_price - zone_low) / zone_range
    else:  # Sell
        position_in_zone = (zone_high - current_price) / zone_range

    # Don't clamp - allow > 1.0 (outside zone) for retouch logic
    # position_in_zone can be negative (way below) or > 1.0 (way above)

    # Calculate distance to TP1 (as percentage)
    if tp1_price:
        tp1_distance = abs(tp1_price - current_price) / current_price
    else:
        tp1_distance = 0.15  # Assume 15% if no TP1 given

    # Decision logic
    if position_in_zone < 0.25:
        # Bottom 25% of zone - PATIENT MODE
        # Price is at good entry level, wait for best fill
        return (90, 90, 180)

    elif position_in_zone < 0.75:
        # Middle 50% of zone - STANDARD MODE
        # Decent entry level, moderate urgency
        return (60, 60, 120)

    else:
        # Top 25% of zone OR outside zone - AGGRESSIVE MODE
        # Place orders and wait for retouch or immediate fill
        # NOTE: We removed the TP1 < 5% skip - only skip if ACTUALLY hit TP1!
        return (30, 30, 60)


class QuantumEntryEngine:
    """
    Adaptive Entry System for zone-based signals.

    Strategy:
    1. Place 4 layered limit orders across the entry zone
    2. Monitor price movement and momentum
    3. Dynamically reposition orders if price moves away
    4. Progressively increase aggression over time
    5. Guarantee fill with market order after timeout
    6. Cancel entry if price already reached TP1 (trade already missed)
    """

    def __init__(self, bybit, symbol: str, side: str, zone_low: float, zone_high: float,
                 base_qty: float, logger, tick_size: float, qty_step: float, min_qty: float,
                 tp1_price: float = None, phase1_timeout: int = 90, phase2_timeout: int = 90,
                 total_timeout: int = 180, instant_market_pct: float = 0.0):
        self.bybit = bybit
        self.symbol = symbol
        self.side = side  # "Buy" or "Sell"
        self.zone_low = zone_low
        self.zone_high = zone_high
        self.zone_mid = (zone_low + zone_high) / 2
        self.zone_range = zone_high - zone_low
        self.base_qty = base_qty
        self.log = logger
        self.tp1_price = tp1_price  # For checking if trade already missed
        self.instant_market_pct = instant_market_pct  # % of position to fill instantly with market order

        # Precision
        self.tick_size = tick_size
        self.qty_step = qty_step
        self.min_qty = min_qty

        # State
        self.filled_qty = 0.0
        self.avg_entry = 0.0
        self.active_orders = {}
        self.start_time = time.time()
        self.price_history = []
        self.instant_fill_info = None  # Track instant market order fill

        # Timeouts (seconds) - now configurable
        self.PHASE_1_TIMEOUT = phase1_timeout   # Patient phase (best prices)
        self.PHASE_2_TIMEOUT = phase2_timeout   # Active phase (chase if needed)
        self.TOTAL_TIMEOUT = total_timeout      # Max total time

    def execute(self) -> Optional[Dict[str, Any]]:
        """
        Main entry execution logic.

        Returns dict with fill info on success:
        {
            "filled": True,
            "avg_entry": 0.1234,
            "total_qty": 10.5,
            "fills": [...]
        }

        Returns None if failed to enter (price moved too far, etc.)
        """
        try:
            # Phase 1: Initial assessment
            initial_price = self.bybit.last_price(CATEGORY, self.symbol)

            # Check if price is already way outside zone → SKIP
            if self._price_too_far(initial_price):
                self.log.info(f"⏭️  SKIP {self.symbol} - price too far from zone ({initial_price})")
                return None

            # Check if price already reached TP1 → SKIP (trade already missed)
            if self._price_past_tp1(initial_price):
                self.log.info(f"⏭️  SKIP {self.symbol} - price already past TP1 ({initial_price} vs TP1 {self.tp1_price})")
                return None

            # HYBRID ENTRY: Check if we should place instant market order
            # If price is in top 25% of zone and instant_market_pct > 0
            position_in_zone = self._calculate_position_in_zone(initial_price)

            if self.instant_market_pct > 0 and position_in_zone > 0.75:
                # Top 25% of zone - place instant partial market order
                instant_qty = self.base_qty * (self.instant_market_pct / 100.0)
                instant_qty = self._round_qty(instant_qty)

                if instant_qty >= self.min_qty:
                    self.log.info(f"⚡ HYBRID ENTRY: Price in top 25% of zone ({position_in_zone:.1%}) - placing instant market order for {self.instant_market_pct}% ({instant_qty} qty)")
                    instant_fill = self._place_instant_market_order(instant_qty)

                    if instant_fill:
                        self.instant_fill_info = instant_fill
                        self.filled_qty = instant_fill.get("qty", 0)
                        self.avg_entry = instant_fill.get("price", initial_price)
                        self.log.info(f"✅ Instant fill: {self.filled_qty} @ {self.avg_entry:.6f}")
                    else:
                        self.log.warning(f"⚠️  Instant market order failed, continuing with limit orders")

            # Analyze orderbook for liquidity
            liquidity_map = self._analyze_orderbook()

            # Phase 2: Place initial layered orders (for remaining quantity)
            remaining_qty = self.base_qty - self.filled_qty
            if remaining_qty >= self.min_qty:
                self._place_initial_layers(initial_price, liquidity_map, target_qty=remaining_qty)
            else:
                # Already fully filled with instant order
                if self.filled_qty >= self.base_qty * 0.95:
                    return self._finalize_entry()

            # Phase 3: Dynamic tracking loop with adaptive exit
            last_check = time.time()
            max_entry_seconds = QUANTUM_MAX_ENTRY_TIME * 60  # Convert minutes to seconds

            while self.filled_qty < self.base_qty * 0.95:  # 95% fill target
                elapsed = time.time() - self.start_time

                # Timeout check - use adaptive max entry time
                if elapsed > max_entry_seconds:
                    self.log.info(f"⏱️  Max entry time reached ({QUANTUM_MAX_ENTRY_TIME} min) for {self.symbol} - attempting emergency fill")
                    return self._emergency_fill()

                # ADAPTIVE EXIT: Check if price is too close to TP1
                # Stop entry phase early if price moved too far toward TP1
                if self.tp1_price and QUANTUM_TP1_PROXIMITY_EXIT > 0:
                    current_price = self.bybit.last_price(CATEGORY, self.symbol)

                    # Calculate how far price has moved from entry toward TP1
                    if self.side == "Buy":
                        # LONG: entry < TP1 (price rising toward TP1)
                        entry_to_tp1 = self.tp1_price - self.zone_mid
                        current_to_tp1 = self.tp1_price - current_price
                        if entry_to_tp1 > 0:
                            proximity = 1.0 - (current_to_tp1 / entry_to_tp1)  # 0.0 = at entry, 1.0 = at TP1
                        else:
                            proximity = 0.0
                    else:
                        # SHORT: entry > TP1 (price falling toward TP1)
                        entry_to_tp1 = self.zone_mid - self.tp1_price
                        current_to_tp1 = current_price - self.tp1_price
                        if entry_to_tp1 > 0:
                            proximity = 1.0 - (current_to_tp1 / entry_to_tp1)  # 0.0 = at entry, 1.0 = at TP1
                        else:
                            proximity = 0.0

                    # If price is too close to TP1 → stop entry phase
                    if proximity >= QUANTUM_TP1_PROXIMITY_EXIT:
                        self.log.info(f"🚀 Price near TP1 ({proximity:.1%} toward target, threshold {QUANTUM_TP1_PROXIMITY_EXIT:.0%}) - stopping entry phase")
                        # Accept whatever fill we have so far
                        if self.filled_qty > 0:
                            return self._finalize_entry()
                        else:
                            self.log.warning(f"⚠️  No fill yet but price near TP1 - attempting emergency fill")
                            return self._emergency_fill()

                # Check for fills every 2 seconds
                if time.time() - last_check > 2:
                    self._check_fills()
                    last_check = time.time()

                # Dynamic adjustments based on phase (using original timeouts for phase transitions)
                if elapsed < self.PHASE_1_TIMEOUT:
                    phase = "PATIENT"
                elif elapsed < self.PHASE_1_TIMEOUT + self.PHASE_2_TIMEOUT:
                    phase = "ACTIVE"
                else:
                    phase = "AGGRESSIVE"

                self._adaptive_reposition(phase)

                time.sleep(2)

            # Successfully filled!
            return self._finalize_entry()

        except Exception as e:
            self.log.error(f"QuantumEntry error for {self.symbol}: {e}")
            # Clean up orders
            self._cancel_all_orders()
            return None

    def _price_too_far(self, price: float) -> bool:
        """
        Check if current price is too far from zone to enter.

        Allows some leeway for retouch scenarios (price came back to zone).
        Only skip if price is REALLY far away (>10% outside zone).
        """
        if self.side == "Buy":
            # For BUY: price too high above zone = skip if > 10% above
            return price > self.zone_high * 1.10  # 10% above zone top
        else:
            # For SELL: price too low below zone = skip if > 10% below
            return price < self.zone_low * 0.90  # 10% below zone bottom

    def _price_past_tp1(self, price: float) -> bool:
        """Check if price already reached TP1 (trade already missed)."""
        if not self.tp1_price:
            return False

        if self.side == "Buy":
            # For BUY/LONG: TP1 is above entry, bad if price already >= TP1
            return price >= self.tp1_price
        else:
            # For SELL/SHORT: TP1 is below entry, bad if price already <= TP1
            return price <= self.tp1_price

    def _analyze_orderbook(self) -> Dict[float, float]:
        """
        Fetch orderbook and identify liquidity clusters.

        Returns dict: {price: total_volume_at_level}
        """
        try:
            ob = self.bybit.orderbook(CATEGORY, self.symbol, limit=50)

            # For BUY signals, we care about ASK side (selling into our buys)
            # For SELL signals, we care about BID side (buying into our sells)
            relevant_side = ob['asks'] if self.side == "Buy" else ob['bids']

            # Group nearby price levels (within 0.2% = one cluster)
            clusters = {}
            for price, size in relevant_side:
                # Only care about levels within our zone
                if self.zone_low <= price <= self.zone_high:
                    # Round to cluster (0.2% buckets)
                    cluster_key = round(price / (self.tick_size * 5)) * (self.tick_size * 5)
                    clusters[cluster_key] = clusters.get(cluster_key, 0) + size

            return clusters

        except Exception as e:
            self.log.debug(f"OrderBook fetch failed: {e}")
            return {}

    def _calculate_optimal_levels(self, current_price: float, liquidity_map: Dict[float, float]) -> List[float]:
        """
        Calculate 4 optimal entry levels based on:
        - Zone position
        - Current price
        - Liquidity clusters
        """
        # Base levels in zone (aggressive → conservative)
        if self.side == "Buy":
            # BUY: Lower prices are better
            base_levels = [
                self.zone_low + (self.zone_range * 0.05),   # 5% in zone - BEST
                self.zone_low + (self.zone_range * 0.30),   # 30% in zone - GOOD
                self.zone_low + (self.zone_range * 0.60),   # 60% in zone - OK
                self.zone_low + (self.zone_range * 0.90),   # 90% in zone - BACKUP
            ]
        else:
            # SELL: Higher prices are better
            base_levels = [
                self.zone_high - (self.zone_range * 0.05),
                self.zone_high - (self.zone_range * 0.30),
                self.zone_high - (self.zone_range * 0.60),
                self.zone_high - (self.zone_range * 0.90),
            ]

        # Adjust based on current price position in zone
        if current_price < self.zone_low:
            # Price below zone - adjust UP to catch it
            base_levels = [p * 1.002 for p in base_levels]
        elif current_price > self.zone_high:
            # Price above zone - adjust DOWN to catch it
            base_levels = [p * 0.998 for p in base_levels]

        # Snap to liquidity if available
        adjusted_levels = []
        for level in base_levels:
            # Find liquidity within ±0.5% of this level
            nearby_liq = self._find_nearby_liquidity(liquidity_map, level, tolerance_pct=0.5)
            adjusted_levels.append(nearby_liq if nearby_liq else level)

        # Round to valid tick size
        return [self._round_price(p) for p in adjusted_levels]

    def _find_nearby_liquidity(self, liquidity_map: Dict[float, float], target: float, tolerance_pct: float = 0.5) -> Optional[float]:
        """Find largest liquidity cluster near target price."""
        if not liquidity_map:
            return None

        tolerance = target * (tolerance_pct / 100.0)
        candidates = {p: vol for p, vol in liquidity_map.items()
                     if abs(p - target) <= tolerance}

        if not candidates:
            return None

        # Return price with highest volume
        return max(candidates.items(), key=lambda x: x[1])[0]

    def _place_initial_layers(self, current_price: float, liquidity_map: Dict[float, float], target_qty: float = None) -> None:
        """
        Place 4 layered limit orders across the zone.

        Args:
            target_qty: Quantity to place (defaults to base_qty if not specified)
        """
        if target_qty is None:
            target_qty = self.base_qty

        optimal_levels = self._calculate_optimal_levels(current_price, liquidity_map)

        # Quantity splits: aggressive layers get more size
        splits = [0.30, 0.35, 0.25, 0.10]  # 30% best, 35% good, 25% ok, 10% backup

        self.log.info(f"📊 Placing {len(splits)} entry layers for {self.symbol} (target: {target_qty:.4f}):")

        for i, (price, split) in enumerate(zip(optimal_levels, splits)):
            qty = self._round_qty(target_qty * split)

            order_id = self._place_limit_order(price, qty)

            if order_id:
                self.active_orders[f"layer_{i}"] = {
                    "order_id": order_id,
                    "price": price,
                    "qty": qty,
                    "layer": i,
                    "placed_at": time.time()
                }

                zone_pct = abs(price - self.zone_low) / self.zone_range * 100 if self.side == "Buy" else abs(self.zone_high - price) / self.zone_range * 100
                self.log.info(f"   Layer {i+1}: {qty:.4f} @ {price:.6f} ({zone_pct:.0f}% in zone)")

    def _check_fills(self) -> bool:
        """Check if any orders were filled and update state."""
        if DRY_RUN:
            return False

        try:
            # Fetch order history for this symbol
            history = self.bybit.order_history(CATEGORY, self.symbol, limit=20)

            for key, order in list(self.active_orders.items()):
                order_id = order['order_id']

                # Check if order is in history as filled
                for h in history:
                    if h.get('orderId') == order_id and h.get('orderStatus') == 'Filled':
                        fill_price = float(h.get('avgPrice') or order['price'])
                        fill_qty = float(h.get('cumExecQty') or order['qty'])

                        self._update_avg_entry(fill_price, fill_qty)

                        self.log.info(f"✅ Layer {order['layer']+1} FILLED: {fill_qty:.4f} @ {fill_price:.6f}")

                        # Remove from active orders
                        del self.active_orders[key]

                        # Cancel other layers (we only want one fill)
                        self._cancel_all_orders()

                        return True

            return False

        except Exception as e:
            self.log.debug(f"Fill check error: {e}")
            return False

    def _adaptive_reposition(self, phase: str) -> None:
        """Dynamically adjust orders based on price movement and phase."""

        try:
            current_price = self.bybit.last_price(CATEGORY, self.symbol)

            # CRITICAL: If price already past TP1 → Cancel all and exit
            if self._price_past_tp1(current_price):
                self.log.warning(f"⚠️  Price past TP1 for {self.symbol} - cancelling entry (missed trade)")
                self._cancel_all_orders()
                raise RuntimeError("Price past TP1 - trade missed")

            # Track price history for momentum calculation
            self.price_history.append((time.time(), current_price))
            self.price_history = self.price_history[-10:]  # Keep last 10 samples (20 seconds)

            # Calculate 5-second momentum
            momentum_5s = 0.0
            if len(self.price_history) >= 3:
                price_5s_ago = self.price_history[-3][1]
                momentum_5s = (current_price - price_5s_ago) / price_5s_ago

            # Determine if price is moving away from our orders
            if self.side == "Buy":
                moving_away = momentum_5s > 0.002  # Rising 0.2% in 5s
                price_favorable = current_price < self.zone_mid
            else:
                moving_away = momentum_5s < -0.002  # Falling 0.2% in 5s
                price_favorable = current_price > self.zone_mid

            # Reposition logic based on phase
            if phase == "PATIENT" and price_favorable:
                # Try to get even better prices
                self._optimize_for_best_price(current_price)

            elif phase in ("ACTIVE", "AGGRESSIVE") and moving_away:
                # Price escaping → chase it
                aggression = 0.003 if phase == "ACTIVE" else 0.005
                self._chase_price(current_price, aggression)

        except Exception as e:
            self.log.debug(f"Reposition error: {e}")

    def _optimize_for_best_price(self, current_price: float) -> None:
        """Move most aggressive order DOWN for better entry when price is favorable."""

        layer_0 = self.active_orders.get('layer_0')
        if not layer_0:
            return

        # If price dropped below our best order → move it down!
        if self.side == "Buy" and current_price < layer_0['price'] * 0.997:
            try:
                self._cancel_order(layer_0['order_id'])
            except:
                pass

            # New better price: just above current
            new_price = max(current_price * 1.0005, self.zone_low)
            new_price = self._round_price(new_price)

            new_order_id = self._place_limit_order(new_price, layer_0['qty'])

            if new_order_id:
                self.active_orders['layer_0'] = {
                    "order_id": new_order_id,
                    "price": new_price,
                    "qty": layer_0['qty'],
                    "layer": 0,
                    "placed_at": time.time()
                }
                self.log.debug(f"🎯 Optimized Layer 0 → {new_price:.6f}")

    def _chase_price(self, current_price: float, aggression: float) -> None:
        """Move orders UP to chase escaping price."""

        threshold = aggression  # 0.3% or 0.5%

        for key, order in list(self.active_orders.items()):
            if self.side == "Buy":
                too_far = order['price'] < current_price * (1 - threshold)
            else:
                too_far = order['price'] > current_price * (1 + threshold)

            if too_far:
                # Cancel and replace closer to current price
                try:
                    self._cancel_order(order['order_id'])
                except:
                    pass

                # New price: slightly more aggressive than current market
                if self.side == "Buy":
                    new_price = current_price * 0.9995  # 0.05% below market
                else:
                    new_price = current_price * 1.0005  # 0.05% above market

                # Keep within zone
                new_price = max(min(new_price, self.zone_high), self.zone_low)
                new_price = self._round_price(new_price)

                new_order_id = self._place_limit_order(new_price, order['qty'])

                if new_order_id:
                    self.active_orders[key] = {
                        "order_id": new_order_id,
                        "price": new_price,
                        "qty": order['qty'],
                        "layer": order['layer'],
                        "placed_at": time.time()
                    }
                    self.log.debug(f"🔄 Chased Layer {order['layer']} → {new_price:.6f}")

    def _emergency_fill(self) -> Optional[Dict[str, Any]]:
        """Timeout → Use market order to guarantee fill."""

        remaining_qty = self.base_qty - self.filled_qty

        if remaining_qty < self.min_qty:
            # Already mostly filled
            return self._finalize_entry()

        self.log.info(f"🚨 Emergency market fill for {self.symbol}: {remaining_qty:.4f}")

        # Cancel all pending orders
        self._cancel_all_orders()

        if DRY_RUN:
            # Simulate market fill at zone_high (worst case)
            fill_price = self.zone_high if self.side == "Buy" else self.zone_low
            self._update_avg_entry(fill_price, remaining_qty)
            return self._finalize_entry()

        # Place market order
        try:
            body = {
                "category": CATEGORY,
                "symbol": self.symbol,
                "side": self.side,
                "orderType": "Market",
                "qty": f"{remaining_qty:.10f}",
                "reduceOnly": False,
            }

            resp = self.bybit.place_order(body)
            order_id = (resp.get("result") or {}).get("orderId")

            if order_id:
                time.sleep(1)  # Wait for fill

                # Fetch fill info
                history = self.bybit.order_history(CATEGORY, self.symbol, limit=5)
                for h in history:
                    if h.get('orderId') == order_id:
                        fill_price = float(h.get('avgPrice') or self.zone_high)
                        fill_qty = float(h.get('cumExecQty') or remaining_qty)
                        self._update_avg_entry(fill_price, fill_qty)
                        break

        except Exception as e:
            self.log.error(f"Emergency fill failed: {e}")
            return None

        return self._finalize_entry()

    def _update_avg_entry(self, fill_price: float, fill_qty: float) -> None:
        """Update average entry price with new fill."""
        total_qty = self.filled_qty + fill_qty
        self.avg_entry = (self.avg_entry * self.filled_qty + fill_price * fill_qty) / total_qty
        self.filled_qty = total_qty

    def _finalize_entry(self) -> Dict[str, Any]:
        """Clean up and return fill info."""
        self._cancel_all_orders()

        return {
            "filled": True,
            "avg_entry": self.avg_entry,
            "total_qty": self.filled_qty,
            "elapsed": time.time() - self.start_time
        }

    # ----- Order Helpers -----

    def _place_limit_order(self, price: float, qty: float) -> Optional[str]:
        """Place a limit order and return order ID."""
        if DRY_RUN:
            return f"DRY_{int(time.time())}"

        try:
            body = {
                "category": CATEGORY,
                "symbol": self.symbol,
                "side": self.side,
                "orderType": "Limit",
                "qty": f"{qty:.10f}",
                "price": f"{price:.10f}",
                "timeInForce": "GTC",
                "reduceOnly": False,
            }

            resp = self.bybit.place_order(body)
            order_id = (resp.get("result") or {}).get("orderId")
            return order_id

        except Exception as e:
            self.log.warning(f"Order placement failed @ {price}: {e}")
            return None

    def _cancel_order(self, order_id: str) -> None:
        """Cancel an order by ID."""
        if DRY_RUN or not order_id or order_id.startswith("DRY_"):
            return

        try:
            self.bybit.cancel_order({
                "category": CATEGORY,
                "symbol": self.symbol,
                "orderId": order_id
            })
        except:
            pass  # Order might already be filled/cancelled

    def _cancel_all_orders(self) -> None:
        """Cancel all active orders for this entry."""
        for order in list(self.active_orders.values()):
            self._cancel_order(order['order_id'])
        self.active_orders.clear()

    # ----- Hybrid Entry Helpers -----

    def _calculate_position_in_zone(self, price: float) -> float:
        """
        Calculate position of price within zone.

        Returns:
            0.0 = bottom of zone, 1.0 = top of zone
        """
        if self.zone_range <= 0:
            return 0.5

        if self.side == "Buy":
            # For BUY: zone_low (bottom) = 0.0, zone_high (top) = 1.0
            position = (price - self.zone_low) / self.zone_range
        else:
            # For SELL: zone_high (bottom) = 0.0, zone_low (top) = 1.0
            position = (self.zone_high - price) / self.zone_range

        return max(0.0, min(1.0, position))

    def _place_instant_market_order(self, qty: float) -> Optional[Dict[str, Any]]:
        """
        Place instant market order for partial fill.

        Returns:
            Dict with {"qty": filled_qty, "price": fill_price} on success
            None on failure
        """
        if DRY_RUN:
            # DRY_RUN: simulate instant fill at current price
            current_price = self.bybit.last_price(CATEGORY, self.symbol)
            return {"qty": qty, "price": current_price}

        try:
            body = {
                "category": CATEGORY,
                "symbol": self.symbol,
                "side": self.side,
                "orderType": "Market",
                "qty": f"{qty:.10f}",
                "reduceOnly": False,
            }

            resp = self.bybit.place_order(body)
            result = resp.get("result") or {}
            order_id = result.get("orderId")

            if not order_id:
                return None

            # Wait briefly for fill
            time.sleep(0.5)

            # Fetch order details to get fill price
            try:
                order_info = self.bybit.query_order(CATEGORY, self.symbol, order_id)
                result = order_info.get("result") or {}

                if result.get("orderStatus") == "Filled":
                    fill_price = float(result.get("avgPrice") or 0)
                    fill_qty = float(result.get("cumExecQty") or 0)

                    if fill_price > 0 and fill_qty > 0:
                        return {"qty": fill_qty, "price": fill_price}
            except Exception as e:
                self.log.warning(f"Failed to fetch instant order details: {e}")

            # Fallback: assume filled at current price
            current_price = self.bybit.last_price(CATEGORY, self.symbol)
            return {"qty": qty, "price": current_price}

        except Exception as e:
            self.log.error(f"Instant market order failed: {e}")
            return None

    # ----- Precision Helpers -----

    def _round_price(self, price: float) -> float:
        """Round price to valid tick size."""
        if self.tick_size <= 0:
            return price
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def _round_qty(self, qty: float) -> float:
        """Round qty down to valid step and ensure min qty."""
        qty = math.floor(qty / self.qty_step) * self.qty_step
        if qty < self.min_qty:
            qty = self.min_qty
        return float(f"{qty:.10f}")
