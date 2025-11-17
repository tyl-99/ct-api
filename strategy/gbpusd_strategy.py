import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
import datetime
import sys
import os
import requests
from dotenv import load_dotenv
import logging
from collections import defaultdict

# logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

class GBPUSDStrategy:
    """
    A Supply and Demand strategy for EUR/USD aiming for a high R:R.

    Logic:
    1. Identifies Supply and Demand zones based on strong price moves away from a consolidated base.
       - Supply: A sharp drop after a base (Rally-Base-Drop or Drop-Base-Drop).
       - Demand: A sharp rally after a base (Drop-Base-Rally or Rally-Base-Rally).
    2. Enters a trade when price returns to a 'fresh' (untested) zone.
    3. The Stop Loss is placed just outside the zone.
    4. Enforces a tuned Risk-to-Reward ratio for better win rate.
    """

    def __init__(self, target_pair="GBP/USD",
                 zone_lookback=300,
                 base_max_candles=4,
                 move_min_ratio=3.5,
                 zone_width_max_pips=18,
                 risk_reward_ratio=3.0, # Set back to 3.0 for 1:3 R:R
                 sl_buffer_pips=4.0,
                 ema_periods: Optional[List[int]] = None,
                 rsi_period: int = 14,
                 rsi_oversold: float = 30.0,
                 rsi_overbought: float = 70.0,
                 enable_volume_filter: bool = False,
                 min_volume_factor: float = 1.2,
                 session_hours_utc: Optional[List[str]] = ("01:00-02:59", "05:00-07:59", "09:00-09:59", "12:00-12:59", "15:00-15:59", "17:00-17:59", "19:00-19:59", "21:00-23:59"),
                 enable_session_hours_filter: bool = True,
                 enable_news_sentiment_filter: bool = False
                 ):
        self.target_pair = target_pair
        self.pip_size = 0.0001 # Standard for most JPY pairs, check for others
        
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
                "currencypair": "GBP-USD",
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
            bull_kw = ["gbp rises", "usd weak", "risk-on", "bullish", "boe hawkish", "fed dovish"]
            bear_kw = ["gbp falls", "usd strong", "risk-off", "bearish", "boe dovish", "fed hawkish"]
            bull_hits = sum(1 for k in bull_kw if k in text)
            bear_hits = sum(1 for k in bear_kw if k in text)
            if bull_hits > bear_hits:
                return "Bullish"
            if bear_hits > bull_hits:
                return "Bearish"
            return "Neutral"
        except Exception:
            return "Neutral"

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

    def analyze_trade_signal(self, df: pd.DataFrame, pair: str) -> Dict[str, Any]:
        """
        Analyzes the market data for the target pair to find trading opportunities.
        """
        current_candle_index = len(df) - 1

        current_price = df['close'].iloc[-1]
        current_datetime = df['timestamp'].iloc[-1] # Assuming 'timestamp' column exists
        
        # --- New Filters --- 
        # Session hours filter
        if not self._is_within_trading_hours(current_datetime):
            self.blocking_reasons_counts["Outside of trading hours"] += 1
            return {"decision": "NO TRADE", "reason": "Outside of trading hours"}

        # Only recalculate zones if it's a new candle
        if self.last_candle_index != current_candle_index:
            lookback_df = df.iloc[-self.zone_lookback:].copy()
            self._find_zones(lookback_df)
            self.last_candle_index = current_candle_index
        
        if not self.zones:
            self.blocking_reasons_counts["No valid supply/demand zones found"] += 1
            return {"decision": "NO TRADE", "reason": "No valid supply/demand zones found"}

        # EMA Filter enabled
        if self.ema_periods:
            for period in self.ema_periods:
                current_ema = self._calculate_ema(df, period) # Use full df for EMA
                if current_ema is None:
                    self.blocking_reasons_counts[f"Insufficient data for EMA({period})"] += 1
                    return {"decision": "NO TRADE", "reason": f"Insufficient data for EMA({period})"}
                
                # Simple EMA filter: Price must be above all bullish EMAs or below all bearish EMAs
                # For S&D, this acts as a trend filter: only buy in uptrend, sell in downtrend
                # Determine rough trend based on last impulse for the latest zone
                latest_zone = None
                if self.zones:
                    latest_zone = self.zones[-1] # Assuming zones are sorted by creation_at_index ascending

                if latest_zone and latest_zone.get('type') == 'demand': # Bullish bias
                    if current_price < current_ema:
                        self.blocking_reasons_counts[f"Price below EMA({period}) for demand trade"] += 1
                        return {"decision": "NO TRADE", "reason": f"Price below EMA({period}) for demand trade"}
                elif latest_zone and latest_zone.get('type') == 'supply': # Bearish bias
                    if current_price > current_ema:
                        self.blocking_reasons_counts[f"Price above EMA({period}) for supply trade"] += 1
                        return {"decision": "NO TRADE", "reason": f"Price above EMA({period}) for supply trade"}

        # RSI Filter (optional)
        current_rsi = self._calculate_rsi(df, self.rsi_period) # Use full df for RSI
        if current_rsi is None:
            self.blocking_reasons_counts[f"Insufficient data for RSI({self.rsi_period})"] += 1
            return {"decision": "NO TRADE", "reason": f"Insufficient data for RSI({self.rsi_period})"}

        # Rest of RSI logic
        latest_zone = None
        if self.zones:
            latest_zone = self.zones[-1]

        if latest_zone and latest_zone.get('type') == 'demand': # Bullish bias
            if current_rsi > self.rsi_overbought:
                self.blocking_reasons_counts["RSI overbought for demand trade"] += 1
                return {"decision": "NO TRADE", "reason": "RSI overbought for demand trade"}
        elif latest_zone and latest_zone.get('type') == 'supply': # Bearish bias
            if current_rsi < self.rsi_oversold:
                self.blocking_reasons_counts["RSI oversold for supply trade"] += 1
                return {"decision": "NO TRADE", "reason": "RSI oversold for supply trade"}

        # Volume Filter (optional)
        if self.enable_volume_filter:
            if 'volume' not in df.columns:
                self.blocking_reasons_counts["Volume column missing"] += 1
                return {"decision": "NO TRADE", "reason": "Volume column missing"}
            avg_volume = self._calculate_average_volume(df)
            current_volume = df['volume'].iloc[-1]
            if avg_volume is None or current_volume < avg_volume * self.min_volume_factor:
                self.blocking_reasons_counts[f"Current volume {current_volume:.2f} < avg volume {avg_volume:.2f} * factor {self.min_volume_factor:.2f}."] += 1
                return {"decision": "NO TRADE", "reason": "Volume too low"}

        # News Sentiment Filter (optional)
        news_sentiment = "Neutral"
        if self.enable_news_sentiment_filter:
            news_sentiment = self._get_news_sentiment(current_datetime)
            latest_zone = None
            if self.zones:
                latest_zone = self.zones[-1]
            
            if latest_zone and latest_zone.get('type') == 'demand' and news_sentiment == "Bearish":
                self.blocking_reasons_counts["Demand trade filtered by bearish news sentiment"] += 1
                return {"decision": "NO TRADE", "reason": "Demand trade filtered out by bearish news sentiment"}
            if latest_zone and latest_zone.get('type') == 'supply' and news_sentiment == "Bullish":
                self.blocking_reasons_counts["Supply trade filtered by bullish news sentiment"] += 1
                return {"decision": "NO TRADE", "reason": "Supply trade filtered out by bullish news sentiment"}

        for zone in self.zones:
            if not zone['is_fresh']:
                # logger.debug(f"Skipping zone {zone.get('type')} {zone.get('price_low'):.5f}-{zone.get('price_high'):.5f} at {current_datetime}: Zone is not fresh.")
                continue

            # Check for entry
            in_supply_zone = zone['type'] == 'supply' and current_price >= zone['price_low'] and current_price <= zone['price_high']
            in_demand_zone = zone['type'] == 'demand' and current_price >= zone['price_low'] and current_price <= zone['price_high']
            
            sl = 0
            tp = 0
            decision = "NO TRADE"
            
            if in_supply_zone:
                zone['is_fresh'] = False # Mark as tested
                decision = "SELL"
                sl = zone['price_high'] + (self.sl_buffer_pips * self.pip_size)
                risk_pips = (sl - current_price) / self.pip_size # Calculate actual risk in pips
                tp = current_price - (risk_pips * self.risk_reward_ratio * self.pip_size)
                # logger.debug(f"SELL signal generated at {current_datetime} in supply zone {zone.get('price_low'):.5f}-{zone.get('price_high'):.5f}.")
                
            elif in_demand_zone:
                zone['is_fresh'] = False # Mark as tested
                decision = "BUY"
                sl = zone['price_low'] - (self.sl_buffer_pips * self.pip_size)
                risk_pips = (current_price - sl) / self.pip_size # Calculate actual risk in pips
                tp = current_price + (risk_pips * self.risk_reward_ratio * self.pip_size)
                # logger.debug(f"BUY signal generated at {current_datetime} in demand zone {zone.get('price_low'):.5f}-{zone.get('price_high'):.5f}.")

            if decision != "NO TRADE":
                return {
                    "decision": decision,
                    "entry_price": current_price,
                    "stop_loss": sl,
                    "take_profit": tp,
            "meta": {
                        "zone_type": zone['type'], 
                        "zone_high": zone['price_high'], 
                        "zone_low": zone['price_low']
                    }
                }
                
        self.blocking_reasons_counts["No entry signal found after all checks"] += 1
        return {"decision": "NO TRADE"}

    def get_blocking_reasons_counts(self) -> Dict[str, int]:
        """Returns a dictionary of counts for each reason a trade was blocked."""
        return dict(self.blocking_reasons_counts)
