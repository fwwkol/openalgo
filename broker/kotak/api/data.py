import httpx
import json
import pandas as pd
import urllib.parse
from database.token_db import get_token, get_br_symbol, get_brexchange
from utils.logging import get_logger
from utils.httpx_client import get_httpx_client

logger = get_logger(__name__)

class BrokerData:
    def __init__(self, auth_token):
        # Updated for Neo API v2: session_token:::session_sid:::base_url:::access_token
        self.session_token, self.session_sid, self.base_url, self.access_token = auth_token.split(":::")
        
        # Override baseUrl with the working quotes server
        self.quotes_base_url = "https://cis.kotaksecurities.com"
        logger.info(f"Using quotes baseUrl: {self.quotes_base_url}")
        
        # Define empty timeframe map since Kotak Neo doesn't support historical data
        self.timeframe_map = {}
        logger.warning("Kotak Neo does not support historical data intervals")

    def _get_kotak_exchange(self, exchange):
        """Map OpenAlgo exchange to Kotak exchange segment"""
        exchange_map = {
            'NSE': 'nse_cm', 
            'BSE': 'bse_cm', 
            'NFO': 'nse_fo',
            'BFO': 'bse_fo', 
            'CDS': 'cde_fo', 
            'MCX': 'mcx_fo', 
            'NSE_INDEX': 'nse_cm', 
            'BSE_INDEX': 'bse_cm'
        }
        return exchange_map.get(exchange)

    def _get_index_symbol(self, symbol):
        """Map OpenAlgo index symbols to Kotak Neo API format"""
        index_map = {
            'NIFTY': 'Nifty 50',
            'NIFTY50': 'Nifty 50',
            'BANKNIFTY': 'Nifty Bank',
            'SENSEX': 'SENSEX',
            'BANKEX': 'BANKEX',
            'FINNIFTY': 'Nifty Fin Service',
            'MIDCPNIFTY': 'NIFTY MIDCAP 100'
        }
        # Return mapped symbol or original symbol if not found
        return index_map.get(symbol.upper(), symbol)

    def _make_quotes_request(self, query, filter_name="all"):
        """Make HTTP request to Neo API v2 quotes endpoint using httpx connection pooling"""
        try:
            # Get the shared httpx client with connection pooling
            client = get_httpx_client()

            # URL encode only spaces, keep pipe character (|) as is
            # The query format is: exchange_segment|symbol (e.g., nse_cm|INFY-EQ)
            encoded_query = urllib.parse.quote(query, safe='|')
            endpoint = f"/script-details/1.0/quotes/neosymbol/{encoded_query}/{filter_name}"

            # Neo API v2 quotes headers - only Authorization (access token), no Auth/Sid
            headers = {
                'Authorization': self.access_token,
                'Content-Type': 'application/json'
            }

            # Construct full URL
            url = f"{self.quotes_base_url}{endpoint}"

            logger.info(f"QUOTES API - Making request to: {url}")
            logger.debug(f"QUOTES API - Using access_token: {self.access_token[:10]}...")

            # Make request using httpx
            response = client.get(url, headers=headers)

            logger.info(f"QUOTES API - Response status: {response.status_code}")

            if response.status_code == 200:
                response_data = json.loads(response.text)
                logger.debug(f"QUOTES API - Raw response: {response.text[:200]}...")  # Log first 200 chars
                return response_data
            else:
                logger.warning(f"QUOTES API - HTTP {response.status_code}: {response.text}")
                return None

        except httpx.HTTPError as e:
            logger.error(f"HTTP error in _make_quotes_request: {e}")
            return None
        except Exception as e:
            logger.error(f"Error in _make_quotes_request: {e}")
            return None

    def get_quotes(self, symbol, exchange):
        """Get live quotes using Neo API v2 quotes endpoint with pSymbol-based queries"""
        try:
            logger.info(f"QUOTES API - Symbol: {symbol}, Exchange: {exchange}")

            # Check if this is an index - use symbol name instead of pSymbol
            if 'INDEX' in exchange.upper():
                # For indices, map to correct Neo API format and use static exchange mapping
                kotak_exchange = self._get_kotak_exchange(exchange)
                neo_symbol = self._get_index_symbol(symbol)
                query = f"{kotak_exchange}|{neo_symbol}"
                logger.info(f"QUOTES API - Index query: {symbol} → {neo_symbol} → {query}")
            else:
                # For regular stocks/F&O, get both pSymbol and brexchange from database
                # In Kotak DB: token = pSymbol, brexchange = nse_cm/nse_fo/bse_cm etc.
                psymbol = get_token(symbol, exchange)
                brexchange = get_brexchange(symbol, exchange)
                logger.info(f"QUOTES API - pSymbol: {psymbol}, brexchange: {brexchange}")

                if not psymbol or not brexchange:
                    logger.error(f"pSymbol or brexchange not found for {symbol} on {exchange}")
                    return self._get_default_quote()

                # Build query using brexchange from database: brexchange|pSymbol
                query = f"{brexchange}|{psymbol}"
                logger.info(f"QUOTES API - Query: {query}")
            
            # Make API request
            response = self._make_quotes_request(query, "all")
            
            if response and isinstance(response, list) and len(response) > 0:
                quote_data = response[0]
                logger.info(f"QUOTES API - Query successful for: {quote_data.get('display_symbol')}")
            else:
                logger.error(f"QUOTES API - Query failed for {symbol}")
                return self._get_default_quote()
            
            if response and isinstance(response, list) and len(response) > 0:
                quote_data = response[0]
                
                # Parse Neo API v2 response format (based on actual API response)
                ohlc_data = quote_data.get('ohlc', {})
                return {
                    'bid': float(quote_data.get('total_buy', 0)),
                    'ask': float(quote_data.get('total_sell', 0)),
                    'open': float(ohlc_data.get('open', 0)),
                    'high': float(ohlc_data.get('high', 0)),
                    'low': float(ohlc_data.get('low', 0)),
                    'ltp': float(quote_data.get('ltp', 0)),
                    'prev_close': float(ohlc_data.get('close', 0)),
                    'volume': float(quote_data.get('last_volume', 0)),
                    'oi': int(quote_data.get('open_int', 0))  # Available in response
                }
            elif response is not None:
                # API returned 200 but empty response - this is normal for some symbols
                logger.info(f"Empty response received for {symbol} - API returned 200 but no data")
                return self._get_default_quote()
            else:
                logger.warning(f"No quote data received for {symbol}")
                return self._get_default_quote()
                
        except Exception as e:
            logger.error(f"Error in get_quotes: {e}")
            return self._get_default_quote()

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """Get market depth using Neo API v2 quotes endpoint with depth filter"""
        try:
            logger.info(f"DEPTH API - Symbol: {symbol}, Exchange: {exchange}")

            # Check if this is an index - use symbol name instead of pSymbol
            if 'INDEX' in exchange.upper():
                # For indices, map to correct Neo API format and use static exchange mapping
                kotak_exchange = self._get_kotak_exchange(exchange)
                neo_symbol = self._get_index_symbol(symbol)
                query = f"{kotak_exchange}|{neo_symbol}"
                logger.debug(f"DEPTH API - Index query: {symbol} → {neo_symbol} → {query}")
            else:
                # For regular stocks/F&O, get both pSymbol and brexchange from database
                # In Kotak DB: token = pSymbol, brexchange = nse_cm/nse_fo/bse_cm etc.
                psymbol = get_token(symbol, exchange)
                brexchange = get_brexchange(symbol, exchange)
                logger.info(f"DEPTH API - pSymbol: {psymbol}, brexchange: {brexchange}")

                if not psymbol or not brexchange:
                    logger.error(f"pSymbol or brexchange not found for {symbol} on {exchange}")
                    return self._get_default_depth()

                # Build query using brexchange from database: brexchange|pSymbol
                query = f"{brexchange}|{psymbol}"
                logger.debug(f"DEPTH API - Query: {query}")
            
            # Make API request with depth filter
            response = self._make_quotes_request(query, "depth")
            
            if response and isinstance(response, list) and len(response) > 0:
                target_quote = response[0]
                depth_data = target_quote.get('depth', {})
                
                # Parse Neo API v2 depth format (based on actual API response)
                bids = []
                asks = []
                
                # Process buy orders (bids) - handle both array and object formats
                buy_data = depth_data.get('buy', [])
                if isinstance(buy_data, list):
                    for bid in buy_data[:5]:  # Top 5 bids
                        bids.append({
                            'price': float(bid.get('price', 0)),
                            'quantity': int(bid.get('quantity', 0))
                        })
                
                # Process sell orders (asks) - handle both array and object formats  
                sell_data = depth_data.get('sell', [])
                if isinstance(sell_data, list):
                    for ask in sell_data[:5]:  # Top 5 asks
                        asks.append({
                            'price': float(ask.get('price', 0)),
                            'quantity': int(ask.get('quantity', 0))
                        })
                
                # Ensure we have 5 levels
                while len(bids) < 5:
                    bids.append({'price': 0, 'quantity': 0})
                while len(asks) < 5:
                    asks.append({'price': 0, 'quantity': 0})
                
                return {
                    'bids': bids,
                    'asks': asks,
                    'totalbuyqty': sum(bid['quantity'] for bid in bids),
                    'totalsellqty': sum(ask['quantity'] for ask in asks)
                }
            else:
                logger.warning(f"No depth data received for {symbol}")
                return self._get_default_depth()

        except Exception as e:
            logger.error(f"Error in get_depth: {e}")
            return self._get_default_depth()

    def _get_default_quote(self):
        """Return default quote structure"""
        return {
            'bid': 0, 'ask': 0, 'open': 0,
            'high': 0, 'low': 0, 'ltp': 0,
            'prev_close': 0, 'volume': 0, 'oi': 0
        }

    def _get_default_depth(self):
        """Return default depth structure"""
        return {
            'bids': [{'price': 0, 'quantity': 0} for _ in range(5)],
            'asks': [{'price': 0, 'quantity': 0} for _ in range(5)],
            'totalbuyqty': 0,
            'totalsellqty': 0
        }

    def get_history(self, symbol: str, exchange: str, interval: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Placeholder for historical data - not supported by Kotak Neo"""
        empty_df = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        logger.warning("Kotak Neo does not support historical data")
        return empty_df

    def get_supported_intervals(self) -> dict:
        """Return supported intervals matching the format expected by intervals.py"""
        intervals = {
            'seconds': [],
            'minutes': [],
            'hours': [],
            'days': [],
            'weeks': [],
            'months': []
        }
        logger.warning("Kotak Neo does not support historical data intervals")
        return intervals
