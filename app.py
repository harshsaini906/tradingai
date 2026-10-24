import os
import json
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, request

# Try to use curl_cffi for native TLS fingerprint impersonation (bypasses NSE Akamai/WAF)
# Fallback seamlessly to standard requests
try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False

app = Flask(__name__)

# Base configuration & endpoints
NSE_BASE_URL = "https://www.nseindia.com"
GAINERS_URL = "https://www.nseindia.com/api/live-analysis-variations?index=gainers"
LOSERS_URL = "https://www.nseindia.com/api/live-analysis-variations?index=loosers"
VOLUME_GAINERS_URL = "https://www.nseindia.com/api/live-analysis-volume-gainers"

COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
}

API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.nseindia.com/',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

# In-memory cache to prevent IP rate-limiting from NSE
_cache = {
    "data": None,
    "last_fetched": 0,
    "ttl": 28  # Cache duration in seconds (synced with frontend 30s auto-refresh)
}

_volume_cache = {
    "data": None,
    "last_fetched": 0,
    "ttl": 28
}

_session = None

def get_session():
    """Initializes and returns a session pre-loaded with NSE cookies."""
    global _session
    if _session is None:
        if USE_CURL_CFFI:
            _session = requests.Session(impersonate="chrome124")
        else:
            _session = requests.Session()
        
        # Warm up session by requesting homepage to acquire required security cookies
        try:
            _session.get(NSE_BASE_URL, headers=COMMON_HEADERS, timeout=12)
        except Exception as e:
            print(f"[NSE] Warning during session warm-up: {e}")
    return _session

def reset_session():
    """Resets the session when cookies expire or authentication fails."""
    global _session
    _session = None
    return get_session()

def clean_stock_data(raw_items):
    """Parses and formats raw stock items from NSE into a clean list of dictionaries."""
    formatted = []
    for item in raw_items:
        try:
            symbol = item.get('symbol', 'N/A')
            ltp = float(item.get('ltp', 0.0) or 0.0)
            prev_price = float(item.get('prev_price', 0.0) or 0.0)
            
            # Calculate absolute price change
            if ltp and prev_price:
                change = round(ltp - prev_price, 2)
            else:
                change = float(item.get('net_price', 0.0) or 0.0)
                
            p_change = float(item.get('perChange', 0.0) or 0.0)
            high = float(item.get('high_price', 0.0) or 0.0)
            low = float(item.get('low_price', 0.0) or 0.0)
            open_price = float(item.get('open_price', 0.0) or 0.0)
            volume = int(item.get('trade_quantity', 0) or 0)
            turnover = float(item.get('turnover', 0.0) or 0.0)
            
            formatted.append({
                "symbol": symbol,
                "ltp": ltp,
                "prev_price": prev_price,
                "change": change,
                "pChange": p_change,
                "high": high,
                "low": low,
                "open": open_price,
                "volume": volume,
                "turnover": turnover
            })
        except Exception:
            continue
    return formatted

def fetch_nse_market_data(index="NIFTY"):
    """
    Fetches real-time Top Gainers and Top Losers from NSE India.
    Implements cookie warmup, browser header impersonation, and caching.
    """
    global _cache
    current_time = time.time()

    # Serve cached data if still within TTL
    if _cache["data"] and (current_time - _cache["last_fetched"] < _cache["ttl"]):
        return _cache["data"]

    session = get_session()
    
    gainers_list = []
    losers_list = []
    status_msg = "Live"
    timestamp_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")

    try:
        # 1. Fetch Gainers
        res_gainers = session.get(GAINERS_URL, headers=API_HEADERS, timeout=10)
        
        # If session expired or unauthorized, reset session and retry once
        if res_gainers.status_code in (401, 403):
            session = reset_session()
            res_gainers = session.get(GAINERS_URL, headers=API_HEADERS, timeout=10)

        # 2. Fetch Losers (Note: NSE endpoint uses 'loosers')
        res_losers = session.get(LOSERS_URL, headers=API_HEADERS, timeout=10)
        if res_losers.status_code in (401, 403):
            session = reset_session()
            res_losers = session.get(LOSERS_URL, headers=API_HEADERS, timeout=10)

        if res_gainers.status_code == 200:
            gainers_json = res_gainers.json()
            gainers_raw = gainers_json.get(index, {}).get('data', [])
            gainers_list = clean_stock_data(gainers_raw)
            # Sort descending by % change
            gainers_list.sort(key=lambda x: x['pChange'], reverse=True)

        if res_losers.status_code == 200:
            losers_json = res_losers.json()
            losers_raw = losers_json.get(index, {}).get('data', [])
            losers_list = clean_stock_data(losers_raw)
            # Sort ascending by % change
            losers_list.sort(key=lambda x: x['pChange'])

        market_data = {
            "gainers": gainers_list,
            "losers": losers_list,
            "last_updated": timestamp_str,
            "status": status_msg,
            "index": index
        }

        # Update cache on successful fetch
        if gainers_list or losers_list:
            _cache["data"] = market_data
            _cache["last_fetched"] = current_time

        return market_data

    except Exception as err:
        print(f"[NSE] Error fetching live data: {err}")
        # If cache exists from earlier fetch, return it with warning status
        if _cache["data"]:
            cached = dict(_cache["data"])
            cached["status"] = "Cached (Connection Issue)"
            return cached
            
        # Fallback if no network or first request failed
        return {
            "gainers": [],
            "losers": [],
            "last_updated": timestamp_str,
            "status": f"Error: {str(err)}",
            "index": index
        }

@app.route('/')
def index():
    """Main route displaying Top Gainers and Top Losers."""
    selected_index = request.args.get('index', 'NIFTY')
    data = fetch_nse_market_data(index=selected_index)
    
    return render_template(
        'index.html',
        gainers=data["gainers"],
        losers=data["losers"],
        last_updated=data["last_updated"],
        status=data["status"],
        current_index=selected_index
    )

@app.route('/api/market-data')
def api_market_data():
    """JSON API endpoint for AJAX polling or dynamic UI updates."""
    selected_index = request.args.get('index', 'NIFTY')
    data = fetch_nse_market_data(index=selected_index)
    return jsonify(data)

def fetch_volume_gainers():
    """
    Fetches real-time Volume Gainers / Spurts from NSE India.
    Endpoint: /api/live-analysis-volume-gainers
    Returns top 25 stocks with highest volume vs. their weekly average.
    """
    global _volume_cache
    current_time = time.time()

    if _volume_cache["data"] and (current_time - _volume_cache["last_fetched"] < _volume_cache["ttl"]):
        return _volume_cache["data"]

    session = get_session()
    timestamp_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")

    vol_headers = dict(API_HEADERS)
    vol_headers['Referer'] = 'https://www.nseindia.com/market-data/volume-gainers-spurts'

    try:
        res = session.get(VOLUME_GAINERS_URL, headers=vol_headers, timeout=10)

        if res.status_code in (401, 403):
            session = reset_session()
            res = session.get(VOLUME_GAINERS_URL, headers=vol_headers, timeout=10)

        if res.status_code == 200:
            raw = res.json()
            items = raw.get('data', [])
            cleaned = []
            for item in items:
                try:
                    ltp = float(item.get('ltp', 0.0) or 0.0)
                    p_change = float(item.get('pChange', 0.0) or 0.0)
                    if p_change != -100.0 and ltp > 0:
                        prev_close = ltp / (1.0 + (p_change / 100.0))
                        change = round(ltp - prev_close, 2)
                    else:
                        change = 0.0

                    cleaned.append({
                        "symbol":        item.get('symbol', 'N/A'),
                        "companyName":   item.get('companyName', ''),
                        "ltp":           ltp,
                        "change":        change,
                        "pChange":       p_change,
                        "volume":        int(item.get('volume', 0) or 0),
                        "week1AvgVol":   int(item.get('week1AvgVolume', 0) or 0),
                        "week2AvgVol":   int(item.get('week2AvgVolume', 0) or 0),
                        "week1VolChg":   round(float(item.get('week1volChange', 0.0) or 0.0), 2),
                        "week2VolChg":   round(float(item.get('week2volChange', 0.0) or 0.0), 2),
                        "turnover":      float(item.get('turnover', 0.0) or 0.0),
                    })
                except Exception:
                    continue

            result = {
                "volumeGainers": cleaned,
                "last_updated":  raw.get('timestamp', timestamp_str),
                "status":        "Live",
                "count":         len(cleaned)
            }

            if cleaned:
                _volume_cache["data"] = result
                _volume_cache["last_fetched"] = current_time
                return result

    except Exception as err:
        print(f"[NSE Volume] Error: {err}")
        if _volume_cache["data"]:
            cached = dict(_volume_cache["data"])
            cached["status"] = "Cached (Connection Issue)"
            return cached

    # Fallback to high volume stocks from market data cache if volume endpoint returns 0
    if _cache["data"]:
        all_mkt = (_cache["data"].get("gainers", []) + _cache["data"].get("losers", []))
        all_mkt_sorted = sorted(all_mkt, key=lambda x: x.get("volume", 0), reverse=True)
        fallback_cleaned = []
        for s in all_mkt_sorted[:25]:
            vol = s.get("volume", 0)
            avg_est = max(1, int(vol * 0.4))
            spurt = round(vol / avg_est, 1)
            fallback_cleaned.append({
                "symbol": s.get("symbol"),
                "companyName": s.get("symbol"),
                "ltp": s.get("ltp", 0.0),
                "change": s.get("change", 0.0),
                "pChange": s.get("pChange", 0.0),
                "volume": vol,
                "week1AvgVol": avg_est,
                "week2AvgVol": avg_est,
                "week1VolChg": spurt,
                "week2VolChg": spurt,
                "turnover": s.get("turnover", 0.0)
            })
        if fallback_cleaned:
            return {"volumeGainers": fallback_cleaned, "last_updated": timestamp_str, "status": "Market Volume Fallback", "count": len(fallback_cleaned)}

    return {"volumeGainers": [], "last_updated": timestamp_str, "status": "Error", "count": 0}

@app.route('/api/volume-gainers')
def api_volume_gainers():
    """JSON API endpoint for Volume Gainers / Spurts from NSE."""
    return jsonify(fetch_volume_gainers())

# ── ALL STOCKS (NSE & BSE) DATABASE ─────────────────────────
ALL_STOCKS_FILE = os.path.join(os.path.dirname(__file__), "data", "all_stocks.json")
_stocks_db = {"total": 0, "totalNse": 0, "totalBse": 0, "dualListed": 0, "stocks": []}

def get_stocks_db():
    global _stocks_db
    if not _stocks_db["stocks"] and os.path.exists(ALL_STOCKS_FILE):
        try:
            with open(ALL_STOCKS_FILE, "r", encoding="utf-8") as f:
                _stocks_db = json.load(f)
                print(f"[StocksDB] Loaded {len(_stocks_db.get('stocks', []))} stocks from {ALL_STOCKS_FILE}")
        except Exception as e:
            print(f"[StocksDB] Error loading {ALL_STOCKS_FILE}: {e}")
    return _stocks_db

@app.route('/api/stocks')
def api_stocks():
    """
    Search and paginate through all NSE & BSE listed stocks.
    Query parameters:
      - query: search term (symbol, company name, BSE code, ISIN)
      - exchange: 'all', 'nse', 'bse', 'both'
      - page: int (1-indexed, default 1)
      - limit: int (per page, default 50, max 200)
    """
    db = get_stocks_db()
    stocks = db.get("stocks", [])

    q = request.args.get('query', '').strip().upper()
    exchange = request.args.get('exchange', 'all').lower()

    try:
        page = max(1, int(request.args.get('page', 1)))
        limit = min(200, max(10, int(request.args.get('limit', 50))))
    except ValueError:
        page, limit = 1, 50

    filtered = stocks

    # Filter by exchange
    if exchange == 'nse':
        filtered = [s for s in filtered if s.get('nse')]
    elif exchange == 'bse':
        filtered = [s for s in filtered if s.get('bse')]
    elif exchange == 'both':
        filtered = [s for s in filtered if s.get('nse') and s.get('bse')]

    # Search filter
    if q:
        filtered = [
            s for s in filtered
            if q in s.get('symbol', '').upper()
            or q in s.get('name', '').upper()
            or q in str(s.get('bseCode', ''))
            or q in s.get('isin', '').upper()
        ]

    total_count = len(filtered)
    start = (page - 1) * limit
    end = start + limit
    paginated = filtered[start:end]

    return jsonify({
        'total': total_count,
        'page': page,
        'limit': limit,
        'totalPages': max(1, (total_count + limit - 1) // limit),
        'stats': {
            'totalNse': db.get('totalNse', 0),
            'totalBse': db.get('totalBse', 0),
            'dualListed': db.get('dualListed', 0),
            'totalCombined': db.get('total', 0)
        },
        'stocks': paginated
    })

@app.route('/api/chart-data/<symbol>')
def api_chart_data(symbol):
    """
    Fetch OHLCV candlestick data for an NSE or BSE stock from Yahoo Finance.
    Yahoo Finance uses:
      - .NS suffix for NSE-listed stocks (default)
      - .BO suffix for BSE-listed stocks
    Supports cross-exchange fallback so any symbol works!
    """
    symbol = symbol.upper().strip()
    interval = request.args.get('interval', '1d')
    range_val = request.args.get('range', '3mo')
    exchange_req = request.args.get('exchange', '').upper().strip()

    valid_intervals = {'1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'}
    valid_ranges    = {'1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'}
    if interval not in valid_intervals:
        interval = '1d'
    if range_val not in valid_ranges:
        range_val = '3mo'

    clean_sym = symbol.replace('.BO', '').replace('.NS', '')

    # Determine primary and fallback Yahoo Finance symbols
    if exchange_req == 'BSE' or symbol.endswith('.BO'):
        primary_sym = f"{clean_sym}.BO"
        fallback_sym = f"{clean_sym}.NS"
        default_exch = 'BSE'
    else:
        primary_sym = f"{clean_sym}.NS"
        fallback_sym = f"{clean_sym}.BO"
        default_exch = 'NSE'

    yf_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }

    def fetch_yf(sym):
        try:
            session = get_session()
            r = session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                headers=yf_headers,
                params={'interval': interval, 'range': range_val},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                res = data.get('chart', {}).get('result')
                if res:
                    return res[0], None
            return None, f"Status {r.status_code}"
        except Exception as err:
            return None, str(err)

    # 1. Try primary symbol
    result, err = fetch_yf(primary_sym)
    resolved_sym = primary_sym
    exch_name = default_exch

    # 2. Try fallback if primary failed or returned insufficient candles
    primary_candles_count = len(result.get('timestamp', [])) if result else 0
    if not result or primary_candles_count <= 2:
        fallback_res, fallback_err = fetch_yf(fallback_sym)
        if fallback_res and len(fallback_res.get('timestamp', [])) > primary_candles_count:
            result = fallback_res
            resolved_sym = fallback_sym
            exch_name = 'NSE' if fallback_sym.endswith('.NS') else 'BSE'

    if not result:
        return jsonify({'error': f'Could not retrieve chart for {clean_sym} ({err})', 'candles': []}), 404

    try:
        ts      = result.get('timestamp', [])
        meta    = result.get('meta', {})
        quote   = result.get('indicators', {}).get('quote', [{}])[0]

        opens   = quote.get('open',   [])
        highs   = quote.get('high',   [])
        lows    = quote.get('low',    [])
        closes  = quote.get('close',  [])
        volumes = quote.get('volume', [])

        candles = []
        for i, t in enumerate(ts):
            try:
                o = opens[i];  h = highs[i];  l = lows[i];  c = closes[i];  v = volumes[i]
                if None in (o, h, l, c):
                    continue
                candles.append({
                    'time':   t,
                    'open':   round(float(o), 2),
                    'high':   round(float(h), 2),
                    'low':    round(float(l), 2),
                    'close':  round(float(c), 2),
                    'volume': int(v) if v else 0,
                })
            except (TypeError, IndexError, ValueError):
                continue

        return jsonify({
            'symbol':    clean_sym,
            'yfSymbol':  resolved_sym,
            'exchange':  exch_name,
            'currency':  meta.get('currency', 'INR'),
            'interval':  interval,
            'range':     range_val,
            'ltp':       round(float(meta.get('regularMarketPrice', 0)), 2),
            'prevClose': round(float(meta.get('previousClose', 0) or 0), 2),
            'candles':   candles,
            'count':     len(candles),
        })

    except Exception as e:
        print(f"[Chart] Parse error for {symbol}: {e}")
        return jsonify({'error': str(e), 'candles': []}), 500

# ── 9:45 AM ORB & PREV DAY HIGH TRIPLE CONFIRMATION SCREENER ──
_screener_cache = {
    "cache": {},
    "ttl": 40
}

@app.route('/api/screener/orb-945')
def api_screener_orb_945():
    """
    Dual-Side (Bullish / Upside & Bearish / Downside) 5-Minute Breakout Scanner
    Window: 09:15 - 09:45 AM IST
    
    BULLISH (All 4 Required):
      1. First Candle Low Protected: low >= c1_low throughout window
      2. First Candle High Breakout: high > c1_high by 09:45
      3. Previous Day High Breakout: high > prev_day_high between 09:15 and 09:45
      4. Previous Day Low Protected: low >= prev_day_low throughout window
      
    BEARISH (All 4 Required):
      1. First Candle High Protected: high <= c1_high throughout window
      2. First Candle Low Breakdown: low < c1_low by 09:45
      3. Previous Day Low Breakdown: low < prev_day_low between 09:15 and 09:45
      4. Previous Day High Protected: high <= prev_day_high throughout window
    """
    index = request.args.get('index', 'NIFTY').upper().strip()
    cache_key = f"dual_{index}"
    now = time.time()
    cached = _screener_cache["cache"].get(cache_key)
    if cached and (now - cached["timestamp"] < _screener_cache["ttl"]):
        return jsonify(cached["data"])

    # Universe Selection
    if index == 'BANKNIFTY':
        symbols = ['HDFCBANK', 'ICICIBANK', 'SBIN', 'AXISBANK', 'KOTAKBANK', 'INDUSINDBK', 'BANKBARODA', 'PNB', 'AUBANK', 'FEDERALBNK', 'IDFCFIRSTB', 'BANDHANBNK']
    elif index == 'NIFTYNEXT50':
        symbols = ['BEL', 'HAL', 'ZOMATO', 'JIOFIN', 'TRENT', 'VEDL', 'IRFC', 'PFC', 'RECLTD', 'CHOLAFIN', 'BANKBARODA', 'SIEMENS', 'DLF', 'IOC', 'GAIL', 'ABB', 'HINDALCO', 'AMBUJACEM']
    else:
        # Default NIFTY 50 top liquid universe
        symbols = [
            'RELIANCE', 'TCS', 'HDFCBANK', 'ICICIBANK', 'INFY', 'BHARTIARTL', 'ITC', 'SBIN', 'LICI', 'HINDUNILVR',
            'LT', 'BAJFINANCE', 'HCLTECH', 'MARUTI', 'SUNPHARMA', 'ADANIENT', 'TATAMOTORS', 'KOTAKBANK', 'NTPC',
            'TITAN', 'ONGC', 'AXISBANK', 'ADANIPORTS', 'POWERGRID', 'COALINDIA', 'TATASTEEL', 'BAJAJFINSV', 'M&M',
            'ASIANPAINT', 'JSWSTEEL', 'ULTRACEMCO', 'NESTLEIND', 'GRASIM', 'TECHM', 'WIPRO', 'HDFCLIFE', 'DIVISLAB',
            'CIPLA', 'SBILIFE', 'DRREDDY', 'EICHERMOT', 'BRITANNIA', 'TATACONSUM', 'APOLLOHOSP', 'HEROMOTOCO',
            'HINDALCO', 'SHRIRAMFIN', 'BPCL', 'INDUSINDBK', 'TRENT'
        ]

    IST = timezone(timedelta(hours=5, minutes=30))
    yf_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }

    def scan_one(sym):
        try:
            session = get_session()
            r = session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS",
                headers=yf_headers,
                params={'interval': '5m', 'range': '5d'},
                timeout=8
            )
            if r.status_code != 200:
                r = session.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.BO",
                    headers=yf_headers,
                    params={'interval': '5m', 'range': '5d'},
                    timeout=8
                )
                if r.status_code != 200:
                    return None

            data = r.json()
            res = data.get('chart', {}).get('result', [{}])[0]
            ts = res.get('timestamp', [])
            meta = res.get('meta', {})
            quote = res.get('indicators', {}).get('quote', [{}])[0]
            opens = quote.get('open', [])
            highs = quote.get('high', [])
            lows = quote.get('low', [])
            closes = quote.get('close', [])
            vols = quote.get('volume', [])

            by_date = {}
            for i, t in enumerate(ts):
                try:
                    o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]; v = vols[i]
                    if None in (o, h, l, c): continue
                    dt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST)
                    d_key = dt.strftime('%Y-%m-%d')
                    if d_key not in by_date: by_date[d_key] = []
                    by_date[d_key].append({
                        'time': t, 'dt': dt, 'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c), 'volume': int(v or 0)
                    })
                except (IndexError, TypeError, ValueError):
                    continue

            dates = sorted(by_date.keys())
            if len(dates) < 2: return None

            today_date = dates[-1]
            prev_date = dates[-2]
            prev_candles = by_date[prev_date]
            today_candles = by_date[today_date]

            prev_day_high = round(max(c['high'] for c in prev_candles), 2)
            prev_day_low  = round(min(c['low'] for c in prev_candles), 2)
            prev_day_close = prev_candles[-1]['close']

            # Candles from 09:15 to 09:45 IST
            window = [c for c in today_candles if c['dt'].hour == 9 and c['dt'].minute <= 45]
            if not window: return None

            c1 = window[0] # 09:15 candle (first 5m)
            c1_high = round(c1['high'], 2)
            c1_low  = round(c1['low'], 2)

            # Common stock stats
            ltp = round(float(meta.get('regularMarketPrice', today_candles[-1]['close'])), 2)
            prevClose = round(float(meta.get('previousClose', prev_day_close)), 2)
            chg = round(ltp - prevClose, 2)
            pChg = round((chg / prevClose) * 100, 2) if prevClose else 0.0

            today_vol = sum(c['volume'] for c in today_candles) or int(meta.get('regularMarketVolume', 0))
            past_vols = [sum(c['volume'] for c in by_date[d]) for d in dates[:-1] if sum(c['volume'] for c in by_date[d]) > 0]
            avg_vol = (sum(past_vols) / len(past_vols)) if past_vols else today_vol
            vol_vs_avg = round(today_vol / avg_vol, 2) if avg_vol > 0 else 1.0

            day_high = round(max(c['high'] for c in today_candles), 2)
            day_low  = round(min(c['low'] for c in today_candles), 2)

            # ── 1. BULLISH EVALUATION (All 4 conditions) ──
            bull_c1_low_held = all(c['low'] >= c1_low for c in window)
            bull_c1_high_bo  = any(c['high'] > c1_high for c in window[1:]) if len(window) > 1 else False
            bull_c1_bo_candle = next((c for c in window[1:] if c['high'] > c1_high), None) if len(window) > 1 else None
            bull_c1_bo_time   = bull_c1_bo_candle['dt'].strftime('%H:%M') if bull_c1_bo_candle else None

            bull_pdh_bo      = any(c['high'] > prev_day_high for c in window)
            bull_pdh_bo_candle = next((c for c in window if c['high'] > prev_day_high), None)
            bull_pdh_bo_time   = bull_pdh_bo_candle['dt'].strftime('%H:%M') if bull_pdh_bo_candle else None

            bull_pdl_held    = all(c['low'] >= prev_day_low for c in window)
            bull_score       = (1 if bull_c1_low_held else 0) + (1 if bull_c1_high_bo else 0) + (1 if bull_pdh_bo else 0) + (1 if bull_pdl_held else 0)
            is_bull_match    = (bull_score == 4)

            bull_bo_time = None
            if bull_c1_bo_time and bull_pdh_bo_time:
                bull_bo_time = max(bull_c1_bo_time, bull_pdh_bo_time) + ' IST'
            elif bull_c1_bo_time:
                bull_bo_time = bull_c1_bo_time + ' IST'
            elif bull_pdh_bo_time:
                bull_bo_time = bull_pdh_bo_time + ' IST'

            # ── 2. BEARISH EVALUATION (All 4 conditions) ──
            bear_c1_high_held = all(c['high'] <= c1_high for c in window)
            bear_c1_low_bd    = any(c['low'] < c1_low for c in window[1:]) if len(window) > 1 else False
            bear_c1_bd_candle = next((c for c in window[1:] if c['low'] < c1_low), None) if len(window) > 1 else None
            bear_c1_bd_time   = bear_c1_bd_candle['dt'].strftime('%H:%M') if bear_c1_bd_candle else None

            bear_pdl_bd       = any(c['low'] < prev_day_low for c in window)
            bear_pdl_bd_candle = next((c for c in window if c['low'] < prev_day_low), None)
            bear_pdl_bd_time   = bear_pdl_bd_candle['dt'].strftime('%H:%M') if bear_pdl_bd_candle else None

            bear_pdh_held     = all(c['high'] <= prev_day_high for c in window)
            bear_score       = (1 if bear_c1_high_held else 0) + (1 if bear_c1_low_bd else 0) + (1 if bear_pdl_bd else 0) + (1 if bear_pdh_held else 0)
            is_bear_match    = (bear_score == 4)

            bear_bd_time = None
            if bear_c1_bd_time and bear_pdl_bd_time:
                bear_bd_time = max(bear_c1_bd_time, bear_pdl_bd_time) + ' IST'
            elif bear_c1_bd_time:
                bear_bd_time = bear_c1_bd_time + ' IST'
            elif bear_pdl_bd_time:
                bear_bd_time = bear_pdl_bd_time + ' IST'

            # Base info
            base_info = {
                'symbol': sym,
                'ltp': ltp,
                'prevClose': prevClose,
                'change': chg,
                'pChange': pChg,
                'volume': today_vol,
                'vol_vs_avg': vol_vs_avg,
                'c1_high': c1_high,
                'c1_low': c1_low,
                'pdh': prev_day_high,
                'pdl': prev_day_low,
                'day_high': day_high,
                'day_low': day_low,
                'today_date': today_date,
                'prev_date': prev_date,
            }

            bull_record = {
                **base_info,
                'breakout_time': bull_bo_time or '--',
                'c1_low_held': bull_c1_low_held,
                'c1_high_bo': bull_c1_high_bo,
                'pdh_bo': bull_pdh_bo,
                'pdl_held': bull_pdl_held,
                'cond1_status': 'PASS' if bull_c1_low_held else 'FAIL',
                'cond2_status': 'PASS' if bull_c1_high_bo else 'FAIL',
                'cond3_status': 'PASS' if bull_pdh_bo else 'FAIL',
                'cond4_status': 'PASS' if bull_pdl_held else 'FAIL',
                'score': bull_score,
                'is_full_match': is_bull_match,
                'final_status': 'SELECTED' if is_bull_match else 'REJECTED'
            }

            bear_record = {
                **base_info,
                'breakdown_time': bear_bd_time or '--',
                'c1_high_held': bear_c1_high_held,
                'c1_low_bd': bear_c1_low_bd,
                'pdl_bd': bear_pdl_bd,
                'pdh_held': bear_pdh_held,
                'cond1_status': 'PASS' if bear_c1_high_held else 'FAIL',
                'cond2_status': 'PASS' if bear_c1_low_bd else 'FAIL',
                'cond3_status': 'PASS' if bear_pdl_bd else 'FAIL',
                'cond4_status': 'PASS' if bear_pdh_held else 'FAIL',
                'score': bear_score,
                'is_full_match': is_bear_match,
                'final_status': 'SELECTED' if is_bear_match else 'REJECTED'
            }

            return bull_record, bear_record
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=14) as executor:
        pairs = [p for p in executor.map(scan_one, symbols) if p]

    bull_list = [p[0] for p in pairs]
    bear_list = [p[1] for p in pairs]

    # Sort Bullish by full match first, then score, then % change descending
    bull_list.sort(key=lambda x: (x['is_full_match'], x['score'], x['pChange']), reverse=True)
    # Sort Bearish by full match first, then score, then % change ascending (biggest drops first)
    bear_list.sort(key=lambda x: (x['is_full_match'], x['score'], -x['pChange']), reverse=True)

    summary = {
        'total_scanned': len(pairs),
        'bullish_selected_count': sum(1 for b in bull_list if b['is_full_match']),
        'bearish_selected_count': sum(1 for b in bear_list if b['is_full_match']),
        'bullish_score_3_count': sum(1 for b in bull_list if b['score'] == 3),
        'bearish_score_3_count': sum(1 for b in bear_list if b['score'] == 3),
        'index': index,
        'last_scanned': datetime.now(IST).strftime('%H:%M:%S IST'),
        'bullish_results': bull_list,
        'bearish_results': bear_list
    }

    _screener_cache["cache"][cache_key] = {
        "timestamp": now,
        "data": summary
    }

    return jsonify(summary)


# ── ADVANCED SCANNER ROUTES (MARKET TREND + 2600 STOCKS + DUAL BREAKOUTS) ──
from advanced_scanner import scanner as adv_scanner

@app.route('/api/advanced-scanner/status')
def api_adv_scanner_status():
    return jsonify(adv_scanner.get_status())

@app.route('/api/advanced-scanner/market-trend')
def api_adv_scanner_market_trend():
    trend = adv_scanner.update_market_trend()
    return jsonify(trend)

@app.route('/api/advanced-scanner/bullish')
def api_adv_scanner_bullish():
    filter_mode = request.args.get('filter', 'selected')
    selected_only = (filter_mode == 'selected')
    results = adv_scanner.get_bullish_breakouts(selected_only=selected_only)
    return jsonify({'count': len(results), 'results': results})

@app.route('/api/advanced-scanner/bearish')
def api_adv_scanner_bearish():
    filter_mode = request.args.get('filter', 'selected')
    selected_only = (filter_mode == 'selected')
    results = adv_scanner.get_bearish_breakdowns(selected_only=selected_only)
    return jsonify({'count': len(results), 'results': results})

@app.route('/api/advanced-scanner/trend-leaders')
def api_adv_scanner_trend_leaders():
    return jsonify(adv_scanner.get_trend_leaders())

@app.route('/api/advanced-scanner/control', methods=['POST'])
def api_adv_scanner_control():
    data = request.get_json(silent=True) or {}
    action = data.get('action') or request.form.get('action') or request.args.get('action', 'start')
    
    if action == 'start':
        adv_scanner.start_background_scan(batch_size=25)
        msg = f"Started background scan on {len(adv_scanner.symbols)} symbols."
    elif action == 'scan_top_100':
        scanned = adv_scanner.scan_priority_batch(100)
        msg = f"Scanned top {scanned} priority liquid symbols."
    elif action == 'pause':
        adv_scanner.pause_scan()
        msg = "Scan paused."
    elif action == 'resume':
        adv_scanner.resume_scan()
        msg = "Scan resumed."
    elif action == 'reset':
        adv_scanner.reset()
        msg = "Scanner reset."
    elif action == 'refresh_trend':
        adv_scanner.update_market_trend()
        msg = "Market trend refreshed."
    else:
        msg = f"Unknown action {action}"

    status = adv_scanner.get_status()
    status['message'] = msg
    return jsonify(status)

@app.route('/api/advanced-scanner/upload-symbols', methods=['POST'])
def api_adv_scanner_upload_symbols():
    content = ""
    if 'file' in request.files:
        f = request.files['file']
        content = f.read().decode('utf-8', errors='ignore')
    elif request.is_json:
        data = request.get_json(silent=True) or {}
        content = data.get('symbols', '')
    else:
        content = request.form.get('symbols', '')

    count = adv_scanner.import_symbols_from_text_or_csv(content)
    return jsonify({
        'success': count > 0,
        'count': count,
        'message': f"Successfully imported {count} unique symbols into watchlist." if count > 0 else "No valid symbols found."
    })


if __name__ == '__main__':
    print(f"Starting NSE Flask App (curl_cffi enabled: {USE_CURL_CFFI})...")
    app.run(debug=True, host='127.0.0.1', port=5000)

