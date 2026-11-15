# Tickerboard

A stock tracker website: live prices, candlestick charts with indicators (SMA, EMA, Bollinger, RSI, MACD),
a Finviz-style snapshot grid, a rule-based analysis scorecard, analyst rating changes, financial statements,
earnings and dividends, options and implied volatility, ownership and insider trading, news, and a Markets tab
with a large-cap heatmap and gainers/losers lists.
Data comes from Yahoo Finance's public endpoints. No API key is needed. The only package is `curl_cffi`
(in `requirements.txt`), which keeps Yahoo from rate-limiting the server.

## Files

| File | What it does |
| --- | --- |
| `index.html` | The whole website (one page, four tabs) |
| `server.py` | Small Python server: serves the page and fetches market data |
| `render.yaml`, `Procfile`, `runtime.txt`, `requirements.txt` | Deploy settings for hosting services |

## Run it on your computer

```
pip install -r requirements.txt
python server.py
```

Then open http://localhost:8765.

## Put it online (free, about 10 minutes)

The site needs a host that runs Python, because the browser can't call Yahoo Finance directly.
[Render](https://render.com) has a free tier and reads the included `render.yaml`.

1. Create a free GitHub account if you don't have one, then create a new repository (for example `tickerboard`).
2. On the repository page, choose **Add file → Upload files** and upload every file in this folder
   (`index.html`, `server.py`, `render.yaml`, `Procfile`, `runtime.txt`, `requirements.txt`, `README.md`). Commit.
3. Sign in to Render with your GitHub account, choose **New → Blueprint**, and pick the repository.
   Render finds `render.yaml` and creates a web service named `tickerboard`.
4. Wait for the first deploy to finish. Your site is live at `https://tickerboard-XXXX.onrender.com`.

Other hosts that run `python server.py` work too (Railway, Fly.io, a VPS). The server listens on the `PORT`
environment variable the host provides.

### Good to know

- **Free Render services sleep** after 15 minutes without visitors. The first visit after that takes about 30 to 60 seconds.
- **Yahoo sometimes rate-limits cloud servers.** The server uses `curl_cffi` to look like a normal browser, retries
  with backoff, caches results, and shows the last good data if Yahoo refuses. If you still see "Yahoo Finance is
  busy", wait a minute; if it persists, use Manual Deploy in Render so the service gets a new IP address.
  Visiting `/api/health` should show `"client": "curl_cffi"`; if it says `urllib`, the build command didn't run
  `pip install -r requirements.txt`.
- Only `index.html` and `/api/...` routes are served publicly. The server code itself is never sent to visitors.
- Data can be delayed up to 15 minutes. The scorecard and news mood tags are automatic, rule-based summaries,
  not financial advice.
