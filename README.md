# X Giveaway Link Tracker

A local, metadata-only dashboard that polls X Recent Search and records:

- Canonical post URL
- Author username
- Post timestamp
- Discovery timestamp

It does **not** store, log, display, validate, or export post text or credentials.

## Requirements

- Python 3.10+
- An approved X Developer Project/App with a bearer token
- Access to `GET /2/tweets/search/recent` for that app

Your normal browser login is not an API credential. Create/use an app in the X Developer Console and copy its bearer token. Keep that token private.

## Start on Windows PowerShell

```powershell
$env:X_BEARER_TOKEN = "your-X-developer-app-bearer-token"
.\start.ps1
```

If PowerShell blocks local scripts, run the app directly with a Python 3.10+ executable and `tracker.py`.

Open <http://127.0.0.1:8765>.

The default polling interval is 10 seconds. The tracker automatically backs off after rate limits and API/network errors. To use a slower interval:

```powershell
$env:POLL_SECONDS = "30"
.\start.ps1
```

## Customize the explicit-giveaway query

The default query requires both API-credit language and giveaway-style language. You may replace it with another X search query:

```powershell
$env:X_SEARCH_QUERY = '("developer credit giveaway" OR "API credit giveaway") -is:retweet -is:reply'
.\start.ps1
```

Keep the query limited to posts that explicitly describe a giveaway. X limits recent search to posts from the last seven days and may impose usage charges or project-level caps.

## One-shot mode

```powershell
python .\tracker.py --once
```

## Data and reset

The SQLite database is created at `data/tracker.db`. It contains metadata only. Stop the app before backing up or removing the database.

## Proxy settings

The tracker connects directly to `api.x.com` by default. This avoids stale Windows or environment proxy settings that can produce `WinError 10013` even when normal connectivity works.

If your network genuinely requires its configured system proxy, enable it explicitly:

```powershell
$env:TRACKER_USE_SYSTEM_PROXY = "1"
.\start.ps1
```

## Security notes

- Never paste the bearer token into the dashboard or commit it to source control.
- The server binds only to `127.0.0.1` by default.
- Opening a recorded link may expose whatever its author posted; the tracker itself does not reproduce that content.
- Provider rules may prohibit sharing account credentials even when a post calls them a giveaway.
