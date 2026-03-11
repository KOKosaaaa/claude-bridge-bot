# Claude Code Telegram Bridge

Telegram бот для удалённого управления сервером через Claude Code CLI.

Отправляете команду в Telegram — Claude выполняет её на сервере и возвращает результат. Идеально для администрирования VPS, редактирования конфигов, деплоя и отладки без SSH-клиента.

## Возможности

- 💬 Текстовые команды к Claude (выполняет Bash, читает/редактирует файлы)
- 📎 Анализ файлов (отправьте документ с подписью)
- 📸 Анализ изображений (скриншоты, логи)
- 🎤 Голосовые сообщения (распознавание речи)
- 🔄 Переключение моделей (Sonnet/Opus)
- 📁 Быстрое переключение между проектами
- 🚫 Отмена текущего запроса

## Примеры использования

- «Покажи статус всех systemd сервисов»
- «Исправь ошибку в /etc/nginx/nginx.conf»
- «Установи docker и запусти контейнер»
- «Проверь логи за последний час»

## Требования

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) установлен и авторизован
- Telegram Bot Token (получить у @BotFather)

## Установка

    git clone https://github.com/KOKosaaaa/claude-bridge-bot.git
    cd claude-bridge-bot
    pip install -r requirements.txt
    cp .env.example .env
    # Отредактируйте .env своими данными
    python bot.py

## Настройка .env

    BOT_TOKEN=your_telegram_bot_token
    ALLOWED_USER_ID=your_telegram_user_id
    WORKING_DIR=/root

Узнать User ID: напишите @userinfobot в Telegram

## Команды бота

- /start - Справка
- /new - Новый разговор (сброс контекста)
- /cancel - Отменить текущий запрос
- /model - Сменить модель (Sonnet/Opus)
- /projects - Список проектов
- /cd - Сменить директорию
- /status - Статус бота и Claude
- /login - Авторизация Claude (если нужно)

## Автозапуск (systemd)

    sudo cp claude-bridge.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now claude-bridge

## Безопасность

- Бот принимает команды только от указанного ALLOWED_USER_ID
- Все остальные пользователи получают «Доступ запрещён»

## Лицензия

MIT
