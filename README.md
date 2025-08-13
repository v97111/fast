# Fast-Cycle Binance Bot

A multi-worker Binance trading bot with Flask dashboard for automated cryptocurrency trading using EMA and volume analysis.

## Features

- **Safe & Fast Trading Modes**: Conservative EMA50-based strategy and faster EMA bounce strategy
- **Multi-Worker Support**: Run multiple trading workers simultaneously with individual budgets
- **Real-time Dashboard**: Mobile-friendly web interface with live PnL tracking
- **Risk Management**: Stop-loss, take-profit, and trailing stop mechanisms
- **Debug & Analytics**: Trade history, rejection analysis, and performance metrics

## Deployment

### Platform Requirements (Railway, Heroku, etc.)

The application is configured for deployment on container platforms:

- **Procfile**: Contains the correct Gunicorn configuration with 2 workers for resilience
- **Health Check**: `/health` endpoint provides fast readiness checks (returns 200 "ok")
- **Environment Variables**: Requires `BINANCE_API_KEY`, `BINANCE_API_SECRET`, and optional `BINANCE_TESTNET`

### Procfile Configuration

```
web: gunicorn -w 2 -k gthread -t 120 -b 0.0.0.0:$PORT main:app
```

- **2 workers** (`-w 2`): Provides redundancy and better performance
- **gthread worker** (`-k gthread`): Uses threaded workers without additional dependencies  
- **120s timeout** (`-t 120`): Allows time for API calls and trade processing
- **Port binding**: Uses platform-provided `$PORT` environment variable

### Environment Variables

Set these in your deployment platform:

```bash
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret
BINANCE_TESTNET=true  # Use testnet (recommended for testing)
PORT=8080  # Usually set automatically by platform
```

### Health Monitoring

- **Health endpoint**: `GET /health` - Fast 200 OK response for readiness checks
- **Status endpoint**: `GET /api/status` - Detailed application state (slower)
- **Dashboard**: `/` - Full web interface with worker management

### Scaling Guidance

- **Memory**: ~200-300MB per worker (4 workers max recommended)
- **CPU**: Single core sufficient for 2-4 workers
- **Network**: Minimal bandwidth for API calls
- **Storage**: Logs trades to `fast_cycle_trades.csv`

### Dependencies

All dependencies are pinned for reproducible deployments:

- `flask==3.1.1` - Web framework
- `gunicorn==23.0.0` - WSGI server
- `python-binance==1.0.29` - Binance API client
- `python-dotenv==1.1.1` - Environment variable loading
- `flask-cors==6.0.1` - Cross-origin resource sharing

## Local Development

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Create `.env` file with API credentials
4. Run: `python main.py` (development) or `gunicorn main:app` (production-like)

## Trading Strategies

### Safe Mode (Conservative)
- EMA50 strict filtering
- Volume ≥ 1.2x 10-day average
- Previous bar dips ≥ 0.6%, next bar closes above previous high

### Fast Mode (Aggressive)  
- EMA ≥ 99.2% of EMA50
- Volume ≥ 0.85x average OR top-3 volume in last 10 bars
- Simple bounce pattern (last close > previous close)

### Risk Management
- **Take Profit**: +1.25% minimum, trailing starts at +1.6% with 0.4% giveback
- **Stop Loss**: -1.5% maximum loss
- **Time Limit**: 45 minutes maximum trade duration