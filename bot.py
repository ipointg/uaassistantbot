import asyncio
import logging
import json
import os
import re
import random
import telegram
import numpy as np
import ollama
import requests
import feedparser
import yt_dlp
from dotenv import load_dotenv
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

load_dotenv()
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import filters, MessageHandler, ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, ConversationHandler
from difflib import SequenceMatcher

WAITING_ARTIST_NAME = 1
WAITING_CATEGORY = 2
WAITING_YOUTUBE_URL = 3


def _load_json(path: str, default_factory):
    if os.path.exists(path):
        with open(path, encoding='utf8') as f:
            return json.load(f)
    return default_factory()


def _save_json(path: str, data):
    with open(path, 'w', encoding='utf8') as f:
        json.dump(list(data) if isinstance(data, set) else data, f, ensure_ascii=False)

USER_ARTISTS_FILE = './user_artists.json'
SENT_RELEASES_FILE = './sent_releases.json'


def load_user_artists() -> dict:
    return _load_json(USER_ARTISTS_FILE, dict)

def save_user_artists(data: dict):
    _save_json(USER_ARTISTS_FILE, data)

def load_sent_releases() -> dict:
    return _load_json(SENT_RELEASES_FILE, dict)

def save_sent_releases(data: dict):
    _save_json(SENT_RELEASES_FILE, data)


user_artists = load_user_artists()
sent_releases = load_sent_releases()


def search_artist(name: str) -> dict | None:
    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': name, 'media': 'music', 'entity': 'musicArtist', 'limit': 1, 'country': 'ua'},
            timeout=10,
        )
        results = r.json().get('results', [])
        return results[0] if results else None
    except Exception:
        return None


def get_latest_releases(artist_id: int) -> list:
    try:
        r = requests.get(
            'https://itunes.apple.com/lookup',
            params={'id': artist_id, 'entity': 'album', 'limit': 5, 'sort': 'recent', 'country': 'ua'},
            timeout=10,
        )
        results = r.json().get('results', [])
        return [x for x in results if x.get('wrapperType') == 'collection']
    except Exception:
        return []


def build_top_text() -> str:
    from datetime import datetime, timedelta
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()

    artist_count: dict[str, int] = {}
    artist_week: dict[str, int] = {}
    for artists in user_artists.values():
        for a in artists:
            artist_count[a['name']] = artist_count.get(a['name'], 0) + 1
            if a.get('added_at', '') >= week_ago:
                artist_week[a['name']] = artist_week.get(a['name'], 0) + 1

    channel_count: dict[str, int] = {}
    channel_week: dict[str, int] = {}
    for channels in user_youtube.values():
        for c in channels:
            channel_count[c['name']] = channel_count.get(c['name'], 0) + 1
            if c.get('added_at', '') >= week_ago:
                channel_week[c['name']] = channel_week.get(c['name'], 0) + 1

    def top_lines(data: dict, limit=10) -> str:
        if not data:
            return '  <i>порожньо</i>'
        sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)[:limit]
        return '\n'.join(f'  {i+1}. {name} — {count} юзерів' for i, (name, count) in enumerate(sorted_items))

    text = (
        f'📊 <b>Статистика підписок</b>\n\n'
        f'🎵 <b>Топ виконавців (overall):</b>\n{top_lines(artist_count)}\n\n'
        f'🎵 <b>Топ виконавців (цього тижня):</b>\n{top_lines(artist_week)}\n\n'
        f'▶️ <b>Топ YouTube каналів (overall):</b>\n{top_lines(channel_count)}\n\n'
        f'▶️ <b>Топ YouTube каналів (цього тижня):</b>\n{top_lines(channel_week)}'
    )
    return text


async def send_weekly_top(bot):
    admin_id = int(os.getenv('ADMIN_ID'))
    await bot.send_message(chat_id=admin_id, text=build_top_text(),
                           parse_mode=telegram.constants.ParseMode.HTML)


async def check_music_releases(bot):
    from datetime import datetime, timedelta
    global sent_releases
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    for user_id_str, artists in user_artists.items():
        user_sent = sent_releases.get(user_id_str, [])
        for artist in artists:
            artist_id = artist['id']
            releases = get_latest_releases(artist_id)
            for release in releases:
                release_id = str(release['collectionId'])
                if release_id in user_sent:
                    continue
                if release.get('releaseDate', '')[:10] < week_ago:
                    continue
                caption = (
                    f'🎵 <b>Новий реліз!</b>\n\n'
                    f'<b>{release["artistName"]}</b> — {release["collectionName"]}\n'
                    f'Дата: {release["releaseDate"][:10]}\n\n'
                    f'<a href="{release["collectionViewUrl"]}">Слухати в Apple Music</a>'
                )
                try:
                    artwork = release.get('artworkUrl100', '').replace('100x100', '600x600')
                    uid = int(user_id_str)
                    if artwork:
                        track(uid, await bot.send_photo(chat_id=uid, photo=artwork, caption=caption,
                                             parse_mode=telegram.constants.ParseMode.HTML))
                    else:
                        track(uid, await bot.send_message(chat_id=uid, text=caption,
                                               parse_mode=telegram.constants.ParseMode.HTML))
                    user_sent.append(release_id)
                except Exception:
                    pass
        sent_releases[user_id_str] = user_sent
    save_sent_releases(sent_releases)


USER_YOUTUBE_FILE = './user_youtube.json'
SENT_YOUTUBE_FILE = './sent_youtube.json'


def load_user_youtube() -> dict:
    return _load_json(USER_YOUTUBE_FILE, dict)

def save_user_youtube(data: dict):
    _save_json(USER_YOUTUBE_FILE, data)

def load_sent_youtube() -> dict:
    return _load_json(SENT_YOUTUBE_FILE, dict)

def save_sent_youtube(data: dict):
    _save_json(SENT_YOUTUBE_FILE, data)


user_youtube = load_user_youtube()
sent_youtube = load_sent_youtube()


def get_channel_id(url_or_handle: str) -> tuple[str, str] | tuple[None, None]:
    try:
        url = url_or_handle.strip()
        if not url.startswith('http'):
            url = f'https://www.youtube.com/@{url.lstrip("@")}'
        ydl_opts = {'quiet': True, 'extract_flat': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            channel_id = info.get('channel_id') or info.get('id')
            channel_name = info.get('channel') or info.get('uploader') or info.get('title') or url
            if channel_id:
                return channel_id, channel_name
    except Exception as e:
        logging.error(f'YouTube channel lookup error: {e}')
    return None, None


def get_channel_videos(channel_id: str) -> list:
    try:
        feed = feedparser.parse(f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}')
        return feed.entries[:5]
    except Exception:
        return []


async def check_youtube(bot):
    from datetime import datetime, timedelta, timezone
    global sent_youtube
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    for user_id_str, channels in user_youtube.items():
        user_sent = sent_youtube.get(user_id_str, [])
        for channel in channels:
            videos = get_channel_videos(channel['id'])
            for video in videos:
                video_id = video.get('yt_videoid', '')
                if not video_id or video_id in user_sent:
                    continue
                published = video.get('published_parsed')
                if published:
                    from datetime import datetime as dt
                    pub_dt = dt(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < week_ago:
                        continue
                title = video.get('title', '')
                link = video.get('link', '')
                thumbnail = f'https://img.youtube.com/vi/{video_id}/maxresdefault.jpg'
                caption = (
                    f'▶️ <b>Нове відео!</b>\n\n'
                    f'<b>{channel["name"]}</b>\n'
                    f'{title}\n\n'
                    f'<a href="{link}">Дивитись</a>'
                )
                try:
                    uid = int(user_id_str)
                    track(uid, await bot.send_photo(chat_id=uid, photo=thumbnail,
                                         caption=caption, parse_mode=telegram.constants.ParseMode.HTML))
                    user_sent.append(video_id)
                except Exception:
                    pass
        sent_youtube[user_id_str] = user_sent
    save_sent_youtube(sent_youtube)


def youtube_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Додати канал', callback_data='yt_add')],
        [InlineKeyboardButton('📋 Мої канали', callback_data='yt_list')],
        [InlineKeyboardButton('❌ Закрити', callback_data='yt_close')],
    ])


async def youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('▶️ <b>YouTube сповіщення</b>\n\nОберіть дію:',
                                    reply_markup=youtube_menu_keyboard(),
                                    parse_mode=telegram.constants.ParseMode.HTML)


async def youtube_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if query.data == 'yt_add':
        await query.message.reply_text('Надішли посилання на канал або @handle:\n\n'
                                       'Наприклад: <code>https://youtube.com/@ChannelName</code>',
                                       parse_mode=telegram.constants.ParseMode.HTML)
        return WAITING_YOUTUBE_URL

    elif query.data == 'yt_list':
        channels = user_youtube.get(user_id, [])
        if not channels:
            await query.message.reply_text('Список порожній. Додай канали!',
                                           reply_markup=youtube_menu_keyboard())
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(f'❌ {c["name"]}', callback_data=f'yt_remove_{c["id"]}')] for c in channels]
        buttons.append([InlineKeyboardButton('⬅️ Назад', callback_data='yt_back')])
        await query.message.reply_text('Твої канали:', reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

    elif query.data.startswith('yt_remove_'):
        channel_id = query.data.replace('yt_remove_', '')
        channels = user_youtube.get(user_id, [])
        removed = next((c['name'] for c in channels if c['id'] == channel_id), None)
        user_youtube[user_id] = [c for c in channels if c['id'] != channel_id]
        save_user_youtube(user_youtube)
        await query.message.reply_text(f'Видалено: {removed}', reply_markup=youtube_menu_keyboard())
        return ConversationHandler.END

    elif query.data == 'yt_back':
        await query.message.reply_text('▶️ <b>YouTube сповіщення</b>\n\nОберіть дію:',
                                       reply_markup=youtube_menu_keyboard(),
                                       parse_mode=telegram.constants.ParseMode.HTML)
        return ConversationHandler.END

    elif query.data == 'yt_close':
        await query.message.delete()
        return ConversationHandler.END


async def receive_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    url = update.message.text.strip()
    await update.message.reply_text('Шукаю канал...')
    channel_id, channel_name = get_channel_id(url)
    if not channel_id:
        await update.message.reply_text('Канал не знайдено. Перевір посилання і спробуй ще раз.',
                                        reply_markup=youtube_menu_keyboard())
        return ConversationHandler.END
    channels = user_youtube.get(user_id, [])
    if any(c['id'] == channel_id for c in channels):
        await update.message.reply_text(f'{channel_name} вже є у твоєму списку.',
                                        reply_markup=youtube_menu_keyboard())
        return ConversationHandler.END
    from datetime import datetime
    channels.append({'id': channel_id, 'name': channel_name, 'added_at': datetime.now().isoformat()})
    user_youtube[user_id] = channels
    save_user_youtube(user_youtube)
    await update.message.reply_text(f'✅ Додано: {channel_name}', reply_markup=youtube_menu_keyboard())
    return ConversationHandler.END


def music_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Додати виконавця', callback_data='music_add')],
        [InlineKeyboardButton('📋 Мій список', callback_data='music_list')],
        [InlineKeyboardButton('❌ Закрити', callback_data='music_close')],
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🎮 Ігрові акції', callback_data='menu_games'),
         InlineKeyboardButton('🎵 Музика', callback_data='menu_music')],
        [InlineKeyboardButton('▶️ YouTube', callback_data='menu_youtube'),
         InlineKeyboardButton('❓ Запитати', callback_data='menu_ask')],
        [InlineKeyboardButton('ℹ️ Про бота', callback_data='menu_about'),
         InlineKeyboardButton('❌ Закрити', callback_data='menu_close')],
    ])


async def show_current_deals(query):
    await query.answer('Завантажую акції...')
    await query.message.edit_text('🔍 Шукаю поточні акції...', parse_mode=telegram.constants.ParseMode.HTML)

    chat_id = query.message.chat_id
    user_id = query.from_user.id
    bot = query.get_bot()
    found = False

    epic = get_free_epic_games()
    for g in epic:
        found = True
        caption = f'🎮 <b>Epic Games — безкоштовно!</b>\n\n<b>{g["title"]}</b>\n{g.get("description","")}\n\n<a href="{g["url"]}">Забрати</a>'
        if g.get('image'):
            track(user_id, await bot.send_photo(chat_id=chat_id, photo=g['image'], caption=caption, parse_mode=telegram.constants.ParseMode.HTML))
        else:
            track(user_id, await bot.send_message(chat_id=chat_id, text=caption, parse_mode=telegram.constants.ParseMode.HTML))

    gog = get_free_gog_games()
    for g in gog:
        found = True
        caption = f'🎮 <b>GOG — безкоштовно!</b>\n\n<b>{g["title"]}</b>\n\n<a href="{g["url"]}">Забрати</a>'
        if g.get('image'):
            track(user_id, await bot.send_photo(chat_id=chat_id, photo=g['image'], caption=caption, parse_mode=telegram.constants.ParseMode.HTML))
        else:
            track(user_id, await bot.send_message(chat_id=chat_id, text=caption, parse_mode=telegram.constants.ParseMode.HTML))

    steam = get_steam_sales()
    if steam:
        found = True
        lines = '\n'.join(f'• <a href="{s["url"]}">{s["name"]}</a>' for s in steam[:8])
        images = [s['image'] for s in steam[:8] if s.get('image')]
        caption = f'🔥 <b>Steam — поточні акції:</b>\n\n{lines}'
        if len(images) > 1:
            media = [telegram.InputMediaPhoto(media=img) for img in images[:10]]
            media[0] = telegram.InputMediaPhoto(media=images[0], caption=caption, parse_mode=telegram.constants.ParseMode.HTML)
            msgs = await bot.send_media_group(chat_id=chat_id, media=media)
            for m in msgs:
                track(user_id, m)
        elif len(images) == 1:
            track(user_id, await bot.send_photo(chat_id=chat_id, photo=images[0], caption=caption, parse_mode=telegram.constants.ParseMode.HTML))
        else:
            track(user_id, await bot.send_message(chat_id=chat_id, text=caption, parse_mode=telegram.constants.ParseMode.HTML, disable_web_page_preview=True))

    await query.message.edit_text(
        'Зараз активних акцій немає.' if not found else '✅ Готово!',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='menu_games')]]),
        parse_mode=telegram.constants.ParseMode.HTML,
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🤖 <b>Хвеська — головне меню</b>\n\nОберіть розділ:',
                                    reply_markup=main_menu_keyboard(),
                                    parse_mode=telegram.constants.ParseMode.HTML)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'menu_games':
        await query.message.edit_text(
            '🎮 <b>Ігрові акції</b>\n\nБот автоматично сповіщає про:\n'
            '• Безкоштовні ігри в Epic Games\n'
            '• Безкоштовні ігри на GOG\n'
            '• Нові акції в Steam',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🔍 Показати поточні акції', callback_data='menu_deals')],
                [InlineKeyboardButton('⬅️ Назад', callback_data='menu_back')],
            ]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )

    elif query.data == 'menu_deals':
        await show_current_deals(query)

    elif query.data == 'menu_youtube':
        await query.message.edit_text(
            '▶️ <b>YouTube сповіщення</b>\n\nОберіть дію:',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('➕ Додати канал', callback_data='yt_add')],
                [InlineKeyboardButton('📋 Мої канали', callback_data='yt_list')],
                [InlineKeyboardButton('⬅️ Назад', callback_data='menu_back')],
            ]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )
        return WAITING_YOUTUBE_URL

    elif query.data == 'menu_music':
        await query.message.edit_text(
            '🎵 <b>Музичні сповіщення</b>\n\nОберіть дію:',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('➕ Додати виконавця', callback_data='music_add')],
                [InlineKeyboardButton('📋 Мій список', callback_data='music_list')],
                [InlineKeyboardButton('⬅️ Назад', callback_data='menu_back')],
            ]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )
        return WAITING_ARTIST_NAME

    elif query.data == 'menu_ask':
        await query.message.edit_text(
            '❓ <b>Запитати Хвеську</b>\n\nНапиши /ask і своє питання:\n<code>/ask що таке бандура?</code>',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='menu_back')]]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )

    elif query.data == 'menu_about':
        await query.message.edit_text(
            'ℹ️ <b>Про Хвеську</b>\n\n'
            'Хвеська — україномовний Telegram-бот з AI.\n\n'
            '<b>Команди:</b>\n'
            '/ask — запитати щось\n'
            '/music — музичні сповіщення\n'
            '/epic — перевірити ігрові акції\n'
            '/menu — це меню\n\n'
            'В групі реагує на згадку <b>хвеськ</b>.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Назад', callback_data='menu_back')]]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )

    elif query.data in ('menu_back', 'menu_close') :
        if query.data == 'menu_close':
            await query.message.delete()
        else:
            await query.message.edit_text(
                '🤖 <b>Хвеська — головне меню</b>\n\nОберіть розділ:',
                reply_markup=main_menu_keyboard(),
                parse_mode=telegram.constants.ParseMode.HTML,
            )


async def music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🎵 <b>Музичні сповіщення</b>\n\nОберіть дію:',
                                    reply_markup=music_menu_keyboard(),
                                    parse_mode=telegram.constants.ParseMode.HTML)


async def music_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if query.data == 'music_add':
        await query.message.reply_text('Напиши ім\'я виконавця:')
        return WAITING_ARTIST_NAME

    elif query.data == 'music_list':
        artists = user_artists.get(user_id, [])
        if not artists:
            await query.message.reply_text('Твій список порожній. Додай виконавців!',
                                           reply_markup=music_menu_keyboard())
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(f'❌ {a["name"]}', callback_data=f'music_remove_{a["id"]}')] for a in artists]
        buttons.append([InlineKeyboardButton('⬅️ Назад', callback_data='music_back')])
        await query.message.reply_text('Твої виконавці:', reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

    elif query.data.startswith('music_remove_'):
        artist_id = int(query.data.replace('music_remove_', ''))
        artists = user_artists.get(user_id, [])
        removed = next((a['name'] for a in artists if a['id'] == artist_id), None)
        user_artists[user_id] = [a for a in artists if a['id'] != artist_id]
        save_user_artists(user_artists)
        await query.message.reply_text(f'Видалено: {removed}', reply_markup=music_menu_keyboard())
        return ConversationHandler.END

    elif query.data == 'music_back':
        await query.message.reply_text('🎵 <b>Музичні сповіщення</b>\n\nОберіть дію:',
                                       reply_markup=music_menu_keyboard(),
                                       parse_mode=telegram.constants.ParseMode.HTML)
        return ConversationHandler.END

    elif query.data == 'music_close':
        await query.message.delete()
        return ConversationHandler.END


async def receive_artist_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    name = update.message.text.strip()
    artist = search_artist(name)
    if not artist:
        await update.message.reply_text(f'Виконавця "{name}" не знайдено. Спробуй ще раз.',
                                        reply_markup=music_menu_keyboard())
        return ConversationHandler.END
    artists = user_artists.get(user_id, [])
    if any(a['id'] == artist['artistId'] for a in artists):
        await update.message.reply_text(f'{artist["artistName"]} вже є у твоєму списку.',
                                        reply_markup=music_menu_keyboard())
        return ConversationHandler.END
    from datetime import datetime
    artists.append({'id': artist['artistId'], 'name': artist['artistName'], 'added_at': datetime.now().isoformat()})
    user_artists[user_id] = artists
    save_user_artists(user_artists)
    await update.message.reply_text(f'✅ Додано: {artist["artistName"]}', reply_markup=music_menu_keyboard())
    return ConversationHandler.END


OLLAMA_MODEL = 'uamarchuan/lapa-v0.1.2-instruct:Q4_K_M'
BOOKS_DIR = './books'
SAMPLE_LENGTH = 1500


def extract_epub_sample(filepath: str) -> str:
    book = epub.read_epub(filepath)
    text = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        text.append(soup.get_text())
    full_text = ' '.join(text).strip()
    return full_text[:SAMPLE_LENGTH]


def load_book_samples() -> str:
    samples = []
    if os.path.exists(BOOKS_DIR):
        for filename in os.listdir(BOOKS_DIR):
            if filename.endswith('.epub'):
                path = os.path.join(BOOKS_DIR, filename)
                try:
                    sample = extract_epub_sample(path)
                    samples.append(sample)
                except Exception as e:
                    logging.warning(f'Не вдалося завантажити {filename}: {e}')
    return '\n\n'.join(samples)


book_samples = load_book_samples()

EPIC_API = 'https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?locale=uk&country=UA&allowCountries=UA'
SENT_GAMES_FILE = './sent_games.json'


def load_sent_games() -> set:
    return set(_load_json(SENT_GAMES_FILE, list))

def save_sent_games(games: set):
    _save_json(SENT_GAMES_FILE, games)


sent_games = load_sent_games()


def get_free_epic_games() -> list:
    try:
        response = requests.get(EPIC_API, timeout=10)
        data = response.json()
        games = data['data']['Catalog']['searchStore']['elements']
        free = []
        for game in games:
            promotions = game.get('promotions') or {}
            offers = promotions.get('promotionalOffers') or []
            for offer_group in offers:
                for offer in offer_group.get('promotionalOffers', []):
                    if offer['discountSetting']['discountPercentage'] == 0:
                        image_url = None
                        for img in game.get('keyImages', []):
                            if img['type'] == 'OfferImageWide':
                                image_url = img['url']
                                break
                        if not image_url and game.get('keyImages'):
                            image_url = game['keyImages'][0]['url']
                        free.append({
                            'title': game['title'],
                            'description': game.get('description', ''),
                            'url': f"https://store.epicgames.com/uk/p/{game.get('productSlug') or game.get('urlSlug', '')}",
                            'image': image_url,
                        })
        return free
    except Exception as e:
        logging.error(f'Помилка Epic API: {e}')
        return []


GOG_API = 'https://catalog.gog.com/v1/catalog?limit=48&order=desc:trending&priceRange=0,0&productType=in:game'
SENT_GOG_FILE = './sent_gog.json'


def load_sent_gog() -> set:
    return set(_load_json(SENT_GOG_FILE, list))

def save_sent_gog(games: set):
    _save_json(SENT_GOG_FILE, games)


sent_gog = load_sent_gog()


def get_free_gog_games() -> list:
    try:
        response = requests.get(GOG_API, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        products = response.json()['products']
        free = []
        for p in products:
            price = p.get('price', {})
            amount = price.get('finalMoney', {}).get('amount', '999')
            if float(amount) == 0.0:
                free.append({
                    'title': p['title'],
                    'image': p.get('coverHorizontal'),
                    'url': p.get('storeLink', 'https://www.gog.com'),
                })
        return free
    except Exception as e:
        logging.error(f'Помилка GOG API: {e}')
        return []


async def check_gog_games(bot):
    global sent_gog
    free_games = get_free_gog_games()
    new_games = [g for g in free_games if g['title'] not in sent_gog]
    if not new_games:
        return
    for game in new_games:
        caption = (
            f'🎮 <b>Безкоштовна гра на GOG!</b>\n\n'
            f'<b>{game["title"]}</b>\n\n'
            f'<a href="{game["url"]}">Забрати безкоштовно</a>'
        )
        async def _send_gog(user_id):
            try:
                if game['image']:
                    await bot.send_photo(chat_id=user_id, photo=game['image'], caption=caption,
                                         parse_mode=telegram.constants.ParseMode.HTML)
                else:
                    await bot.send_message(chat_id=user_id, text=caption,
                                           parse_mode=telegram.constants.ParseMode.HTML)
            except Exception:
                pass
        await asyncio.gather(*[_send_gog(uid) for uid in known_users])
        sent_gog.add(game['title'])
    save_sent_gog(sent_gog)


STEAM_API = 'https://store.steampowered.com/api/featuredcategories/?l=ukrainian&cc=UA'
SENT_STEAM_FILE = './sent_steam.json'


def load_sent_steam() -> set:
    return set(_load_json(SENT_STEAM_FILE, list))

def save_sent_steam(items: set):
    _save_json(SENT_STEAM_FILE, items)


sent_steam = load_sent_steam()


def get_app_name(url: str) -> str:
    try:
        parts = url.rstrip('/').split('/')
        if 'app' in parts:
            app_id = parts[parts.index('app') + 1]
            r = requests.get(f'https://store.steampowered.com/api/appdetails?appids={app_id}&filters=basic&l=ukrainian', timeout=5)
            data = r.json().get(app_id, {}).get('data', {})
            return data.get('name') or url
    except Exception:
        pass
    return url


def get_steam_sales() -> list:
    try:
        response = requests.get(STEAM_API, timeout=10)
        data = response.json()
        sales = []
        for k in ['0', '1', '2', '3', '4', '5', '6']:
            section = data.get(k, {})
            for item in section.get('items', []):
                url = item.get('url')
                image = item.get('header_image')
                if not url:
                    continue
                name = get_app_name(url) if '/app/' in url else item.get('name', url)
                sales.append({'name': name, 'url': url, 'image': image})
        return sales
    except Exception as e:
        logging.error(f'Помилка Steam API: {e}')
        return []


async def check_steam_sales(bot):
    global sent_steam
    sales = get_steam_sales()
    new_sales = [s for s in sales if s['url'] not in sent_steam]
    if not new_sales:
        return

    lines = '\n'.join(f'• <a href="{s["url"]}">{s["name"]}</a>' for s in new_sales)
    caption = f'🔥 <b>Нові акції в Steam!</b>\n\n{lines}'
    images = [s['image'] for s in new_sales if s.get('image')]

    async def _send_steam(user_id):
        try:
            if len(images) == 1:
                await bot.send_photo(chat_id=user_id, photo=images[0], caption=caption,
                                     parse_mode=telegram.constants.ParseMode.HTML)
            elif len(images) > 1:
                media = [telegram.InputMediaPhoto(media=img) for img in images[:10]]
                media[0] = telegram.InputMediaPhoto(media=images[0], caption=caption,
                                                    parse_mode=telegram.constants.ParseMode.HTML)
                await bot.send_media_group(chat_id=user_id, media=media)
            else:
                await bot.send_message(chat_id=user_id, text=caption,
                                       parse_mode=telegram.constants.ParseMode.HTML)
        except Exception:
            pass
    await asyncio.gather(*[_send_steam(uid) for uid in known_users])

    for sale in new_sales:
        sent_steam.add(sale['url'])
    save_sent_steam(sent_steam)


async def check_epic_games(bot):
    global sent_games
    free_games = get_free_epic_games()
    new_games = [g for g in free_games if g['title'] not in sent_games]
    if not new_games:
        return
    for game in new_games:
        caption = (
            f'🎮 <b>Безкоштовна гра в Epic Games!</b>\n\n'
            f'<b>{game["title"]}</b>\n'
            f'{game["description"]}\n\n'
            f'<a href="{game["url"]}">Забрати безкоштовно</a>'
        )
        async def _send_epic(user_id):
            try:
                if game['image']:
                    await bot.send_photo(chat_id=user_id, photo=game['image'], caption=caption,
                                         parse_mode=telegram.constants.ParseMode.HTML)
                else:
                    await bot.send_message(chat_id=user_id, text=caption,
                                           parse_mode=telegram.constants.ParseMode.HTML)
            except Exception:
                pass
        await asyncio.gather(*[_send_epic(uid) for uid in known_users])
        sent_games.add(game['title'])
    save_sent_games(sent_games)

SYSTEM_PROMPT = (
    'Ти Хвеська — дівчина, україномовний чат-бот у Telegram. '
    'ВАЖЛИВО: ти жінка. ЗАВЖДИ вживай жіночий рід коли говориш про себе. '
    'Правильно: "я знайома", "я зробила", "я сказала", "я думаю", "мені відомо". '
    'Неправильно: "я знайомий", "я зробив", "я сказав". '
    'Відповідай коротко, по суті та виключно українською мовою. '
    'Не повторюй одне й те саме.'
)

if book_samples:
    SYSTEM_PROMPT += (
        ' Спілкуйся в стилі наступних українських текстів:\n\n'
        + book_samples
    )


async def ask_ai(message: str) -> str:
    from datetime import datetime
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    system_with_date = SYSTEM_PROMPT + f' Зараз {now}.'
    client = ollama.AsyncClient()
    response = await client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {'role': 'system', 'content': system_with_date},
            {'role': 'user', 'content': message},
        ],
        options={
            'temperature': 0.8,
            'repeat_penalty': 1.3,
        },
    )
    return response['message']['content']

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

USERS_FILE = './users.json'


def load_users() -> set:
    return set(_load_json(USERS_FILE, list))

def save_users(users: set):
    _save_json(USERS_FILE, users)


known_users = load_users()

CONTENT_DIR = './content'
os.makedirs(CONTENT_DIR, exist_ok=True)

CONTENT_FILES = {
    'quote': f'{CONTENT_DIR}/quotes.json',
    'joke': f'{CONTENT_DIR}/jokes.json',
    'meme': f'{CONTENT_DIR}/memes.json',
    'news': f'{CONTENT_DIR}/news.json',
}


def load_content(category: str) -> list:
    return _load_json(CONTENT_FILES[category], list)

def save_content(category: str, data: list):
    _save_json(CONTENT_FILES[category], data)


def add_content(category: str, item: dict):
    data = load_content(category)
    data.append(item)
    save_content(category, data)


def category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📝 Цитата', callback_data='cat_quote'),
         InlineKeyboardButton('😂 Жарт', callback_data='cat_joke')],
        [InlineKeyboardButton('🖼 Мем', callback_data='cat_meme'),
         InlineKeyboardButton('📰 Новина', callback_data='cat_news')],
        [InlineKeyboardButton('❌ Скасувати', callback_data='cat_cancel')],
    ])


async def admin_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv('ADMIN_ID')):
        return
    msg = update.message
    if msg.photo:
        context.user_data['pending'] = {'type': 'photo', 'file_id': msg.photo[-1].file_id, 'caption': msg.caption or ''}
    elif msg.text:
        context.user_data['pending'] = {'type': 'text', 'text': msg.text}
    else:
        return
    await msg.reply_text('Що це?', reply_markup=category_keyboard())
    return WAITING_CATEGORY


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'cat_cancel':
        context.user_data.pop('pending', None)
        await query.message.edit_text('Скасовано.')
        return ConversationHandler.END

    category = query.data.replace('cat_', '')
    pending = context.user_data.get('pending')
    if not pending:
        await query.message.edit_text('Щось пішло не так, спробуй ще раз.')
        return ConversationHandler.END

    add_content(category, pending)
    context.user_data.pop('pending', None)

    labels = {'quote': 'цитату', 'joke': 'жарт', 'meme': 'мем', 'news': 'новину'}
    await query.message.edit_text(f'✅ Збережено як {labels[category]}!')
    return ConversationHandler.END


greetings = _load_json('./Textbase/greetings.json', dict)
jokes = _load_json('./Textbase/jokes.json', dict)
functionality = _load_json('./Textbase/bot_functionality.json', dict)
generic_replies = _load_json('./Textbase/generic_replies.json', dict)


async def meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memes = load_content('meme')
    if not memes:
        await update.message.reply_text('Мемів ще немає. Перешли боту фото!')
        return
    m = random.choice(memes)
    track(update.effective_user.id, await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=m['file_id'],
        caption=m.get('caption') or '',
    ))


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text='Напиши питання після команди: /ask що таке бандура',
                                       reply_to_message_id=update.message.message_id)
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                       action=telegram.constants.ChatAction.TYPING)
    question = ' '.join(context.args)
    reply = await ask_ai(question)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=reply,
                                   reply_to_message_id=update.message.message_id)


BOT_MSG_IDS_FILE = './bot_msg_ids.json'


def load_bot_msg_ids() -> dict:
    if os.path.exists(BOT_MSG_IDS_FILE):
        with open(BOT_MSG_IDS_FILE, encoding='utf8') as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_bot_msg_ids(data: dict):
    with open(BOT_MSG_IDS_FILE, 'w', encoding='utf8') as f:
        json.dump({str(k): v for k, v in data.items()}, f)


bot_message_ids: dict[int, list[int]] = load_bot_msg_ids()
_bot_msg_ids_dirty = False


def track(user_id: int, msg):
    global _bot_msg_ids_dirty
    if msg is None:
        return msg
    bot_message_ids.setdefault(user_id, []).append(msg.message_id)
    _bot_msg_ids_dirty = True
    return msg


async def _flush_bot_msg_ids():
    global _bot_msg_ids_dirty
    while True:
        await asyncio.sleep(30)
        if _bot_msg_ids_dirty:
            save_bot_msg_ids(bot_message_ids)
            _bot_msg_ids_dirty = False


async def menu_button_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if action == 'menu_games':
        return await update.message.reply_text(
            '🎮 <b>Ігрові акції</b>\n\nБот автоматично сповіщає про:\n'
            '• Безкоштовні ігри в Epic Games\n'
            '• Безкоштовні ігри на GOG\n'
            '• Нові акції в Steam',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🔍 Показати поточні акції', callback_data='menu_deals')],
            ]),
            parse_mode=telegram.constants.ParseMode.HTML,
        )


def main_reply_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton('🎮 Ігрові акції'), KeyboardButton('🎵 Музика')],
        [KeyboardButton('▶️ YouTube'), KeyboardButton('❓ Запитати')],
        [KeyboardButton('ℹ️ Про бота'), KeyboardButton('🗑 Очистити чат')],
        [KeyboardButton('⌨️ Сховати')],
    ], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    known_users.add(update.effective_chat.id)
    save_users(known_users)
    track(update.effective_user.id, await context.bot.send_message(
        chat_id=update.effective_chat.id, text=functionality["0"],
        parse_mode=telegram.constants.ParseMode.HTML,
        reply_markup=main_reply_keyboard()))


async def userlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv('ADMIN_ID')):
        return
    lines = [f'👥 <b>Юзери бота ({len(known_users)}):</b>\n']
    for uid in known_users:
        try:
            chat = await context.bot.get_chat(uid)
            name = chat.full_name or ''
            username = f' @{chat.username}' if chat.username else ''
            lines.append(f'• {name}{username} (<code>{uid}</code>)')
        except Exception:
            lines.append(f'• <code>{uid}</code>')
    await update.message.reply_text('\n'.join(lines), parse_mode=telegram.constants.ParseMode.HTML)


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv('ADMIN_ID')):
        return
    await update.message.reply_text(build_top_text(), parse_mode=telegram.constants.ParseMode.HTML)


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f'Унікальних юзерів: {len(known_users)}')


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.getenv('ADMIN_ID')):
        return
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text='Напиши текст після команди: /broadcast Привіт всім!')
        return
    text = ' '.join(context.args)

    async def _send_broadcast(user_id):
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            return True
        except Exception:
            return False

    results = await asyncio.gather(*[_send_broadcast(uid) for uid in known_users])
    success = sum(results)
    failed = len(results) - success
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f'Розіслано: {success} ✓, не доставлено: {failed} ✗')


AGGRESSION_TRIGGERS = [
    'дурний', 'тупий', 'ідіот', 'дебіл', 'кретин', 'придурок',
    'заткнись', 'іди нахуй', 'пішов нахуй', 'відвали', 'заткнись',
    'бот тупий', 'бот дурний', 'нікчема', 'мудак',
]


async def maybe_send_meme(bot, chat_id: int, user_id: int, reply_to: int = None) -> bool:
    memes = load_content('meme')
    if not memes:
        return False
    m = random.choice(memes)
    track(user_id, await bot.send_photo(
        chat_id=chat_id,
        photo=m['file_id'],
        caption=m.get('caption') or '',
        reply_to_message_id=reply_to,
    ))
    return True


def get_random_joke() -> str:
    custom = load_content('joke')
    all_jokes = list(jokes.values()) + [j['text'] for j in custom if j.get('type') == 'text' and j.get('text')]
    return random.choice(all_jokes) if all_jokes else '😶'


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id not in known_users:
        known_users.add(update.effective_user.id)
        save_users(known_users)

    greeting_number = random.randint(0, 22)
    generic_reply_number = random.randint(0, 52)
    message_lowercase = update.message.text.lower()
    is_private = update.effective_chat.type == 'private'

    if update.message.text == '⌨️ Сховати':
        await update.message.reply_text('Клавіатуру сховано. Напиши /start щоб повернути.',
                                        reply_markup=ReplyKeyboardRemove())
        return

    if update.message.text == '🗑 Очистити чат':
        uid = update.effective_user.id
        ids = bot_message_ids.pop(uid, [])
        save_bot_msg_ids(bot_message_ids)
        clear_msg = await update.message.reply_text('🗑 Очищую...')
        for msg_id in ids:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except Exception:
                pass
        try:
            await update.message.delete()
            await clear_msg.delete()
        except Exception:
            pass
        return

    uid = update.effective_user.id

    if update.message.text == '🎮 Ігрові акції':
        track(uid, await menu_button_from_text(update, context, 'menu_games'))
        return
    if update.message.text == '🎵 Музика':
        track(uid, await update.message.reply_text('🎵 <b>Музичні сповіщення</b>\n\nОберіть дію:',
                                        reply_markup=music_menu_keyboard(),
                                        parse_mode=telegram.constants.ParseMode.HTML))
        return
    if update.message.text == '▶️ YouTube':
        track(uid, await update.message.reply_text('▶️ <b>YouTube сповіщення</b>\n\nОберіть дію:',
                                        reply_markup=youtube_menu_keyboard(),
                                        parse_mode=telegram.constants.ParseMode.HTML))
        return
    if update.message.text == '❓ Запитати':
        track(uid, await update.message.reply_text('Напиши /ask і своє питання:\n<code>/ask що таке бандура?</code>',
                                        parse_mode=telegram.constants.ParseMode.HTML))
        return
    if update.message.text == 'ℹ️ Про бота':
        track(uid, await update.message.reply_text(functionality["0"], parse_mode=telegram.constants.ParseMode.HTML))
        return

    # Агресія → мем
    if any(trigger in message_lowercase for trigger in AGGRESSION_TRIGGERS):
        await maybe_send_meme(context.bot, update.effective_chat.id, uid,
                              reply_to=update.message.message_id)
        return

    if is_private:
        uid = update.effective_user.id
        if 'вмієш' in message_lowercase:
            track(uid, await context.bot.send_message(chat_id=update.effective_chat.id, text=functionality["0"],
                                           reply_to_message_id=update.message.message_id,
                                           parse_mode=telegram.constants.ParseMode.HTML))
            return
        if 'жарт' in message_lowercase:
            track(uid, await context.bot.send_message(chat_id=update.effective_chat.id, text=f'<i>{get_random_joke()}</i>',
                                           reply_to_message_id=update.message.message_id,
                                           parse_mode=telegram.constants.ParseMode.HTML))
            return
        if random.random() < 0.1:
            if await maybe_send_meme(context.bot, update.effective_chat.id, uid,
                                     reply_to=update.message.message_id):
                return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        reply = await ask_ai(update.message.text)
        track(uid, await context.bot.send_message(chat_id=update.effective_chat.id, text=reply,
                                       reply_to_message_id=update.message.message_id))
        return

    if 'хвеськ' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        if 'жарт' in message_lowercase:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f'<i>{get_random_joke()}</i>',
                                           reply_to_message_id=update.message.message_id,
                                           parse_mode=telegram.constants.ParseMode.HTML)
        elif 'вмієш' in message_lowercase:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=functionality["0"],
                                           reply_to_message_id=update.message.message_id,
                                           parse_mode=telegram.constants.ParseMode.HTML)
        else:
            if random.random() < 0.1:
                if await maybe_send_meme(context.bot, update.effective_chat.id, uid,
                                         reply_to=update.message.message_id):
                    pass
                else:
                    reply = await ask_ai(update.message.text)
                    await context.bot.send_message(chat_id=update.effective_chat.id,
                                                   text=reply,
                                                   reply_to_message_id=update.message.message_id)
            else:
                reply = await ask_ai(update.message.text)
                await context.bot.send_message(chat_id=update.effective_chat.id,
                                               text=reply,
                                               reply_to_message_id=update.message.message_id)
    elif 'бачу' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Поцілуй пизду собачу!",
                                       reply_to_message_id=update.message.message_id)
    elif 'чуєш' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="На хую переночуєш!",
                                       reply_to_message_id=update.message.message_id)
    elif 'жарт' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f'<i>{get_random_joke()}</i>',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)
    elif 'по русні' in message_lowercase:
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='русні пізда!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)
    elif 'русня' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='йобана блядь русня!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)
    elif 'слава україні' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='Героям Слава!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)
    elif 'слава нації' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='Смерть ворогам!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)
    elif 'україна' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='Україна понад усе!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)

    elif 'путін' in message_lowercase:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                           action=telegram.constants.ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await context.bot.send_message(chat_id=update.effective_chat.id, text='хуйло!',
                                       reply_to_message_id=update.message.message_id,
                                       parse_mode=telegram.constants.ParseMode.HTML)

    else:
        generic_replies_match_array = []
        if is_private:
            matches_array = []
            for item in generic_replies:
                generic_reply_lowercase = generic_replies[f'{item}'].lower()
                match = SequenceMatcher(None, generic_reply_lowercase, message_lowercase).find_longest_match()
                if match.size >= 10:
                    generic_replies_match_array.append(generic_replies[f'{item}'])
                    matches_array.append(match.size)

            if generic_replies_match_array and np.max(matches_array) >= 10:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                                   action=telegram.constants.ChatAction.TYPING)
                await asyncio.sleep(0.5)
                await context.bot.send_message(chat_id=update.effective_chat.id, text=generic_replies_match_array[
                    random.randint(0, len(generic_replies_match_array) - 1)], reply_to_message_id=update.message.message_id)

        is_reply_to_bot = (
            getattr(update.message, 'reply_to_message', None) is not None and
            getattr(update.message.reply_to_message, 'from_user', None) is not None and
            getattr(update.message.reply_to_message.from_user, 'is_bot', False)
        )
        if is_reply_to_bot and not generic_replies_match_array:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id,
                                               action=telegram.constants.ChatAction.TYPING)
            reply = await ask_ai(update.message.text)
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text=reply,
                                           reply_to_message_id=update.message.message_id)


if __name__ == '__main__':
    application = ApplicationBuilder().token(os.getenv('BOT_TOKEN')).build()

    async def epic(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != int(os.getenv('ADMIN_ID')):
            return
        await check_epic_games(context.bot)
        await check_gog_games(context.bot)
        await check_steam_sales(context.bot)

    admin_filter = filters.User(user_id=int(os.getenv('ADMIN_ID')))

    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(admin_filter & filters.FORWARDED, admin_content_handler),
        ],
        states={
            WAITING_CATEGORY: [CallbackQueryHandler(category_callback, pattern='^cat_')],
        },
        fallbacks=[],
        per_message=False,
    )

    youtube_conv = ConversationHandler(
        entry_points=[
            CommandHandler('youtube', youtube),
            CallbackQueryHandler(youtube_button, pattern='^yt_'),
        ],
        states={
            WAITING_YOUTUBE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_youtube_url)],
        },
        fallbacks=[],
        per_message=False,
    )

    music_conv = ConversationHandler(
        entry_points=[
            CommandHandler('music', music),
            CommandHandler('menu', menu),
            CallbackQueryHandler(music_button, pattern='^music_'),
            CallbackQueryHandler(menu_button, pattern='^menu_'),
        ],
        states={
            WAITING_ARTIST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_artist_name)],
        },
        fallbacks=[],
        per_message=False,
    )

    start_handler = CommandHandler('start', start)
    ask_handler = CommandHandler('ask', ask)
    meme_handler = CommandHandler('meme', meme)
    users_handler = CommandHandler('users', users)
    top_handler = CommandHandler('top', top)
    userlist_handler = CommandHandler('userlist', userlist)
    broadcast_handler = CommandHandler('broadcast', broadcast)
    epic_handler = CommandHandler('epic', epic)
    echo_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), echo)

    application.add_handler(admin_conv)
    application.add_handler(youtube_conv)
    application.add_handler(music_conv)
    application.add_handler(start_handler)
    application.add_handler(ask_handler)
    application.add_handler(meme_handler)
    application.add_handler(users_handler)
    application.add_handler(top_handler)
    application.add_handler(userlist_handler)
    application.add_handler(broadcast_handler)
    application.add_handler(epic_handler)
    application.add_handler(echo_handler)

    async def post_init(app):
        scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Kyiv'))
        scheduler.add_job(
            check_epic_games,
            CronTrigger(hour=17, minute=30, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.add_job(
            check_gog_games,
            CronTrigger(hour=17, minute=30, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.add_job(
            check_steam_sales,
            CronTrigger(hour='*/2', minute=0, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.add_job(
            check_music_releases,
            CronTrigger(hour=10, minute=0, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.add_job(
            check_youtube,
            CronTrigger(hour='*/3', minute=0, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.add_job(
            send_weekly_top,
            CronTrigger(day_of_week='sun', hour=11, minute=0, timezone=pytz.timezone('Europe/Kyiv')),
            args=[app.bot],
        )
        scheduler.start()
        asyncio.create_task(_flush_bot_msg_ids())

    application.post_init = post_init

    application.run_polling()
