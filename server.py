"""Tickerboard server.

Serves index.html and fetches market data from Yahoo Finance's public endpoints
(no API key). Works locally and on hosts like Render.

Run locally:  python server.py   then open http://localhost:8765
Deploy:       see README.md

Yahoo rate-limits ("HTTP 429") requests that don't look like a real browser, and
cloud servers get hit hardest. So this server:
  * uses curl_cffi (in requirements.txt) to make requests look like Chrome,
    falling back to plain urllib if it isn't installed,
  * batches watchlist quotes into 2 requests instead of one per symbol,
  * caches everything and serves the last good copy if Yahoo refuses,
  * limits how many requests go to Yahoo at once and retries 429s with backoff.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    from curl_cffi import requests as curl_requests  # browser-like TLS fingerprint
except Exception:  # pragma: no cover
    curl_requests = None

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
Q1 = "https://query1.finance.yahoo.com"
Q2 = "https://query2.finance.yahoo.com"

MARKET = ["^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "^TNX", "CL=F", "GC=F", "BTC-USD", "EURUSD=X"]

# Large US companies by sector for the market heatmap (Finviz-style)
HEATMAP = {
    "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "ACN", "IBM", "QCOM", "TXN", "INTU", "NOW", "MU", "AMAT", "PLTR"],
    "Communication": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CMCSA"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "NKE", "SBUX"],
    "Financial": ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "C", "BLK", "SCHW"],
    "Healthcare": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "PFE", "DHR"],
    "Consumer Defensive": ["WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ"],
    "Industrials": ["GE", "CAT", "RTX", "HON", "UBER", "BA", "UNP", "DE", "LMT"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    "Utilities": ["NEE", "SO", "DUK"],
    "Real Estate": ["PLD", "AMT", "EQIX"],
    "Basic Materials": ["LIN", "SHW", "FCX"],
}
SCREENERS = {"day_gainers", "day_losers", "most_actives", "undervalued_growth_stocks",
             "growth_technology_stocks", "small_cap_gainers", "aggressive_small_caps"}

SUMMARY_SETS = {
    "main": "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile,"
            "recommendationTrend,calendarEvents,upgradeDowngradeHistory,earningsTrend",
    "earnings": "earningsHistory,earningsTrend,earnings,calendarEvents",
    "ownership": "majorHoldersBreakdown,institutionOwnership,fundOwnership,"
                 "insiderTransactions,netSharePurchaseActivity,insiderHolders",
}


class UpstreamError(Exception):
    def __init__(self, status, msg=""):
        super().__init__(msg or f"HTTP {status}")
        self.status = status


class Http:
    """One shared browser-like session with throttling and 429 retries."""

    def __init__(self):
        self.gate = threading.BoundedSemaphore(4)
        self.pace_lock = threading.Lock()
        self.last_call = 0.0
        if curl_requests:
            self.session = curl_requests.Session(impersonate="chrome")
            self.mode = "curl_cffi"
        else:
            jar = http.cookiejar.CookieJar()
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            self.opener.addheaders = [("User-Agent", UA), ("Accept", "application/json,text/plain,*/*"),
                                      ("Accept-Language", "en-US,en;q=0.9")]
            self.mode = "urllib"

    def _pace(self):
        with self.pace_lock:  # keep at least 120 ms between calls to Yahoo
            wait = 0.12 - (time.time() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.time()

    def _once(self, url, params, timeout):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if self.mode == "curl_cffi":
            r = self.session.get(url, timeout=timeout)
            return r.status_code, r.text
        try:
            with self.opener.open(url, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def get(self, url, params=None, timeout=12, raw=False):
        delays = [0, 1.5, 4]
        status = 0
        for d in delays:
            if d:
                time.sleep(d)
            with self.gate:
                self._pace()
                try:
                    status, text = self._once(url, params, timeout)
                except Exception as e:
                    status, text = 0, str(e)
            if status == 200:
                return text if raw else json.loads(text)
            if status not in (429, 0, 500, 502, 503, 504):
                break
        raise UpstreamError(status)


class Yahoo:
    def __init__(self):
        self.http = Http()
        self.crumb = None
        self.crumb_lock = threading.Lock()
        self.cache = {}

    # ---- crumb (needed by quote, quoteSummary and options) ----
    def _ensure_crumb(self, force=False):
        with self.crumb_lock:
            if self.crumb and not force:
                return self.crumb
            for warm in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
                try:
                    self.http.get(warm, raw=True, timeout=8)
                except Exception:
                    pass  # these set cookies even when they return 404
                for host in (Q1, Q2):
                    try:
                        c = self.http.get(f"{host}/v1/test/getcrumb", raw=True, timeout=8).strip()
                        if c and "<" not in c and len(c) < 40:
                            self.crumb = c
                            return c
                    except UpstreamError:
                        continue
            raise UpstreamError(429, "Yahoo refused a session token")

    def _crumbed(self, url, params=None):
        params = dict(params or {})
        for attempt in range(2):
            params["crumb"] = self._ensure_crumb(force=attempt > 0)
            try:
                return self.http.get(url, params)
            except UpstreamError as e:
                if e.status in (401, 403) and attempt == 0:
                    continue
                raise

    # ---- cache: fresh for ttl, otherwise refetch; serve stale copy if Yahoo refuses ----
    def cached(self, key, ttl, fn):
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:
            if hit and time.time() - hit[0] < 24 * 3600:
                return hit[1]
            raise
        if len(self.cache) > 3000:
            cutoff = sorted(t for t, _ in self.cache.values())[len(self.cache) // 2]
            self.cache = {k: v for k, v in self.cache.items() if v[0] > cutoff}
        self.cache[key] = (time.time(), val)
        return val

    # ---- endpoints ----
    def chart(self, symbol, rng="5y", interval="1d"):
        params = {"range": rng, "interval": interval, "includePrePost": "false", "events": "div,splits"}
        url = f"{Q1}/v8/finance/chart/{urllib.parse.quote(symbol)}"
        ttl = 60 if interval in ("1m", "2m", "5m", "15m") else 300
        return self.cached(("chart", symbol, rng, interval), ttl, lambda: self.http.get(url, params))

    def summary(self, symbol, kind="main"):
        modules = SUMMARY_SETS.get(kind, SUMMARY_SETS["main"])
        url = f"{Q2}/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
        ttl = 300 if kind == "main" else 3600
        return self.cached(("summary", symbol, kind), ttl, lambda: self._crumbed(url, {"modules": modules}))

    def search(self, query):
        params = {"q": query, "quotesCount": 8, "newsCount": 0}
        return self.cached(("search", query.lower()), 900, lambda: self.http.get(f"{Q1}/v1/finance/search", params))

    def news(self, symbol):
        params = {"q": symbol, "quotesCount": 0, "newsCount": 25}
        return self.cached(("news", symbol), 600,
                           lambda: self.http.get(f"{Q1}/v1/finance/search", params).get("news", []))

    FIN_TYPES = {
        "income": ["TotalRevenue", "CostOfRevenue", "GrossProfit", "ResearchAndDevelopment",
                   "SellingGeneralAndAdministration", "OperatingExpense", "OperatingIncome",
                   "InterestExpense", "PretaxIncome", "TaxProvision", "NetIncome", "EBITDA",
                   "DilutedEPS", "DilutedAverageShares"],
        "balance": ["TotalAssets", "CurrentAssets", "CashCashEquivalentsAndShortTermInvestments",
                    "AccountsReceivable", "Inventory", "TotalLiabilitiesNetMinorityInterest",
                    "CurrentLiabilities", "TotalDebt", "LongTermDebt", "StockholdersEquity",
                    "RetainedEarnings"],
        "cashflow": ["OperatingCashFlow", "CapitalExpenditure", "FreeCashFlow", "InvestingCashFlow",
                     "FinancingCashFlow", "RepurchaseOfCapitalStock", "CashDividendsPaid",
                     "StockBasedCompensation"],
    }

    def financials(self, symbol, freq="annual"):
        freq = "quarterly" if freq == "quarterly" else "annual"
        names = [n for group in self.FIN_TYPES.values() for n in group]
        url = f"{Q2}/ws/fundamentals-timeseries/v1/finance/timeseries/{urllib.parse.quote(symbol)}"
        params = {"type": ",".join(freq + n for n in names), "period1": 1451606400, "period2": int(time.time())}

        def fetch():
            d = self.http.get(url, params)
            out = {}
            for r in d.get("timeseries", {}).get("result", []) or []:
                t = r["meta"]["type"][0]
                rows = [x for x in (r.get(t) or []) if x and x.get("reportedValue")]
                if rows:
                    out[t[len(freq):]] = [{"date": x["asOfDate"], "value": x["reportedValue"]["raw"]} for x in rows]
            return {"freq": freq, "groups": self.FIN_TYPES, "data": out}
        return self.cached(("fin", symbol, freq), 6 * 3600, fetch)

    def options(self, symbol, date=None):
        url = f"{Q2}/v7/finance/options/{urllib.parse.quote(symbol)}"
        params = {"date": int(date)} if date else {}
        return self.cached(("opt", symbol, str(date)), 600, lambda: self._crumbed(url, params))

    def iv_term(self, symbol):
        def atm(chain, spot):
            calls, puts = chain.get("calls", []), chain.get("puts", [])
            common = {c["strike"] for c in calls} & {p["strike"] for p in puts}
            strikes = sorted(common, key=lambda k: abs(k - spot))[:2]
            ivs = [o["impliedVolatility"] for o in calls + puts
                   if o["strike"] in strikes and o.get("impliedVolatility", 0) > 0.01
                   and ((o.get("bid") or 0) > 0 or (o.get("ask") or 0) > 0)]
            return sum(ivs) / len(ivs) if ivs else None

        def fetch():
            res = self.options(symbol)["optionChain"]["result"][0]
            spot = res["quote"].get("regularMarketPrice")
            all_dates = res.get("expirationDates", [])
            # 8 expirations spread across the curve keeps Yahoo requests low
            dates = all_dates[:5] + all_dates[5::max(1, (len(all_dates) - 5) // 3 or 1)][:3]

            def one(ts):
                try:
                    ch = self.options(symbol, ts)["optionChain"]["result"][0]["options"][0]
                    calls, puts = ch.get("calls", []), ch.get("puts", [])
                    return {"exp": ts, "days": max((ts - time.time()) / 86400, 0.5), "iv": atm(ch, spot),
                            "callOI": sum(c.get("openInterest") or 0 for c in calls),
                            "putOI": sum(p.get("openInterest") or 0 for p in puts),
                            "callVol": sum(c.get("volume") or 0 for c in calls),
                            "putVol": sum(p.get("volume") or 0 for p in puts)}
                except Exception:
                    return None
            with ThreadPoolExecutor(max_workers=3) as ex:
                rows = [r for r in ex.map(one, sorted(set(dates))) if r]
            return {"spot": spot, "term": rows}
        return self.cached(("ivterm", symbol), 900, fetch)

    def batch_quote(self, symbols):
        """Full quote fields for up to ~100 symbols in one request."""
        def fetch():
            out = []
            for i in range(0, len(symbols), 50):
                q = self._crumbed(f"{Q1}/v7/finance/quote", {"symbols": ",".join(symbols[i:i + 50])})
                out += q.get("quoteResponse", {}).get("result", [])
            return out
        return self.cached(("bq", tuple(symbols)), 60, fetch)

    def heatmap(self):
        order = [s for group in HEATMAP.values() for s in group]
        rows = {r["symbol"]: r for r in self.batch_quote(order)}
        return [{"sector": sec, "stocks": [
                    {"symbol": s, "name": rows[s].get("shortName"), "cap": rows[s].get("marketCap"),
                     "change": rows[s].get("regularMarketChangePercent"), "price": rows[s].get("regularMarketPrice")}
                    for s in syms if s in rows]} for sec, syms in HEATMAP.items()]

    def screener(self, scr_id, count=25):
        if scr_id not in SCREENERS:
            raise UpstreamError(404)
        url = f"{Q1}/v1/finance/screener/predefined/saved"

        def fetch():
            d = self._crumbed(url, {"scrIds": scr_id, "count": count})
            quotes = d["finance"]["result"][0]["quotes"]
            keep = ["symbol", "shortName", "regularMarketPrice", "regularMarketChangePercent", "regularMarketVolume",
                    "averageDailyVolume3Month", "marketCap", "trailingPE", "fiftyTwoWeekChangePercent"]
            return [{k: q.get(k) for k in keep} for q in quotes]
        return self.cached(("scr", scr_id, count), 120, fetch)

    def quotes(self, symbols):
        """Prices for many symbols in 2 requests: batch quote + batch sparkline."""
        key = tuple(sorted(symbols))

        def fetch():
            out = {s: {"symbol": s} for s in symbols}
            try:
                q = self._crumbed(f"{Q1}/v7/finance/quote", {"symbols": ",".join(symbols)})
                for r in q.get("quoteResponse", {}).get("result", []):
                    s = r.get("symbol")
                    if s in out:
                        out[s].update(name=r.get("shortName") or r.get("longName") or s,
                                      price=r.get("regularMarketPrice"),
                                      prev=r.get("regularMarketPreviousClose"),
                                      changePct=r.get("regularMarketChangePercent"),
                                      currency=r.get("currency"))
            except Exception:
                pass
            for i in range(0, len(symbols), 20):  # spark takes up to 20 symbols per call
                chunk = symbols[i:i + 20]
                try:
                    sp = self.http.get(f"{Q1}/v8/finance/spark",
                                       {"symbols": ",".join(chunk), "range": "1mo", "interval": "1d"})
                except Exception:
                    continue
                for s, d in sp.items():
                    if s in out and isinstance(d, dict):
                        closes = [c for c in (d.get("close") or []) if c is not None]
                        out[s]["spark"] = closes[-22:]
                        if out[s].get("price") is None and closes:
                            out[s]["price"] = closes[-1]
                            out[s]["prev"] = closes[-2] if len(closes) > 1 else None
            return [out[s] for s in symbols]
        return self.cached(("quotes", key), 45, fetch)


YF = Yahoo()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            # only the page itself is public; never serve server code or other files
            if u.path in ("/", "/index.html"):
                self.path = "/index.html"
                return super().do_GET()
            return self._json({"error": "not found"}, 404)
        p = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        sym = lambda: p["symbol"].upper().strip()[:20]
        try:
            if u.path == "/api/chart":
                return self._json(YF.chart(sym(), p.get("range", "5y"), p.get("interval", "1d")))
            if u.path == "/api/summary":
                return self._json(YF.summary(sym(), p.get("set", "main")))
            if u.path == "/api/search":
                return self._json(YF.search(p.get("q", "")[:40]))
            if u.path == "/api/financials":
                return self._json(YF.financials(sym(), p.get("freq", "annual")))
            if u.path == "/api/options":
                return self._json(YF.options(sym(), p.get("date")))
            if u.path == "/api/ivterm":
                return self._json(YF.iv_term(sym()))
            if u.path == "/api/news":
                return self._json(YF.news(sym()))
            if u.path == "/api/quotes":
                syms = [s.strip().upper() for s in p.get("symbols", "").split(",") if s.strip()][:40]
                if p.get("market"):
                    syms = MARKET + [s for s in syms if s not in MARKET]
                return self._json(YF.quotes(syms))
            if u.path == "/api/heatmap":
                return self._json(YF.heatmap())
            if u.path == "/api/screener":
                return self._json(YF.screener(p.get("id", "day_gainers"), min(int(p.get("count", 25)), 50)))
            if u.path == "/api/quote":
                syms = [s.strip().upper() for s in p.get("symbols", "").split(",") if s.strip()][:10]
                return self._json(YF.batch_quote(syms))
            if u.path == "/api/health":
                return self._json({"ok": True, "client": YF.http.mode})
            return self._json({"error": "unknown route"}, 404)
        except KeyError as e:
            return self._json({"error": f"Missing parameter {e}"}, 400)
        except UpstreamError as e:
            if e.status == 404:
                return self._json({"error": "Symbol not found"}, 404)
            if e.status == 429:
                return self._json({"error": "Yahoo Finance is busy right now. Try again in a minute"}, 503)
            return self._json({"error": f"Yahoo Finance returned an error ({e})"}, 502)
        except Exception as e:
            return self._json({"error": f"Could not reach the data source: {e}"}, 502)


if __name__ == "__main__":
    # Cloud hosts (Render, Railway, Fly...) set PORT and need 0.0.0.0; locally stay on 127.0.0.1
    host = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    shown = "localhost" if host == "127.0.0.1" else host
    print(f"Tickerboard running at http://{shown}:{PORT}  (requests via {YF.http.mode}; Ctrl+C to stop)")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
