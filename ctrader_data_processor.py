# Standard library imports
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

# Third-party imports  
import pandas as pd
from dotenv import load_dotenv
from twisted.internet import reactor, defer
from twisted.internet.defer import TimeoutError
from twisted.internet.base import ReactorBase
from twisted.internet.error import ReactorAlreadyRunning

# cTrader API imports (exactly like ctrader.py)
from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

# Forex symbols mapping with IDs (same as ctrader.py)
FOREX_SYMBOLS = {
    "EUR/USD": 1,
    "GBP/USD": 2,
    "EUR/JPY": 3,
    "USD/JPY": 4,
    "GBP/JPY": 7,
    "EUR/GBP": 9
}

# Symbol ID to name mapping
ID_TO_SYMBOL = {v: k for k, v in FOREX_SYMBOLS.items()}

class CTraderDataProcessor:
    """
    Connects to cTrader OpenAPI to fetch real trading data:
    - Closed deals (real trades)
    - Open positions 
    - Account statistics
    - Historical trendbars for charts
    """

    
    def __init__(self, account_id: Optional[int] = None):
        """
        Initialize CTraderDataProcessor
        
        Args:
            account_id: Account ID to process. If None, loads from CTRADER_ACCOUNT_ID env var
        """
        # cTrader credentials (same pattern as ctrader.py)
        self.client_id = os.getenv("CTRADER_CLIENT_ID")
        self.client_secret = os.getenv("CTRADER_CLIENT_SECRET")
        
        if account_id is None:
            account_id = os.getenv("CTRADER_ACCOUNT_ID")
            if account_id:
                account_id = int(account_id)
        
        self.account_id = account_id
        
        if not all([self.client_id, self.client_secret, self.account_id]):
            raise ValueError("Missing cTrader credentials in environment variables")
        
        # API connection
        self.host = EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(self.host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        
        # Data storage
        self.closed_deals = []
        self.pending_deal_orders = {}  # Store deals waiting for order details
        self.open_positions = []
        self.trendbars_data = {}
        self.account_info = {}
        
        # Request tracking
        self.pending_requests = 0
        self.max_wait_time = 60  # Increased timeout for order detail requests
        self.max_order_detail_requests = 50  # Maximum number of order detail requests (increase from 10)
        self.order_detail_timeout = 30  # Timeout for individual order detail requests (seconds)
        
        # Exit tracking
        self.exit_code = None  # Store exit code to exit after reactor stops
        self.data_ready = False  # Flag to indicate data is ready
        self.result_data = None  # Store final result data
        
        # Setup logging
        logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
    
    def _start_client_on_reactor(self):
        """Start client service on the reactor thread"""
        try:
            # Set up client callbacks
            self.client.setConnectedCallback(self.on_connected)
            self.client.setDisconnectedCallback(self.on_disconnected)
            self.client.setMessageReceivedCallback(self.on_message_received)
            
            # Start the client service
            self.client.startService()
            
            # Schedule timeout handler
            reactor.callLater(self.max_wait_time, self.timeout_handler)
            
            self.logger.info("Client service started on reactor thread")
        except Exception as e:
            self.logger.error(f"Error starting client on reactor thread: {e}")
            import traceback
            traceback.print_exc()
    
    def connect_and_fetch_data(self):
        """Main entry point to connect and fetch all data"""
        try:
            # Set up client callbacks first (these are thread-safe)
            self.client.setConnectedCallback(self.on_connected)
            self.client.setDisconnectedCallback(self.on_disconnected)
            self.client.setMessageReceivedCallback(self.on_message_received)
            
            # Try to start reactor - this will tell us if it's already running
            try:
                # Try to start reactor first to check if it's running
                # This will raise ReactorAlreadyRunning if reactor is in another thread
                reactor.run()
                # If we get here, we started the reactor ourselves
                self._reactor_was_running = False
                
            except ReactorAlreadyRunning:
                # Reactor is already running in another thread (from SimpleTrader)
                # We need to schedule client operations on the reactor thread
                self._reactor_was_running = True
                self.logger.info("Reactor already running in another thread, scheduling client operations on reactor thread...")
                
                # Schedule client operations on the reactor thread
                reactor.callFromThread(self._start_client_on_reactor)
                
                # Wait for data_ready flag
                import time
                start_time = time.time()
                while not self.data_ready and (time.time() - start_time) < self.max_wait_time:
                    time.sleep(0.1)
                
                if not self.data_ready:
                    self.logger.error("Timeout waiting for data to be ready")
                    self.cleanup_and_exit(False)
                return  # Exit early since we handled the reactor-running case
            
            # If we get here, we're starting our own reactor
            # Now start the client service and schedule timeout
            self.client.startService()
            reactor.callLater(self.max_wait_time, self.timeout_handler)
            
        except Exception as e:
            self.logger.error(f"Connection error: {e}")
            import traceback
            traceback.print_exc()
            self.cleanup_and_exit(False)
    
    def on_connected(self, client):
        """Called when connected to cTrader"""
        self.authenticate_app()
    
    def on_disconnected(self, client, reason):
        """Called when disconnected from cTrader"""
        if self.exit_code is None:
            self.logger.warning(f"Unexpected disconnection from cTrader server: {reason}")
        else:
            self.logger.debug(f"Disconnected from cTrader server (normal shutdown)")
    
    def on_message_received(self, client, message):
        """Handle incoming messages"""
        pass  # Let individual handlers manage responses
    
    def authenticate_app(self):
        """Authenticate the application"""
        app_auth = ProtoOAApplicationAuthReq()
        app_auth.clientId = self.client_id
        app_auth.clientSecret = self.client_secret
        
        deferred = self.client.send(app_auth)
        deferred.addCallbacks(self.on_app_auth_success, self.on_error)
    
    def on_app_auth_success(self, response):
        """App authentication successful"""
        access_token = os.getenv("CTRADER_ACCESS_TOKEN")
        self.authenticate_user(access_token)
    
    def authenticate_user(self, access_token):
        """Authenticate the user with access token"""
        user_auth = ProtoOAAccountAuthReq()
        user_auth.ctidTraderAccountId = self.account_id
        user_auth.accessToken = access_token
        
        deferred = self.client.send(user_auth)
        deferred.addCallbacks(self.on_user_auth_success, self.on_error)
    
    def on_user_auth_success(self, response):
        """User authentication successful - start data fetching"""
        # Fetch all required data
        self.fetch_closed_deals()
        self.fetch_open_positions()
        self.fetch_account_info()
        self.fetch_trendbars_for_pairs()
    
    def fetch_closed_deals(self, days_back=365):
        """Fetch closed deals using exact timestamp filtering"""
        try:
            # Use exact statement period timestamps from your CSV
            # Statement start: 14 Jul 2025 09:34:39.775, end with current datetime for latest data
            start_date = datetime.datetime(2025, 7, 14, 9, 34, 39)  # Exact start from statement
            end_date = datetime.datetime.now()  # Current datetime to get all trades up to now
            
            from_timestamp = int(start_date.timestamp() * 1000)
            to_timestamp = int(end_date.timestamp() * 1000)
            
            deal_req = ProtoOADealListReq()
            deal_req.ctidTraderAccountId = self.account_id
            deal_req.fromTimestamp = from_timestamp
            deal_req.toTimestamp = to_timestamp
            deal_req.maxRows = 1000  # Request max possible
            
            self.pending_requests += 1
            deferred = self.client.send(deal_req)
            deferred.addCallbacks(self.on_deals_received, self.on_error)
            
        except Exception as e:
            self.logger.error(f"Error fetching deals: {e}")
    
    def on_deals_received(self, response):
        """Process received deals"""
        try:
            parsed = Protobuf.extract(response)
            self.pending_requests -= 1
            
            if hasattr(parsed, 'deal') and parsed.deal:
                for deal in parsed.deal:
                    # Only process closed positions with actual P&L
                    if (hasattr(deal, 'closePositionDetail') and 
                        deal.closePositionDetail and 
                        deal.closePositionDetail.grossProfit != 0):
                        
                        # Convert timestamp and get symbol
                        deal_time = datetime.datetime.utcfromtimestamp(deal.executionTimestamp / 1000)
                        symbol_name = ID_TO_SYMBOL.get(deal.symbolId, "UNKNOWN")
                        
                        # Convert raw cTrader values to proper format (matching your statement)
                        raw_volume = getattr(deal, 'volume', 0)
                        raw_profit = deal.closePositionDetail.grossProfit
                        raw_commission = getattr(deal.closePositionDetail, 'commission', 0)
                        raw_swap = getattr(deal.closePositionDetail, 'swap', 0)
                        # Use entryPrice from closePositionDetail as requested
                        raw_price = getattr(deal.closePositionDetail, 'entryPrice', 0) or getattr(deal, 'executionPrice', 0)
                        
                        # Try to get SL and TP from closePositionDetail or deal
                        raw_sl = getattr(deal.closePositionDetail, 'stopLoss', 0) or getattr(deal, 'stopLoss', 0)
                        raw_tp = getattr(deal.closePositionDetail, 'takeProfit', 0) or getattr(deal, 'takeProfit', 0)
                        raw_close_price = getattr(deal.closePositionDetail, 'closePrice', 0) or getattr(deal, 'closePrice', 0)
                        
                        # cTrader API returns values in different formats, let's try minimal conversion
                        lots = raw_volume / 100000000  # Much smaller divisor for lots
                        
                        # Fix P&L conversion based on statement comparison
                        profit_usd = raw_profit / 100  # Gross profit
                        commission_usd = raw_commission / 100  # Commission (usually negative)
                        swap_usd = raw_swap / 100  # Swap
                        
                        # Calculate net P&L like your statement (Net USD = gross + commission + swap)
                        net_pnl = profit_usd + commission_usd + swap_usd
                        
                        # Deal prices are already in correct format - NO conversion needed
                        actual_price = raw_price
                        actual_sl = raw_sl if raw_sl else 0
                        actual_tp = raw_tp if raw_tp else 0
                        actual_close = raw_close_price if raw_close_price else actual_price
                        
                        # Set pip size based on symbol
                        if 'JPY' in symbol_name:
                            pip_size = 0.01  # JPY pairs
                        else:
                            pip_size = 0.0001  # Major pairs
                        
                        # Calculate pips gained
                        direction = 'BUY' if getattr(deal, 'tradeSide', 0) == ProtoOATradeSide.BUY else 'SELL'
                        
                        # If we have proper entry and close prices, use them
                        if actual_close > 0 and abs(actual_close - actual_price) > pip_size * 0.1:
                            if direction == "BUY":
                                pips = (actual_close - actual_price) / pip_size
                            else:  # SELL
                                pips = (actual_price - actual_close) / pip_size
                        else:
                            # Calculate pips from P&L (more reliable when close price = entry price)
                            if 'JPY' in symbol_name:
                                pip_value_per_lot = 1.0
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
                        
                        # Store deal data and fetch order details for actual SL/TP
                        order_id = getattr(deal, 'orderId', None)
                        
                        # Create basic trade data first
                        basic_trade_data = {
                            'deal_id': getattr(deal, 'dealId', 0),
                            'order_id': order_id,
                            'symbol_name': symbol_name,
                            'deal_time': deal_time.isoformat(),
                            'direction': direction,
                            'actual_price': actual_price,
                            'actual_close': actual_close,
                            'pips': pips,
                            'lots': lots,
                            'net_pnl': net_pnl,
                            'commission_usd': commission_usd,
                            'swap_usd': swap_usd,
                            'pip_size': pip_size
                        }
                        
                        # If SL/TP available from deal, use them directly
                        if actual_sl and actual_tp:
                            self.complete_trade_data(basic_trade_data, actual_sl, actual_tp)
                        # Otherwise, fetch order details for actual SL/TP (limit to prevent API overload)
                        elif order_id and len(self.pending_deal_orders) < self.max_order_detail_requests:
                            self.pending_deal_orders[order_id] = basic_trade_data
                            self.fetch_order_details(order_id)
                        else:
                            # Fallback: use estimation for remaining deals
                            if not order_id:
                                self.logger.warning(f"No order ID for deal {basic_trade_data['deal_id']}, using estimation")
                            else:
                                self.logger.warning(f"Limiting order details requests ({len(self.pending_deal_orders)}/{self.max_order_detail_requests}), using estimation for deal {basic_trade_data['deal_id']}")
                            estimated_sl, estimated_tp = self.estimate_sl_tp(basic_trade_data)
                            self.complete_trade_data(basic_trade_data, estimated_sl, estimated_tp)
            
            self.logger.info(f"Processed {len(self.closed_deals)} closed deals")
            self.check_completion()
            
        except Exception as e:
            self.logger.error(f"Error processing deals: {e}")
            self.pending_requests -= 1
            self.check_completion()
    
    def fetch_order_details(self, order_id):
        """Fetch order details to get actual SL/TP values"""
        try:
            order_req = ProtoOAOrderDetailsReq()
            order_req.ctidTraderAccountId = self.account_id
            order_req.orderId = order_id
            
            self.pending_requests += 1
            deferred = self.client.send(order_req)
            deferred.addCallbacks(
                lambda resp, oid=order_id: self.on_order_details_received(resp, oid),
                lambda failure, oid=order_id: self.on_order_error(failure, oid)
            )
            
            # Add timeout for this specific request
            reactor.callLater(self.order_detail_timeout, self.timeout_order_request, order_id)
            
        except Exception as e:
            self.logger.error(f"Error fetching order details for {order_id}: {e}")
    
    def on_order_details_received(self, response, order_id):
        """Process received order details"""
        try:
            parsed = Protobuf.extract(response)
            self.pending_requests -= 1
            
            # Check if order is still pending (might have timed out already)
            if order_id not in self.pending_deal_orders:
                self.logger.debug(f"Received order details for order {order_id} that was already processed (likely timed out)")
                self.check_completion()
                return
            
            basic_trade_data = self.pending_deal_orders[order_id]
            
            # Extract SL/TP from order details
            actual_sl = 0
            actual_tp = 0
            
            if hasattr(parsed, 'order') and parsed.order:
                order = parsed.order
                
                # Try to get SL/TP and execution price from order using correct attributes
                raw_limit_price = getattr(order, 'limitPrice', 0)  # Usually Take Profit
                raw_stop_price = getattr(order, 'stopPrice', 0)    # Usually Stop Loss
                raw_execution_price = getattr(order, 'executionPrice', 0)  # Actual entry price
                
                # Also check traditional attributes as fallback
                raw_sl_fallback = getattr(order, 'stopLoss', 0)
                raw_tp_fallback = getattr(order, 'takeProfit', 0)
                
                # Convert to actual prices based on symbol
                symbol_name = basic_trade_data['symbol_name']
                direction = basic_trade_data['direction']
                
                # Order details prices are ALREADY in correct format - NO conversion needed  
                limit_price = raw_limit_price if raw_limit_price else 0
                stop_price = raw_stop_price if stop_price > 0 else 0
                execution_price = raw_execution_price if raw_execution_price else 0
                
                # Traditional SL/TP from deal data are also already correct - NO conversion needed
                sl_fallback = raw_sl_fallback if raw_sl_fallback else 0
                tp_fallback = raw_tp_fallback if raw_tp_fallback else 0
                
                # Compare execution price with current entry price
                current_entry = basic_trade_data['actual_price']
                
                if execution_price > 0:
                    # Only use execution price if it's significantly different from current entry price
                    price_diff = abs(execution_price - current_entry)
                    threshold = basic_trade_data['pip_size'] * 2  # 2 pips threshold
                    
                    if price_diff > threshold:
                        # Use execution price as it's significantly different
                        basic_trade_data['actual_price'] = execution_price
                        
                        # Recalculate pips with the updated entry price
                        close_price = basic_trade_data['actual_close']
                        pip_size = basic_trade_data['pip_size']
                        
                        if direction == "BUY":
                            updated_pips = (close_price - execution_price) / pip_size
                        else:  # SELL
                            updated_pips = (execution_price - close_price) / pip_size
                        
                        basic_trade_data['pips'] = updated_pips
                        self.logger.info(f"Using execution price for order {order_id}: {execution_price} (was {current_entry:.5f}), pips: {updated_pips:.1f}")
                    else:
                        # Keep original entry price as execution price is too similar
                        self.logger.info(f"Keeping original entry price for order {order_id}: {current_entry:.5f} (execution: {execution_price}, diff: {price_diff:.5f})")
                else:
                    self.logger.info(f"No execution price for order {order_id}, keeping original: {current_entry:.5f}")
                
                # Simple mapping - the API names tell us directly what they are
                actual_sl = stop_price if stop_price > 0 else sl_fallback
                actual_tp = limit_price if limit_price > 0 else tp_fallback
                
                self.logger.info(f"Order {order_id}: SL={actual_sl}, TP={actual_tp}")
            
            # If still no SL/TP, use estimation as fallback
            if not actual_sl or not actual_tp:
                self.logger.warning(f"No SL/TP in order details for {order_id}, using estimation")
                estimated_sl, estimated_tp = self.estimate_sl_tp(basic_trade_data)
                actual_sl = actual_sl or estimated_sl
                actual_tp = actual_tp or estimated_tp
            
            # Complete the trade data
            self.complete_trade_data(basic_trade_data, actual_sl, actual_tp)
            
            # Remove from pending
            del self.pending_deal_orders[order_id]
            
            self.check_completion()
            
        except Exception as e:
            self.logger.error(f"Error processing order details for {order_id}: {e}")
            # Fallback to estimation
            if order_id in self.pending_deal_orders:
                basic_trade_data = self.pending_deal_orders[order_id]
                estimated_sl, estimated_tp = self.estimate_sl_tp(basic_trade_data)
                self.complete_trade_data(basic_trade_data, estimated_sl, estimated_tp)
                del self.pending_deal_orders[order_id]
            
            self.pending_requests -= 1
            self.check_completion()
    
    def on_order_error(self, failure, order_id):
        """Handle order details request errors"""
        # Check if this is a timeout error (expected) vs other errors
        error_type = str(type(failure.value).__name__) if hasattr(failure, 'value') else str(failure)
        
        if 'TimeoutError' in error_type or 'CancelledError' in error_type:
            if order_id in self.pending_deal_orders:
                self.logger.debug(f"Order {order_id} request timed out (will be handled by timeout handler)")
        else:
            self.logger.warning(f"Error fetching order {order_id}: {error_type}")
        
        # Fallback to estimation if order is still pending
        if order_id in self.pending_deal_orders:
            basic_trade_data = self.pending_deal_orders[order_id]
            estimated_sl, estimated_tp = self.estimate_sl_tp(basic_trade_data)
            self.complete_trade_data(basic_trade_data, estimated_sl, estimated_tp)
            del self.pending_deal_orders[order_id]
        
        self.pending_requests -= 1
        self.check_completion()
    
    def timeout_order_request(self, order_id):
        """Handle timeout for order detail requests"""
        if order_id in self.pending_deal_orders:
            self.logger.debug(f"Order {order_id} detail request timed out after {self.order_detail_timeout}s, using estimation")
            basic_trade_data = self.pending_deal_orders[order_id]
            estimated_sl, estimated_tp = self.estimate_sl_tp(basic_trade_data)
            self.complete_trade_data(basic_trade_data, estimated_sl, estimated_tp)
            del self.pending_deal_orders[order_id]
            self.pending_requests -= 1
            self.check_completion()
    
    def complete_trade_data(self, basic_data, sl_value, tp_value):
        """Complete trade data with SL/TP and add to closed_deals"""
        symbol_name = basic_data['symbol_name']
        
        # Clean decimal formatting based on currency pair type
        if 'JPY' in symbol_name:
            # JPY pairs: 3 decimal places (e.g., 147.403)
            entry_price = float(f"{basic_data['actual_price']:.3f}")
            close_price = float(f"{basic_data['actual_close']:.3f}")
            sl_formatted = float(f"{sl_value:.3f}") if sl_value else None
            tp_formatted = float(f"{tp_value:.3f}") if tp_value else None
        else:
            # Major pairs: 5 decimal places (e.g., 1.34365) 
            entry_price = float(f"{basic_data['actual_price']:.5f}")
            close_price = float(f"{basic_data['actual_close']:.5f}")
            sl_formatted = float(f"{sl_value:.5f}") if sl_value else None
            tp_formatted = float(f"{tp_value:.5f}") if tp_value else None
            
        trade_data = {
            'Trade ID': int(basic_data['deal_id']),
            'pair': symbol_name,
            'Entry DateTime': basic_data['deal_time'],
            'Buy/Sell': basic_data['direction'],
            'Entry Price': entry_price,
            'SL': sl_formatted if sl_formatted is not None else 'N/A',
            'TP': tp_formatted if tp_formatted is not None else 'N/A', 
            'Close Price': close_price,
            'Pips': float(f"{basic_data['pips']:.1f}"),  # 1 decimal place for pips
            'Lots': float(f"{basic_data['lots']:.3f}"),  # 3 decimal places for lots
            'PnL': float(f"{basic_data['net_pnl']:.2f}"),  # 2 decimal places for money
            'Win/Lose': 'WIN' if basic_data['net_pnl'] > 0 else 'LOSE',
            'Commission': float(f"{basic_data['commission_usd']:.2f}"),
            'Swap': float(f"{basic_data['swap_usd']:.2f}")
        }
        
        self.closed_deals.append(trade_data)
    
    def estimate_sl_tp(self, basic_data):
        """Fallback estimation for SL/TP when not available from order details"""
        actual_price = basic_data['actual_price']
        direction = basic_data['direction']
        pips = basic_data['pips']
        pip_size = basic_data['pip_size']
        
        # Calculate distance from entry to close to estimate risk
        if abs(pips) > 0:
            if pips > 0:  # Winning trade - likely hit TP
                risk_distance = abs(pips) * pip_size * 1.5  # SL was probably 1.5x further back
                reward_distance = abs(pips) * pip_size  # TP was the close price
            else:  # Losing trade - likely hit SL
                risk_distance = abs(pips) * pip_size  # SL was the close price
                reward_distance = abs(pips) * pip_size * 2.5  # TP was probably 2.5x further
        else:
            # Default to 25 pips risk, 50 pips reward for major pairs
            risk_distance = 25 * pip_size
            reward_distance = 50 * pip_size
        
        # Calculate estimated values differently for BUY vs SELL
        if direction == "BUY":
            estimated_sl = actual_price - risk_distance  # SL below entry for BUY
            estimated_tp = actual_price + reward_distance  # TP above entry for BUY
        else:  # SELL
            estimated_sl = actual_price + risk_distance  # SL above entry for SELL
            estimated_tp = actual_price - reward_distance  # TP below entry for SELL
        
        return estimated_sl, estimated_tp
    
    def fetch_open_positions(self):
        """Fetch current open positions"""
        try:
            positions_req = ProtoOAReconcileReq()
            positions_req.ctidTraderAccountId = self.account_id
            
            self.pending_requests += 1
            deferred = self.client.send(positions_req)
            deferred.addCallbacks(self.on_positions_received, self.on_error)
            
        except Exception as e:
            self.logger.error(f"Error fetching positions: {e}")
    
    def on_positions_received(self, response):
        """Process received positions"""
        try:
            parsed = Protobuf.extract(response)
            self.pending_requests -= 1
            
            if hasattr(parsed, 'position') and parsed.position:
                for position in parsed.position:
                    # Check if position has required fields
                    if not hasattr(position, 'symbolId'):
                        continue  # Skip positions without symbolId
                    
                    symbol_name = ID_TO_SYMBOL.get(position.symbolId, "UNKNOWN")
                    
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
                    
                    self.open_positions.append(position_data)
            
            self.logger.info(f"Processed {len(self.open_positions)} open positions")
            self.check_completion()
            
        except Exception as e:
            self.logger.error(f"Error processing positions: {e}")
            self.pending_requests -= 1
            self.check_completion()
    
    def fetch_account_info(self):
        """Fetch account information"""
        try:
            trader_req = ProtoOATraderReq()
            trader_req.ctidTraderAccountId = self.account_id
            
            self.pending_requests += 1
            deferred = self.client.send(trader_req)
            deferred.addCallbacks(self.on_account_info_received, self.on_error)
            
        except Exception as e:
            self.logger.error(f"Error fetching account info: {e}")
    
    def on_account_info_received(self, response):
        """Process account information"""
        try:
            parsed = Protobuf.extract(response)
            self.pending_requests -= 1
            
            if hasattr(parsed, 'trader'):
                trader = parsed.trader
                self.account_info = {
                    'account_id': self.account_id,
                    'balance': round(getattr(trader, 'balance', 0) / 100, 2),  # Convert from cents to dollars
                    'equity': round(getattr(trader, 'equity', 0) / 100, 2),
                    'free_margin': round(getattr(trader, 'freeMargin', 0) / 100, 2),
                    'margin': round(getattr(trader, 'margin', 0) / 100, 2),
                    'margin_level': round(getattr(trader, 'marginLevel', 0) / 100, 2),
                    'currency': 'USD'
                }
            
            self.logger.info(f"Account info retrieved")
            self.check_completion()
            
        except Exception as e:
            self.logger.error(f"Error processing account info: {e}")
            self.pending_requests -= 1
            self.check_completion()
    
    def fetch_trendbars_for_pairs(self):
        """Fetch recent trendbar data for all pairs"""
        for symbol, symbol_id in FOREX_SYMBOLS.items():
            self.fetch_trendbars(symbol, symbol_id)
    
    def fetch_trendbars(self, symbol, symbol_id, period="H1", count=200):
        """Fetch trendbar data for a specific symbol"""
        try:
            # Calculate from timestamp (last 'count' periods)
            now = datetime.datetime.now()
            
            # Estimate time range based on period
            if period == "M30":
                hours_back = count * 30 / 60
            elif period == "H1":
                hours_back = count
            elif period == "H4":
                hours_back = count * 4
            else:
                hours_back = count  # Default to H1
            
            from_time = now - datetime.timedelta(hours=hours_back)
            from_timestamp = int(from_time.timestamp() * 1000)
            
            trendbar_req = ProtoOAGetTrendbarsReq()
            trendbar_req.ctidTraderAccountId = self.account_id
            trendbar_req.symbolId = symbol_id
            trendbar_req.period = getattr(ProtoOATrendbarPeriod, period, ProtoOATrendbarPeriod.H1)
            trendbar_req.fromTimestamp = from_timestamp
            trendbar_req.toTimestamp = int(datetime.datetime.now().timestamp() * 1000)
            trendbar_req.count = count
            
            self.pending_requests += 1
            deferred = self.client.send(trendbar_req)
            deferred.addCallbacks(
                lambda resp, sym=symbol: self.on_trendbars_received(resp, sym),
                self.on_error
            )
            
        except Exception as e:
            self.logger.error(f"Error fetching trendbars for {symbol}: {e}")
    
    def on_trendbars_received(self, response, symbol):
        """Process received trendbar data"""
        try:
            parsed = Protobuf.extract(response)
            self.pending_requests -= 1
            
            if hasattr(parsed, 'trendbar') and parsed.trendbar:
                candles = []
                
                for bar in parsed.trendbar:
                    # Convert timestamp
                    bar_time = datetime.datetime.utcfromtimestamp(bar.utcTimestampInMinutes * 60)
                    
                    # Get raw values and check if they need conversion
                    raw_open = getattr(bar, 'open', 0)
                    raw_high = getattr(bar, 'high', 0)
                    raw_low = getattr(bar, 'low', 0)
                    raw_close = getattr(bar, 'close', 0)
                    
                    # cTrader API returns prices in correct format already
                    candle_data = {
                        'timestamp': bar_time.isoformat(),
                        'open': float(raw_open) if raw_open else 0.0,
                        'high': float(raw_high) if raw_high else 0.0,
                        'low': float(raw_low) if raw_low else 0.0,
                        'close': float(raw_close) if raw_close else 0.0,
                        'volume': getattr(bar, 'volume', 0)
                    }
                    
                    candles.append(candle_data)
                
                self.trendbars_data[symbol] = candles
                self.logger.info(f"Retrieved {len(candles)} candles for {symbol}")
            
            self.check_completion()
            
        except Exception as e:
            self.logger.error(f"Error processing trendbars for {symbol}: {e}")
            self.pending_requests -= 1
            self.check_completion()
    
    def check_completion(self):
        """Check if all data has been fetched and process results"""
        # Check if all requests are done AND no pending order details
        if self.pending_requests <= 0 and len(self.pending_deal_orders) == 0:
            self.process_and_prepare_data()
            self.cleanup_and_exit(True)
        elif len(self.pending_deal_orders) > 0:
            self.logger.info(f"Waiting for {len(self.pending_deal_orders)} order details...")
    
    def process_and_prepare_data(self):
        """Process all fetched data and prepare for return (without saving to Firebase)"""
        try:
            # Process trades data by symbol
            trades_by_symbol = {}
            summary_stats = {
                'total_pairs': 0,
                'total_trades': len(self.closed_deals),
                'total_wins': 0,
                'total_losses': 0,
                'total_pnl': 0.0,
                'pairs_summary': {},
                'account_info': self.account_info,
                'open_positions': self.open_positions,
                'last_updated': datetime.datetime.now().isoformat()
            }
            
            # Group trades by symbol
            for deal in self.closed_deals:
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
                    'fibonacci_accuracy': 0.0  # Not calculated from API data
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
            
            # Prepare result data structure
            self.result_data = {
                'summary_stats': summary_stats,
                'trades_by_symbol': trades_by_symbol,
                'trendbars_data': self.trendbars_data,
                'account_id': self.account_id,
                'timestamp': datetime.datetime.now().isoformat()
            }
            
            self.data_ready = True
            self.logger.info(f"Data prepared successfully!")
            self.logger.info(f"Total trades: {summary_stats['total_trades']}")
            self.logger.info(f"Win rate: {summary_stats.get('overall_win_rate', 0):.1f}%")
            self.logger.info(f"Total P&L: ${summary_stats['total_pnl']:.2f}")
            
        except Exception as e:
            self.logger.error(f"Error processing data: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            raise
    
    def on_error(self, failure):
        """Handle API errors"""
        self.logger.error(f"API Error: {failure}")
        self.pending_requests = max(0, self.pending_requests - 1)
        self.check_completion()
    
    def timeout_handler(self):
        """Handle connection timeout"""
        self.logger.warning("Connection timeout - stopping...")
        self.cleanup_and_exit(False)
    
    def cleanup_and_exit(self, success=True):
        """Clean up and exit"""
        exit_code = 0 if success else 1
        if success:
            self.logger.info("Data processing completed successfully!")
        else:
            self.logger.error("Data processing failed or timed out")
        # Store exit code and stop reactor (only if we started it)
        self.exit_code = exit_code
        try:
            # Only stop reactor if we started it (not if it was already running)
            if reactor.running and not getattr(self, '_reactor_was_running', False):
                reactor.callLater(0.1, lambda: None)  # Small delay to ensure cleanup
                reactor.stop()
        except Exception as e:
            # If reactor is in another thread, we can't stop it
            self.logger.debug(f"Could not stop reactor (may be in another thread): {e}")
            pass
    
    def get_result_data(self) -> Optional[Dict[str, Any]]:
        """Get the processed result data"""
        return self.result_data if self.data_ready else None

