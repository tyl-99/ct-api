#!/usr/bin/env python3
"""
Lightweight trader orchestrator

Flow per pair (mirrors ctrader.py style, simplified):
1) Fetch latest M30 candles via cTrader OpenAPI
2) Pass DataFrame to mapped strategy (same mapping as ctrader.py)
3) If signal -> scrape ForexFactory calendar (via Selenium) and parse events
4) Call Gemini 2.5 Flash with strategy metrics + ForexFactory news for recommendation
5) Send Pushover notification with SL/TP, EMA/RSI, news, and LLM recommendation

Environment variables required:
- CTRADER_CLIENT_ID
- CTRADER_CLIENT_SECRET
- CTRADER_ACCOUNT_ID
- CTRADER_ACCESS_TOKEN (if required by your setup)
- GEMINI_APIKEY
- PUSHOVER_APP_TOKEN (optional, falls back to hardcoded)
- PUSHOVER_USER_KEY (optional, falls back to hardcoded)
"""

import os
import sys
import time
import json
import logging
import datetime
import argparse
import asyncio
import threading
from typing import Dict, Any, Optional, List

import requests
import pandas as pd
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOANewOrderReq
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOACancelOrderReq
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOADealListReq, ProtoOAReconcileReq, ProtoOATraderReq, ProtoOAOrderDetailsReq
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor

# Strategy imports (mirror ctrader.py, but allow missing files gracefully)
try:
    from strategy.eurusd_strategy import EURUSDSTRATEGY
except Exception:
    EURUSDSTRATEGY = None
try:
    from strategy.gbpusd_strategy import GBPUSDStrategy as GBPUSDSTRATEGY
except Exception:
    GBPUSDSTRATEGY = None
try:
    from strategy.eurgbp_strategy import EURGBPStrategy as EURGBPSTRATEGY
except Exception:
    EURGBPSTRATEGY = None
try:
    from strategy.usdjpy_strategy import USDJPYStrategy as USDJPYSTRATEGY
except Exception:
    USDJPYSTRATEGY = None
try:
    from strategy.gbpjpy_strategy import GBPJPYStrategy as GBPJPYSTRATEGY
except Exception:
    GBPJPYSTRATEGY = None
try:
    from strategy.eurjpy_strategy import EURJPYStrategy as EURJPYSTRATEGY
except Exception:
    EURJPYSTRATEGY = None

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

FOREX_SYMBOLS = {
    "EUR/USD": 1,
    "GBP/USD": 2,
    "EUR/JPY": 3,
    "USD/JPY": 4,
    "GBP/JPY": 7,
    "EUR/GBP": 9,
}

PAIR_TIMEFRAMES = {
    "EUR/USD": "M30",
    "GBP/USD": "M30",
    "EUR/JPY": "M30",
    "EUR/GBP": "M30",
    "USD/JPY": "M30",
    "GBP/JPY": "M30",
}


class SimpleTrader:
    def __init__(self):
        self.client_id = os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET")
        self.account_id = os.getenv("CTRADER_ACCOUNT_ID")
        if self.account_id:
            try:
                self.account_id = int(self.account_id)
            except Exception:
                raise ValueError("CTRADER_ACCOUNT_ID must be integer")
        if not all([self.client_id, self.client_secret, self.account_id]):
            raise ValueError("Missing cTrader credentials envs")

        self.host = EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(self.host, EndPoints.PROTOBUF_PORT, TcpProtocol)

        # Strategy instances mapping
        self.strategies: Dict[str, Any] = {
            "EUR/USD": EURUSDSTRATEGY() if EURUSDSTRATEGY else None,
            "GBP/USD": GBPUSDSTRATEGY() if GBPUSDSTRATEGY else None,
            "EUR/JPY": EURJPYSTRATEGY() if EURJPYSTRATEGY else None,
            "EUR/GBP": EURGBPSTRATEGY() if EURGBPSTRATEGY else None,
            "USD/JPY": USDJPYSTRATEGY() if USDJPYSTRATEGY else None,
            "GBP/JPY": GBPJPYSTRATEGY() if GBPJPYSTRATEGY else None,
        }

        self.gemini_api_key = os.getenv("GEMINI_APIKEY")
        self.pushover_app_token = os.getenv("PUSHOVER_APP_TOKEN")
        self.pushover_user_key = os.getenv("PUSHOVER_USER_KEY")

        # Risk management - load risk amounts per pair from .env
        self.risk_amounts = {}
        default_risk = float(os.getenv("RISK_DEFAULT", "50"))  # Default risk = 50 USD
        for pair in FOREX_SYMBOLS.keys():
            # Format: RISK_EUR_USD=50, RISK_GBP_USD=75, etc.
            env_key = f"RISK_{pair.replace('/', '_')}"
            risk_value = os.getenv(env_key)
            if risk_value:
                try:
                    self.risk_amounts[pair] = float(risk_value)
                except ValueError:
                    self.risk_amounts[pair] = default_risk
            else:
                self.risk_amounts[pair] = default_risk

        # Testing flags
        self.force_signal: Optional[str] = None  # BUY/SELL
        self.force_entry: Optional[float] = None
        self.force_sl: Optional[float] = None
        self.force_tp: Optional[float] = None
        self.dry_run_notify: bool = False

        # Pair iteration state
        self.pairs = [p for p in PAIR_TIMEFRAMES.keys() if self.strategies.get(p)]
        self.current_pair: Optional[str] = None
        self.pair_index: int = 0
        self.trendbar: pd.DataFrame = pd.DataFrame()
        
        # News cache - scraped on-demand when requested via API endpoints
        self.all_news_events: List[Dict[str, Any]] = []
        
        # API async support
        self._api_pending_requests: Dict[str, Dict[str, Any]] = {}  # request_id -> {event, df, error, loop}
        self._api_request_counter: int = 0
        self._api_lock = threading.Lock()
        self._authenticated: bool = False
        
        # Order execution tracking
        self._api_pending_orders: Dict[str, Dict[str, Any]] = {}  # request_id -> {event, result, error}
        self._api_order_counter: int = 0
        
        # Account data fetching support
        self._account_data_pending: Dict[str, Any] = {}  # Stores pending account data requests
        self._account_data_lock = threading.Lock()

    def connect(self, api_mode: bool = False) -> None:
        """Connect to cTrader. If api_mode=True, reactor runs indefinitely for API requests."""
        self._api_mode = api_mode
        self.client.setConnectedCallback(self.connected)
        self.client.setDisconnectedCallback(self.disconnected)
        self.client.setMessageReceivedCallback(self.on_message)
        self.client.startService()
        if api_mode:
            # Keep reactor running indefinitely for API mode
            reactor.run(installSignalHandlers=0)
        else:
            reactor.run()

    def connected(self, client):
        logger.info("Connected to cTrader")
        self._authenticated = False  # Reset on new connection
        self.authenticate_app()

    def disconnected(self, client, reason):
        logger.warning("Disconnected from cTrader: %s", reason)
        self._authenticated = False
        # Clear any pending requests that might be waiting
        with self._api_lock:
            for request_id, pending in list(self._api_pending_requests.items()):
                if not pending["event"].is_set():
                    pending["error"] = f"Connection lost: {reason}"
                    pending["event"].set()

    def on_message(self, client, message):
        pass

    def authenticate_app(self):
        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        d = self.client.send(req)
        d.addCallbacks(self.on_app_auth_success, self.on_error)

    def on_app_auth_success(self, response):
        access = os.getenv("CTRADER_ACCESS_TOKEN") or ""
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        if access:
            req.accessToken = access
        d = self.client.send(req)
        d.addCallbacks(self.on_user_auth_success, self.on_error)

    def on_user_auth_success(self, response):
        logger.info("User authenticated")
        self._authenticated = True
        # For API mode, we don't auto-process pairs - just keep reactor running
        # News scraping is now done on-demand when requested via API endpoints
        if self.pairs:
            # Only auto-process if running in CLI mode (not API mode)
            # API mode will be detected by checking if reactor is already running
            if not hasattr(self, '_api_mode') or not self._api_mode:
                self.pair_index = 0
                self.process_current_pair()
        else:
            logger.info("No pairs configured; running in API-only mode")

    def _period_to_proto(self, tf: str):
        # Mirror ctrader.py usage: use enum Value lookup by string name
        try:
            return ProtoOATrendbarPeriod.Value(tf)
        except Exception:
            return ProtoOATrendbarPeriod.Value("M30")

    def fetch_m30_trendbars(self, pair: str, weeks: int = 6) -> pd.DataFrame:
        # This path is now event-driven via callbacks. Keep method for compatibility tests.
        symbol_id = FOREX_SYMBOLS.get(pair)
        if not symbol_id:
            return pd.DataFrame()
        timeframe = PAIR_TIMEFRAMES.get(pair, "M30")
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account_id
        req.period = ProtoOATrendbarPeriod.Value(timeframe)
        req.symbolId = symbol_id
        now = datetime.datetime.utcnow()
        since = now - datetime.timedelta(weeks=weeks)
        req.fromTimestamp = int(since.timestamp() * 1000)
        req.toTimestamp = int(now.timestamp() * 1000)
        d = self.client.send(req)
        d.addCallbacks(lambda resp: self._on_trendbars_response(pair, resp), self.on_error)
        return pd.DataFrame()  # actual data handled async
    
    async def fetch_trendbars_async(self, pair: str, timeframe: str = "M30", weeks: int = 6) -> pd.DataFrame:
        """
        Async method to fetch trendbars for API calls.
        Returns DataFrame or None if error/timeout.
        """
        # Wait for authentication
        timeout_count = 0
        while not self._authenticated and timeout_count < 30:
            await asyncio.sleep(0.5)
            timeout_count += 1
        
        if not self._authenticated:
            raise Exception("Trader not authenticated yet. Please wait for connection to establish.")
        
        # Check if client is connected (with fallback for older client versions)
        is_connected = True
        if hasattr(self.client, 'isConnected'):
            is_connected = self.client.isConnected()
        elif hasattr(self.client, '_connected'):
            is_connected = self.client._connected
        
        if not is_connected:
            raise Exception("cTrader client is not connected. Connection may have been lost. Please check the connection status.")
        
        symbol_id = FOREX_SYMBOLS.get(pair)
        if not symbol_id:
            raise ValueError(f"Invalid pair: {pair}")
        
        # Generate unique request ID
        # Use threading.Event since callback runs in different thread
        import threading as th
        event = th.Event()
        loop = asyncio.get_event_loop()
        
        with self._api_lock:
            self._api_request_counter += 1
            request_id = f"req_{self._api_request_counter}_{pair}_{time.time()}"
            self._api_pending_requests[request_id] = {
                "event": event,
                "df": None,
                "error": None,
                "loop": loop
            }
        
        try:
            # Create request
            req = ProtoOAGetTrendbarsReq()
            req.ctidTraderAccountId = self.account_id
            req.period = ProtoOATrendbarPeriod.Value(timeframe)
            req.symbolId = symbol_id
            now = datetime.datetime.utcnow()
            since = now - datetime.timedelta(weeks=weeks)
            req.fromTimestamp = int(since.timestamp() * 1000)
            req.toTimestamp = int(now.timestamp() * 1000)
            
            # Send request - request_id is already captured in the lambda closures
            d = self.client.send(req)
            d.addCallbacks(
                lambda resp: self._on_trendbars_response_api(request_id, pair, resp),
                lambda err: self._on_trendbars_error_api(request_id, err)
            )
            
            # Wait for response (with timeout)
            # Use threading.Event.wait() in a thread-safe way with asyncio
            pending = self._api_pending_requests[request_id]
            event = pending["event"]
            
            # Run the blocking wait in an executor
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, event.wait),
                    timeout=60.0  # Increased timeout to 60 seconds
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for trendbars for {pair} (asyncio timeout after 60s)")
                # Mark as timeout error
                pending["error"] = "Timeout waiting for response from cTrader server (60s timeout)"
                # Make sure event is set so we don't hang
                event.set()
            
            # Get result
            result = pending["df"]
            error = pending["error"]
            
            # Cleanup
            with self._api_lock:
                if request_id in self._api_pending_requests:
                    del self._api_pending_requests[request_id]
            
            if error:
                raise Exception(error)
            
            return result
            
        except Exception as e:
            # Cleanup on error
            with self._api_lock:
                if request_id in self._api_pending_requests:
                    del self._api_pending_requests[request_id]
            raise
    
    def _on_trendbars_response_api(self, request_id: str, pair: str, response):
        """Callback for async API trendbar requests"""
        try:
            parsed = Protobuf.extract(response)
            trendbars = getattr(parsed, 'trendbar', None)
            if not trendbars:
                logger.warning(f"No trendbars for {pair}")
                with self._api_lock:
                    if request_id in self._api_pending_requests:
                        self._api_pending_requests[request_id]["df"] = pd.DataFrame()
                        self._api_pending_requests[request_id]["event"].set()
                return
            
            rows = []
            for tb in trendbars:
                rows.append({
                    'timestamp': datetime.datetime.utcfromtimestamp(tb.utcTimestampInMinutes * 60),
                    'open': (tb.low + tb.deltaOpen) / 1e5,
                    'high': (tb.low + tb.deltaHigh) / 1e5,
                    'low': tb.low / 1e5,
                    'close': (tb.low + tb.deltaClose) / 1e5,
                    'volume': tb.volume,
                })
            df = pd.DataFrame(rows)
            df.sort_values('timestamp', inplace=True)
            df['candle_range'] = df['high'] - df['low']
            
            # Set result and notify
            with self._api_lock:
                if request_id in self._api_pending_requests:
                    self._api_pending_requests[request_id]["df"] = df
                    self._api_pending_requests[request_id]["event"].set()
                    
        except Exception as e:
            logger.error(f"Trendbar parse error for {pair}: {e}")
            with self._api_lock:
                if request_id in self._api_pending_requests:
                    self._api_pending_requests[request_id]["error"] = str(e)
                    self._api_pending_requests[request_id]["event"].set()
    
    def _on_trendbars_error_api(self, request_id: str, error):
        """Error callback for async API trendbar requests"""
        # Extract error message more carefully
        error_msg = str(error)
        if hasattr(error, 'value'):
            error_msg = str(error.value)
        elif hasattr(error, 'getErrorMessage'):
            error_msg = error.getErrorMessage()
        
        # Check for timeout/cancellation errors
        if 'Deferred' in error_msg or 'timeout' in error_msg.lower() or 'cancelled' in error_msg.lower():
            error_msg = f"Request timed out or was cancelled: {error_msg}. Connection may be unstable."
            logger.error(f"API trendbar request timeout/cancellation for {request_id}: {error_msg}")
        else:
            logger.error(f"API trendbar request error for {request_id}: {error_msg}")
        
        with self._api_lock:
            if request_id in self._api_pending_requests:
                self._api_pending_requests[request_id]["error"] = error_msg
                self._api_pending_requests[request_id]["event"].set()
    
    async def execute_order_async(
        self,
        pair: str,
        order_type: str,  # "LIMIT" or "STOP"
        side: str,  # "BUY" or "SELL"
        volume: float,  # in lots
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        expiration_minutes: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Async method to execute LIMIT or STOP orders for API calls.
        
        Args:
            pair: Forex pair (e.g., "EUR/USD")
            order_type: "LIMIT" or "STOP"
            side: "BUY" or "SELL"
            volume: Volume in lots (e.g., 0.01)
            entry_price: Entry price (limit price for LIMIT, stop price for STOP)
            stop_loss: Optional stop loss price
            take_profit: Optional take profit price
            expiration_minutes: Optional expiration in minutes (default: None, no expiration)
        
        Returns:
            Dict with order result
        """
        # Wait for authentication
        timeout_count = 0
        while not self._authenticated and timeout_count < 30:
            await asyncio.sleep(0.5)
            timeout_count += 1
        
        if not self._authenticated:
            raise Exception("Trader not authenticated yet")
        
        # Validate pair
        symbol_id = FOREX_SYMBOLS.get(pair)
        if not symbol_id:
            raise ValueError(f"Invalid pair: {pair}")
        
        # Validate order type
        if order_type.upper() not in ["LIMIT", "STOP"]:
            raise ValueError(f"Invalid order_type: {order_type}. Must be 'LIMIT' or 'STOP'")
        
        # Validate side
        if side.upper() not in ["BUY", "SELL"]:
            raise ValueError(f"Invalid side: {side}. Must be 'BUY' or 'SELL'")
        
        # Generate unique request ID
        import threading as th
        event = th.Event()
        
        with self._api_lock:
            self._api_order_counter += 1
            request_id = f"order_{self._api_order_counter}_{pair}_{time.time()}"
            self._api_pending_orders[request_id] = {
                "event": event,
                "result": None,
                "error": None
            }
        
        try:
            # Convert volume to protocol format (lots * 100000, then round to nearest 1000)
            volume_protocol = round(float(volume) * 100000)
            volume_protocol = round(volume_protocol / 1000) * 1000  # Must be multiple of 1000
            volume_protocol = max(volume_protocol, 1000)  # Minimum 1000 (0.01 lots)
            
            # Create order request
            order = ProtoOANewOrderReq()
            order.ctidTraderAccountId = self.account_id
            order.symbolId = symbol_id
            order.volume = int(volume_protocol) * 100  # Final protocol format
            
            # Set order type
            if order_type.upper() == "LIMIT":
                order.orderType = ProtoOAOrderType.LIMIT
                # Round price based on pair precision
                if 'JPY' in pair:
                    order.limitPrice = round(float(entry_price), 3)
                else:
                    order.limitPrice = round(float(entry_price), 5)
            else:  # STOP
                order.orderType = ProtoOAOrderType.STOP
                # Round price based on pair precision
                if 'JPY' in pair:
                    order.stopPrice = round(float(entry_price), 3)
                else:
                    order.stopPrice = round(float(entry_price), 5)
            
            # Set trade side
            if side.upper() == "BUY":
                order.tradeSide = ProtoOATradeSide.BUY
            else:
                order.tradeSide = ProtoOATradeSide.SELL
            
            # Set stop loss and take profit if provided
            if stop_loss is not None:
                if 'JPY' in pair:
                    order.stopLoss = round(float(stop_loss), 3)
                else:
                    order.stopLoss = round(float(stop_loss), 5)
            
            if take_profit is not None:
                if 'JPY' in pair:
                    order.takeProfit = round(float(take_profit), 3)
                else:
                    order.takeProfit = round(float(take_profit), 5)
            
            # Set expiration if provided
            if expiration_minutes:
                current_timestamp = int(time.time() * 1000)
                order.expirationTimestamp = current_timestamp + (expiration_minutes * 60 * 1000)
            
            # Send order
            d = self.client.send(order)
            d.addCallbacks(
                lambda resp: self._on_order_response_api(request_id, resp),
                lambda err: self._on_order_error_api(request_id, err)
            )
            
            # Wait for response (with timeout)
            pending = self._api_pending_orders[request_id]
            event = pending["event"]
            
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, event.wait),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for order response for {pair}")
                pending["error"] = "Timeout waiting for order response"
            
            # Get result
            result = pending["result"]
            error = pending["error"]
            
            # Cleanup
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    del self._api_pending_orders[request_id]
            
            if error:
                raise Exception(error)
            
            return result
            
        except Exception as e:
            # Cleanup on error
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    del self._api_pending_orders[request_id]
            raise
    
    def _on_order_response_api(self, request_id: str, response):
        """Callback for async API order requests"""
        try:
            parsed = Protobuf.extract(response)
            
            # Print full cTrader response for debugging
            logger.info("=" * 80)
            logger.info("📦 FULL cTRADER ORDER RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"Response type: {type(parsed)}")
            logger.info(f"Response attributes: {dir(parsed)}")
            
            # Print all attributes of the response
            for attr in dir(parsed):
                if not attr.startswith('_'):
                    try:
                        value = getattr(parsed, attr)
                        if not callable(value):
                            logger.info(f"  {attr}: {value}")
                    except Exception:
                        pass
            
            # Try to print as string representation
            try:
                logger.info(f"\nFull response string:\n{parsed}")
            except Exception:
                pass
            
            # Try to print as JSON-like structure
            try:
                response_dict = {}
                for attr in dir(parsed):
                    if not attr.startswith('_') and not callable(getattr(parsed, attr, None)):
                        try:
                            val = getattr(parsed, attr)
                            if val is not None:
                                response_dict[attr] = str(val)
                        except Exception:
                            pass
                logger.info(f"\nResponse as dict:\n{json.dumps(response_dict, indent=2, default=str)}")
            except Exception as e:
                logger.info(f"Could not convert to dict: {e}")
            
            logger.info("=" * 80)
            
            # Check for errors
            if hasattr(parsed, 'errorCode') and parsed.errorCode:
                error_msg = f"{parsed.errorCode}: {getattr(parsed, 'description', 'No description')}"
                logger.error(f"Order error: {error_msg}")
                with self._api_lock:
                    if request_id in self._api_pending_orders:
                        self._api_pending_orders[request_id]["error"] = error_msg
                        self._api_pending_orders[request_id]["event"].set()
                return
            
            # Extract order/position info
            result = {
                "status": "success",
                "message": "Order executed successfully"
            }
            
            # Extract order information (for LIMIT/STOP orders, this is the main info)
            if hasattr(parsed, 'order') and parsed.order:
                order = parsed.order
                logger.info(f"📋 Order details:")
                logger.info(f"  Order ID: {order.orderId}")
                logger.info(f"  Order Type: {order.orderType}")
                logger.info(f"  Order Status: {order.orderStatus}")
                logger.info(f"  Volume: {order.tradeData.volume}")
                logger.info(f"  Trade Side: {order.tradeData.tradeSide}")
                logger.info(f"  Symbol ID: {order.tradeData.symbolId}")
                
                if hasattr(order, 'limitPrice') and order.limitPrice:
                    logger.info(f"  Limit Price: {order.limitPrice}")
                    result["limit_price"] = order.limitPrice
                
                if hasattr(order, 'stopPrice') and order.stopPrice:
                    logger.info(f"  Stop Price: {order.stopPrice}")
                    result["stop_price"] = order.stopPrice
                
                if hasattr(order, 'stopLoss') and order.stopLoss:
                    logger.info(f"  Stop Loss: {order.stopLoss}")
                    result["stop_loss"] = order.stopLoss
                
                if hasattr(order, 'takeProfit') and order.takeProfit:
                    logger.info(f"  Take Profit: {order.takeProfit}")
                    result["take_profit"] = order.takeProfit
                
                if hasattr(order, 'executionType'):
                    logger.info(f"  Execution Type: {order.executionType}")
                    result["execution_type"] = str(order.executionType)
                
                if hasattr(order, 'orderStatus'):
                    result["order_status"] = str(order.orderStatus)
                
                if hasattr(order, 'expirationTimestamp') and order.expirationTimestamp:
                    exp_time = datetime.datetime.utcfromtimestamp(order.expirationTimestamp / 1000)
                    logger.info(f"  Expiration: {exp_time}")
                    result["expiration_timestamp"] = order.expirationTimestamp
                    result["expiration_time"] = exp_time.isoformat() + "Z"
                
                if hasattr(order, 'executedVolume'):
                    logger.info(f"  Executed Volume: {order.executedVolume}")
                    result["executed_volume"] = order.executedVolume
                
                result["order_id"] = order.orderId
                result["volume"] = order.tradeData.volume / 100000  # Convert to lots
                result["side"] = "BUY" if order.tradeData.tradeSide == 1 else "SELL"
            
            # Extract position information (may be created but not filled yet for LIMIT/STOP orders)
            if hasattr(parsed, 'position') and parsed.position:
                position = parsed.position
                logger.info(f"📊 Position details:")
                logger.info(f"  Position ID: {position.positionId}")
                logger.info(f"  Position Status: {position.positionStatus}")
                logger.info(f"  Volume: {position.tradeData.volume}")
                logger.info(f"  Price: {position.price}")
                
                # Only include position info if it's actually filled (volume > 0)
                if position.tradeData.volume > 0:
                    result["position_id"] = position.positionId
                    result["position_volume"] = position.tradeData.volume / 100000  # Convert to lots
                    result["position_price"] = position.price
                    if hasattr(position, 'stopLoss') and position.stopLoss:
                        result["position_stop_loss"] = position.stopLoss
                    if hasattr(position, 'takeProfit') and position.takeProfit:
                        result["position_take_profit"] = position.takeProfit
                else:
                    # Position created but order not filled yet (LIMIT/STOP order pending)
                    result["position_id"] = position.positionId
                    result["position_status"] = str(position.positionStatus)
                    result["note"] = "Order accepted but not filled yet. Position will be created when order executes."
            
            # Extract execution type from root level if available
            if hasattr(parsed, 'executionType'):
                logger.info(f"📌 Execution Type: {parsed.executionType}")
                result["execution_type"] = str(parsed.executionType)
            
            # Log the final result that will be returned
            logger.info(f"\n✅ Final result to return:\n{json.dumps(result, indent=2, default=str)}")
            logger.info("=" * 80)
            
            # Set result and notify
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    self._api_pending_orders[request_id]["result"] = result
                    self._api_pending_orders[request_id]["event"].set()
                    
        except Exception as e:
            logger.error(f"Order parse error: {e}", exc_info=True)
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    self._api_pending_orders[request_id]["error"] = str(e)
                    self._api_pending_orders[request_id]["event"].set()
    
    def _on_order_error_api(self, request_id: str, error):
        """Error callback for async API order requests"""
        logger.error(f"API order request error: {error}")
        with self._api_lock:
            if request_id in self._api_pending_orders:
                self._api_pending_orders[request_id]["error"] = str(error)
                self._api_pending_orders[request_id]["event"].set()
    
    async def cancel_order_async(self, order_id: int) -> Dict[str, Any]:
        """
        Async method to cancel an existing pending order.
        
        Args:
            order_id: The unique ID of the order to cancel
        
        Returns:
            Dict with cancellation result
        """
        # Wait for authentication
        timeout_count = 0
        while not self._authenticated and timeout_count < 30:
            await asyncio.sleep(0.5)
            timeout_count += 1
        
        if not self._authenticated:
            raise Exception("Trader not authenticated yet")
        
        # Generate unique request ID
        import threading as th
        event = th.Event()
        
        with self._api_lock:
            self._api_order_counter += 1
            request_id = f"cancel_{self._api_order_counter}_{order_id}_{time.time()}"
            self._api_pending_orders[request_id] = {
                "event": event,
                "result": None,
                "error": None
            }
        
        try:
            # Create cancel order request
            cancel_req = ProtoOACancelOrderReq()
            cancel_req.ctidTraderAccountId = self.account_id
            cancel_req.orderId = int(order_id)
            
            logger.info(f"Cancelling order ID: {order_id}")
            
            # Send cancel request
            d = self.client.send(cancel_req)
            d.addCallbacks(
                lambda resp: self._on_cancel_order_response_api(request_id, resp),
                lambda err: self._on_cancel_order_error_api(request_id, err)
            )
            
            # Wait for response (with timeout)
            pending = self._api_pending_orders[request_id]
            event = pending["event"]
            
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, event.wait),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for cancel order response for order {order_id}")
                pending["error"] = "Timeout waiting for cancel order response"
            
            # Get result
            result = pending["result"]
            error = pending["error"]
            
            # Cleanup
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    del self._api_pending_orders[request_id]
            
            if error:
                raise Exception(error)
            
            return result
            
        except Exception as e:
            # Cleanup on error
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    del self._api_pending_orders[request_id]
            raise
    
    def _on_cancel_order_response_api(self, request_id: str, response):
        """Callback for async API cancel order requests"""
        try:
            parsed = Protobuf.extract(response)
            
            # Print full cTrader response for debugging
            logger.info("=" * 80)
            logger.info("📦 FULL cTRADER CANCEL ORDER RESPONSE:")
            logger.info("=" * 80)
            logger.info(f"Response type: {type(parsed)}")
            
            # Print all attributes
            for attr in dir(parsed):
                if not attr.startswith('_'):
                    try:
                        value = getattr(parsed, attr)
                        if not callable(value):
                            logger.info(f"  {attr}: {value}")
                    except Exception:
                        pass
            
            # Try to print as string representation
            try:
                logger.info(f"\nFull response string:\n{parsed}")
            except Exception:
                pass
            
            logger.info("=" * 80)
            
            # Check for errors
            if hasattr(parsed, 'errorCode') and parsed.errorCode:
                error_msg = f"{parsed.errorCode}: {getattr(parsed, 'description', 'No description')}"
                logger.error(f"Cancel order error: {error_msg}")
                with self._api_lock:
                    if request_id in self._api_pending_orders:
                        self._api_pending_orders[request_id]["error"] = error_msg
                        self._api_pending_orders[request_id]["event"].set()
                return
            
            # Extract result
            result = {
                "status": "success",
                "message": "Order cancelled successfully"
            }
            
            # Extract order ID if available
            if hasattr(parsed, 'orderId'):
                logger.info(f"📋 Cancelled Order ID: {parsed.orderId}")
                result["order_id"] = parsed.orderId
            
            # Log the final result
            logger.info(f"\n✅ Final result to return:\n{json.dumps(result, indent=2, default=str)}")
            logger.info("=" * 80)
            
            # Set result and notify
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    self._api_pending_orders[request_id]["result"] = result
                    self._api_pending_orders[request_id]["event"].set()
                    
        except Exception as e:
            logger.error(f"Cancel order parse error: {e}", exc_info=True)
            with self._api_lock:
                if request_id in self._api_pending_orders:
                    self._api_pending_orders[request_id]["error"] = str(e)
                    self._api_pending_orders[request_id]["event"].set()
    
    def _on_cancel_order_error_api(self, request_id: str, error):
        """Error callback for async API cancel order requests"""
        logger.error(f"API cancel order request error: {error}")
        with self._api_lock:
            if request_id in self._api_pending_orders:
                self._api_pending_orders[request_id]["error"] = str(error)
                self._api_pending_orders[request_id]["event"].set()

    async def get_trade_signal_async(self, pair: str, timeframe: str = "M30", weeks: int = 6) -> Dict[str, Any]:
        """
        Async method to fetch trendbars and run strategy analysis.
        Returns trendbars data, strategy output/decision, and technical indicators.
        
        Args:
            pair: Forex pair (e.g., "EUR/USD")
            timeframe: Timeframe string (default: "M30")
            weeks: Number of weeks of historical data (default: 6)
        
        Returns:
            Dict containing:
                - trendbars: DataFrame converted to records
                - strategy_signal: Enhanced strategy analysis output with:
                    - decision: BUY/SELL/NO TRADE
                    - entry_price: Entry price for the trade
                    - stop_loss: Stop loss price
                    - take_profit: Take profit price
                    - risk_pips: Risk in pips (calculated)
                    - reward_pips: Reward in pips (calculated)
                    - risk_reward_ratio: R:R ratio (calculated)
                    - distance_to_entry_pips: Distance from current price to entry (calculated)
                - indicators: Technical indicators snapshot (EMAs, RSI, ATR, trend_alignment, etc.)
                - pair: The pair analyzed
                - timeframe: The timeframe used
                - count: Number of trendbars
        """
        # Wait for authentication
        timeout_count = 0
        while not self._authenticated and timeout_count < 30:
            await asyncio.sleep(0.5)
            timeout_count += 1
        
        if not self._authenticated:
            raise Exception("Trader not authenticated yet. Please wait for connection to establish.")
        
        # Check if client is connected (with fallback for older client versions)
        is_connected = True
        if hasattr(self.client, 'isConnected'):
            is_connected = self.client.isConnected()
        elif hasattr(self.client, '_connected'):
            is_connected = self.client._connected
        
        if not is_connected:
            raise Exception("cTrader client is not connected. Connection may have been lost. Please check the connection status.")
        
        # Validate pair
        if pair not in FOREX_SYMBOLS:
            raise ValueError(f"Invalid pair: {pair}")
        
        # Fetch trendbars with retry logic
        max_retries = 2
        retry_count = 0
        df = None
        
        while retry_count <= max_retries:
            try:
                df = await self.fetch_trendbars_async(pair=pair, timeframe=timeframe, weeks=weeks)
                break  # Success, exit retry loop
            except Exception as e:
                error_msg = str(e)
                if 'timeout' in error_msg.lower() or 'cancelled' in error_msg.lower() or 'Deferred' in error_msg:
                    retry_count += 1
                    if retry_count <= max_retries:
                        logger.warning(f"Retry {retry_count}/{max_retries} for {pair} after timeout error")
                        await asyncio.sleep(1.0)  # Wait before retry
                        continue
                    else:
                        raise Exception(f"Failed to fetch trendbars after {max_retries} retries: {error_msg}")
                else:
                    # Non-timeout error, don't retry
                    raise
        
        if df is None or df.empty:
            return {
                "pair": pair,
                "timeframe": timeframe,
                "weeks": weeks,
                "count": 0,
                "trendbars": [],
                "strategy_signal": {
                    "decision": "NO TRADE",
                    "reason": "No trendbar data available",
                    "stop_loss": 0,
                    "take_profit": 0,
                    "volume": 0
                },
                "indicators": {}
            }
        
        # Run strategy analysis
        strategy_signal = self.analyze_strategy(pair, df)
        
        # Safety check: ensure strategy_signal is not None
        if strategy_signal is None:
            strategy_signal = {"decision": "NO TRADE", "reason": "Strategy returned None"}
        
        # Ensure strategy_signal is a dict
        if not isinstance(strategy_signal, dict):
            strategy_signal = {"decision": "NO TRADE", "reason": f"Strategy returned invalid type: {type(strategy_signal)}"}
        
        # Compute technical indicators snapshot
        indicators = self.compute_indicators_snapshot(df)
        
        # Enhance strategy signal with risk metrics
        enhanced_signal = strategy_signal.copy()
        entry = strategy_signal.get("entry_price")
        sl = strategy_signal.get("stop_loss")
        tp = strategy_signal.get("take_profit")
        current_price = indicators.get("current_price", entry)
        
        # Check if decision is NO TRADE
        decision = enhanced_signal.get("decision") or enhanced_signal.get("action")
        if decision in (None, "NO TRADE", "HOLD", "NONE"):
            # Ensure NO TRADE responses always have stop_loss=0, take_profit=0, volume=0
            enhanced_signal["stop_loss"] = 0
            enhanced_signal["take_profit"] = 0
            enhanced_signal["volume"] = 0
            enhanced_signal["entry_price"] = enhanced_signal.get("entry_price", 0)
        elif entry and sl and tp:
            # Calculate risk metrics for valid trades
            decision_str = enhanced_signal.get("decision")
            pip_size = 0.01 if 'JPY' in pair else 0.0001
            
            if decision_str == "BUY":
                risk_price_diff = abs(entry - sl)
                reward_price_diff = abs(tp - entry)
            elif decision_str == "SELL":
                risk_price_diff = abs(sl - entry)
                reward_price_diff = abs(entry - tp)
            else:
                risk_price_diff = None
                reward_price_diff = None
            
            if risk_price_diff is not None and reward_price_diff is not None:
                # Convert price differences to pips
                risk_pips = risk_price_diff / pip_size
                reward_pips = reward_price_diff / pip_size
                
                enhanced_signal["risk_pips"] = round(risk_pips, 2)
                enhanced_signal["reward_pips"] = round(reward_pips, 2)
                if risk_pips > 0:
                    enhanced_signal["risk_reward_ratio"] = round(reward_pips / risk_pips, 2)
                enhanced_signal["distance_to_entry_pips"] = round(abs(current_price - entry) / pip_size, 2) if current_price else 0
            
            # Calculate volume based on risk amount and stop loss distance
            if decision_str in ("BUY", "SELL") and entry and sl:
                # Build price map for pip value calculation
                price_map = self._build_price_map(pair, df, current_price)
                calculated_volume = self.calculate_volume_from_risk(pair, entry, sl, decision_str, price_map)
                enhanced_signal["volume"] = calculated_volume
                enhanced_signal["risk_amount_usd"] = self.risk_amounts.get(pair, 50.0)
            else:
                # Ensure volume is set (default to 0 if not provided)
                if "volume" not in enhanced_signal:
                    enhanced_signal["volume"] = 0
        
        # Convert DataFrame to records for JSON serialization
        df_copy = df.copy()
        df_copy['timestamp'] = df_copy['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        trendbars_records = df_copy.to_dict('records')
        
        return {
            "pair": pair,
            "timeframe": timeframe,
            "weeks": weeks,
            "count": len(trendbars_records),
            "trendbars": trendbars_records,
            "strategy_signal": enhanced_signal,
            "indicators": indicators
        }
    
    def calculate_volume_from_risk(
        self,
        pair: str,
        entry_price: float,
        stop_loss: float,
        decision: str,
        price_map: dict
    ) -> float:
        """
        Calculate lot size based on actual pip value per each pair.
        price_map must contain:
            - "EURUSD"
            - "GBPUSD"
            - "USDJPY"
        """

        # Risk per trade (USD)
        risk_amount = self.risk_amounts.get(pair, 50.0)

        # Pip size
        pip_size = 0.01 if "JPY" in pair else 0.0001

        # Stop loss distance in pips
        if decision.upper() == "BUY":
            sl_distance_pips = abs(entry_price - stop_loss) / pip_size
        else:
            sl_distance_pips = abs(stop_loss - entry_price) / pip_size

        if sl_distance_pips <= 0:
            return 0.0

        # Lot size standard
        lot_contract = 100000

        # -------- PIP VALUE CALCULATION -------- #
        pair_clean = pair.replace("/", "")

        if pair_clean in ["EURUSD", "GBPUSD"]:
            # USD-quoted majors → fixed $10 per pip per lot
            pip_value = (pip_size * lot_contract)  # = $10

        elif pair_clean == "USDJPY":
            usd_jpy = price_map.get("USDJPY", entry_price)
            pip_value = (pip_size * lot_contract) / usd_jpy

        elif pair_clean == "EURGBP":
            gbpusd = price_map.get("GBPUSD")
            if gbpusd:
                pip_value = (pip_size * lot_contract) / gbpusd
            else:
                pip_value = 7.50  # fallback approx

        elif pair_clean in ["EURJPY", "GBPJPY"]:
            usd_jpy = price_map.get("USDJPY")
            if usd_jpy:
                pip_value = (pip_size * lot_contract) / usd_jpy
            else:
                pip_value = 7.0  # fallback
            
        else:
            # Unknown pair fallback
            pip_value = 10.0

        # Calculate volume
        volume = risk_amount / (sl_distance_pips * pip_value)

        # Round & ensure minimum 0.01
        volume = max(round(volume, 2), 0.01)

        return volume
    
    def _build_price_map(self, pair: str, df: pd.DataFrame, current_price: Optional[float] = None) -> Dict[str, float]:
        """
        Build a price map with current prices for reference pairs.
        Used for calculating pip values in cross-currency pairs.
        
        Args:
            pair: Current pair being analyzed
            df: DataFrame with trendbar data
            current_price: Current price of the pair (optional, will use from df if not provided)
        
        Returns:
            Dictionary with EURUSD, GBPUSD, USDJPY prices
        """
        price_map = {}
        
        # Get current price for the pair being analyzed
        if current_price is None and df is not None and not df.empty:
            current_price = float(df['close'].iloc[-1])
        
        pair_clean = pair.replace("/", "")
        
        # Set the current pair's price
        if current_price:
            price_map[pair_clean] = current_price
        
        # Set reference pairs - use defaults if not available
        # These are approximate values, but should be close enough for pip value calculation
        if "EURUSD" not in price_map:
            price_map["EURUSD"] = 1.08000  # Approximate default
        if "GBPUSD" not in price_map:
            price_map["GBPUSD"] = 1.27000  # Approximate default
        if "USDJPY" not in price_map:
            price_map["USDJPY"] = 150.000  # Approximate default
        
        # If we have the current pair's price, use it
        if pair_clean == "EURUSD" and current_price:
            price_map["EURUSD"] = current_price
        elif pair_clean == "GBPUSD" and current_price:
            price_map["GBPUSD"] = current_price
        elif pair_clean == "USDJPY" and current_price:
            price_map["USDJPY"] = current_price
        
        return price_map

    
    def analyze_strategy(self, pair: str, df: pd.DataFrame) -> Dict[str, Any]:
        strat = self.strategies.get(pair)
        if not strat or df is None or df.empty:
            return {"decision": "NO TRADE", "reason": "No data or strategy"}
        try:
            return strat.analyze_trade_signal(df, pair)
        except Exception as e:
            return {"decision": "NO TRADE", "reason": str(e)}

    def _gemini_generate(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "x-goog-api-key": self.gemini_api_key,
            "Content-Type": "application/json"
        }
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        return r.json()

    def scrape_all_news(self) -> None:
        """Scrape ForexFactory calendar once at startup and cache all events"""
        try:
            today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            
            # Initialize Selenium
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            # Railway/deployment environment settings
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-software-rasterizer')
            
            # Set Chrome binary location for Railway/Nixpacks
            chrome_bin = os.getenv('GOOGLE_CHROME_BIN') or os.getenv('CHROMIUM_BIN') or '/nix/store/*/chromium-*/bin/chromium'
            if chrome_bin and chrome_bin != '/nix/store/*/chromium-*/bin/chromium':
                chrome_options.binary_location = chrome_bin
            
            # Try webdriver-manager first (auto-downloads ChromeDriver), then fallback to system
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=chrome_options)
            except Exception:
                try:
                    # Fallback: try with system chromedriver
                    from selenium.webdriver.chrome.service import Service
                    chromedriver_path = os.getenv('CHROMEDRIVER_PATH') or '/usr/bin/chromedriver'
                    service = Service(executable_path=chromedriver_path)
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                except Exception:
                    # Final fallback: let Selenium find it automatically
                    driver = webdriver.Chrome(options=chrome_options)
            driver.implicitly_wait(10)
            
            # Format date for ForexFactory
            dt = datetime.datetime.strptime(today_str, "%Y-%m-%d")
            month_abbr = dt.strftime("%b").lower()
            ff_date = f"{month_abbr}{dt.day}.{dt.year}"
            url = f"https://www.forexfactory.com/calendar?day={ff_date}"
            
            logger.info(f"Loading ForexFactory: {url}")
            driver.get(url)
            
            # Wait for calendar table
            wait = WebDriverWait(driver, 20)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "calendar__table")))
            
            events = []
            rows = driver.find_elements(By.CLASS_NAME, "calendar__row")
            
            for row in rows:
                try:
                    # Get time
                    time_cells = row.find_elements(By.CLASS_NAME, "calendar__time")
                    if not time_cells:
                        continue
                    time_str = time_cells[0].text.strip()
                    
                    # Get currency
                    currency_cells = row.find_elements(By.CLASS_NAME, "calendar__currency")
                    currency = currency_cells[0].text.strip() if currency_cells else ""
                    
                    # Get impact
                    impact = "low"
                    impact_cells = row.find_elements(By.CLASS_NAME, "calendar__impact")
                    if impact_cells:
                        impact_spans = impact_cells[0].find_elements(By.TAG_NAME, "span")
                        if impact_spans:
                            classes = impact_spans[0].get_attribute("class")
                            if "icon--ff-impact-red" in classes:
                                impact = "high"
                            elif "icon--ff-impact-ora" in classes or "icon--ff-impact-yel" in classes:
                                impact = "medium"
                    
                    # Get event title
                    event_cells = row.find_elements(By.CLASS_NAME, "calendar__event")
                    if not event_cells:
                        continue
                    title_spans = event_cells[0].find_elements(By.CLASS_NAME, "calendar__event-title")
                    title = title_spans[0].text.strip() if title_spans else ""
                    
                    if not title:
                        continue
                    
                    # Get values
                    actual_cells = row.find_elements(By.CLASS_NAME, "calendar__actual")
                    actual = actual_cells[0].text.strip() if actual_cells else None
                    
                    forecast_cells = row.find_elements(By.CLASS_NAME, "calendar__forecast")
                    forecast = forecast_cells[0].text.strip() if forecast_cells else None
                    
                    previous_cells = row.find_elements(By.CLASS_NAME, "calendar__previous")
                    previous = previous_cells[0].text.strip() if previous_cells else None
                    
                    event = {
                        "date": today_str,
                        "time": time_str,
                        "time_utc": time_str,
                        "currency": currency,
                        "impact": impact,
                        "title": title,
                        "actual": actual if actual else None,
                        "forecast": forecast if forecast else None,
                        "previous": previous if previous else None
                    }
                    
                    events.append(event)
                    
                except Exception:
                    continue
            
            driver.quit()
            self.all_news_events = events
            logger.info(f"✅ Scraped {len(events)} total events from ForexFactory")
            
        except Exception as e:
            logger.error(f"Error scraping ForexFactory with Selenium: {e}")
            self.all_news_events = []

    def get_news_for_pair(self, pair: str) -> Dict[str, Any]:
        """Filter cached news events for a specific pair"""
        try:
            # If news hasn't been scraped yet, scrape it now (fallback for direct method calls)
            if not self.all_news_events:
                logger.warning("News not scraped yet, scraping now...")
                self.scrape_all_news()
            
            # Extract currencies from pair
            try:
                base, quote = pair.split("/")
            except Exception:
                base, quote = pair, ""
            
            currencies = [base, quote]
            
            # Filter by currencies and impact (high/medium only)
            filtered_events = []
            for event in self.all_news_events:
                event_currency = event.get("currency", "")
                event_impact = event.get("impact", "")
                if event_currency in currencies and event_impact in ["high", "medium"]:
                    filtered_events.append(event)
            
            logger.info(f"✅ Found {len(filtered_events)} relevant events for {pair}")
            
            summary = ""
            if filtered_events:
                summary = f"Found {len(filtered_events)} relevant events for {pair} today."
            else:
                summary = f"No high/medium impact events for {pair} today."
            
            return {
                "events": filtered_events[:10],
                "summary": summary
            }
            
        except Exception as e:
            logger.error(f"Error filtering news for {pair}: {e}")
            return {"events": [], "summary": f"Error filtering news: {e}"}

    def llm_recommendation(self, pair: str, strategy_signal: Dict[str, Any], indicators: Dict[str, Any], news: Dict[str, Any]) -> str:
        """Ask Gemini 2.5 Flash for a trade recommendation given strategy signal + indicators + news."""
        try:
            # Enhance signal with risk metrics
            enhanced_signal = strategy_signal.copy()
            entry = strategy_signal.get("entry_price")
            sl = strategy_signal.get("stop_loss")
            tp = strategy_signal.get("take_profit")
            current_price = indicators.get("current_price", entry)
            
            if entry and sl and tp:
                # Calculate risk metrics
                if enhanced_signal.get("decision") == "BUY":
                    risk_pips = abs(entry - sl)
                    reward_pips = abs(tp - entry)
                else:  # SELL
                    risk_pips = abs(sl - entry)
                    reward_pips = abs(entry - tp)
                
                enhanced_signal["risk_pips"] = round(risk_pips, 5)
                enhanced_signal["reward_pips"] = round(reward_pips, 5)
                if risk_pips > 0:
                    enhanced_signal["risk_reward_ratio"] = round(reward_pips / risk_pips, 2)
                enhanced_signal["distance_to_entry_pips"] = round(abs(current_price - entry), 5) if current_price else 0
            
            prompt = (
                "You are an expert forex trading assistant. Analyze the Supply & Demand strategy signal, technical indicators, and news events. "
                "Consider: trend alignment, RSI conditions, volatility (ATR), session timing, and upcoming news impact. "
                "Provide a concise recommendation (<= 8 lines) with: action (BUY/SELL/HOLD), confidence level (low/medium/high), "
                "and clear rationale combining technical analysis and news context. "
                "If conflicting signals exist, explain the conflict and provide balanced assessment."
            )
            model = "gemini-2.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {
                "contents": [
                    {"parts": [
                        {"text": prompt},
                        {"text": f"PAIR: {pair}"},
                        {"text": f"SIGNAL: {json.dumps(enhanced_signal)}"},
                        {"text": f"INDICATORS: {json.dumps(indicators)}"},
                        {"text": f"NEWS: {json.dumps(news)[:4000]}"}
                    ]}
                ],
                "generationConfig": {
                    "temperature": 0.4,
                }
            }
            resp = self._gemini_generate(url, payload)
            text = ""
            try:
                text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                text = ""
            return text or "No recommendation available."
        except Exception as e:
            return f"Error getting recommendation: {e}"

    def compute_indicators_snapshot(self, df: pd.DataFrame) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            close = df["close"]
            current_price = float(close.iloc[-1])
            out["current_price"] = current_price
            
            # EMAs
            ema_values = {}
            for p in [20, 50, 200]:
                if len(close) >= p:
                    ema_val = float(pd.Series.ewm(close, span=p, adjust=False).mean().iloc[-1])
                    ema_values[f"ema_{p}"] = ema_val
                    out[f"ema_{p}"] = ema_val
                    # Price position relative to EMA
                    out[f"price_vs_ema_{p}"] = "above" if current_price > ema_val else "below"
            
            # Trend alignment (bullish if price > EMA20 > EMA50 > EMA200)
            if len(ema_values) >= 3:
                if current_price > ema_values.get("ema_20", 0) > ema_values.get("ema_50", 0) > ema_values.get("ema_200", 0):
                    out["trend_alignment"] = "bullish"
                elif current_price < ema_values.get("ema_20", 0) < ema_values.get("ema_50", 0) < ema_values.get("ema_200", 0):
                    out["trend_alignment"] = "bearish"
                else:
                    out["trend_alignment"] = "mixed"
            
            # RSI 14
            if len(close) >= 15:
                delta = close.diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
                rsi_val = float(rsi.iloc[-1])
                out["rsi_14"] = rsi_val
                out["rsi_status"] = "overbought" if rsi_val > 70 else ("oversold" if rsi_val < 30 else "neutral")
            
            # ATR 14 (Average True Range)
            if len(df) >= 15:
                high = df["high"]
                low = df["low"]
                prev_close = close.shift(1)
                # True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
                tr1 = high - low
                tr2 = (high - prev_close).abs()
                tr3 = (low - prev_close).abs()
                true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                # ATR = Simple Moving Average of True Range (14 period)
                atr = true_range.rolling(window=14).mean()
                atr_val = float(atr.iloc[-1])
                out["atr_14"] = atr_val
                # Volatility context
                out["volatility_vs_atr"] = "high" if (high.iloc[-1] - low.iloc[-1]) > atr_val * 1.5 else ("low" if (high.iloc[-1] - low.iloc[-1]) < atr_val * 0.5 else "normal")
            
            # Recent price action (last 5 candles)
            if len(df) >= 5:
                recent_high = float(df["high"].iloc[-5:].max())
                recent_low = float(df["low"].iloc[-5:].min())
                out["recent_high_5"] = recent_high
                out["recent_low_5"] = recent_low
            
            # Volume context (if available)
            if "volume" in df.columns and len(df) >= 20:
                recent_volume = float(df["volume"].iloc[-1])
                avg_volume = float(df["volume"].iloc[-20:].mean())
                out["volume"] = recent_volume
                out["volume_vs_avg"] = "above" if recent_volume > avg_volume * 1.2 else ("below" if recent_volume < avg_volume * 0.8 else "normal")
            
            # Time context
            if "timestamp" in df.columns and len(df) > 0:
                last_timestamp = df["timestamp"].iloc[-1]
                if isinstance(last_timestamp, pd.Timestamp):
                    out["timestamp"] = last_timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                    hour = last_timestamp.hour
                    if 0 <= hour < 8:
                        out["session"] = "Asian"
                    elif 8 <= hour < 16:
                        out["session"] = "European"
                    else:
                        out["session"] = "American"
        except Exception:
            pass
        return out

    def send_pushover(self, title: str, message: str) -> None:
        app_token = self.pushover_app_token or "ah7dehvsrm6j3pmwg9se5h7svwj333"
        user_key = self.pushover_user_key or "u4ipwwnphbcs2j8iiosg3gqvompfs2"
        if not self.pushover_app_token or not self.pushover_user_key:
            logger.warning("Pushover env missing; using hardcoded fallback like ctrader.py")
        payload = {
            "token": app_token,
            "user": user_key,
            "message": message,
            "title": title,
            "priority": 1,
            "sound": "cashregister",
        }
        try:
            r = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10)
            if r.status_code != 200:
                logger.error("Pushover failed: %s", r.text)
        except Exception as e:
            logger.error("Pushover error: %s", e)

    def format_notification(self, pair: str, signal: Dict[str, Any], indicators: Dict[str, Any], news: Dict[str, Any], reco: str) -> str:
        lines = []
        action = signal.get("decision") or signal.get("action") or "UNKNOWN"
        entry = signal.get("entry_price")
        sl = signal.get("stop_loss")
        tp = signal.get("take_profit")
        rr = signal.get("risk_reward_ratio")
        lines.append(f"🎯 {action} SIGNAL")
        lines.append(f"💱 Pair: {pair}")
        lines.append("")
        lines.append("📊 TRADE LEVELS:")
        if entry is not None:
            lines.append(f"Entry: {entry:.5f}")
        if sl is not None:
            lines.append(f"Stop Loss: {sl:.5f}")
        if tp is not None:
            lines.append(f"Take Profit: {tp:.5f}")
        if rr is not None:
            lines.append(f"R:R: {rr}")
        lines.append("")
        lines.append("📈 INDICATORS:")
        for k in ["ema_20", "ema_50", "ema_200", "rsi_14", "atr_14"]:
            if k in indicators:
                val = indicators[k]
                fmt = f"{val:.2f}" if isinstance(val, float) else str(val)
                lines.append(f"{k.upper()}: {fmt}")
        # Add context indicators
        if "trend_alignment" in indicators:
            lines.append(f"Trend: {indicators['trend_alignment'].upper()}")
        if "rsi_status" in indicators:
            lines.append(f"RSI Status: {indicators['rsi_status'].upper()}")
        if "session" in indicators:
            lines.append(f"Session: {indicators['session']}")
        lines.append("")
        lines.append("📰 FOREX FACTORY NEWS:")
        if isinstance(news, dict):
            events = news.get("events") or []
            for ev in events[:3]:
                t = ev.get("time_utc", "?")
                cur = ev.get("currency", "?")
                imp = ev.get("impact", "?")
                title_ev = ev.get("title", "?")
                lines.append(f"- [{t}] {cur} {imp}: {title_ev}")
            summary = news.get("summary")
            if summary:
                lines.append(f"Summary: {summary[:180]}")
        lines.append("")
        lines.append("🤖 LLM RECOMMENDATION:")
        lines.append(reco[:600])
        lines.append("")
        lines.append(f"⏰ {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        return "\n".join(lines)

    def run_once_for_pair(self, pair: str) -> None:
        try:
            df = self.fetch_m30_trendbars(pair, weeks=6)
            # Forced signal mode for testing the full downstream pipeline
            if self.force_signal:
                # Fallbacks based on last close and candle range
                last_close = float(df['close'].iloc[-1]) if df is not None and not df.empty else 0.0
                last_range = float(df['candle_range'].iloc[-1]) if df is not None and not df.empty else 0.0
                
                # If no data, use reasonable defaults for testing
                if last_close == 0.0:
                    # Default prices for common pairs
                    default_prices = {
                        "EUR/USD": 1.08000,
                        "GBP/USD": 1.29000,
                        "USD/JPY": 149.500,
                        "EUR/JPY": 161.500,
                        "GBP/JPY": 192.500,
                        "EUR/GBP": 0.84000,
                    }
                    last_close = default_prices.get(pair, 1.0)
                    last_range = last_close * 0.002  # 0.2% range
                
                entry = self.force_entry if self.force_entry is not None else last_close
                if self.force_sl is not None and self.force_tp is not None:
                    sl = self.force_sl
                    tp = self.force_tp
                else:
                    # Heuristic SL/TP using last candle range and 1:3 RR
                    rng = last_range if last_range > 0 else (entry * 0.001)
                    if self.force_signal.upper() == 'BUY':
                        sl = entry - rng
                        tp = entry + (rng * 3.0)
                    else:
                        sl = entry + rng
                        tp = entry - (rng * 3.0)
                signal = {
                    "decision": self.force_signal.upper(),
                    "entry_price": entry,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "risk_reward_ratio": 3.0,
                    "reason": "Forced test signal"
                }
            else:
                signal = self.analyze_strategy(pair, df)
            action = signal.get("decision") or signal.get("action")
            if action in (None, "NO TRADE", "HOLD", "NONE"):
                logger.info("No trade for %s: %s", pair, signal.get("reason", ""))
                return
            indicators = self.compute_indicators_snapshot(df)
            news = self.get_news_for_pair(pair)
            reco = self.llm_recommendation(pair, signal, indicators, news)
            if "HOLD" in reco.upper():
                logger.info("LLM recommendation is HOLD for %s; skipping notification", pair)
                return
            message = self.format_notification(pair, signal, indicators, news, reco)
            title = f"{action} Signal - {pair}"
            if self.dry_run_notify:
                logger.info("[Dry Run] Would send Pushover: %s\n%s", title, message)
            else:
                self.send_pushover(title, message)
        except Exception as e:
            logger.error("Error running pair %s: %s", pair, e)

    def run(self) -> None:
        # Lifecycle now managed by Twisted reactor in connect()
        self.connect()

    def on_error(self, failure):
        error_msg = str(failure)
        if hasattr(failure, 'value'):
            error_msg = str(failure.value)
        elif hasattr(failure, 'getErrorMessage'):
            error_msg = failure.getErrorMessage()
        
        logger.error("API error: %s", error_msg)
        
        # If this is a connection error, mark as not authenticated
        if 'connection' in error_msg.lower() or 'disconnect' in error_msg.lower():
            self._authenticated = False
        
        # Only call move_next_pair if we're in CLI mode (not API mode)
        if not hasattr(self, '_api_mode') or not self._api_mode:
            self.move_next_pair()

    def _on_trendbars_response(self, pair: str, response):
        try:
            parsed = Protobuf.extract(response)
            trendbars = getattr(parsed, 'trendbar', None)
            if not trendbars:
                logger.warning("No trendbars for %s", pair)
                # If forced signal mode, continue with empty DataFrame
                if self.force_signal:
                    self.trendbar = pd.DataFrame()
                    self._after_trendbars_ready()
                else:
                    self.move_next_pair()
                return
            rows = []
            for tb in trendbars:
                rows.append({
                    'timestamp': datetime.datetime.utcfromtimestamp(tb.utcTimestampInMinutes * 60),
                    'open': (tb.low + tb.deltaOpen) / 1e5,
                    'high': (tb.low + tb.deltaHigh) / 1e5,
                    'low': tb.low / 1e5,
                    'close': (tb.low + tb.deltaClose) / 1e5,
                    'volume': tb.volume,
                })
            df = pd.DataFrame(rows)
            df.sort_values('timestamp', inplace=True)
            df['candle_range'] = df['high'] - df['low']
            self.trendbar = df
            # Continue pipeline now that df is ready
            self._after_trendbars_ready()
        except Exception as e:
            logger.error("Trendbar parse error for %s: %s", pair, e)
            self.move_next_pair()

    def process_current_pair(self):
        self.current_pair = self.pairs[self.pair_index]
        logger.info("Processing %s", self.current_pair)
        # request trendbars asynchronously
        self.fetch_m30_trendbars(self.current_pair, weeks=6)

    def _after_trendbars_ready(self):
        pair = self.current_pair
        df = self.trendbar
        # Use forced or real strategy
        if self.force_signal:
            last_close = float(df['close'].iloc[-1]) if df is not None and not df.empty else 0.0
            last_range = float(df['candle_range'].iloc[-1]) if df is not None and not df.empty else 0.0
            
            # If no data, use reasonable defaults for testing
            if last_close == 0.0:
                # Default prices for common pairs
                default_prices = {
                    "EUR/USD": 1.08000,
                    "GBP/USD": 1.29000,
                    "USD/JPY": 149.500,
                    "EUR/JPY": 161.500,
                    "GBP/JPY": 192.500,
                    "EUR/GBP": 0.84000,
                }
                last_close = default_prices.get(pair, 1.0)
                last_range = last_close * 0.002  # 0.2% range
            
            entry = self.force_entry if self.force_entry is not None else last_close
            if self.force_sl is not None and self.force_tp is not None:
                sl = self.force_sl
                tp = self.force_tp
            else:
                rng = last_range if last_range > 0 else (entry * 0.001)
                if self.force_signal.upper() == 'BUY':
                    sl = entry - rng
                    tp = entry + (rng * 3.0)
                else:
                    sl = entry + rng
                    tp = entry - (rng * 3.0)
            signal = {
                "decision": self.force_signal.upper(),
                "entry_price": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "risk_reward_ratio": 3.0,
                "reason": "Forced test signal"
            }
        else:
            signal = self.analyze_strategy(pair, df)
        action = signal.get("decision") or signal.get("action")
        if action in (None, "NO TRADE", "HOLD", "NONE"):
            logger.info("No trade for %s: %s", pair, signal.get("reason", ""))
            self.move_next_pair()
            return
        indicators = self.compute_indicators_snapshot(df)
        news = self.get_news_for_pair(pair)
        reco = self.llm_recommendation(pair, signal, indicators, news)
        if "HOLD" in reco.upper():
            logger.info("LLM recommendation is HOLD for %s; skipping notification", pair)
            self.move_next_pair()
            return
        message = self.format_notification(pair, signal, indicators, news, reco)
        title = f"{action} Signal - {pair}"
        if self.dry_run_notify:
            logger.info("[Dry Run] Would send Pushover: %s\n%s", title, message)
        else:
            self.send_pushover(title, message)
        self.move_next_pair()

    def move_next_pair(self):
        if self.pair_index < len(self.pairs) - 1:
            self.pair_index += 1
            reactor.callLater(1.5, self.process_current_pair)
        else:
            logger.info("All pairs processed; stopping")
            reactor.stop()
    
    # Symbol ID to name mapping for account data
    ID_TO_SYMBOL = {v: k for k, v in FOREX_SYMBOLS.items()}
    
    async def fetch_account_data_async(self, account_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Fetch all account data: closed deals, open positions, and account info.
        Uses the existing reactor and client connection.
        
        Args:
            account_id: Optional account ID. If None, uses self.account_id
        
        Returns:
            Dictionary with summary_stats, trades_by_symbol, account_info, open_positions
        """
        if account_id is None:
            account_id = self.account_id
        
        # Wait for authentication
        timeout_count = 0
        while not self._authenticated and timeout_count < 50:
            await asyncio.sleep(0.1)
            timeout_count += 1
        
        if not self._authenticated:
            raise ValueError("Not authenticated to cTrader")
        
        # Create request tracking structure
        request_id = f"account_data_{account_id}_{int(time.time() * 1000)}"
        event = asyncio.Event()
        loop = asyncio.get_event_loop()
        
        with self._account_data_lock:
            self._account_data_pending[request_id] = {
                'event': event,
                'loop': loop,
                'account_id': account_id,
                'closed_deals': [],
                'open_positions': [],
                'account_info': {},
                'pending_deal_orders': {},
                'pending_requests': 0,
                'data_ready': False,
                'error': None
            }
        
        data_state = self._account_data_pending[request_id]
        
        # Schedule data fetching on reactor thread
        reactor.callFromThread(self._fetch_account_data_on_reactor, account_id, request_id)
        
        # Wait for data with timeout (60 seconds)
        try:
            await asyncio.wait_for(event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            with self._account_data_lock:
                if request_id in self._account_data_pending:
                    self._account_data_pending[request_id]['error'] = "Timeout waiting for account data"
                    del self._account_data_pending[request_id]
            raise ValueError("Timeout waiting for account data")
        
        # Get result
        with self._account_data_lock:
            if request_id not in self._account_data_pending:
                raise ValueError("Account data request was cancelled or failed")
            
            if data_state['error']:
                error = data_state['error']
                del self._account_data_pending[request_id]
                raise ValueError(error)
            
            # Process and return data
            result = self._process_account_data(data_state)
            del self._account_data_pending[request_id]
            return result
    
    def _fetch_account_data_on_reactor(self, account_id: int, request_id: str):
        """Fetch account data - called on reactor thread"""
        data_state = self._account_data_pending.get(request_id)
        if not data_state:
            return
        
        # Fetch closed deals
        self._fetch_closed_deals(account_id, request_id)
        # Fetch open positions
        self._fetch_open_positions(account_id, request_id)
        # Fetch account info
        self._fetch_account_info(account_id, request_id)
    
    def _fetch_closed_deals(self, account_id: int, request_id: str):
        """Fetch closed deals"""
        try:
            start_date = datetime.datetime(2025, 7, 14, 9, 34, 39)
            end_date = datetime.datetime.now()
            
            deal_req = ProtoOADealListReq()
            deal_req.ctidTraderAccountId = account_id
            deal_req.fromTimestamp = int(start_date.timestamp() * 1000)
            deal_req.toTimestamp = int(end_date.timestamp() * 1000)
            deal_req.maxRows = 1000
            
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] += 1
            
            deferred = self.client.send(deal_req)
            deferred.addCallbacks(
                lambda resp: self._on_deals_received(resp, request_id),
                lambda failure: self._on_account_data_error(failure, request_id, "deals")
            )
        except Exception as e:
            logger.error(f"Error fetching deals: {e}")
            self._on_account_data_error(e, request_id, "deals")
    
    def _on_deals_received(self, response, request_id: str):
        """Process received deals"""
        try:
            parsed = Protobuf.extract(response)
            data_state = self._account_data_pending.get(request_id)
            if not data_state:
                return
            
            data_state['pending_requests'] -= 1
            
            if hasattr(parsed, 'deal') and parsed.deal:
                for deal in parsed.deal:
                    if (hasattr(deal, 'closePositionDetail') and 
                        deal.closePositionDetail and 
                        deal.closePositionDetail.grossProfit != 0):
                        
                        deal_time = datetime.datetime.utcfromtimestamp(deal.executionTimestamp / 1000)
                        symbol_name = self.ID_TO_SYMBOL.get(deal.symbolId, "UNKNOWN")
                        
                        raw_volume = getattr(deal, 'volume', 0)
                        raw_profit = deal.closePositionDetail.grossProfit
                        raw_commission = getattr(deal.closePositionDetail, 'commission', 0)
                        raw_swap = getattr(deal.closePositionDetail, 'swap', 0)
                        raw_price = getattr(deal.closePositionDetail, 'entryPrice', 0) or getattr(deal, 'executionPrice', 0)
                        raw_sl = getattr(deal.closePositionDetail, 'stopLoss', 0) or getattr(deal, 'stopLoss', 0)
                        raw_tp = getattr(deal.closePositionDetail, 'takeProfit', 0) or getattr(deal, 'takeProfit', 0)
                        raw_close_price = getattr(deal.closePositionDetail, 'closePrice', 0) or getattr(deal, 'closePrice', 0)
                        
                        lots = raw_volume / 100000000
                        profit_usd = raw_profit / 100
                        commission_usd = raw_commission / 100
                        swap_usd = raw_swap / 100
                        net_pnl = profit_usd + commission_usd + swap_usd
                        
                        actual_price = raw_price
                        actual_sl = raw_sl if raw_sl else 0
                        actual_tp = raw_tp if raw_tp else 0
                        actual_close = raw_close_price if raw_close_price else actual_price
                        
                        pip_size = 0.01 if 'JPY' in symbol_name else 0.0001
                        direction = 'BUY' if getattr(deal, 'tradeSide', 0) == ProtoOATradeSide.BUY else 'SELL'
                        
                        if actual_close > 0 and abs(actual_close - actual_price) > pip_size * 0.1:
                            if direction == "BUY":
                                pips = (actual_close - actual_price) / pip_size
                            else:
                                pips = (actual_price - actual_close) / pip_size
                        else:
                            pip_value_per_lot = 1.0
                            if lots > 0:
                                estimated_pips = profit_usd / (lots * pip_value_per_lot)
                                if abs(estimated_pips) > 100:
                                    estimated_pips = estimated_pips / 10
                                if abs(estimated_pips) > 100:
                                    estimated_pips = estimated_pips / 5
                                pips = estimated_pips
                            else:
                                pips = 0
                        
                        if 'JPY' in symbol_name:
                            entry_price = float(f"{actual_price:.3f}")
                            close_price = float(f"{actual_close:.3f}")
                            sl_formatted = float(f"{actual_sl:.3f}") if actual_sl else None
                            tp_formatted = float(f"{actual_tp:.3f}") if actual_tp else None
                        else:
                            entry_price = float(f"{actual_price:.5f}")
                            close_price = float(f"{actual_close:.5f}")
                            sl_formatted = float(f"{actual_sl:.5f}") if actual_sl else None
                            tp_formatted = float(f"{actual_tp:.5f}") if actual_tp else None
                        
                        trade_data = {
                            'Trade ID': int(getattr(deal, 'dealId', 0)),
                            'pair': symbol_name,
                            'Entry DateTime': deal_time.isoformat(),
                            'Buy/Sell': direction,
                            'Entry Price': entry_price,
                            'SL': sl_formatted if sl_formatted is not None else 'N/A',
                            'TP': tp_formatted if tp_formatted is not None else 'N/A',
                            'Close Price': close_price,
                            'Pips': float(f"{pips:.1f}"),
                            'Lots': float(f"{lots:.3f}"),
                            'PnL': float(f"{net_pnl:.2f}"),
                            'Win/Lose': 'WIN' if net_pnl > 0 else 'LOSE',
                            'Commission': float(f"{commission_usd:.2f}"),
                            'Swap': float(f"{swap_usd:.2f}")
                        }
                        
                        data_state['closed_deals'].append(trade_data)
            
            self._check_account_data_completion(request_id)
            
        except Exception as e:
            logger.error(f"Error processing deals: {e}")
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] -= 1
            self._on_account_data_error(e, request_id, "deals")
    
    def _fetch_open_positions(self, account_id: int, request_id: str):
        """Fetch open positions"""
        try:
            positions_req = ProtoOAReconcileReq()
            positions_req.ctidTraderAccountId = account_id
            
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] += 1
            
            deferred = self.client.send(positions_req)
            deferred.addCallbacks(
                lambda resp: self._on_positions_received(resp, request_id),
                lambda failure: self._on_account_data_error(failure, request_id, "positions")
            )
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            self._on_account_data_error(e, request_id, "positions")
    
    def _on_positions_received(self, response, request_id: str):
        """Process received positions"""
        try:
            parsed = Protobuf.extract(response)
            data_state = self._account_data_pending.get(request_id)
            if not data_state:
                return
            
            data_state['pending_requests'] -= 1
            
            if hasattr(parsed, 'position') and parsed.position:
                for position in parsed.position:
                    if not hasattr(position, 'symbolId'):
                        continue
                    
                    symbol_name = self.ID_TO_SYMBOL.get(position.symbolId, "UNKNOWN")
                    
                    position_data = {
                        'position_id': position.positionId,
                        'symbol': symbol_name,
                        'symbol_id': position.symbolId,
                        'volume': position.volume,
                        'direction': 'BUY' if position.tradeSide == ProtoOATradeSide.BUY else 'SELL',
                        'entry_price': getattr(position, 'price', 0),
                        'current_price': getattr(position, 'currentPrice', 0),
                        'unrealized_pnl': getattr(position, 'unrealizedPnL', 0),
                        'commission': getattr(position, 'usedMargin', 0)
                    }
                    
                    data_state['open_positions'].append(position_data)
            
            self._check_account_data_completion(request_id)
            
        except Exception as e:
            logger.error(f"Error processing positions: {e}")
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] -= 1
            self._on_account_data_error(e, request_id, "positions")
    
    def _fetch_account_info(self, account_id: int, request_id: str):
        """Fetch account information"""
        try:
            trader_req = ProtoOATraderReq()
            trader_req.ctidTraderAccountId = account_id
            
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] += 1
            
            deferred = self.client.send(trader_req)
            deferred.addCallbacks(
                lambda resp: self._on_account_info_received(resp, request_id, account_id),
                lambda failure: self._on_account_data_error(failure, request_id, "account_info")
            )
        except Exception as e:
            logger.error(f"Error fetching account info: {e}")
            self._on_account_data_error(e, request_id, "account_info")
    
    def _on_account_info_received(self, response, request_id: str, account_id: int):
        """Process account information"""
        try:
            parsed = Protobuf.extract(response)
            data_state = self._account_data_pending.get(request_id)
            if not data_state:
                return
            
            data_state['pending_requests'] -= 1
            
            if hasattr(parsed, 'trader'):
                trader = parsed.trader
                data_state['account_info'] = {
                    'account_id': account_id,
                    'balance': round(getattr(trader, 'balance', 0) / 100, 2),
                    'equity': round(getattr(trader, 'equity', 0) / 100, 2),
                    'free_margin': round(getattr(trader, 'freeMargin', 0) / 100, 2),
                    'margin': round(getattr(trader, 'margin', 0) / 100, 2),
                    'margin_level': round(getattr(trader, 'marginLevel', 0) / 100, 2),
                    'currency': 'USD'
                }
            
            self._check_account_data_completion(request_id)
            
        except Exception as e:
            logger.error(f"Error processing account info: {e}")
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] -= 1
            self._on_account_data_error(e, request_id, "account_info")
    
    def _fetch_trendbars_for_pairs(self, account_id: int, request_id: str):
        """Fetch trendbars for all pairs"""
        for symbol, symbol_id in FOREX_SYMBOLS.items():
            self._fetch_trendbars(symbol, symbol_id, account_id, request_id)
    
    def _fetch_trendbars(self, symbol: str, symbol_id: int, account_id: int, request_id: str, period: str = "H1", count: int = 200):
        """Fetch trendbars for a specific symbol"""
        try:
            now = datetime.datetime.now()
            hours_back = count if period == "H1" else (count * 30 / 60 if period == "M30" else count * 4 if period == "H4" else count)
            from_time = now - datetime.timedelta(hours=hours_back)
            
            trendbar_req = ProtoOAGetTrendbarsReq()
            trendbar_req.ctidTraderAccountId = account_id
            trendbar_req.symbolId = symbol_id
            trendbar_req.period = getattr(ProtoOATrendbarPeriod, period, ProtoOATrendbarPeriod.H1)
            trendbar_req.fromTimestamp = int(from_time.timestamp() * 1000)
            trendbar_req.toTimestamp = int(datetime.datetime.now().timestamp() * 1000)
            trendbar_req.count = count
            
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] += 1
            
            deferred = self.client.send(trendbar_req)
            deferred.addCallbacks(
                lambda resp, sym=symbol: self._on_trendbars_received_for_account_data(resp, sym, request_id),
                lambda failure, sym=symbol: self._on_account_data_error(failure, request_id, f"trendbars_{sym}")
            )
        except Exception as e:
            logger.error(f"Error fetching trendbars for {symbol}: {e}")
            self._on_account_data_error(e, request_id, f"trendbars_{symbol}")
    
    def _on_trendbars_received_for_account_data(self, response, symbol: str, request_id: str):
        """Process trendbars for account data"""
        try:
            parsed = Protobuf.extract(response)
            data_state = self._account_data_pending.get(request_id)
            if not data_state:
                return
            
            data_state['pending_requests'] -= 1
            
            if hasattr(parsed, 'trendbar') and parsed.trendbar:
                candles = []
                for bar in parsed.trendbar:
                    bar_time = datetime.datetime.utcfromtimestamp(bar.utcTimestampInMinutes * 60)
                    raw_open = getattr(bar, 'open', 0)
                    raw_high = getattr(bar, 'high', 0)
                    raw_low = getattr(bar, 'low', 0)
                    raw_close = getattr(bar, 'close', 0)
                    
                    candle_data = {
                        'timestamp': bar_time.isoformat(),
                        'open': float(raw_open) if raw_open else 0.0,
                        'high': float(raw_high) if raw_high else 0.0,
                        'low': float(raw_low) if raw_low else 0.0,
                        'close': float(raw_close) if raw_close else 0.0,
                        'volume': getattr(bar, 'volume', 0)
                    }
                    candles.append(candle_data)
                
                data_state['trendbars_data'][symbol] = candles
            
            self._check_account_data_completion(request_id)
            
        except Exception as e:
            logger.error(f"Error processing trendbars for {symbol}: {e}")
            data_state = self._account_data_pending.get(request_id)
            if data_state:
                data_state['pending_requests'] -= 1
            self._on_account_data_error(e, request_id, f"trendbars_{symbol}")
    
    def _check_account_data_completion(self, request_id: str):
        """Check if all account data requests are complete"""
        data_state = self._account_data_pending.get(request_id)
        if not data_state:
            return
        
        if data_state['pending_requests'] <= 0:
            data_state['data_ready'] = True
            # Signal the async event
            loop = data_state.get('loop')
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(data_state['event'].set)
            else:
                # Fallback: set event directly if we're in the same thread
                try:
                    data_state['event'].set()
                except:
                    pass
    
    def _on_account_data_error(self, failure, request_id: str, operation: str):
        """Handle account data fetch errors"""
        data_state = self._account_data_pending.get(request_id)
        if not data_state:
            return
        
        data_state['pending_requests'] -= 1
        error_msg = str(failure) if not isinstance(failure, Exception) else str(failure)
        logger.error(f"Error in {operation} for account data: {error_msg}")
        
        # Still check completion in case other requests succeed
        self._check_account_data_completion(request_id)
    
    def _process_account_data(self, data_state: Dict[str, Any]) -> Dict[str, Any]:
        """Process and format account data for return"""
        closed_deals = data_state['closed_deals']
        open_positions = data_state['open_positions']
        account_info = data_state['account_info']
        
        # Group trades by symbol
        trades_by_symbol = {}
        summary_stats = {
            'total_pairs': 0,
            'total_trades': len(closed_deals),
            'total_wins': 0,
            'total_losses': 0,
            'total_pnl': 0.0,
            'pairs_summary': {},
            'account_info': account_info,
            'open_positions': open_positions,
            'last_updated': datetime.datetime.now().isoformat()
        }
        
        for deal in closed_deals:
            symbol = deal['pair']
            symbol_key = symbol.replace('/', '_')
            
            if symbol_key not in trades_by_symbol:
                trades_by_symbol[symbol_key] = []
            
            trades_by_symbol[symbol_key].append(deal)
        
        # Calculate summary statistics
        for symbol_key, trades in trades_by_symbol.items():
            wins = sum(1 for t in trades if t['Win/Lose'] == 'WIN')
            losses = len(trades) - wins
            total_pnl = sum(t['PnL'] for t in trades)
            
            summary_stats['pairs_summary'][symbol_key] = {
                'total_trades': len(trades),
                'wins': wins,
                'losses': losses,
                'total_pnl': total_pnl,
                'win_rate': (wins / len(trades) * 100) if trades else 0.0,
                'avg_pnl': (total_pnl / len(trades)) if trades else 0.0,
                'fibonacci_accuracy': 0.0
            }
            
            summary_stats['total_wins'] += wins
            summary_stats['total_losses'] += losses
            summary_stats['total_pnl'] += total_pnl
        
        summary_stats['total_pairs'] = len(trades_by_symbol)
        if summary_stats['total_trades'] > 0:
            summary_stats['overall_win_rate'] = (summary_stats['total_wins'] / summary_stats['total_trades']) * 100
            summary_stats['avg_pnl'] = summary_stats['total_pnl'] / summary_stats['total_trades']
        else:
            summary_stats['overall_win_rate'] = 0.0
            summary_stats['avg_pnl'] = 0.0
        
        return {
            'summary_stats': summary_stats,
            'trades_by_symbol': trades_by_symbol,
            'account_id': data_state['account_id'],
            'timestamp': datetime.datetime.now().isoformat()
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a single scan of pairs or test a forced signal.")
    parser.add_argument("--pair", type=str, default=None, help="Run only this pair (e.g., EUR/USD)")
    parser.add_argument("--force-signal", type=str, choices=["BUY", "SELL"], default=None, help="Force a trade signal for testing")
    parser.add_argument("--force-entry", type=float, default=None, help="Forced entry price")
    parser.add_argument("--force-sl", type=float, default=None, help="Forced stop loss")
    parser.add_argument("--force-tp", type=float, default=None, help="Forced take profit")
    parser.add_argument("--dry-run-notify", action="store_true", help="Do not send Pushover; log message instead")
    args = parser.parse_args()

    try:
        trader = SimpleTrader()
        # Apply testing args
        trader.force_signal = args.force_signal
        trader.force_entry = args.force_entry
        trader.force_sl = args.force_sl
        trader.force_tp = args.force_tp
        trader.dry_run_notify = bool(args.dry_run_notify)

        if args.pair:
            trader.pairs = [args.pair]

        trader.run()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"Error: {e}")


