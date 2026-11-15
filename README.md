OPEN IN https://tickerboard.onrender.com

# Tickerboard

A stock tracker website: live prices, candlestick charts with indicators (SMA, EMA, Bollinger, RSI, MACD),
key stats, a rule-based analysis scorecard, financial statements, options and implied volatility, and news.
Data comes from Yahoo Finance's public endpoints. No API key and no third-party packages are needed.

## Files

| File | What it does |
| --- | --- |
| `index.html` | The whole website (one page, four tabs) |
| `server.py` | Small Python server: serves the page and fetches market data |
| `render.yaml`, `Procfile`, `runtime.txt`, `requirements.txt` | Deploy settings for hosting services |

## Run it on your computer

```
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
- **Yahoo sometimes rate-limits cloud servers.** If a deployed site shows "Yahoo returned HTTP 429", wait a few
  minutes or redeploy so the service gets a new IP address. The server caches results (1 to 60 minutes depending
  on the data) to keep requests low.
- Only `index.html` and `/api/...` routes are served publicly. The server code itself is never sent to visitors.
- Data can be delayed up to 15 minutes. The scorecard and news mood tags are automatic, rule-based summaries,
  not financial advice.
