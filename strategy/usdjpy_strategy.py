import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
import datetime
import sys
import os
import requests
from dotenv import load_dotenv
try:
    from numba import njit
    HAS_NUMBA = True
except Exception:
    HAS_NUMBA = False
import time
import logging
from collections import defaultdict

# logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

class USDJPYStrategy:
    """
    A Supply and Demand strategy for USD/JPY aiming for a high R:R.

    Logic:
    1. Identifies Supply and Demand zones based on strong price moves away from a consolidated base.
       - Supply: A sharp drop after a base (Rally-Base-Drop or Drop-Base-Drop).
       - Demand: A sharp rally after a base (Drop-Base-Rally or Rally-Base-Rally).
    2. Enters a trade when price returns to a 'fresh' (untested) zone.
    3. The Stop Loss is placed just outside the zone.
    4. Enforces a tuned Risk-to-Reward ratio for better win rate.
    """

    def __init__(self, target_pair="USD/JPY",
                 zone_lookback=300,
                 base_max_candles=4,
                 move_min_ratio=4.5,
                 zone_width_max_pips=14,
                 risk_reward_ratio=3.0, # Set back to 3.0 for 1:3 R:R
                 sl_buffer_pips=4.0,
                 ema_periods: Optional[List[int]] = None,
                 rsi_period: int = 14,
                 rsi_oversold: float = 30.0,
                 rsi_overbought: float = 70.0,
                 enable_volume_filter: bool = False,
                 min_volume_factor: float = 1.2,
                 session_hours_utc: Optional[List[str]] = ("00:00-00:59", "04:00-05:59", "07:00-07:59", "13:00-15:59", "19:00-19:59", "22:00-23:59"),
                 enable_session_hours_filter: bool = True, # Enabled with approved windows
                 enable_news_sentiment_filter: bool = False
                 ):
        self.target_pair = target_pair
        self.pip_size = 0.01 # Corrected for JPY pairs
        
        # Zone parameters
        self.zone_lookback = zone_lookback
        self.base_max_candles = base_max_candles
        self.move_min_ratio = move_min_ratio
        self.zone_width_max_pips = zone_width_max_pips

                 # Risk Management
        self.risk_reward_ratio = risk_reward_ratio
        self.sl_buffer_pips = sl_buffer_pips
        
        # Indicator parameters
        self.ema_periods = ema_periods if ema_periods is not None else []
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.enable_volume_filter = enable_volume_filter
        self.min_volume_factor = min_volume_factor
        self.session_hours_utc = list(session_hours_utc) if session_hours_utc else []
        self.enable_session_hours_filter = enable_session_hours_filter
        self.enable_news_sentiment_filter = enable_news_sentiment_filter

        # Internal State
        self.zones = [] # Stores {'type', 'price_high', 'price_low', 'created_at', 'is_fresh'}
        self.last_candle_index = -1
        self.blocking_reasons_counts = defaultdict(int) # Initialize counter for blocking reasons
        # Profiling
        self.profile_enabled = os.getenv("BACKTEST_PROFILE", "0") == "1"
        self.prof = defaultdict(float)
        self.prof_calls = 0

    # =====================
    # Numba-accelerated zone scan (logic-identical, faster)
    # =====================
    if HAS_NUMBA:
        @staticmethod
        @njit(cache=True, fastmath=False)
        def _zone_scan_numba(highs, lows, closes, candle_ranges,
                             zone_lookback, base_max_candles,
                             move_min_ratio, zone_width_max_pips,
                             pip_size, start_i, upto_idx):
            n = upto_idx + 1
            type_code = np.zeros(n, dtype=np.int8)  # 1=demand, -1=supply, 0=none
            base_high_out = np.zeros(n, dtype=np.float64)
            base_low_out = np.zeros(n, dtype=np.float64)
            for i in range(start_i, upto_idx + 1):
                window_start_min = 0
                tmp = i - zone_lookback
                if tmp > window_start_min:
                    window_start_min = tmp
                # Try base_len 1..base_max_candles
                for base_len in range(1, base_max_candles + 1):
                    base_start = i - base_len
                    if base_start < window_start_min:
                        break
                    # Compute avg_base_range over [base_start, i)
                    cnt = 0
                    s = 0.0
                    for k in range(base_start, i):
                        s += candle_ranges[k]
                        cnt += 1
                    if cnt == 0:
                        continue
                    avg_base_range = s / cnt
                    impulse_range = candle_ranges[i]
                    if avg_base_range == 0.0:
                        continue
                    if impulse_range > avg_base_range * move_min_ratio:
                        # base_high = max highs[base_start:i)
                        # base_low  = min lows[base_start:i)
                        bh = highs[base_start]
                        bl = lows[base_start]
                        for k in range(base_start + 1, i):
                            if highs[k] > bh:
                                bh = highs[k]
                            if lows[k] < bl:
                                bl = lows[k]
                        zone_width_pips = (bh - bl) / pip_size
                        if zone_width_pips > 0.0 and zone_width_pips < zone_width_max_pips:
                            c = closes[i]
                            if c > bh:
                                type_code[i] = 1
                                base_high_out[i] = bh
                                base_low_out[i] = bl
                                break
                            elif c < bl:
                                type_code[i] = -1
                                base_high_out[i] = bh
                                base_low_out[i] = bl
                                break
                # end base_len loop
            return type_code, base_high_out, base_low_out

    def _is_strong_move(self, candles: pd.DataFrame) -> bool:
        """Check if the move away from the base is significant."""
        if len(candles) < 2:
            return False
        
        first_candle = candles.iloc[0]
        last_candle = candles.iloc[-1]
        
        move_size = abs(last_candle['close'] - first_candle['open'])
        avg_body_size = candles['body_size'].mean() # Ensure 'body_size' is calculated in _find_zones

        return move_size > avg_body_size * self.move_min_ratio

    def _calculate_ema(self, df: pd.DataFrame, period: int, column: str = 'close') -> Optional[float]:
        """Return EMA value using precomputed column if available, else compute on the fly."""
        pre_col = f"ema_{period}"
        if pre_col in df.columns:
            val = df[pre_col].iloc[-1]
            return float(val) if pd.notna(val) else None
        if len(df) < period:
            return None
        ema_series = pd.Series.ewm(df[column], span=period, adjust=False).mean()
        return float(ema_series.iloc[-1])

    def _calculate_rsi(self, df: pd.DataFrame, period: int) -> Optional[float]:
        """Return RSI using precomputed column if available, else compute on the fly."""
        pre_col = f"rsi_{period}"
        if pre_col in df.columns:
            val = df[pre_col].iloc[-1]
            return float(val) if pd.notna(val) else None
        if len(df) < period + 1:
            return None
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    def _calculate_average_volume(self, df: pd.DataFrame, lookback_period: int = 20) -> Optional[float]:
        """Calculate average volume over a lookback period."""
        if 'volume' not in df.columns:
            return None
        if len(df) < lookback_period:
            return None
        return float(df['volume'].iloc[-lookback_period:].mean())

    def _is_within_trading_hours(self, current_datetime: pd.Timestamp) -> bool:
        """Check if the current time falls within defined trading session hours (UTC)."""
        if not self.enable_session_hours_filter or not self.session_hours_utc:
            return True

        hhmm = current_datetime.strftime("%H:%M")
        for session in self.session_hours_utc:
            start_str, end_str = session.split("-")
            if start_str <= hhmm <= end_str:
                return True
        return False

    def _get_news_sentiment(self, current_datetime: datetime.datetime) -> str:
        """Lightweight news sentiment stub. Returns 'Neutral' if API unavailable.
        If FOREXNEWS_API_TOKEN is set, attempts a basic fetch from forexnewsapi.com and
        derives a naive sentiment; otherwise defaults to 'Neutral'."""
        try:
            token = os.getenv("FOREXNEWS_API_TOKEN")
            if not token:
                return "Neutral"

            params = {
                "currencypair": "USD-JPY", # Updated for USD/JPY
                "items": 10,
                "date": "today",
                "token": token,
            }
            resp = requests.get("https://forexnewsapi.com/api/v1", params=params, timeout=5)
            if resp.status_code != 200:
                return "Neutral"
            data = resp.json()
            titles = [it.get("title", "") for it in data.get("data", [])]
            text = " ".join(titles).lower()
            bull_kw = ["yen weak", "usdjpy rises", "risk-on", "bullish", "fed hawkish", "boj dovish"] # Updated for USD/JPY
            bear_kw = ["yen strong", "usdjpy falls", "risk-off", "bearish", "fed dovish", "boj hawkish"] # Updated for USD/JPY
            bull_hits = sum(1 for k in bull_kw if k in text)
            bear_hits = sum(1 for k in bear_kw if k in text)
            if bull_hits > bear_hits:
                return "Bullish"
            if bear_hits > bull_hits:
                return "Bearish"
            return "Neutral"
        except Exception:
            return "Neutral"

    def prepare(self, df: pd.DataFrame, pair: str) -> None:
        """One-time setup: cache arrays and remember target pair."""
        try:
            self.opens = df['open'].to_numpy(copy=False)
            self.highs = df['high'].to_numpy(copy=False)
            self.lows = df['low'].to_numpy(copy=False)
            self.closes = df['close'].to_numpy(copy=False)
            self.timestamps = df['timestamp'].to_numpy(copy=False)
            self.prepared_pair = pair
            self.candle_ranges = (self.highs - self.lows)
            self._zones_scanned_upto = -1
        except Exception:
            pass

    def _find_zones(self, df: pd.DataFrame):
        """Identifies and stores Supply and Demand zones based on explosive moves from a base."""
        self.zones = []
        df['body_size'] = abs(df['open'] - df['close'])
        df['candle_range'] = df['high'] - df['low']

        i = self.base_max_candles
        while i < len(df) - 1:
            base_found = False
            for base_len in range(1, self.base_max_candles + 1):
                base_start = i - base_len
                base_candles = df.iloc[base_start:i]
                
                # Condition 1: Base candles must have small ranges
                avg_base_range = base_candles['candle_range'].mean()
                
                # Condition 2: Find the explosive move candle after the base
                impulse_candle = df.iloc[i]

                # Condition 3: Explosive move must be much larger than base candles
                if impulse_candle['candle_range'] > avg_base_range * self.move_min_ratio:
                    base_high = base_candles['high'].max()
                    base_low = base_candles['low'].min()
                    zone_width_pips = (base_high - base_low) / self.pip_size

                    if zone_width_pips > 0 and zone_width_pips < self.zone_width_max_pips:
                        # Explosive move upwards creates a DEMAND zone
                        if impulse_candle['close'] > base_high:
                            self.zones.append({
                                'type': 'demand', 
                                'price_high': base_high, 
                                'price_low': base_low,
                                'created_at': i, 'is_fresh': True
                            })
                            base_found = True
                            break 
                        
                        # Explosive move downwards creates a SUPPLY zone
                        elif impulse_candle['close'] < base_low:
                            self.zones.append({
                                'type': 'supply', 
                                'price_high': base_high, 
                                'price_low': base_low,
                                'created_at': i, 'is_fresh': True
                            })
                            base_found = True
                            break
            
            if base_found:
                i += 1 # Move to the next candle after the impulse
            else:
                i += 1
        
        # Remove overlapping zones, keeping the most recent one
        if self.zones:
            self.zones = sorted(self.zones, key=lambda x: x['created_at'], reverse=True)
            unique_zones = []
            seen_ranges = []
            for zone in self.zones:
                is_overlap = False
                for seen_high, seen_low in seen_ranges:
                    if not (zone['price_high'] < seen_low or zone['price_low'] > seen_high):
                        is_overlap = True
                        break
                if not is_overlap:
                    unique_zones.append(zone)
                    seen_ranges.append((zone['price_high'], zone['price_low']))
            self.zones = unique_zones

    def find_all_zones(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Scans the entire DataFrame and identifies all historical Supply and Demand zones.
        This should be called once at the start of a backtest.
        """
        all_zones = []
        df['body_size'] = abs(df['open'] - df['close'])
        df['candle_range'] = df['high'] - df['low']

        i = self.base_max_candles
        while i < len(df) - 1:
            base_found = False
            # Look for a base of 1 to base_max_candles
            for base_len in range(1, self.base_max_candles + 1):
                base_start = i - base_len
                base_candles = df.iloc[base_start:i]
                impulse_candle = df.iloc[i]
                
                # Condition 1: Base candles should be relatively small
                avg_base_range = base_candles['candle_range'].mean()
                if avg_base_range == 0: continue # Avoid division by zero

                # Condition 2: Impulse candle must be significantly larger than base candles
                if impulse_candle['candle_range'] > avg_base_range * self.move_min_ratio:
                    base_high = base_candles['high'].max()
                    base_low = base_candles['low'].min()
                    zone_width_pips = (base_high - base_low) / self.pip_size

                    # Condition 3: Zone width must be within a reasonable limit
                    if 0 < zone_width_pips < self.zone_width_max_pips:
                        zone_type = None
                        if impulse_candle['close'] > base_high: # Explosive move up creates Demand
                            zone_type = 'demand'
                        elif impulse_candle['close'] < base_low: # Explosive move down creates Supply
                            zone_type = 'supply'
                        
                        if zone_type:
                            all_zones.append({
                                'type': zone_type, 
                                'price_high': base_high, 
                                'price_low': base_low,
                                'created_at_index': i,
                                'is_fresh': True
                            })
                            base_found = True
                            break # Move to the next candle after finding a valid zone from this base
            
            if base_found:
                i += base_len # Skip past the candles that formed the zone
            else:
                i += 1
        
        # Filter out overlapping zones, keeping the one created last (most recent)
        if not all_zones:
            return []
            
        all_zones = sorted(all_zones, key=lambda x: x['created_at_index'], reverse=True)
        unique_zones = []
        seen_ranges = []
        for zone in all_zones:
            is_overlap = any(not (zone['price_high'] < seen_low or zone['price_low'] > seen_high) for seen_high, seen_low in seen_ranges)
            if not is_overlap:
                unique_zones.append(zone)
                seen_ranges.append((zone['price_high'], zone['price_low']))
        
        return sorted(unique_zones, key=lambda x: x['created_at_index'])

    def check_entry_signal(self, current_price: float, zone: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Checks if the current price provides an entry signal for a given fresh zone.
        This is called on each candle against available zones.
        """
        decision = "NO TRADE"
        sl = 0
        tp = 0

        in_supply_zone = zone['type'] == 'supply' and zone['price_low'] <= current_price <= zone['price_high']
        in_demand_zone = zone['type'] == 'demand' and zone['price_low'] <= current_price <= zone['price_high']

        if in_supply_zone:
            decision = "SELL"
            sl = zone['price_high'] + (self.sl_buffer_pips * self.pip_size)
            risk_pips = (sl - current_price) / self.pip_size
            tp = current_price - (risk_pips * self.risk_reward_ratio * self.pip_size)

        elif in_demand_zone:
            decision = "BUY"
            sl = zone['price_low'] - (self.sl_buffer_pips * self.pip_size)
            risk_pips = (current_price - sl) / self.pip_size
            tp = current_price + (risk_pips * self.risk_reward_ratio * self.pip_size)

        if decision != "NO TRADE":
                        return {
                "decision": decision,
                "entry_price": current_price,
                "stop_loss": sl,
                "take_profit": tp,
                "meta": { "zone_type": zone['type'], "zone_high": zone['price_high'], "zone_low": zone['price_low']}
            }
        
        return None

    def analyze_trade_signal_at_index(self, df: pd.DataFrame, idx: int, pair: str) -> Dict[str, Any]:
        """Index-based analyzer to avoid per-iteration DataFrame copies."""
        current_candle_index = idx
        if self.profile_enabled:
            self.prof_calls += 1

        current_price = df['close'].iloc[idx]
        current_datetime = df['timestamp'].iloc[idx]
        
        # --- New Filters --- 
        # Session hours filter
        if not self._is_within_trading_hours(current_datetime):
            self.blocking_reasons_counts["Outside of trading hours"] += 1
            return {"decision": "NO TRADE", "reason": "Outside of trading hours"}

        # Only recalculate zones if it's a new candle (default to legacy for parity)
        if self.last_candle_index != current_candle_index:
            t_z = time.perf_counter()
            if os.getenv('BACKTEST_ZONE_MODE', 'legacy') == 'legacy':
                start_idx = max(0, current_candle_index - self.zone_lookback)
                lookback_df = df.iloc[start_idx:current_candle_index + 1].copy()
                self._find_zones(lookback_df)
            else:
                self._update_zones_upto(current_candle_index)
            if self.profile_enabled:
                self.prof['zones_ms'] += (time.perf_counter() - t_z) * 1000
            self.last_candle_index = current_candle_index

        # Guard: zones must exist after update
        if not self.zones:
            self.blocking_reasons_counts["No valid supply/demand zones found"] += 1
            return {"decision": "NO TRADE", "reason": "No valid supply/demand zones found"}

        # EMA Filter enabled
        if self.ema_periods:
            hist_df = df.iloc[:current_candle_index + 1]
            t_ema = time.perf_counter()
            for period in self.ema_periods:
                current_ema = self._calculate_ema(hist_df, period)
                if current_ema is None:
                    self.blocking_reasons_counts[f"Insufficient data for EMA({period})"] += 1
                    return {"decision": "NO TRADE", "reason": f"Insufficient data for EMA({period})"}
                latest_zone = self.zones[-1] if self.zones else None
                if latest_zone and latest_zone.get('type') == 'demand' and current_price < current_ema:
                    self.blocking_reasons_counts[f"Price below EMA({period}) for demand trade"] += 1
                    return {"decision": "NO TRADE", "reason": f"Price below EMA({period}) for demand trade"}
                if latest_zone and latest_zone.get('type') == 'supply' and current_price > current_ema:
                    self.blocking_reasons_counts[f"Price above EMA({period}) for supply trade"] += 1
                    return {"decision": "NO TRADE", "reason": f"Price above EMA({period}) for supply trade"}
            if self.profile_enabled:
                self.prof['ema_ms'] += (time.perf_counter() - t_ema) * 1000

        # RSI Filter (optional)
        hist_df = df.iloc[:current_candle_index + 1]
        t_rsi = time.perf_counter()
        current_rsi = self._calculate_rsi(hist_df, self.rsi_period)
        if self.profile_enabled:
            self.prof['rsi_ms'] += (time.perf_counter() - t_rsi) * 1000
        if current_rsi is None:
            self.blocking_reasons_counts[f"Insufficient data for RSI({self.rsi_period})"] += 1
            return {"decision": "NO TRADE", "reason": f"Insufficient data for RSI({self.rsi_period})"}

        latest_zone = self.zones[-1] if self.zones else None
        if latest_zone and latest_zone.get('type') == 'demand' and current_rsi > self.rsi_overbought:
            self.blocking_reasons_counts["RSI overbought for demand trade"] += 1
            return {"decision": "NO TRADE", "reason": "RSI overbought for demand trade"}
        if latest_zone and latest_zone.get('type') == 'supply' and current_rsi < self.rsi_oversold:
            self.blocking_reasons_counts["RSI oversold for supply trade"] += 1
            return {"decision": "NO TRADE", "reason": "RSI oversold for supply trade"}

        # Volume Filter (optional)
        if self.enable_volume_filter:
            if 'volume' not in df.columns:
                self.blocking_reasons_counts["Volume column missing"] += 1
                return {"decision": "NO TRADE", "reason": "Volume column missing"}
            t_vol = time.perf_counter()
            avg_volume = self._calculate_average_volume(hist_df)
            current_volume = df['volume'].iloc[current_candle_index]
            if self.profile_enabled:
                self.prof['volume_ms'] += (time.perf_counter() - t_vol) * 1000
            if avg_volume is None or current_volume < avg_volume * self.min_volume_factor:
                self.blocking_reasons_counts[f"Current volume {current_volume:.2f} < avg volume {avg_volume:.2f} * factor {self.min_volume_factor:.2f}."] += 1
                return {"decision": "NO TRADE", "reason": "Volume too low"}

        # News Sentiment Filter (optional)
        news_sentiment = "Neutral"
        if self.enable_news_sentiment_filter:
            t_news = time.perf_counter()
            news_sentiment = self._get_news_sentiment(current_datetime)
            latest_zone = self.zones[-1] if self.zones else None
            if latest_zone and latest_zone.get('type') == 'demand' and news_sentiment == "Bearish":
                self.blocking_reasons_counts["Demand trade filtered by bearish news sentiment"] += 1
                return {"decision": "NO TRADE", "reason": "Demand trade filtered out by bearish news sentiment"}
            if latest_zone and latest_zone.get('type') == 'supply' and news_sentiment == "Bullish":
                self.blocking_reasons_counts["Supply trade filtered by bullish news sentiment"] += 1
                return {"decision": "NO TRADE", "reason": "Supply trade filtered out by bullish news sentiment"}
            if self.profile_enabled:
                self.prof['news_ms'] += (time.perf_counter() - t_news) * 1000

        # Zone entry evaluation
        t_zone_loop = time.perf_counter()
        for zone in self.zones:
            if not zone['is_fresh']:
                continue
            in_supply_zone = zone['type'] == 'supply' and zone['price_low'] <= current_price <= zone['price_high']
            in_demand_zone = zone['type'] == 'demand' and zone['price_low'] <= current_price <= zone['price_high']
            sl = 0.0
            tp = 0.0
            decision = "NO TRADE"
            if in_supply_zone:
                zone['is_fresh'] = False
                decision = "SELL"
                sl = zone['price_high'] + (self.sl_buffer_pips * self.pip_size)
                risk_pips = (sl - current_price) / self.pip_size
                tp = current_price - (risk_pips * self.risk_reward_ratio * self.pip_size)
            elif in_demand_zone:
                zone['is_fresh'] = False
                decision = "BUY"
                sl = zone['price_low'] - (self.sl_buffer_pips * self.pip_size)
                risk_pips = (current_price - sl) / self.pip_size
                tp = current_price + (risk_pips * self.risk_reward_ratio * self.pip_size)
            if decision != "NO TRADE":
                if self.profile_enabled:
                    self.prof['zone_loop_ms'] += (time.perf_counter() - t_zone_loop) * 1000
                return {
                    "decision": decision,
                    "entry_price": current_price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "meta": {"zone_type": zone['type'], "zone_high": zone['price_high'], "zone_low": zone['price_low']}
                }
        if self.profile_enabled:
            self.prof['zone_loop_ms'] += (time.perf_counter() - t_zone_loop) * 1000
        self.blocking_reasons_counts["No entry signal found after all checks"] += 1
        return {"decision": "NO TRADE", "reason": "No entry signal found after all checks"}

    def _add_zone_incremental(self, zone_type: str, base_high: float, base_low: float, created_at_idx: int) -> None:
        new_range = (base_high, base_low)
        filtered = []
        for z in self.zones:
            if not (new_range[0] < z['price_low'] or new_range[1] > z['price_high']):
                continue
            filtered.append(z)
        self.zones = filtered
        self.zones.append({
            'type': zone_type,
            'price_high': base_high,
            'price_low': base_low,
            'created_at': created_at_idx,
            'is_fresh': True
        })

    def _update_zones_upto(self, upto_idx: int) -> None:
        """Incrementally compute zones (Numba fast path if available), preserving legacy rules."""
        if not hasattr(self, 'highs') or self.highs is None:
            return
        start_i = max(getattr(self, '_zones_scanned_upto', -1) + 1, self.base_max_candles)
        if start_i > upto_idx:
            return
        use_numba = HAS_NUMBA and os.getenv('BACKTEST_USE_NUMBA', '1') == '1'
        if use_numba:
            type_code, bh_arr, bl_arr = USDJPYStrategy._zone_scan_numba(
                self.highs, self.lows, self.closes, self.candle_ranges,
                int(self.zone_lookback), int(self.base_max_candles),
                float(self.move_min_ratio), float(self.zone_width_max_pips),
                float(self.pip_size), int(start_i), int(upto_idx)
            )
            for i in range(start_i, upto_idx + 1):
                tc = int(type_code[i])
                if tc == 0:
                    continue
                base_high = float(bh_arr[i])
                base_low = float(bl_arr[i])
                if tc == 1:
                    self._add_zone_incremental('demand', base_high, base_low, i)
                elif tc == -1:
                    self._add_zone_incremental('supply', base_high, base_low, i)
        else:
            pip = self.pip_size
            for i in range(start_i, upto_idx + 1):
                window_start_min = max(0, i - self.zone_lookback)
                for base_len in range(1, self.base_max_candles + 1):
                    base_start = i - base_len
                    if base_start < window_start_min:
                        break
                    base_slice = slice(base_start, i)
                    cr = self.candle_ranges[base_slice]
                    if cr.size == 0:
                        continue
                    avg_base_range = float(cr.mean())
                    impulse_range = float(self.candle_ranges[i])
                    if avg_base_range == 0:
                        continue
                    if impulse_range > avg_base_range * self.move_min_ratio:
                        base_high = float(self.highs[base_slice].max())
                        base_low = float(self.lows[base_slice].min())
                        zone_width_pips = (base_high - base_low) / pip
                        if 0 < zone_width_pips < self.zone_width_max_pips:
                            c = float(self.closes[i])
                            if c > base_high:
                                self._add_zone_incremental('demand', base_high, base_low, i)
                                break
                            if c < base_low:
                                self._add_zone_incremental('supply', base_high, base_low, i)
                                break
                # prune old zones outside lookback
                if self.zones:
                    cutoff = i - self.zone_lookback
                    if cutoff > 0:
                        self.zones = [z for z in self.zones if z.get('created_at', i) >= cutoff]
        # After adding, reconcile overlaps like legacy: keep newest
        if self.zones:
            zones_sorted = sorted(self.zones, key=lambda z: z.get('created_at', 0), reverse=True)
            unique = []
            seen = []
            for z in zones_sorted:
                overlap = any(not (z['price_high'] < low or z['price_low'] > high) for high, low in seen)
                if not overlap:
                    unique.append(z)
                    seen.append((z['price_high'], z['price_low']))
            self.zones = unique
        self._zones_scanned_upto = upto_idx

    def analyze_trade_signal(self, df: pd.DataFrame, pair: str) -> Dict[str, Any]:
        if df is None or len(df) == 0:
            return {"decision": "NO TRADE", "reason": "No data available"}
        return self.analyze_trade_signal_at_index(df, len(df) - 1, pair)

    def get_blocking_reasons_counts(self) -> Dict[str, int]:
        """Returns a dictionary of counts for each reason a trade was blocked."""
        return dict(self.blocking_reasons_counts)
