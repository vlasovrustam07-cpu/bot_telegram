"""
Телеграм-бот для постинга треков из папки tracks/

Устройство:
- ты общаешься с ботом (шлёшь команды) в личке или в любом чате — это "чат управления"
- а сами треки бот публикует в отдельный телеграм-канал (CHANNEL_ID)

Что делает:
- берёт .mp3/.flac/... файлы из папки TRACKS_DIR
- хранит очередь публикации и список уже опубликованных треков (файл state.json)
- команда /queue    — показать текущую очередь
- команда /shuffle  — перемешать очередь
- команда /remove <точное имя файла> — убрать конкретный трек из очереди (не удаляя сам файл)
- команда /post_now — выложить следующий трек из очереди прямо сейчас (в канал)
- команда /time HH:MM [HH:MM ...] — задать время(на) автопостинга, можно несколько
  раз в день (например /time 09:00 18:30 — 2 раза в день), /time off — отключить
- команда /status   — показать текущие настройки и статистику
- пришли боту аудиофайл прямо в чат — он сохранится в tracks/ и встанет в очередь

Подписи к трекам:
- Артист определяется автоматически из имени файла "Артист - Название.mp3"
- Можно переопределить вручную ниже в словаре CUSTOM_ARTISTS (по имени файла)
- Текст подписи под артистом задаётся в EXTRA_CAPTION_TEXT

Установка зависимостей:
    pip install python-telegram-bot==21.4 mutagen Pillow --break-system-packages
    (mutagen нужен, чтобы доставать обложку, встроенную в сам аудиофайл;
     Pillow — чтобы сжать её до валидного Telegram-thumbnail)

Запуск:
    1) впиши BOT_TOKEN, CHANNEL_ID и OWNER_ID ниже
    2) положи треки в папку tracks/ рядом со скриптом
    3) python3 bot.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import re
from datetime import time as dtime
from html.parser import HTMLParser
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

try:
    from PIL import Image
except ImportError:
    Image = None

from telegram import MessageEntity, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ======================= НАСТРОЙКИ (редактируй здесь) =======================

# Токен бота, который выдал @BotFather
BOT_TOKEN = "8567546608:AAElngw-Wez7biwDepqV4OS9-Dpnn7_BL_4"

# ID канала, куда бот будет ПОСТИТЬ треки.
# Это что-то вроде -1001234567890 (бот должен быть добавлен в канал как админ
# с правом публикации сообщений).
# Узнать ID канала можно, например, переслав любое сообщение из канала боту
# @getidsbot, либо через @userinfobot.
CHANNEL_ID = "-1003346755402"

# Твой личный Telegram ID (или ID группы), откуда ты будешь УПРАВЛЯТЬ ботом —
# команды /queue, /shuffle, /post_now, /time, /status будут работать только
# из этого чата, чтобы посторонние не могли ими воспользоваться.
# Узнать свой ID можно у @userinfobot (напиши ему /start).
# Если оставить None — командами сможет пользоваться кто угодно (не рекомендуется).
OWNER_ID = 1384276449  # например: 123456789

# Папка с треками (относительно этого файла, либо абсолютный путь)
TRACKS_DIR = Path(__file__).parent / "tracks"

# Файл, в котором бот хранит очередь и список опубликованных треков
STATE_FILE = Path(__file__).parent / "state.json"

# Поддерживаемые расширения файлов
AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".ogg"}

# Текст, который будет писаться под именем артиста в подписи.
# Можно использовать HTML-ссылку, например:
EXTRA_CAPTION_TEXT = (
    "МЯСО - 🤍\n"
    "хуйня - 👎\n"
    "\n"
    "⚪️"
    '<a href="https://t.me/reasen17"> sc !& 17 | follow</a>'
)

# Файл-ид стикера, который будет отправляться в тот же чат перед аудио.
# Укажи свой sticker file_id, например: "5278544515072809442"
STICKER_FILE_ID = "5278544515072809442"


# Если хочешь вручную задать артиста для конкретного файла (а не по имени файла) —
# впиши сюда: "имя_файла.mp3": "Имя Артиста"
CUSTOM_ARTISTS = {
    # "example_track.mp3": "Bring Me The Horizon",
}

# =============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------- Работа с состоянием ---------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {"queue": [], "posted": [], "schedule_times": []}

    # миграция со старого формата (один schedule_time) на список schedule_times
    if "schedule_times" not in state:
        old = state.pop("schedule_time", None)
        state["schedule_times"] = [old] if old else []

    state.setdefault("queue", [])
    state.setdefault("posted", [])
    state.setdefault("removed", [])
    return state


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_all_tracks() -> list[str]:
    """Список всех файлов треков в TRACKS_DIR (только имена файлов)."""
    if not TRACKS_DIR.exists():
        return []
    return sorted(
        p.name for p in TRACKS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def sync_queue_with_folder(state: dict) -> dict:
    """Добавляет в очередь новые файлы, которых там ещё нет, и убирает те,
    что уже удалены из папки. Уже опубликованные треки, а также треки,
    убранные вручную командой /remove, в очередь не возвращаются."""
    all_tracks = set(get_all_tracks())
    posted = set(state.get("posted", []))
    removed = set(state.get("removed", []))
    queue = [t for t in state.get("queue", []) if t in all_tracks]
    queued_set = set(queue)

    for track in all_tracks:
        if track not in posted and track not in queued_set and track not in removed:
            queue.append(track)

    state["queue"] = queue
    return state


def guess_artist(filename: str) -> str:
    """Пытается вытащить имя артиста из имени файла.

    Поддерживается формат вида "Артист - Название.mp3" или "Артист-Название.mp3".
    """
    if filename in CUSTOM_ARTISTS:
        return CUSTOM_ARTISTS[filename]

    name = Path(filename).stem.strip()
    artist = re.split(r"\s*-\s*", name, maxsplit=1)[0].strip()
    return artist


def make_hashtag(artist: str) -> str:
    """Превращает имя артиста в валидный хэштег."""
    tag = "".join(ch for ch in artist if ch.isalnum() or ch in "_")
    if not tag:
        tag = "unknown"
    return f"#{tag}"


def build_caption(filename: str) -> str:
    artist = guess_artist(filename)
    hashtag = make_hashtag(artist)
    return f"{hashtag}\n\n{EXTRA_CAPTION_TEXT}"


class CaptionHTMLParser(HTMLParser):
    """Converts Telegram HTML markup to plain text with MessageEntity objects."""

    ENTITY_TAGS = {
        "b": MessageEntity.BOLD,
        "strong": MessageEntity.BOLD,
        "i": MessageEntity.ITALIC,
        "em": MessageEntity.ITALIC,
        "u": MessageEntity.UNDERLINE,
        "ins": MessageEntity.UNDERLINE,
        "s": MessageEntity.STRIKETHROUGH,
        "strike": MessageEntity.STRIKETHROUGH,
        "del": MessageEntity.STRIKETHROUGH,
        "code": MessageEntity.CODE,
        "pre": MessageEntity.PRE,
        "tg-spoiler": MessageEntity.SPOILER,
        "blockquote": MessageEntity.BLOCKQUOTE,
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.entities: list[MessageEntity] = []
        self.open_entities: list[dict] = []
        self.position = 0

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.position += len(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        kwargs = {}

        if tag == "a":
            entity_type = MessageEntity.TEXT_LINK
            url = attrs_dict.get("href")
            if not url:
                return
            kwargs["url"] = url
        elif tag == "tg-emoji":
            entity_type = MessageEntity.CUSTOM_EMOJI
            emoji_id = attrs_dict.get("emoji-id")
            if not emoji_id:
                return
            kwargs["custom_emoji_id"] = emoji_id
        else:
            entity_type = self.ENTITY_TAGS.get(tag)
            if entity_type is None:
                return

        self.open_entities.append(
            {
                "tag": tag,
                "type": entity_type,
                "offset": self.position,
                "kwargs": kwargs,
            }
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.open_entities) - 1, -1, -1):
            entity = self.open_entities[index]
            if entity["tag"] != tag:
                continue

            self.open_entities.pop(index)
            length = self.position - entity["offset"]
            if length > 0:
                self.entities.append(
                    MessageEntity(
                        type=entity["type"],
                        offset=entity["offset"],
                        length=length,
                        **entity["kwargs"],
                    )
                )
            return

    def close_open_entities(self) -> None:
        while self.open_entities:
            entity = self.open_entities.pop()
            length = self.position - entity["offset"]
            if length > 0:
                self.entities.append(
                    MessageEntity(
                        type=entity["type"],
                        offset=entity["offset"],
                        length=length,
                        **entity["kwargs"],
                    )
                )

    def get_text_and_entities(self) -> tuple[str, list[MessageEntity]]:
        self.close_open_entities()
        text = "".join(self.parts)
        entities = MessageEntity.adjust_message_entities_to_utf_16(text, self.entities)
        return text, list(entities)


def build_caption_with_entities(filename: str) -> tuple[str, list[MessageEntity]]:
    parser = CaptionHTMLParser()
    parser.feed(build_caption(filename))
    parser.close()
    return parser.get_text_and_entities()


def extract_embedded_cover(filepath: Path) -> bytes | None:
    """Пытается вытащить обложку, встроенную в сам аудиофайл (ID3 APIC,
    FLAC PICTURE, MP4 covr и т.д.) с помощью mutagen."""
    if MutagenFile is None:
        logger.warning("mutagen не установлен — не могу читать встроенные обложки")
        return None

    try:
        audio = MutagenFile(filepath)
    except Exception:
        logger.warning("mutagen не смог открыть %s", filepath.name, exc_info=True)
        return None

    if audio is None:
        logger.info("mutagen не распознал формат файла %s", filepath.name)
        return None

    # ID3 (mp3): картинки лежат в tags как APIC:*
    tags = getattr(audio, "tags", None)
    if tags is not None:
        for key in tags.keys():
            if str(key).startswith("APIC"):
                try:
                    return tags[key].data
                except Exception:
                    pass

    # FLAC: список Picture-объектов
    pictures = getattr(audio, "pictures", None)
    if pictures:
        try:
            return pictures[0].data
        except Exception:
            pass

    # MP4/M4A: обложка лежит в audio.tags["covr"]
    if tags is not None and "covr" in tags:
        try:
            return bytes(tags["covr"][0])
        except Exception:
            pass

    logger.info("В файле %s нет встроенной обложки (ни APIC, ни PICTURE, ни covr)", filepath.name)
    return None


def get_thumbnail_bytes(filepath: Path) -> bytes | None:
    """Достаёт обложку, встроенную в аудиофайл, и приводит её к формату,
    который Telegram реально принимает как thumbnail:
    JPEG, стороны <= 320px, размер файла <= 200KB.
    Без этого Telegram молча отбрасывает картинку (обычно вшитые обложки
    намного крупнее лимита), из-за чего тамбнейлы не показывались."""
    raw = extract_embedded_cover(filepath)
    if raw is None:
        return None

    if Image is None:
        logger.warning(
            "Pillow не установлен — не могу сжать обложку до валидного thumbnail. "
            "Установи: pip install Pillow"
        )
        return None

    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((320, 320))

        quality = 85
        while quality >= 30:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= 200 * 1024:
                logger.info(
                    "Обложка для %s сжата до %d байт (quality=%d)",
                    filepath.name, len(data), quality,
                )
                return data
            quality -= 10

        logger.info(
            "Обложка для %s сжата не идеально, %d байт (лимит 200KB)",
            filepath.name, len(data),
        )
        return data  # лучшее, что получилось, даже если чуть больше лимита
    except Exception:
        logger.warning("Не удалось обработать встроенную обложку в %s", filepath.name, exc_info=True)
        return None


# ------------------------------- Постинг ------------------------------------

async def post_track(context: ContextTypes.DEFAULT_TYPE, filename: str) -> bool:
    """Публикует один трек по имени файла. Возвращает True, если успешно."""
    filepath = TRACKS_DIR / filename
    if not filepath.exists():
        logger.warning("Файл не найден: %s", filepath)
        return False

    caption, caption_entities = build_caption_with_entities(filename)
    thumb_bytes = get_thumbnail_bytes(filepath)
    thumbnail = None
    if thumb_bytes:
        thumbnail = io.BytesIO(thumb_bytes)
        thumbnail.name = "thumbnail.jpg"
        logger.info("Отправляю %s с thumbnail (%d байт)", filename, len(thumb_bytes))
    else:
        logger.info("Отправляю %s БЕЗ thumbnail", filename)

    with open(filepath, "rb") as audio_file:
        await context.bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=audio_file,
            caption=caption,
            caption_entities=caption_entities,
            thumbnail=thumbnail,
        )
    return True


async def post_next_from_queue(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Берёт следующий трек из очереди, публикует, обновляет состояние.
    Возвращает имя опубликованного файла или None, если очередь пуста."""
    state = load_state()
    state = sync_queue_with_folder(state)

    if not state["queue"]:
        save_state(state)
        return None

    track = state["queue"].pop(0)
    ok = await post_track(context, track)

    if ok:
        state["posted"].append(track)
    else:
        # файл пропал/битый — просто выкидываем его из очереди, не постим
        pass

    save_state(state)
    return track if ok else None


# ------------------------------ Планировщик ---------------------------------

async def scheduled_post_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    track = await post_next_from_queue(context)
    if track is None:
        logger.info("Автопостинг: очередь пуста, постить нечего.")
    else:
        logger.info("Автопостинг: опубликован %s", track)


def parse_hhmm(raw: str) -> str:
    """Парсит строку вида '18:30' и возвращает нормализованную 'HH:MM'
    либо бросает ValueError, если формат неверный."""
    hh_str, mm_str = raw.split(":")
    hh, mm = int(hh_str), int(mm_str)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Неверное время: {raw}")
    return f"{hh:02d}:{mm:02d}"


def reschedule_daily_jobs(app: Application, times: list[str]) -> None:
    """Полностью пересобирает автопостинг-задачи под список времён 'HH:MM'.
    Если times пуст — автопостинг отключается (0 раз в день)."""
    # убираем все старые задачи автопостинга
    for job in app.job_queue.jobs():
        if job.name and job.name.startswith("daily_post"):
            job.schedule_removal()

    for i, t in enumerate(times):
        hh, mm = map(int, t.split(":"))
        app.job_queue.run_daily(
            scheduled_post_job,
            time=dtime(hour=hh, minute=mm),
            name=f"daily_post_{i}",
        )


# -------------------------------- Хендлеры -----------------------------------

def is_allowed(update: Update) -> bool:
    """Проверяет, что команду прислал владелец (если OWNER_ID задан)."""
    if OWNER_ID is None:
        return True
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Этот бот приватный.")
        return
    await update.message.reply_text(
        "Привет! Я бот для постинга треков.\n\n"
        "/queue — показать очередь\n"
        "/shuffle — перемешать очередь\n"
        "/remove <точное имя файла> — убрать конкретный трек из очереди "
        "(имя бери как в /queue)\n"
        "/post_now — выложить следующий трек сейчас\n"
        "/time HH:MM [HH:MM ...] — задать время(на) автопостинга "
        "(можно несколько раз в день, например /time 09:00 18:30)\n"
        "/time off — отключить автопостинг\n"
        "/status — статус и настройки\n\n"
        "Просто пришли мне аудиофайл (как музыку или как документ) — "
        "добавлю его в очередь на публикацию."
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    state = load_state()
    state = sync_queue_with_folder(state)
    save_state(state)

    if not state["queue"]:
        await update.message.reply_text("Очередь пуста.")
        return

    lines = [f"{i+1}. {name}" for i, name in enumerate(state["queue"])]
    header = f"Текущая очередь ({len(lines)}):\n"

    # Telegram ограничивает длину одного сообщения (~4096 символов) —
    # показываем ВСЮ очередь, но при необходимости разбиваем на несколько сообщений.
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await update.message.reply_text(chunk.rstrip())
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk.rstrip())


async def cmd_shuffle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    state = load_state()
    state = sync_queue_with_folder(state)
    random.shuffle(state["queue"])
    save_state(state)
    await update.message.reply_text("Очередь перемешана 🔀")


async def cmd_post_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    track = await post_next_from_queue(context)
    if track is None:
        await update.message.reply_text("Очередь пуста, постить нечего.")
    else:
        await update.message.reply_text(f"Опубликовано: {track}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Укажи точное имя файла, как оно показано в /queue.\n"
            "Пример: /remove Artist - Title.mp3"
        )
        return

    name = " ".join(context.args).strip()

    state = load_state()
    state = sync_queue_with_folder(state)

    if name not in state["queue"]:
        # ищем похожие варианты, чтобы подсказать, если опечатался
        close = [t for t in state["queue"] if name.lower() in t.lower()]
        if close:
            suggestion = "\n\nПохожие треки в очереди:\n" + "\n".join(f"- {t}" for t in close[:10])
        else:
            suggestion = ""
        await update.message.reply_text(
            f"Трек «{name}» не найден в очереди (проверь точное написание, "
            f"как в /queue)." + suggestion
        )
        return

    state["queue"].remove(name)
    state.setdefault("removed", [])
    if name not in state["removed"]:
        state["removed"].append(name)
    save_state(state)

    await update.message.reply_text(f"Убрано из очереди: {name}")


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    if not context.args:
        state = load_state()
        times = state.get("schedule_times", [])
        if not times:
            current = "автопостинг отключён (0 раз в день)"
        else:
            current = f"{len(times)} раз(а) в день, в: {', '.join(times)}"
        await update.message.reply_text(
            f"Текущее расписание: {current}\n\n"
            "Как задать:\n"
            "/time 18:30 — 1 раз в день\n"
            "/time 12:00 20:00 — 2 раза в день\n"
            "/time 09:00 15:00 21:00 — 3 раза в день\n"
            "/time off — отключить автопостинг (0 раз в день)"
        )
        return

    if len(context.args) == 1 and context.args[0].lower() in ("off", "0"):
        state = load_state()
        state["schedule_times"] = []
        save_state(state)
        reschedule_daily_jobs(context.application, [])
        await update.message.reply_text("Автопостинг отключён (0 раз в день).")
        return

    times: list[str] = []
    for raw in context.args:
        try:
            times.append(parse_hhmm(raw))
        except ValueError:
            await update.message.reply_text(
                f"Неверный формат времени: '{raw}'. Пример: /time 09:00 18:30"
            )
            return

    # убираем дубликаты и сортируем по времени
    times = sorted(set(times))

    state = load_state()
    state["schedule_times"] = times
    save_state(state)

    reschedule_daily_jobs(context.application, times)

    await update.message.reply_text(
        f"Готово! Теперь бот будет постить {len(times)} раз(а) в день, в: "
        f"{', '.join(times)}."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    state = load_state()
    state = sync_queue_with_folder(state)
    save_state(state)

    times = state.get("schedule_times", [])
    if not times:
        schedule = "отключён (0 раз в день)"
    else:
        schedule = f"{len(times)} раз(а) в день, в: {', '.join(times)}"

    text = (
        f"Треков в очереди: {len(state['queue'])}\n"
        f"Уже опубликовано: {len(state['posted'])}\n"
        f"Автопостинг: {schedule}\n"
        f"Канал для постинга: {CHANNEL_ID}\n"
        f"Папка с треками: {TRACKS_DIR}"
    )
    await update.message.reply_text(text)


# --------------------------- Приём треков в лички ----------------------------

def unique_track_path(filename: str) -> Path:
    """Возвращает путь в TRACKS_DIR для filename, добавляя суффикс _1, _2...
    если файл с таким именем уже есть (чтобы не перезаписать существующий трек)."""
    candidate = TRACKS_DIR / filename
    if not candidate.exists():
        return candidate

    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = TRACKS_DIR / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


async def cmd_incoming_track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Владелец прислал аудиофайл прямо в чат с ботом — сохраняем в TRACKS_DIR
    и добавляем в очередь на публикацию."""
    if not is_allowed(update):
        return

    message = update.message
    tg_file = message.audio or message.document
    if tg_file is None:
        return

    filename = tg_file.file_name or f"{tg_file.file_unique_id}.mp3"
    ext = Path(filename).suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        await message.reply_text(
            f"Это не похоже на аудиофайл ({ext or 'без расширения'}). "
            f"Поддерживаются: {', '.join(sorted(AUDIO_EXTENSIONS))}"
        )
        return

    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    dest = unique_track_path(filename)

    try:
        file = await tg_file.get_file()
        await file.download_to_drive(custom_path=dest)
    except Exception:
        logger.warning("Не удалось скачать присланный трек %s", filename, exc_info=True)
        await message.reply_text("Не получилось скачать файл, попробуй ещё раз.")
        return

    state = load_state()
    state = sync_queue_with_folder(state)
    save_state(state)

    position = state["queue"].index(dest.name) + 1 if dest.name in state["queue"] else len(state["queue"])
    await message.reply_text(
        f"Добавлено в очередь: {dest.name}\nПозиция в очереди: {position}"
    )


# ---------------------------------- main -------------------------------------

def main() -> None:
    if not TRACKS_DIR.exists():
        TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Создана папка для треков: %s", TRACKS_DIR)

    # инициализация/синхронизация состояния при старте
    state = load_state()
    state = sync_queue_with_folder(state)
    save_state(state)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("shuffle", cmd_shuffle))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("post_now", cmd_post_now))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.ALL, cmd_incoming_track))

    # восстановить расписание, если оно было задано раньше
    times = state.get("schedule_times", [])
    if times:
        reschedule_daily_jobs(app, times)
        logger.info("Восстановлено расписание автопостинга: %s", ", ".join(times))
    else:
        logger.info("Автопостинг отключён (0 раз в день).")

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
