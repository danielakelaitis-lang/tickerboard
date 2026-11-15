"""Tickerboard local server.

Serves index.html and proxies Yahoo Finance's public endpoints so the page
can get live prices, history, fundamentals and search with no API key.

Run locally:  python server.py   then open http://localhost:8765
Deploy:       see README.md (Render, Railway or any host that runs Python 3)
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

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class Yahoo:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", UA), ("Accept", "application/json,*/*")]
        self.crumb = None
        self.lock = threading.Lock()
        self.cache = {}

    def _get(self, url, timeout=12):
        with self.opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _ensure_crumb(self, force=False):
        with self.lock:
            if self.crumb and not force:
                return self.crumb
            try:
                self.opener.open("https://fc.yahoo.com", timeout=8)
            except Exception:
                pass  # this sets cookies even when it 404s
            with self.opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8) as r:
                self.crumb = r.read().decode().strip()
            return self.crumb

    def cached(self, key, ttl, fn):
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        if len(self.cache) > 3000:  # keep memory bounded on a public server
            cutoff = sorted(t for t, _ in self.cache.values())[len(self.cache) // 2]
            self.cache = {k: v for k, v in self.cache.items() if v[0] > cutoff}
        self.cache[key] = (time.time(), val)
        return val

    def _crumbed(self, url_no_crumb):
        sep = "&" if "?" in url_no_crumb else "?"
        for attempt in range(2):
            crumb = self._ensure_crumb(force=attempt > 0)
            try:
                return self._get(f"{url_no_crumb}{sep}crumb={urllib.parse.quote(crumb)}")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    continue
                raise

    def chart(self, symbol, rng="5y", interval="1d"):
        q = urllib.parse.urlencode({"range": rng, "interval": interval,
                                    "includePrePost": "false", "events": "div,splits"})
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
        return self.cached(("chart", symbol, rng, interval), 60, lambda: self._get(url))

    def summary(self, symbol):
        modules = ("price,summaryDetail,defaultKeyStatistics,financialData,"
                   "assetProfile,recommendationTrend,calendarEvents")

        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
               f"{urllib.parse.quote(symbol)}?modules={modules}")
        return self.cached(("summary", symbol), 300, lambda: self._crumbed(url))

    def search(self, query):
        q = urllib.parse.urlencode({"q": query, "quotesCount": 8, "newsCount": 0})
        url = f"https://query1.finance.yahoo.com/v1/finance/search?{q}"
        return self.cached(("search", query.lower()), 600, lambda: self._get(url))

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
        types = ",".join(freq + n for n in names)
        url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
               f"{urllib.parse.quote(symbol)}?type={types}&period1=1451606400&period2={int(time.time())}")

        def fetch():
            d = self._get(url)
            out = {}
            for r in d.get("timeseries", {}).get("result", []) or []:
                t = r["meta"]["type"][0]
                rows = [x for x in (r.get(t) or []) if x and x.get("reportedValue")]
                if rows:
                    out[t[len(freq):]] = [{"date": x["asOfDate"], "value": x["reportedValue"]["raw"]} for x in rows]
            return {"freq": freq, "groups": self.FIN_TYPES, "data": out}
        return self.cached(("fin", symbol, freq), 3600, fetch)

    def options(self, symbol, date=None):
        url = f"https://query2.finance.yahoo.com/v7/finance/options/{urllib.parse.quote(symbol)}"
        if date:
            url += f"?date={int(date)}"
        return self.cached(("opt", symbol, str(date)), 300, lambda: self._crumbed(url))

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
            first = self.options(symbol)
            res = first["optionChain"]["result"][0]
            spot = res["quote"].get("regularMarketPrice")
            dates = res.get("expirationDates", [])[:14]

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
            with ThreadPoolExecutor(max_workers=6) as ex:
                rows = [r for r in ex.map(one, dates) if r]
            return {"spot": spot, "term": rows}
        return self.cached(("ivterm", symbol), 300, fetch)

    def news(self, symbol):
        q = urllib.parse.urlencode({"q": symbol, "quotesCount": 0, "newsCount": 25})
        url = f"https://query1.finance.yahoo.com/v1/finance/search?{q}"
        return self.cached(("news", symbol), 300, lambda: self._get(url).get("news", []))

    def quotes(self, symbols):
        def one(sym):
            try:
                d = self.chart(sym, "1mo", "1d")
                res = d["chart"]["result"][0]
                m = res["meta"]
                closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
                price = m.get("regularMarketPrice")
                prev = closes[-2] if len(closes) >= 2 else m.get("chartPreviousClose")
                return {"symbol": sym, "name": m.get("shortName") or m.get("longName") or sym,
                        "price": price, "prev": prev, "currency": m.get("currency"),
                        "spark": closes[-22:]}
            except Exception as e:
                return {"symbol": sym, "error": str(e)}
        with ThreadPoolExecutor(max_workers=8) as ex:
            return list(ex.map(one, symbols))


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
        try:
            if u.path == "/api/chart":
                return self._json(YF.chart(p["symbol"].upper(), p.get("range", "5y"), p.get("interval", "1d")))
            if u.path == "/api/summary":
                return self._json(YF.summary(p["symbol"].upper()))
            if u.path == "/api/search":
                return self._json(YF.search(p.get("q", "")))
            if u.path == "/api/financials":
                return self._json(YF.financials(p["symbol"].upper(), p.get("freq", "annual")))
            if u.path == "/api/options":
                return self._json(YF.options(p["symbol"].upper(), p.get("date")))
            if u.path == "/api/ivterm":
                return self._json(YF.iv_term(p["symbol"].upper()))
            if u.path == "/api/news":
                return self._json(YF.news(p["symbol"].upper()))
            if u.path == "/api/health":
                return self._json({"ok": True})
            if u.path == "/api/quotes":
                syms = [s.strip().upper() for s in p.get("symbols", "").split(",") if s.strip()][:40]
                return self._json(YF.quotes(syms))
            return self._json({"error": "unknown route"}, 404)
        except KeyError as e:
            return self._json({"error": f"Missing parameter {e}"}, 400)
        except urllib.error.HTTPError as e:
            msg = "Symbol not found" if e.code == 404 else f"Yahoo returned HTTP {e.code}"
            return self._json({"error": msg}, 502 if e.code != 404 else 404)
        except Exception as e:
            return self._json({"error": f"Could not reach the data source: {e}"}, 502)


if __name__ == "__main__":
    # Cloud hosts (Render, Railway, Fly...) set PORT and need 0.0.0.0; locally stay on 127.0.0.1
    host = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    srv = ThreadingHTTPServer((host, PORT), Handler)
    shown = "localhost" if host == "127.0.0.1" else host
    print(f"Tickerboard running at http://{shown}:{PORT}  (Ctrl+C to stop)")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
