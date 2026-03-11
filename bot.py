#!/usr/bin/env python3
"""
Claude Code Telegram Bridge
Мост между Telegram и Claude Code с сохранением контекста
"""
import asyncio
# Загрузка .env файла
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv не установлен, используем системные переменные


import os
import sys
import re
import subprocess
import time
import json
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Для распознавания голоса
try:
    import speech_recognition as sr
    from pydub import AudioSegment
    VOICE_ENABLED = True
except ImportError:
    VOICE_ENABLED = False

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
WORKING_DIR = os.getenv("WORKING_DIR", "/root")
# Проверка конфигурации
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set. Create .env file or set environment variable.")
    sys.exit(1)
if not ALLOWED_USER_ID:
    print("ERROR: ALLOWED_USER_ID not set. Create .env file or set environment variable.")
    sys.exit(1)



# Проекты для быстрого переключения
PROJECTS = {
    "vpn": "/etc/sing-box",
    "warp": "/etc/wireguard",
    "cloudflared": "/etc/cloudflared",
    "home": "/root",
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояние
start_new_session = False
current_model = "opus"  # sonnet или opus
current_process = None  # Текущий процесс Claude для отмены
history = []  # История сообщений
processing_lock = asyncio.Lock()  # Защита от параллельных запросов


TOOL_ICONS = {
    "Bash": "🖥",
    "Read": "📖",
    "Edit": "✏️",
    "Write": "📝",
    "Glob": "🔍",
    "Grep": "🔎",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
}
THINKING_ICONS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _tool_input_preview(name: str, inp: dict) -> str:
    """Краткое описание вызова инструмента"""
    if name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    if name in ("Read", "Edit", "Write"):
        return inp.get("file_path", str(inp)[:80])
    if name == "Glob":
        return inp.get("pattern", str(inp)[:80])
    if name == "Grep":
        return inp.get("pattern", str(inp)[:80])
    return str(inp)[:100]


def _result_preview(content) -> str:
    """Краткий вывод результата инструмента"""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(parts).strip()
    else:
        text = str(content).strip()
    lines = text.splitlines()
    preview = "\n".join(lines[:6])
    if len(lines) > 6 or len(preview) > 300:
        preview = preview[:300] + "..."
    return preview or "(пусто)"


def _build_status(tool_log: list, elapsed: int, model_icon: str, thinking_step: int) -> str:
    """Собирает текст статус-сообщения из лога инструментов"""
    mins, secs = elapsed // 60, elapsed % 60
    icon = THINKING_ICONS[thinking_step % len(THINKING_ICONS)]
    lines = []
    # Показываем последние 8 записей чтобы не переполнить Telegram
    for entry in tool_log[-8:]:
        t_icon = TOOL_ICONS.get(entry["name"], "🔧")
        lines.append(f"{t_icon} {entry['name']}({entry['input']})")
        if entry["result"] is not None:
            lines.append(f"```\n{entry['result']}\n```")
    footer = f"\n{model_icon} {icon} Работаю... {mins}:{secs:02d}"
    body = "\n".join(lines)
    # Ограничиваем общую длину
    if len(body) > 3600:
        body = "..." + body[-3600:]
    return (body + footer).strip()


async def send_to_claude(text: str, message: Message, status_msg: Message):
    """Отправляет текст в Claude со стримингом ответа (stream-json)"""
    global start_new_session, current_process

    try:
        cmd = [
            "claude", "-p", text,
            "--model", current_model,
            "--allowedTools", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
            "--output-format", "stream-json",
            "--verbose",
        ]

        if not start_new_session:
            cmd.insert(2, "--continue")
        else:
            start_new_session = False
            history.clear()

        history.append({"role": "user", "text": text[:200] + "..." if len(text) > 200 else text})

        print(f"[DEBUG] Запуск Claude stream-json, model={current_model}")
        sys.stdout.flush()

        current_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=WORKING_DIR,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
        )

        start_time = time.time()
        model_icon = "🟣" if current_model == "opus" else "🔵"
        tool_log = []        # [{"name", "input", "result"}]
        final_result = ""
        last_update = 0
        thinking_step = 0
        line_buf = b""

        try:
            while True:
                # Общий таймаут 5 минут
                if time.time() - start_time > 300:
                    final_result = "⏱ Таймаут — Claude не ответил за 5 минут"
                    break

                try:
                    if current_process is None or current_process.stdout is None:
                        break
                    chunk = await asyncio.wait_for(
                        current_process.stdout.read(4096),
                        timeout=5
                    )
                except asyncio.TimeoutError:
                    # Обновляем статус пока ждём
                    elapsed = int(time.time() - start_time)
                    thinking_step += 1
                    try:
                        status_text = _build_status(tool_log, elapsed, model_icon, thinking_step)
                        if status_text != _get_last_text(status_msg):
                            await status_msg.edit_text(status_text, parse_mode=ParseMode.MARKDOWN)
                    except:
                        pass
                    continue

                if not chunk:
                    break

                # Разбираем построчно
                line_buf += chunk
                while b"\n" in line_buf:
                    line, line_buf = line_buf.split(b"\n", 1)
                    raw = line.decode("utf-8", errors="ignore").strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "assistant":
                        # Ищем tool_use блоки
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "tool_use":
                                name = block.get("name", "Tool")
                                inp = block.get("input", {})
                                tool_log.append({
                                    "name": name,
                                    "input": _tool_input_preview(name, inp),
                                    "result": None,
                                })
                                print(f"[DEBUG] Tool call: {name}")
                                sys.stdout.flush()
                                # Немедленное обновление при вызове инструмента
                                elapsed = int(time.time() - start_time)
                                thinking_step += 1
                                try:
                                    status_text = _build_status(tool_log, elapsed, model_icon, thinking_step)
                                    if status_text != _get_last_text(status_msg):
                                        await status_msg.edit_text(status_text, parse_mode=ParseMode.MARKDOWN)
                                        _update_last_text(status_msg, status_text)
                                        last_update = time.time()
                                except:
                                    pass

                    elif etype == "tool_result":
                        # Заполняем результат последнего инструмента
                        content = event.get("content", "")
                        is_error = event.get("is_error", False)
                        preview = _result_preview(content)
                        if tool_log:
                            tool_log[-1]["result"] = ("❌ " if is_error else "") + preview
                        print(f"[DEBUG] Tool result ({len(str(content))} chars)")
                        sys.stdout.flush()
                        # Обновляем сразу после результата
                        elapsed = int(time.time() - start_time)
                        thinking_step += 1
                        try:
                            status_text = _build_status(tool_log, elapsed, model_icon, thinking_step)
                            await status_msg.edit_text(status_text, parse_mode=ParseMode.MARKDOWN)
                            last_update = time.time()
                        except:
                            pass

                    elif etype == "result":
                        final_result = event.get("result", "")
                        print(f"[DEBUG] Final result: {len(final_result)} chars")
                        sys.stdout.flush()

            await current_process.wait()

        except asyncio.CancelledError:
            final_result = "🚫 Запрос отменён"
        finally:
            current_process = None

        if not final_result.strip():
            final_result = "❓ Пустой ответ от Claude"

        print(f"[DEBUG] Отправляем финальный ответ: {len(final_result)} символов")
        sys.stdout.flush()

        history.append({"role": "assistant", "text": final_result[:200] + "..." if len(final_result) > 200 else final_result})
        if len(history) > 20:
            history.pop(0)
            history.pop(0)

        # Финальное сообщение
        if len(final_result) <= 4000:
            try:
                await status_msg.edit_text(final_result)
            except:
                pass
        else:
            try:
                await status_msg.delete()
            except:
                pass
            parts = [final_result[i:i+4000] for i in range(0, len(final_result), 4000)]
            for part in parts:
                await message.answer(part)
                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[DEBUG] Ошибка send_to_claude: {e}")
        sys.stdout.flush()
        try:
            await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
        except:
            pass


# Вспомогательная — запоминаем последний отправленный текст чтобы не делать дублирующие edit
_last_status_text: dict = {}

def _update_last_text(msg: Message, text: str):
    _last_status_text[msg.message_id] = text
    # Очищаем старые записи (более 100)
    if len(_last_status_text) > 100:
        oldest = list(_last_status_text.keys())[0]
        del _last_status_text[oldest]

def _get_last_text(msg: Message) -> str:
    return _last_status_text.get(msg.message_id, "")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    await message.answer(
        "🤖 *Claude Code Bridge (VPN Server)*\n\n"
        "*Основные:*\n"
        "/new - Новый разговор\n"
        "/cancel - Отменить запрос\n"
        "/history - История сообщений\n\n"
        "*Настройки:*\n"
        "/model - Сменить модель (sonnet/opus)\n"
        "/projects - Список проектов\n"
        "/cd <путь> - Сменить директорию\n"
        "/status - Статус\n\n"
        "*Файлы:*\n"
        "📎 Отправь файл с подписью - Claude его проанализирует\n\n"
        f"📁 Текущий проект: `{WORKING_DIR}`\n"
        f"🤖 Модель: `{current_model}`",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(Command("new"))
async def cmd_new(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    global start_new_session
    start_new_session = True
    history.clear()
    await message.answer("🆕 Новый разговор начат. История очищена.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    global current_process
    if current_process:
        try:
            current_process.kill()
            current_process = None
            await message.answer("🚫 Запрос отменён")
        except:
            await message.answer("❌ Не удалось отменить")
    else:
        await message.answer("ℹ️ Нет активного запроса")


@dp.message(Command("history"))
async def cmd_history(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    if not history:
        await message.answer("📜 История пуста")
        return

    text = "📜 *История сессии:*\n\n"
    for i, msg in enumerate(history[-10:], 1):  # Последние 10
        role = "👤" if msg["role"] == "user" else "🤖"
        text += f"{role} {msg['text']}\n\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("model"))
async def cmd_model(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="🔵 Sonnet (быстрый)" + (" ✓" if current_model == "sonnet" else ""), callback_data="model_sonnet")
    builder.button(text="🟣 Opus (умный)" + (" ✓" if current_model == "opus" else ""), callback_data="model_opus")
    builder.adjust(1)

    await message.answer(
        f"🤖 Текущая модель: *{current_model}*\n\n"
        "Выбери модель:",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.MARKDOWN
    )


@dp.callback_query(F.data.startswith("model_"))
async def cb_model(callback: CallbackQuery):
    if callback.from_user.id != ALLOWED_USER_ID:
        return

    global current_model
    current_model = callback.data.replace("model_", "")

    await callback.message.edit_text(f"✅ Модель изменена: *{current_model}*", parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@dp.message(Command("projects"))
async def cmd_projects(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    builder = InlineKeyboardBuilder()
    for name, path in PROJECTS.items():
        mark = " ✓" if path == WORKING_DIR else ""
        builder.button(text=f"📁 {name}{mark}", callback_data=f"project_{name}")
    builder.adjust(2)

    await message.answer(
        f"📁 Текущий: `{WORKING_DIR}`\n\nВыбери проект:",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.MARKDOWN
    )


@dp.callback_query(F.data.startswith("project_"))
async def cb_project(callback: CallbackQuery):
    if callback.from_user.id != ALLOWED_USER_ID:
        return

    global WORKING_DIR
    name = callback.data.replace("project_", "")

    if name in PROJECTS:
        WORKING_DIR = PROJECTS[name]
        await callback.message.edit_text(f"✅ Проект: *{name}*\n`{WORKING_DIR}`", parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@dp.message(Command("cd"))
async def cmd_cd(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    global WORKING_DIR

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(f"📁 Текущая директория: `{WORKING_DIR}`", parse_mode=ParseMode.MARKDOWN)
        return

    new_dir = args[1].strip()
    if os.path.isdir(new_dir):
        WORKING_DIR = new_dir
        await message.answer(f"✅ Директория: `{WORKING_DIR}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer(f"❌ Не существует: {new_dir}")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        version = result.stdout.strip()
    except:
        version = "?"

    try:
        with open(os.path.expanduser("~/.claude.json"), "r") as f:
            auth = "✅" if "oauthAccount" in f.read() else "❌"
    except:
        auth = "❓"

    await message.answer(
        f"📊 *Статус (VPN Server)*\n\n"
        f"Авторизация: {auth}\n"
        f"Версия: `{version}`\n"
        f"Модель: `{current_model}`\n"
        f"Директория: `{WORKING_DIR}`\n"
        f"История: {len(history)} сообщений\n"
        f"Процесс: {'🟢 активен' if current_process else '⚪ нет'}",
        parse_mode=ParseMode.MARKDOWN
    )


login_process = None  # Глобальный процесс логина чтобы не убивался

@dp.message(Command("login"))
async def cmd_login(message: Message):
    global login_process
    if message.from_user.id != ALLOWED_USER_ID:
        return

    print("[DEBUG] Команда /login получена")
    sys.stdout.flush()

    status_msg = await message.answer("🔐 Запускаю авторизацию Claude...")

    try:
        # Проверим может уже залогинен
        print("[DEBUG] Проверяю статус авторизации...")
        sys.stdout.flush()
        check = await asyncio.create_subprocess_exec(
            "claude", "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
        )
        try:
            out, _ = await asyncio.wait_for(check.communicate(), timeout=10)
            auth_result = out.decode('utf-8', errors='ignore')
            print(f"[DEBUG] Статус авторизации: {auth_result[:100]}")
            sys.stdout.flush()
            if '"loggedIn": true' in auth_result:
                await status_msg.edit_text("✅ Claude уже авторизован! Можешь писать сообщения.")
                return
        except asyncio.TimeoutError:
            print("[DEBUG] Таймаут проверки статуса")
            sys.stdout.flush()
            try:
                check.kill()
            except:
                pass

        # Убиваем старый процесс если есть
        if login_process:
            try:
                login_process.kill()
            except:
                pass
            login_process = None

        # Запускаем claude auth login
        print("[DEBUG] Запускаю claude auth login...")
        sys.stdout.flush()
        login_process = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
        )
        print(f"[DEBUG] Процесс запущен, PID: {login_process.pid}")
        sys.stdout.flush()

        # Читаем вывод пока не найдём URL
        output = ""
        try:
            while True:
                chunk = await asyncio.wait_for(login_process.stdout.read(512), timeout=5)
                if not chunk:
                    break
                output += chunk.decode('utf-8', errors='ignore')
                print(f"[DEBUG] Прочитано: {len(output)} байт")
                sys.stdout.flush()
                if 'https://claude.ai/oauth' in output:
                    break
        except asyncio.TimeoutError:
            print(f"[DEBUG] Таймаут чтения. Вывод: {output[:200]}")
            sys.stdout.flush()

        url_match = re.search(r'https://claude\.ai/oauth/authorize\S+', output)

        if url_match:
            url = url_match.group(0)
            print(f"[DEBUG] URL найден: {url[:80]}...")
            sys.stdout.flush()
            await status_msg.edit_text(
                f"🔐 Авторизация Claude\n\n"
                f"Открой ссылку, залогинься и разреши доступ:\n\n"
                f"{url}\n\n"
                f"После авторизации отправь /authcheck"
            )
            # Процесс живёт и ждёт callback
            proc_ref = login_process
            asyncio.create_task(_wait_login_done(proc_ref, message))
        else:
            print(f"[DEBUG] URL не найден. Вывод: {output[:300]}")
            sys.stdout.flush()
            await status_msg.edit_text(f"❌ Не удалось получить ссылку.\n\nОтвет: {output[:1000]}")
            if login_process:
                login_process.kill()
                login_process = None

    except Exception as e:
        print(f"[DEBUG] Ошибка /login: {e}")
        sys.stdout.flush()
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except:
            pass


async def _wait_login_done(proc, message: Message):
    """Фоновая задача — ждёт завершения логина и уведомляет"""
    global login_process
    if proc is None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=300)
        if proc.returncode == 0:
            await message.answer("✅ Авторизация успешна! Claude готов к работе.")
        else:
            await message.answer("❌ Авторизация не удалась. Попробуй /login ещё раз.")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except:
            pass
        await message.answer("⏱ Таймаут авторизации (5 мин). Попробуй /login ещё раз.")
    except Exception:
        pass
    finally:
        login_process = None


@dp.message(Command("authcheck"))
async def cmd_authcheck(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        return

    try:
        check = await asyncio.create_subprocess_exec(
            "claude", "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
        )
        out, _ = await check.communicate()
        result = out.decode('utf-8', errors='ignore')

        if '"loggedIn": true' in result or '"loggedIn":true' in result:
            await message.answer("✅ Claude авторизован! Можешь отправлять сообщения.")
        else:
            await message.answer("❌ Ещё не авторизован. Перейди по ссылке из /login и залогинься.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# Обработка голосовых сообщений
@dp.message(F.voice)
async def handle_voice(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    if not VOICE_ENABLED:
        await message.answer("❌ Распознавание голоса не установлено")
        return

    status_msg = await message.answer("🎤 Распознаю голос...")

    try:
        # Скачиваем голосовое
        voice = message.voice
        file = await bot.get_file(voice.file_id)

        tmp_dir = "/tmp/claude_voice"
        os.makedirs(tmp_dir, exist_ok=True)

        ogg_path = f"{tmp_dir}/voice_{message.message_id}.ogg"
        wav_path = f"{tmp_dir}/voice_{message.message_id}.wav"

        await bot.download_file(file.file_path, ogg_path)

        # Конвертируем OGG в WAV
        audio = AudioSegment.from_ogg(ogg_path)
        audio.export(wav_path, format="wav")

        # Распознаём
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        # Пробуем Google Speech Recognition
        try:
            text = recognizer.recognize_google(audio_data, language="ru-RU")
        except sr.UnknownValueError:
            await status_msg.edit_text("❓ Не удалось распознать речь")
            return
        except sr.RequestError as e:
            await status_msg.edit_text(f"❌ Ошибка сервиса: {e}")
            return

        # Удаляем временные файлы
        try:
            os.remove(ogg_path)
            os.remove(wav_path)
        except:
            pass

        # Показываем распознанный текст
        await status_msg.edit_text(f"🎤 Распознано:\n_{text}_\n\n⏳ Отправляю Claude...", parse_mode=ParseMode.MARKDOWN)

        # Отправляем в Claude
        if processing_lock.locked():
            await status_msg.edit_text("⏳ Подожди, обрабатываю предыдущий запрос...")
            return
        
        async with processing_lock:
            await send_to_claude(text, message, status_msg)

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")


# Обработка фото
@dp.message(F.photo)
async def handle_photo(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    photo = message.photo[-1]  # Берём самое большое фото
    caption = message.caption or "Что на этом изображении?"

    status_msg = await message.answer("📸 Скачиваю фото...")

    try:
        # Создаём временную директорию
        tmp_dir = "/tmp/claude_photos"
        os.makedirs(tmp_dir, exist_ok=True)

        # Имя файла
        file_path = f"{tmp_dir}/photo_{message.message_id}.jpg"

        # Скачиваем
        file = await bot.get_file(photo.file_id)
        await bot.download_file(file.file_path, file_path)

        await status_msg.edit_text(f"📸 Фото сохранено\n⏳ Отправляю Claude для анализа...")

        # Формируем запрос для Claude с путём к изображению
        # Claude Code может читать изображения через команду с флагом --file или напрямую через путь
        prompt = f"Проанализируй это изображение: {file_path}\n\n{caption}"

        # Отправляем в Claude
        if processing_lock.locked():
            await status_msg.edit_text("⏳ Подожди, обрабатываю предыдущий запрос...")
            return
        
        async with processing_lock:
            await send_to_claude(prompt, message, status_msg)

        # Не удаляем файл сразу, чтобы Claude успел его прочитать
        # Удалим позже через cron или вручную

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")


# Обработка файлов
@dp.message(F.document)
async def handle_document(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    doc = message.document
    caption = message.caption or "Проанализируй этот файл"

    # Скачиваем файл
    status_msg = await message.answer("📥 Скачиваю файл...")

    try:
        # Создаём временную директорию
        tmp_dir = "/tmp/claude_files"
        os.makedirs(tmp_dir, exist_ok=True)

        file_path = f"{tmp_dir}/{doc.file_name}"

        # Скачиваем
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, file_path)

        await status_msg.edit_text(f"📄 Файл: {doc.file_name}\n⏳ Отправляю Claude...")

        # Читаем содержимое файла
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # Ограничиваем размер
            if len(content) > 50000:
                content = content[:50000] + "\n\n... (файл обрезан, слишком большой)"

            # Формируем запрос
            prompt = f"Файл `{doc.file_name}`:\n```\n{content}\n```\n\n{caption}"

        except:
            # Бинарный файл
            prompt = f"Файл {doc.file_name} сохранён в {file_path}. {caption}"

        # Отправляем в Claude
        if processing_lock.locked():
            await status_msg.edit_text("⏳ Подожди, обрабатываю предыдущий запрос...")
            return
        
        async with processing_lock:
            await send_to_claude(prompt, message, status_msg)

        # Удаляем временный файл
        try:
            os.remove(file_path)
        except:
            pass

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")


# Обработка текста
@dp.message(F.text)
async def handle_message(message: Message):
    if message.from_user.id != ALLOWED_USER_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    print(f"[DEBUG] Получено сообщение: {message.text[:50]}...")
    sys.stdout.flush()

    # Если есть активный процесс логина и сообщение похоже на auth-код — передаём его
    if login_process and login_process.returncode is None:
        text = message.text.strip()
        # Auth-код — длинная строка без пробелов с # внутри
        if '#' in text and len(text) > 30 and ' ' not in text:
            print(f"[DEBUG] Обнаружен auth-код, передаю в процесс логина...")
            sys.stdout.flush()
            try:
                login_process.stdin.write((text + "\n").encode())
                await login_process.stdin.drain()
                await message.answer("🔐 Код авторизации отправлен. Ожидаю подтверждение...")
                # Ждём завершения процесса
                try:
                    await asyncio.wait_for(login_process.wait(), timeout=30)
                    # Проверяем статус
                    check = await asyncio.create_subprocess_exec(
                        "claude", "auth", "status",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
                    )
                    out, _ = await check.communicate()
                    if '"loggedIn": true' in out.decode():
                        await message.answer("✅ Авторизация успешна! Claude готов к работе.")
                    else:
                        await message.answer("❌ Авторизация не прошла. Попробуй /login ещё раз.")
                except asyncio.TimeoutError:
                    await message.answer("⏱ Таймаут. Проверь /authcheck")
                return
            except Exception as e:
                print(f"[DEBUG] Ошибка передачи auth-кода: {e}")
                sys.stdout.flush()

    # Проверяем что нет активного запроса
    if processing_lock.locked():
        await message.answer("⏳ Подожди, обрабатываю предыдущий запрос...")
        return
    
    model_icon = "🟣" if current_model == "opus" else "🔵"
    status_msg = await message.answer(f"{model_icon} Думаю...")

    print(f"[DEBUG] Отправляю в send_to_claude...")
    sys.stdout.flush()
    
    async with processing_lock:
        await send_to_claude(message.text, message, status_msg)
    
    print(f"[DEBUG] send_to_claude завершён")
    sys.stdout.flush()


async def main():
    print("🚀 Claude Code Bridge (VPN Server) запущен")
    print(f"👤 Пользователь: {ALLOWED_USER_ID}")
    print(f"📁 Директория: {WORKING_DIR}")
    print(f"🤖 Модель: {current_model}")

    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
