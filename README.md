# HyperMate Telegram Bot

A Telegram bot for tracking Hyperliquid wallet positions and transfers in real-time.

## Features

- 🔍 **Position Monitoring**: Track perpetual positions, TWAP orders, and unrealized P&L
- 💰 **Transfer Monitoring**: Monitor spot trades, transfers, deposits, and withdrawals
- 📊 **Statistics**: View trading statistics and portfolio performance
- 🏷️ **Wallet Management**: Add/remove wallets with custom aliases
- 📈 **Real-time Alerts**: Automatic notifications for new positions and transfers
- 🔒 **Secure Storage**: Encrypted wallet data with SQLite database
- 🔄 **Data Migration**: Automatic migration from legacy JSON storage

## Commands

- `/start` - Get started with the bot
- `/add <wallet_address> <alias>` - Add a wallet to track
- `/list` - Show all tracked wallets
- `/remove <alias>` - Remove a wallet from tracking
- `/positions <alias>` - View current positions and balances
- `/stats <alias>` - View trading statistics

## Local Development

### Prerequisites
- Python 3.8+
- SQLite (usually included with Python)

### Setup
1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set required environment variables:
   ```bash
   export BOT_TOKEN=your_bot_token_here
   export WALLET_ENCRYPTION_KEY=your_32_character_encryption_key
   ```
4. Run the bot: `python bot.py`

### Environment Variables
- `BOT_TOKEN` (required) - Your Telegram bot token from @BotFather
- `WALLET_ENCRYPTION_KEY` (required) - 32+ character string for encrypting sensitive data
- `LOG_LEVEL` (optional) - Set to `DEBUG` for verbose logging (default: `INFO`)
- `DEBUG` (optional) - Set to `true` for development mode (default: `false`)

## Railway Deployment

### Prerequisites
- GitHub account
- Railway account
- Telegram bot token from @BotFather

### Deployment Steps

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/yourusername/hypermate-bot.git
   git push -u origin main
   ```

2. **Deploy to Railway**
   - Visit [railway.app](https://railway.app) and sign in
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your repository
   - Add environment variables (see below)
   - Deploy!

3. **Environment Variables**
   Set these in Railway dashboard:
   - `BOT_TOKEN` (required) - Your Telegram bot token
   - `WALLET_ENCRYPTION_KEY` (required) - A secure 32+ character encryption key
   - `LOG_LEVEL` (optional) - Set to `DEBUG` for verbose logging
   - `DEBUG` (optional) - Set to `true` for development mode

### Cost
- Railway offers a free tier with 512MB RAM and 1GB storage
- Pro plans start at $5/month for always-on deployments

## Technical Details

- **Backend**: Python with `python-telegram-bot` library
- **APIs**: Hyperliquid API for position and transfer data
- **Scheduling**: APScheduler for background monitoring tasks
- **Database**: SQLite with `aiosqlite` for persistent data storage
- **Security**: `cryptography` (Fernet) for encrypting sensitive wallet data
- **HTTP Client**: `aiohttp` for async API requests
- **Account Management**: `eth-account` for Ethereum wallet operations

## Architecture

The bot runs several key components:

### Background Tasks
1. **Position Monitoring** (every 30 seconds) - Checks for new positions, TWAP orders, and P&L changes
2. **Transfer Monitoring** (every 30 seconds, offset by 15s) - Monitors spot trades and transfers

### Data Storage
- **SQLite Database**: Primary storage for tracked wallets and user data
- **Encrypted Storage**: Private keys and sensitive data encrypted with Fernet
- **Migration System**: Automatic migration from legacy JSON files to database
- **Backup Files**: JSON files maintained for data redundancy

### Key Files
- `bot.py` - Main bot application (2300+ lines)
- `config.py` - Configuration management
- `hypermate.db` - SQLite database
- `requirements.txt` - Python dependencies
- `railway.toml` - Railway deployment configuration
- `Procfile` - Process definition for deployment

## Dependencies

The bot requires the following Python packages:
- `python-telegram-bot` - Telegram Bot API wrapper
- `aiohttp` - Async HTTP client
- `APScheduler` - Background task scheduling
- `hyperliquid-python-sdk` - Hyperliquid API integration
- `cryptography` - Data encryption
- `aiosqlite` - Async SQLite database operations

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues or questions, please create an issue on GitHub. 