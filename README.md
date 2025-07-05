# HyperMate Telegram Bot

A Telegram bot for tracking Hyperliquid wallet positions and transfers in real-time.

## Features

- 🔍 **Position Monitoring**: Track perpetual positions, TWAP orders, and unrealized P&L
- 💰 **Transfer Monitoring**: Monitor spot trades, transfers, deposits, and withdrawals
- 📊 **Statistics**: View trading statistics and portfolio performance
- 🏷️ **Wallet Management**: Add/remove wallets with custom aliases
- 📈 **Real-time Alerts**: Automatic notifications for new positions and transfers

## Commands

- `/start` - Get started with the bot
- `/add <wallet_address> <alias>` - Add a wallet to track
- `/list` - Show all tracked wallets
- `/remove <alias>` - Remove a wallet from tracking
- `/positions <alias>` - View current positions and balances
- `/stats <alias>` - View trading statistics

## Local Development

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set your bot token: `export BOT_TOKEN=your_bot_token_here`
4. Run the bot: `python bot.py`

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
   - Add environment variable: `BOT_TOKEN` with your bot token
   - Deploy!

3. **Environment Variables**
   Set these in Railway dashboard:
   - `BOT_TOKEN` (required) - Your Telegram bot token
   - `LOG_LEVEL` (optional) - Set to `DEBUG` for verbose logging
   - `DEBUG` (optional) - Set to `true` for development mode

### Cost
- Railway offers a free tier with 512MB RAM and 1GB storage
- Pro plans start at $5/month for always-on deployments

## Technical Details

- **Backend**: Python with `python-telegram-bot` library
- **APIs**: Hyperliquid API for position and transfer data
- **Scheduling**: APScheduler for background monitoring tasks
- **Storage**: JSON file for wallet data persistence

## Architecture

The bot runs two main background tasks:
1. **Position Monitoring** (every 30 seconds) - Checks for new positions, TWAP orders, and P&L changes
2. **Transfer Monitoring** (every 30 seconds, offset by 15s) - Monitors spot trades and transfers

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues or questions, please create an issue on GitHub. 