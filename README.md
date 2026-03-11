# Claude Code Telegram Bridge

Telegram bot for Claude Code CLI.

## Features

- Text queries to Claude
- File and image analysis
- Voice messages
- Model switching (Sonnet/Opus)
- Project switching

## Installation

    git clone https://github.com/KOKosaaaa/claude-bridge-bot.git
    cd claude-bridge-bot
    pip install -r requirements.txt
    cp .env.example .env
    python bot.py

## Configuration (.env)

    BOT_TOKEN=your_telegram_bot_token
    ALLOWED_USER_ID=your_telegram_user_id

Get User ID: message @userinfobot on Telegram

## Commands

- /start - Help
- /new - New conversation
- /cancel - Cancel request
- /model - Switch model
- /status - Status

## License

MIT
