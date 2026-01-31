import telebot
import os
import yt_dlp
import re
import time
import threading
import concurrent.futures
import requests
import json
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs, quote, unquote
from yandex_music import Client
from yandex_music.exceptions import UnauthorizedError, NetworkError
from telebot import types
import vk_api
from vk_api.audio import VkAudio
from vk_api.exceptions import VkApiError, ApiError

# --- 1. ЗАГРУЗКА КОНФИГУРАЦИИ ---
load_dotenv()
bot = telebot.TeleBot(os.environ.get('BOT_TOKEN'))

# Инициализация клиента Яндекс.Музыки
YM_TOKEN = os.environ.get('YANDEX_MUSIC_TOKEN')
ym_client = None
if YM_TOKEN:
    try:
        ym_client = Client(YM_TOKEN).init()
        print("✅ Клиент Яндекс.Музыки успешно инициализирован.")
    except UnauthorizedError:
        print("❌ Ошибка авторизации Яндекс.Музыки: неверный токен.")
    except NetworkError:
        print("⚠️  Ошибка сети при подключении к Яндекс.Музыке.")
    except Exception as e:
        print(f"⚠️  Неизвестная ошибка инициализации Яндекс.Музыки: {e}")

# --- Инициализация клиента VK через ручной токен ---
VK_MANUAL_TOKEN = os.environ.get('VK_MANUAL_TOKEN')
vk_audio = None
user_search_history = {}
ym_client_lock = threading.Lock()
vk_audio_lock = threading.Lock()


def init_vk_client():
    """Инициализирует VK клиент с ручным токеном из .env"""
    global vk_audio
    if VK_MANUAL_TOKEN:
        try:
            # Используем vk_api.VkApi без дополнительных параметров
            vk_session = vk_api.VkApi(token=VK_MANUAL_TOKEN)
            vk_audio = VkAudio(vk_session)
            print("✅ Клиент ВК Музыки успешно инициализирован (ручной токен).")
            return True
        except Exception as e:
            print(f"⚠️  Ошибка инициализации ВК Музыки: {e}")
            print(f"⚠️  Убедитесь, что токен VK_MANUAL_TOKEN в .env файле корректен и не истёк.")
    else:
        print("⚠️  Токен VK (VK_MANUAL_TOKEN) не указан в .env файле.")
    return False


# Первоначальная инициализация VK
init_vk_client()

# --- 2. ОБЩИЕ НАСТРОЙКИ И ФУНКЦИИ ---
AUDIO_CACHE_DIR = "audio_cache"
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)


def is_youtube_playlist(url):
    """Проверяет, является ли ссылка плейлистом YouTube"""
    try:
        parsed = urlparse(url)
        if 'youtube.com' in parsed.netloc or 'youtu.be' in parsed.netloc:
            query_params = parse_qs(parsed.query)
            if 'list' in query_params or 'playlist' in query_params:
                return True
    except:
        pass
    return False


# --- 3. ПОИСК В ЯНДЕКС.МУЗЫКЕ ---
def search_yandex_music(query, search_type="all", limit=15):
    """Ищет треки в Яндекс.Музыке."""
    if not ym_client:
        print("[Yandex] Клиент не настроен для поиска")
        return []

    try:
        print(f"[Yandex] Поиск: '{query}' (тип: {search_type})")
        search_result = ym_client.search(query, type_='track', page=0)

        if not search_result or not search_result.tracks:
            print(f"[Yandex] По запросу '{query}' ничего не найдено")
            return []

        tracks = search_result.tracks.results[:limit]
        print(f"[Yandex] Найдено {len(tracks)} треков по запросу '{query}'")

        formatted_results = []
        for track in tracks:
            try:
                title = track.title if hasattr(track, 'title') else ''

                if search_type == "artist":
                    artists = [artist.name for artist in track.artists] if hasattr(track,
                                                                                   'artists') and track.artists else []
                    if not any(query.lower() in artist.lower() for artist in artists):
                        continue
                elif search_type == "title":
                    if query.lower() not in title.lower():
                        continue

                artists_str = ', '.join(
                    [artist.name for artist in track.artists]) if track.artists else 'Неизвестный исполнитель'
                album_name = track.albums[0].title if track.albums else 'Неизвестный альбом'
                album_id = track.albums[0].id if track.albums else 0
                duration_ms = track.duration_ms if hasattr(track, 'duration_ms') else 0
                duration_str = f"{duration_ms // 60000}:{str((duration_ms % 60000) // 1000).zfill(2)}"

                formatted_results.append({
                    'title': title,
                    'artists': artists_str,
                    'album': album_name,
                    'track_id': track.id,
                    'album_id': album_id,
                    'duration': duration_str,
                    'track_obj': track,
                    'source': 'yandex'
                })

            except Exception as e:
                print(f"[Yandex] Ошибка форматирования трека: {e}")
                continue

        return formatted_results

    except Exception as e:
        print(f"[Yandex] Ошибка поиска: {e}")
        return []


# --- 4. ПОИСК В VK МУЗЫКЕ (ИСПРАВЛЕННЫЙ) ---
def search_vk_music(query, limit=15):
    """Ищет треки в VK Музыке с обработкой ошибок."""
    global vk_audio

    # Пропускаем служебные запросы
    if query in ['🔍 Поиск', 'Поиск', 'search', '']:
        print("[VK] Получен служебный запрос, пропускаю.")
        return []

    # Проверяем инициализацию клиента
    if not vk_audio:
        print("[VK] Клиент не инициализирован, пытаюсь инициализировать...")
        if not init_vk_client():
            print("[VK] Не удалось инициализировать клиент")
            return []

    try:
        print(f"[VK] Поиск: '{query}'")

        # Используем блокировку для потокобезопасности
        with vk_audio_lock:
            # Получаем итератор от search() и преобразуем его в список
            results_iter = vk_audio.search(q=query, count=limit)
            results = list(results_iter)  # Ключевое исправление здесь

        if not results:  # Теперь results - это обычный список
            print(f"[VK] По запросу '{query}' ничего не найдено")
            return []

        formatted_results = []
        for i, track in enumerate(results):
            try:
                # Безопасное получение данных из трека
                title = track.get('title', 'Без названия')
                artist = track.get('artist', 'Неизвестный исполнитель')
                duration = track.get('duration', 0)

                # Форматируем продолжительность
                minutes = duration // 60
                seconds = duration % 60
                duration_str = f"{minutes}:{str(seconds).zfill(2)}"

                # Получаем URL для прослушивания
                url = track.get('url')

                # Пропускаем треки без URL
                if not url:
                    continue

                formatted_results.append({
                    'index': i + 1,
                    'title': title,
                    'artist': artist,
                    'full_title': f"{artist} - {title}",
                    'duration': duration_str,
                    'url': url,
                    'track_id': track.get('id'),
                    'owner_id': track.get('owner_id'),
                    'source': 'vk'
                })

            except Exception as e:
                print(f"[VK] Ошибка форматирования трека {i}: {e}")
                continue

        print(f"[VK] Найдено {len(formatted_results)} треков по запросу '{query}'")
        return formatted_results

    except (VkApiError, ApiError) as e:
        # Обрабатываем ошибки API VK
        print(f"[VK] Ошибка API при поиске: {e}")

        # Если ошибка связана с авторизацией, сбрасываем клиент
        if "access" in str(e).lower() or "token" in str(e).lower() or "auth" in str(e).lower():
            print("[VK] Токен недействителен. Нужно обновить VK_MANUAL_TOKEN в .env файле.")
            vk_audio = None

        return []
    except Exception as e:
        print(f"[VK] Неизвестная ошибка поиска: {e}")
        return []


# --- 5. СКАЧИВАНИЕ И ОБРАБОТКА ССЫЛОК ---
def download_yandex_track_fast(track_id, album_id):
    """Скачивает трек из Яндекс.Музыки"""
    if not ym_client:
        return None, None, None, "Клиент Яндекс.Музыки не настроен."

    try:
        with ym_client_lock:
            tracks = ym_client.tracks([f"{track_id}:{album_id}"])

        if not tracks:
            return None, None, None, "Трек не найден."

        track = tracks[0]

        with ym_client_lock:
            download_info = track.get_download_info()

        if not download_info:
            return None, None, None, "Информация для скачивания недоступна."

        best_info = min(
            [info for info in download_info if info.codec == 'mp3'],
            key=lambda x: x.bitrate_in_kbps,
            default=None
        )

        if not best_info:
            best_info = download_info[0] if download_info else None
            if not best_info:
                return None, None, None, "Нет подходящего формата."

        safe_title = "".join([c for c in track.title if c.isalnum() or c in (' ', '-', '_')]).strip()
        safe_artists = "_".join([a.name for a in track.artists[:1]]) if track.artists else "Unknown"
        filename = f"{safe_artists} - {safe_title}.mp3"
        filepath = os.path.join(AUDIO_CACHE_DIR, filename)

        track.download(filepath, codec='mp3', bitrate_in_kbps=best_info.bitrate_in_kbps)
        return filepath, track.title, ", ".join(
            [a.name for a in track.artists]) if track.artists else "Unknown Artist", "success"

    except Exception as e:
        print(f"[Yandex] Ошибка скачивания: {e}")
        return None, None, None, f"Ошибка скачивания: {str(e)}"


def download_from_youtube_fast(query, is_url=False):
    """Скачивает аудио с YouTube"""
    ydl_opts = {
        'format': 'worstaudio/worst',
        'outtmpl': os.path.join(AUDIO_CACHE_DIR, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 10,
        'retries': 1,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',
        }],
        'default_search': 'ytsearch1:' if not is_url else None,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': True,
    }

    try:
        if is_url and is_youtube_playlist(query):
            return None, None, None, "playlist"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)

            if not info:
                return None, None, None, "no_info"

            if 'entries' in info:
                video = info['entries'][0] if info['entries'] else None
            else:
                video = info

            if not video:
                return None, None, None, "no_video"

            title = video.get('title', 'Без названия')
            uploader = video.get('uploader', 'Неизвестный автор')

            for file in os.listdir(AUDIO_CACHE_DIR):
                if file.endswith('.mp3'):
                    audio_path = os.path.join(AUDIO_CACHE_DIR, file)
                    new_name = f"{uploader[:20]} - {title[:30]}.mp3"
                    new_path = os.path.join(AUDIO_CACHE_DIR, new_name)
                    try:
                        os.rename(audio_path, new_path)
                        return new_path, title, uploader, "success"
                    except:
                        return audio_path, title, uploader, "success"

            return None, title, uploader, "no_file"

    except Exception as e:
        print(f"[!] Ошибка YouTube: {e}")
        return None, None, None, "error"


# --- 6. УНИВЕРСАЛЬНЫЙ ПОИСК ---
def unified_search(query, source="all", search_type="all", limit=10):
    """Универсальная функция поиска музыки."""
    results = []

    if source in ["all", "yandex"] and ym_client:
        yandex_results = search_yandex_music(query, search_type, limit)
        results.extend(yandex_results)

    if source in ["all", "vk"]:
        vk_results = search_vk_music(query, limit)
        results.extend(vk_results)

    for i, result in enumerate(results):
        result['global_index'] = i + 1

    return results


def show_search_results(chat_id, query, results, page=0):
    """Показывает результаты поиска"""
    if not results:
        return "❌ По вашему запросу ничего не найдено."

    user_search_history[chat_id] = {
        'query': query,
        'results': results,
        'timestamp': time.time()
    }

    start_idx = page * 5
    end_idx = start_idx + 5
    page_results = results[start_idx:end_idx]

    message_text = f"🔍 *Результаты поиска: '{query}'*\n\n"

    yandex_count = len([r for r in results if r.get('source') == 'yandex'])
    vk_count = len([r for r in results if r.get('source') == 'vk'])

    message_text += f"*Найдено:* {len(results)} треков (🎵 Яндекс: {yandex_count}, 🎧 ВК: {vk_count})\n"
    message_text += f"*Страница:* {page + 1}/{(len(results) + 4) // 5}\n\n"

    for track in page_results:
        idx = track.get('global_index', 0)
        title = track.get('title', 'Без названия')
        source_icon = "🎵" if track.get('source') == 'yandex' else "🎧"

        if track.get('source') == 'yandex':
            artists = track.get('artists', 'Неизвестный исполнитель')
            message_text += f"{idx}. {source_icon} *{title}*\n"
            message_text += f"   👤 {artists}\n"
        else:
            artist = track.get('artist', 'Неизвестный исполнитель')
            message_text += f"{idx}. {source_icon} *{title}*\n"
            message_text += f"   👤 {artist}\n"

        duration = track.get('duration', '0:00')
        message_text += f"   ⏱ {duration}\n\n"

    message_text += "Выберите трек для скачивания:"

    return message_text


def create_search_keyboard(results, page=0, results_per_page=5):
    """Создает инлайн-клавиатуру для результатов поиска"""
    markup = types.InlineKeyboardMarkup(row_width=2)

    start_idx = page * results_per_page
    end_idx = start_idx + results_per_page
    page_results = results[start_idx:end_idx]

    for track in page_results:
        source_icon = "🎵" if track.get('source') == 'yandex' else "🎧"
        btn_text = f"{source_icon} {track.get('global_index', 0)}. {track.get('title', 'Трек')[:15]}..."
        if track.get('source') == 'yandex':
            btn_data = f"dl_yandex_{track.get('track_id', 0)}_{track.get('album_id', 0)}_{page}"
        else:
            # Для VK отправляем информацию о треке
            url_encoded = quote(track.get('url', ''), safe='')
            btn_data = f"info_vk_{track.get('track_id', 0)}_{track.get('owner_id', 0)}_{url_encoded[:100]}_{page}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=btn_data))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))

    if end_idx < len(results):
        nav_buttons.append(types.InlineKeyboardButton("Вперед ▶️", callback_data=f"page_{page + 1}"))

    if nav_buttons:
        markup.add(*nav_buttons)

    filter_buttons = [
        types.InlineKeyboardButton("🔄 Новый поиск", callback_data="new_search"),
        types.InlineKeyboardButton("🎵 Только Яндекс", callback_data="filter_yandex"),
        types.InlineKeyboardButton("🎧 Только ВК", callback_data="filter_vk")
    ]

    markup.add(*filter_buttons)

    return markup


# --- 7. ОБРАБОТЧИКИ КОМАНД TELEGRAM ---

# Новые команды для проверки статуса
@bot.message_handler(commands=['status', 'check_vk', 'check'])
def handle_status(message):
    """Показывает статус подключения к сервисам"""
    status_text = "📊 *Статус подключений бота*\n\n"

    if ym_client:
        try:
            account_info = ym_client.me.account_status()
            status_text += "✅ *Яндекс.Музыка*: Авторизован\n"
        except:
            status_text += "❌ *Яндекс.Музыка*: Ошибка авторизации\n"
    else:
        status_text += "⚠️  *Яндекс.Музыка*: Токен не указан\n"

    if vk_audio:
        status_text += "✅ *ВК Музыка*: Клиент инициализирован\n"
    else:
        status_text += "❌ *ВК Музыка*: Клиент не инициализирован\n"
        if VK_MANUAL_TOKEN:
            status_text += "   Токен указан, но возможно он недействителен или истёк\n"
        else:
            status_text += "   Токен не указан в .env файле\n"

    status_text += "\n*Проверка работы:*\n"
    status_text += "• `/search_vk тест` - проверить поиск в ВК\n"
    status_text += "• `/search_yandex тест` - проверить Яндекс\n"

    bot.reply_to(message, status_text, parse_mode='Markdown')


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_liked = types.KeyboardButton('🎵 Мне понравилось')
    btn_search = types.KeyboardButton('🔍 Поиск музыки')
    btn_vk = types.KeyboardButton('🎧 ВК музыка')
    btn_help = types.KeyboardButton('📋 Помощь')
    keyboard.row(btn_liked, btn_search)
    keyboard.row(btn_vk, btn_help)

    welcome_text = (
        "🎵 *Универсальный музыкальный бот* 🎵\n\n"
        "⚡ *Полная интеграция Яндекс и ВК музыки!*\n\n"
        "*Что умеет бот:*\n"
        "• Скачивать треки из *YouTube* (по названию или ссылке)\n"
        "• Скачивать треки из *Яндекс.Музыки* (по ссылке или через поиск)\n"
        "• 🔍 *Искать и скачивать треки из Яндекс.Музыки*\n"
        "• 🎧 *Искать треки из ВК Музыки*\n"
        "• 📥 Скачивать треки из 'Мне понравилось' (в разработке)\n\n"
        "*Основные команды:*\n"
        "• `/search <запрос>` - поиск во всех источниках\n"
        "• `/search_yandex <запрос>` - поиск только в Яндекс.Музыке\n"
        "• `/search_vk <запрос>` - поиск только в ВК Музыке\n"
        "• `/search_artist <исполнитель>` - поиск по исполнителю\n"
        "• `/search_title <название>` - поиск по названию трека\n"
        "• `/status` - проверка подключений\n"
        "• `/get_vk_token` - инструкция по получению токена VK\n"
        "• `/help` - это сообщение\n\n"
        "*Важно:* Скачивание из VK временно не работает, но поиск доступен!\n\n"
        "*Примеры:*\n"
        "• `/search Би-2 Полковник`\n"
        "• `/search_vk Мальчик на драйве`\n"
        "• `/status` - проверить подключения\n\n"
        "⚠️ Плейлисты YouTube не поддерживаются."
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown',
                 disable_web_page_preview=True, reply_markup=keyboard)


# Команда для получения инструкции по токену VK
@bot.message_handler(commands=['get_vk_token', 'token'])
def handle_get_token(message):
    """Инструкция по получению токена VK"""
    token_instructions = (
        "🔑 *Как получить токен VK:*\n\n"
        "1. *Откройте браузер* и войдите в свой аккаунт VK\n"
        "2. *Перейдите по ссылке* (подставьте свой CLIENT_ID):\n"
        "`https://oauth.vk.com/authorize?client_id=ВАШ_CLIENT_ID&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=audio,offline&response_type=token&v=5.199&state=123456`\n\n"
        "3. *Разрешите доступ* приложению к аудиозаписям\n"
        "4. *Скопируйте токен* из адресной строки:\n"
        "После авторизации вас перенаправит на страницу с URL вида:\n"
        "`https://oauth.vk.com/blank.html#access_token=ВАШ_ТОКЕН&...`\n"
        "Скопируйте всё после `access_token=` и до следующего `&`\n\n"
        "5. *Вставьте токен* в файл `.env` как значение `VK_MANUAL_TOKEN`\n\n"
        "*Где взять CLIENT_ID:*\n"
        "1. Создайте приложение на https://vk.com/editapp?act=create\n"
        "2. Выберите тип 'Standalone'\n"
        "3. В настройках приложения скопируйте 'ID приложения'\n\n"
        "*Важно:* Токен действует несколько месяцев. При ошибках поиска обновите токен."
    )
    bot.reply_to(message, token_instructions, parse_mode='Markdown',
                 disable_web_page_preview=True)


@bot.message_handler(commands=['search'])
def handle_search_all(message):
    query = message.text.replace('/search', '').strip()

    if not query:
        bot.reply_to(message, "📝 Использование: `/search <запрос>`", parse_mode='Markdown')
        return

    wait_msg = bot.reply_to(message, f"🔍 Ищу '{query}' во всех источниках...")

    results = unified_search(query, source="all", limit=10)

    if not results:
        bot.edit_message_text(f"❌ По запросу '{query}' ничего не найдено.",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        return

    message_text = show_search_results(message.chat.id, query, results, page=0)
    keyboard = create_search_keyboard(results, page=0)

    bot.edit_message_text(message_text,
                          chat_id=message.chat.id,
                          message_id=wait_msg.message_id,
                          parse_mode='Markdown',
                          reply_markup=keyboard)


@bot.message_handler(commands=['search_yandex'])
def handle_search_yandex(message):
    if not ym_client:
        bot.reply_to(message, "❌ Клиент Яндекс.Музыки не настроен. Укажите YANDEX_MUSIC_TOKEN в .env")
        return

    query = message.text.replace('/search_yandex', '').strip()

    if not query:
        bot.reply_to(message, "📝 Использование: `/search_yandex <запрос>`", parse_mode='Markdown')
        return

    wait_msg = bot.reply_to(message, f"🎵 Ищу '{query}' в Яндекс.Музыке...")

    results = unified_search(query, source="yandex", limit=15)

    if not results:
        bot.edit_message_text(f"❌ По запросу '{query}' ничего не найдено.",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        return

    message_text = show_search_results(message.chat.id, query, results, page=0)
    keyboard = create_search_keyboard(results, page=0)

    bot.edit_message_text(message_text,
                          chat_id=message.chat.id,
                          message_id=wait_msg.message_id,
                          parse_mode='Markdown',
                          reply_markup=keyboard)


@bot.message_handler(commands=['search_vk'])
def handle_search_vk(message):
    if not VK_MANUAL_TOKEN:
        bot.reply_to(message,
                     "❌ Токен VK не указан.\n\n"
                     "Добавьте VK_MANUAL_TOKEN в файл .env\n"
                     "Используйте /get_vk_token для инструкции",
                     parse_mode='Markdown')
        return

    query = message.text.replace('/search_vk', '').strip()

    if not query:
        bot.reply_to(message, "📝 Использование: `/search_vk <запрос>`", parse_mode='Markdown')
        return

    wait_msg = bot.reply_to(message, f"🎧 Ищу '{query}' в ВК Музыке...")

    # Проверяем инициализацию клиента
    if not vk_audio:
        bot.edit_message_text("🔄 Инициализирую клиент VK...",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        if not init_vk_client():
            bot.edit_message_text("❌ Не удалось инициализировать клиент VK.\n"
                                  "Проверьте токен в .env файле и используйте /status",
                                  chat_id=message.chat.id,
                                  message_id=wait_msg.message_id)
            return

    results = search_vk_music(query, limit=15)

    if not results:
        bot.edit_message_text(f"❌ По запросу '{query}' ничего не найдено.",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        return

    message_text = show_search_results(message.chat.id, query, results, page=0)
    keyboard = create_search_keyboard(results, page=0)

    bot.edit_message_text(message_text,
                          chat_id=message.chat.id,
                          message_id=wait_msg.message_id,
                          parse_mode='Markdown',
                          reply_markup=keyboard)


@bot.message_handler(commands=['search_artist'])
def handle_search_artist(message):
    if not ym_client:
        bot.reply_to(message, "❌ Клиент Яндекс.Музыки не настроен.")
        return

    query = message.text.replace('/search_artist', '').strip()

    if not query:
        bot.reply_to(message, "📝 Использование: `/search_artist <исполнитель>`", parse_mode='Markdown')
        return

    wait_msg = bot.reply_to(message, f"👤 Ищу исполнителя '{query}'...")

    results = unified_search(query, source="yandex", search_type="artist", limit=15)

    if not results:
        bot.edit_message_text(f"❌ Исполнитель '{query}' не найден.",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        return

    message_text = show_search_results(message.chat.id, f"исполнитель: {query}", results, page=0)
    keyboard = create_search_keyboard(results, page=0)

    bot.edit_message_text(message_text,
                          chat_id=message.chat.id,
                          message_id=wait_msg.message_id,
                          parse_mode='Markdown',
                          reply_markup=keyboard)


@bot.message_handler(commands=['search_title'])
def handle_search_title(message):
    if not ym_client:
        bot.reply_to(message, "❌ Клиент Яндекс.Музыки не настроен.")
        return

    query = message.text.replace('/search_title', '').strip()

    if not query:
        bot.reply_to(message, "📝 Использование: `/search_title <название трека>`", parse_mode='Markdown')
        return

    wait_msg = bot.reply_to(message, f"💿 Ищу трек '{query}'...")

    results = unified_search(query, source="yandex", search_type="title", limit=15)

    if not results:
        bot.edit_message_text(f"❌ Трек '{query}' не найден.",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
        return

    message_text = show_search_results(message.chat.id, f"трек: {query}", results, page=0)
    keyboard = create_search_keyboard(results, page=0)

    bot.edit_message_text(message_text,
                          chat_id=message.chat.id,
                          message_id=wait_msg.message_id,
                          parse_mode='Markdown',
                          reply_markup=keyboard)


@bot.message_handler(func=lambda message: message.text == '🎵 Мне понравилось')
def handle_liked_button(message):
    bot.reply_to(message, "🎵 *Мне понравилось*\n\n"
                          "Эта функция в разработке.\n"
                          "Скоро можно будет скачивать треки из вашего плейлиста 'Мне понравилось' Яндекс.Музыки.",
                 parse_mode='Markdown')


@bot.message_handler(func=lambda message: message.text == '🔍 Поиск музыки')
def handle_search_button(message):
    bot.reply_to(message,
                 "🔍 *Поиск музыки*\n\n"
                 "Выберите тип поиска:\n"
                 "• `/search <запрос>` - поиск везде\n"
                 "• `/search_yandex <запрос>` - только Яндекс\n"
                 "• `/search_vk <запрос>` - только ВК\n"
                 "• `/search_artist <исполнитель>` - по исполнителю\n"
                 "• `/search_title <название>` - по названию\n"
                 "• `/status` - проверить подключения\n\n"
                 "*Пример:* `/search Би-2 Полковник`",
                 parse_mode='Markdown')


@bot.message_handler(func=lambda message: message.text == '🎧 ВК музыка')
def handle_vk_button(message):
    status = "✅ Активен" if vk_audio else "❌ Не активен"
    bot.reply_to(message,
                 f"🎧 *ВК Музыка*\n\n"
                 f"Статус: {status}\n\n"
                 "Для поиска музыки в ВК используйте команды:\n"
                 "• `/search_vk <запрос>` - поиск в ВК\n"
                 "• `/status` - детальная проверка подключения\n"
                 "• `/get_vk_token` - инструкция по получению токена\n\n"
                 "*Пример:* `/search_vk Мальчик на драйве`",
                 parse_mode='Markdown')


@bot.message_handler(func=lambda message: message.text == '📋 Помощь')
def handle_help_button(message):
    send_welcome(message)


# Обработка ссылок на музыку
@bot.message_handler(func=lambda m: m.text and any(x in m.text for x in ['music.yandex', 'youtube.com', 'youtu.be']))
def handle_music_link(message):
    """Обрабатывает прямые ссылки на музыку"""
    wait_msg = bot.reply_to(message, "🔗 Анализирую ссылку...")
    url = message.text.strip()

    # Упрощенная обработка ссылок
    if 'music.yandex' in url:
        import re
        match = re.search(r'music\.yandex\.\w+/album/(\d+)/track/(\d+)', url)
        if match:
            album_id, track_id = match.groups()
            audio_path, title, performer, status = download_yandex_track_fast(int(track_id), int(album_id))
            if status == "success" and audio_path:
                with open(audio_path, 'rb') as audio_file:
                    bot.send_audio(
                        chat_id=message.chat.id,
                        audio=audio_file,
                        title=title[:64],
                        performer=performer[:64],
                        caption=f"🎵 {title} (Яндекс.Музыка)",
                        timeout=60
                    )
                try:
                    os.remove(audio_path)
                except:
                    pass
                bot.delete_message(message.chat.id, wait_msg.message_id)
                return
        bot.edit_message_text(f"❌ Не удалось обработать Яндекс-ссылку",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)
    elif 'youtube.com' in url or 'youtu.be' in url:
        audio_path, title, performer, status = download_from_youtube_fast(url, is_url=True)
        if status == "success" and audio_path:
            with open(audio_path, 'rb') as audio_file:
                bot.send_audio(
                    chat_id=message.chat.id,
                    audio=audio_file,
                    title=title[:64],
                    performer=performer[:64],
                    caption=f"🎵 {title} (YouTube)",
                    timeout=60
                )
            try:
                os.remove(audio_path)
            except:
                pass
            bot.delete_message(message.chat.id, wait_msg.message_id)
        else:
            bot.edit_message_text(f"❌ Ошибка загрузки с YouTube: {status}",
                                  chat_id=message.chat.id,
                                  message_id=wait_msg.message_id)
    else:
        bot.edit_message_text(f"❌ Формат ссылки не поддерживается или временно не работает",
                              chat_id=message.chat.id,
                              message_id=wait_msg.message_id)


# Обработка inline-кнопок
@bot.callback_query_handler(
    func=lambda call: call.data.startswith(('dl_', 'page_', 'filter_', 'new_search', 'info_vk')))
def handle_search_callback(call):
    """Обрабатывает все callback-запросы от поиска"""
    try:
        chat_id = call.message.chat.id

        if call.data == 'new_search':
            bot.answer_callback_query(call.id, "Введите новый поисковый запрос")
            bot.edit_message_text("🔍 Введите новый поисковый запрос:\n\n"
                                  "• `/search <запрос>` - поиск везде\n"
                                  "• `/search_yandex <запрос>` - только Яндекс\n"
                                  "• `/search_vk <запрос>` - только ВК\n"
                                  "• `/search_artist <исполнитель>` - по исполнителю\n"
                                  "• `/search_title <название>` - по названию",
                                  chat_id=chat_id,
                                  message_id=call.message.message_id,
                                  parse_mode='Markdown')
            return

        elif call.data.startswith('filter_'):
            filter_type = call.data.replace('filter_', '')
            bot.answer_callback_query(call.id, f"Применяю фильтр: {filter_type}")

            if chat_id not in user_search_history:
                return

            history = user_search_history[chat_id]
            query = history['query']
            all_results = history['results']

            if filter_type == 'yandex':
                filtered_results = [r for r in all_results if r.get('source') == 'yandex']
            elif filter_type == 'vk':
                filtered_results = [r for r in all_results if r.get('source') == 'vk']
            else:
                filtered_results = all_results

            if not filtered_results:
                bot.edit_message_text(f"❌ Нет результатов с фильтром '{filter_type}'",
                                      chat_id=chat_id,
                                      message_id=call.message.message_id)
                return

            for i, result in enumerate(filtered_results):
                result['global_index'] = i + 1

            user_search_history[chat_id]['results'] = filtered_results

            message_text = show_search_results(chat_id, query, filtered_results, page=0)
            keyboard = create_search_keyboard(filtered_results, page=0)

            bot.edit_message_text(message_text,
                                  chat_id=chat_id,
                                  message_id=call.message.message_id,
                                  parse_mode='Markdown',
                                  reply_markup=keyboard)
            return

        elif call.data.startswith('page_'):
            page = int(call.data.split('_')[1])

            if chat_id not in user_search_history:
                bot.answer_callback_query(call.id, "❌ Результаты поиска устарели")
                return

            history = user_search_history[chat_id]
            query = history['query']
            results = history['results']

            message_text = show_search_results(chat_id, query, results, page=page)
            keyboard = create_search_keyboard(results, page=page)

            bot.edit_message_text(message_text,
                                  chat_id=chat_id,
                                  message_id=call.message.message_id,
                                  parse_mode='Markdown',
                                  reply_markup=keyboard)
            bot.answer_callback_query(call.id)
            return

        elif call.data.startswith('dl_yandex'):
            parts = call.data.split('_')
            track_id = int(parts[2])
            album_id = int(parts[3])
            page = int(parts[4]) if len(parts) > 4 else 0

            bot.answer_callback_query(call.id, "⏳ Скачиваю...")
            bot.edit_message_text("⏳ Скачиваю трек из Яндекс.Музыки...",
                                  chat_id=chat_id,
                                  message_id=call.message.message_id)

            audio_path, title, performer, status = download_yandex_track_fast(track_id, album_id)

            if audio_path and os.path.exists(audio_path):
                with open(audio_path, 'rb') as audio_file:
                    bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=title[:64] if title else "Трек",
                        performer=performer[:64] if performer else None,
                        caption=f"🎵 {title} (Яндекс.Музыка)",
                        timeout=60
                    )

                try:
                    os.remove(audio_path)
                except:
                    pass

                if chat_id in user_search_history:
                    history = user_search_history[chat_id]
                    query = history['query']
                    results = history['results']

                    message_text = show_search_results(chat_id, query, results, page=page)
                    keyboard = create_search_keyboard(results, page=page)

                    bot.edit_message_text(f"✅ Трек '{title}' скачан!\n\n" + message_text,
                                          chat_id=chat_id,
                                          message_id=call.message.message_id,
                                          parse_mode='Markdown',
                                          reply_markup=keyboard)
                else:
                    bot.edit_message_text(f"✅ Трек '{title}' успешно скачан!",
                                          chat_id=chat_id,
                                          message_id=call.message.message_id)
            else:
                bot.edit_message_text(f"❌ Ошибка скачивания: {status}",
                                      chat_id=chat_id,
                                      message_id=call.message.message_id)

        elif call.data.startswith('info_vk'):
            parts = call.data.split('_')
            if len(parts) >= 6:
                track_id = parts[2]
                owner_id = parts[3]
                url_encoded = parts[4]
                page = parts[5] if len(parts) > 5 else 0

                # Декодируем URL
                url = unquote(url_encoded) if url_encoded else ""

                bot.answer_callback_query(call.id, "ℹ️  Информация о треке VK")

                info_text = (
                    f"🎧 *Трек из VK*\n\n"
                    f"Скачивание треков из VK через бота временно не работает.\n"
                    f"Вы можете прослушать этот трек по ссылке:\n\n"
                )

                if url:
                    info_text += f"[Ссылка для прослушивания]({url})\n\n"

                info_text += (
                    f"*ID трека:* `{track_id}`\n"
                    f"*ID владельца:* `{owner_id}`\n\n"
                    f"_Ссылка действительна ограниченное время_"
                )

                bot.send_message(chat_id, info_text, parse_mode='Markdown',
                                 disable_web_page_preview=False if url else True)
            return

    except Exception as e:
        print(f"[!] Ошибка обработки callback: {e}")
        try:
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:50]}")
        except:
            pass


# --- ЗАПУСК БОТА ---
if __name__ == '__main__':
    print("=" * 60)
    print("🤖 ТЕЛЕГРАМ-БОТ ЗАПУЩЕН! (Упрощённая версия с ручным токеном VK)")
    print("=" * 60)
    print(f"📁 Папка кэша: {os.path.abspath(AUDIO_CACHE_DIR)}")

    if ym_client:
        try:
            account_info = ym_client.me.account_status()
            print(f"✅ Яндекс.Музыка: Авторизован как {account_info.account.login}")
        except:
            print("✅ Яндекс.Музыка: Модуль активен")
    else:
        print("⚠️  Яндекс.Музыка: Модуль отключен (добавьте YANDEX_MUSIC_TOKEN в .env)")

    if vk_audio:
        print("✅ ВК Музыка: Модуль активен (ручной токен)")
    else:
        print("⚠️  ВК Музыка: Модуль отключен (добавьте VK_MANUAL_TOKEN в .env)")
        if VK_MANUAL_TOKEN:
            print("   Токен указан, но клиент не инициализирован. Проверьте токен.")

    print("🎬 YouTube: Модуль активен")
    print("=" * 60)
    print("ℹ️  Используйте команды в боте:")
    print("   /status - проверка подключений")
    print("   /get_vk_token - инструкция по получению токена VK")
    print("   /search_vk тест - проверить поиск в VK")
    print("=" * 60)

    try:
        bot.infinity_polling(timeout=120, long_polling_timeout=60)
    except Exception as e:
        print(f"❌ Критическая ошибка бота: {e}")
        print("Проверьте токены в .env файле и перезапустите бота.")