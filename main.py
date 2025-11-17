#!/usr/bin/env python3
"""
FastAPI server for cTrader trendbar data
"""

import os
import logging
import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from trader import SimpleTrader, FOREX_SYMBOLS, PAIR_TIMEFRAMES

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="cTrader Trendbar API",
    description="API server for fetching forex trendbar data from cTrader",
    version="1.0.0"
)

# Global trader instance
trader_instance: Optional[SimpleTrader] = None


class TrendbarRequest(BaseModel):
    pair: str
    timeframe: Optional[str] = None
    weeks: Optional[int] = 6


class NewsRequest(BaseModel):
    pair: Optional[str] = None
    impact: Optional[str] = None  # "high", "medium", "low", or None for all
    refresh: Optional[bool] = False  # Force refresh news cache


class OrderRequest(BaseModel):
    pair: str
    order_type: Optional[str] = "LIMIT"  # "LIMIT" or "STOP" (default: "LIMIT")
    side: str  # "BUY" or "SELL"
    volume: float  # Volume in lots (e.g., 0.01)
    entry_price: float  # Limit price for LIMIT orders, stop price for STOP orders
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    expiration_minutes: Optional[int] = 15  # Optional expiration in minutes


class CancelOrderRequest(BaseModel):
    order_id: int  # The unique ID of the order to cancel


@app.on_event("startup")
async def startup():
    """Initialize trader connection on startup"""
    global trader_instance
    try:
        logger.info("Initializing trader connection...")
        trader_instance = SimpleTrader()
        # Start Twisted reactor in background thread (API mode)
        import threading
        reactor_thread = threading.Thread(
            target=lambda: trader_instance.connect(api_mode=True),
            daemon=True
        )
        reactor_thread.start()
        logger.info("Trader connection initialized")
    except Exception as e:
        logger.error(f"Failed to initialize trader: {e}")
        raise


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "cTrader Trendbar API",
        "available_pairs": list(FOREX_SYMBOLS.keys())
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    return {"status": "healthy", "trader_connected": trader_instance.client is not None}


@app.post("/trendbar")
async def get_trendbar(request: TrendbarRequest):
    """
    Fetch trendbar data for a given pair
    
    Args:
        request: TrendbarRequest with pair, optional timeframe and weeks
    
    Returns:
        JSON response with trendbar data (timestamp, open, high, low, close, volume, candle_range)
    """
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    
    # Validate pair
    if request.pair not in FOREX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pair. Available pairs: {list(FOREX_SYMBOLS.keys())}"
        )
    
    # Use default timeframe if not provided
    timeframe = request.timeframe or PAIR_TIMEFRAMES.get(request.pair, "M30")
    weeks = request.weeks or 6
    
    try:
        logger.info(f"Fetching trendbars for {request.pair}, timeframe: {timeframe}, weeks: {weeks}")
        
        # Fetch trendbars (this will use async callback)
        df = await trader_instance.fetch_trendbars_async(
            pair=request.pair,
            timeframe=timeframe,
            weeks=weeks
        )
        
        if df is None or df.empty:
            raise HTTPException(
                status_code=404,
                detail=f"No trendbar data found for {request.pair}"
            )
        
        # Convert DataFrame to JSON
        # Convert timestamp to string for JSON serialization
        df_copy = df.copy()
        df_copy['timestamp'] = df_copy['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        
        # Convert to records format
        records = df_copy.to_dict('records')
        
        return JSONResponse(content={
            "pair": request.pair,
            "timeframe": timeframe,
            "weeks": weeks,
            "count": len(records),
            "data": records
        })
        
    except Exception as e:
        logger.error(f"Error fetching trendbars: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching trendbars: {str(e)}")


@app.get("/trendbar/{pair}")
async def get_trendbar_get(pair: str, timeframe: Optional[str] = None, weeks: Optional[int] = 6):
    """
    GET endpoint for fetching trendbar data
    
    Args:
        pair: Forex pair (e.g., EUR/USD)
        timeframe: Optional timeframe (defaults to pair's default)
        weeks: Optional number of weeks (default: 6)
    """
    request = TrendbarRequest(pair=pair, timeframe=timeframe, weeks=weeks)
    return await get_trendbar(request)


@app.post("/news")
async def get_news(request: NewsRequest):
    """
    Get forex news events from ForexFactory
    
    Args:
        request: NewsRequest with optional pair, impact filter, and refresh flag
    
    Returns:
        JSON response with news events
    """
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    
    try:
        # Force refresh if requested
        if request.refresh:
            logger.info("Refreshing news cache...")
            trader_instance.scrape_all_news()
        
        # If pair is specified, filter for that pair
        if request.pair:
            if request.pair not in FOREX_SYMBOLS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid pair. Available pairs: {list(FOREX_SYMBOLS.keys())}"
                )
            
            news_data = trader_instance.get_news_for_pair(request.pair)
            events = news_data.get("events", [])
            
            # Apply impact filter if specified
            if request.impact:
                events = [e for e in events if e.get("impact", "").lower() == request.impact.lower()]
            
            return JSONResponse(content={
                "pair": request.pair,
                "count": len(events),
                "events": events,
                "summary": news_data.get("summary", "")
            })
        else:
            # Return all news events
            all_events = trader_instance.all_news_events
            
            # Apply impact filter if specified
            if request.impact:
                all_events = [e for e in all_events if e.get("impact", "").lower() == request.impact.lower()]
            
            return JSONResponse(content={
                "count": len(all_events),
                "events": all_events,
                "summary": f"Found {len(all_events)} total events"
            })
            
    except Exception as e:
        logger.error(f"Error fetching news: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching news: {str(e)}")


@app.get("/news")
async def get_news_get(pair: Optional[str] = None, impact: Optional[str] = None, refresh: Optional[bool] = False):
    """
    GET endpoint for fetching forex news
    
    Args:
        pair: Optional forex pair (e.g., EUR/USD) to filter events
        impact: Optional impact filter ("high", "medium", "low")
        refresh: Optional flag to force refresh news cache
    """
    request = NewsRequest(pair=pair, impact=impact, refresh=refresh)
    return await get_news(request)


@app.get("/news/{pair}")
async def get_news_for_pair(pair: str, impact: Optional[str] = None):
    """
    GET endpoint for fetching news for a specific pair
    
    Args:
        pair: Forex pair (e.g., EUR/USD)
        impact: Optional impact filter ("high", "medium", "low")
    """
    request = NewsRequest(pair=pair, impact=impact, refresh=False)
    return await get_news(request)


@app.post("/news/refresh")
async def refresh_news():
    """
    Force refresh the news cache by scraping ForexFactory
    
    Returns:
        Status message and count of events scraped
    """
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    
    try:
        logger.info("Refreshing news cache...")
        trader_instance.scrape_all_news()
        count = len(trader_instance.all_news_events)
        
        return JSONResponse(content={
            "status": "success",
            "message": f"News cache refreshed successfully",
            "events_count": count,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z"
        })
        
    except Exception as e:
        logger.error(f"Error refreshing news: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error refreshing news: {str(e)}")


@app.post("/order")
async def execute_order(request: OrderRequest):
    """
    Execute a LIMIT or STOP order
    
    Args:
        request: OrderRequest with order parameters
    
    Returns:
        JSON response with order execution result
    """
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    
    # Validate pair
    if request.pair not in FOREX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pair. Available pairs: {list(FOREX_SYMBOLS.keys())}"
        )
    
    # Set default order type if not provided
    order_type = (request.order_type or "LIMIT").upper()
    
    # Validate order type
    if order_type not in ["LIMIT", "STOP"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid order_type. Must be 'LIMIT' or 'STOP'"
        )
    
    # Validate side
    if request.side.upper() not in ["BUY", "SELL"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid side. Must be 'BUY' or 'SELL'"
        )
    
    # Validate volume
    if request.volume < 0.01:
        raise HTTPException(
            status_code=400,
            detail="Volume must be at least 0.01 lots"
        )
    
    if request.volume > 100:
        raise HTTPException(
            status_code=400,
            detail="Volume cannot exceed 100 lots"
        )
    
    try:
        logger.info(
            f"Executing {order_type} {request.side} order for {request.pair}: "
            f"volume={request.volume} lots, entry={request.entry_price}"
        )
        
        result = await trader_instance.execute_order_async(
            pair=request.pair,
            order_type=order_type,
            side=request.side,
            volume=request.volume,
            entry_price=request.entry_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            expiration_minutes=request.expiration_minutes
        )
        
        return JSONResponse(content={
            "status": "success",
            "pair": request.pair,
            "order_type": order_type,
            "side": request.side,
            "volume": request.volume,
            "entry_price": request.entry_price,
            **result
        })
        
    except ValueError as e:
        logger.error(f"Validation error executing order: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error executing order: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error executing order: {str(e)}")


@app.post("/order/cancel")
async def cancel_order(request: CancelOrderRequest):
    """
    Cancel an existing pending order
    
    Args:
        request: CancelOrderRequest with order_id
    
    Returns:
        JSON response with cancellation result
    """
    if trader_instance is None:
        raise HTTPException(status_code=503, detail="Trader not initialized")
    
    # Validate order_id
    if request.order_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid order_id. Must be a positive integer"
        )
    
    try:
        logger.info(f"Cancelling order ID: {request.order_id}")
        
        result = await trader_instance.cancel_order_async(order_id=request.order_id)
        
        return JSONResponse(content={
            "status": "success",
            "order_id": request.order_id,
            **result
        })
        
    except ValueError as e:
        logger.error(f"Validation error cancelling order: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error cancelling order: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error cancelling order: {str(e)}")


@app.delete("/order/{order_id}")
async def cancel_order_delete(order_id: int):
    """
    DELETE endpoint for cancelling an order
    
    Args:
        order_id: The unique ID of the order to cancel
    """
    request = CancelOrderRequest(order_id=order_id)
    return await cancel_order(request)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

