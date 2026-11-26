"""Daily motorcycle news bot. Python 3.11+, curated sources and summaries."""
import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

LOG = logging.getLogger('moto')
QUERIES = [
    ('Мотоновости', ''),
    ('Экипировка', ''),
    ('Мотопутешествия', ''),
    ('Мотогаджеты', ''),
    ('Мировые мотособытия', ''),
]

def load_env():
    p = Path('.env')
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\"\''))

def api(method, payload):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    req = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    # Never log raw network exceptions: their URL contains the secret token.
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected request')
    return result['result']

def key(title):
    title = title.rsplit(' - ', 1)[0]
    return hashlib.sha256(re.sub(r'\W+', '', title.casefold()).encode()).hexdigest()

def parse_feed(raw, category, now):
    articles = []
    for item in ET.fromstring(raw).findall('./channel/item'):
        try:
            date = parsedate_to_datetime(item.findtext('pubDate', ''))
            if date.tzinfo is None:
                continue
            age = now - date
            title = html.unescape(item.findtext('title', '')).strip()
            link = item.findtext('link', '').strip()
            if not title or urlsplit(link).scheme != 'https' or not timedelta(0) <= age <= timedelta(hours=48):
                continue
            articles.append(dict(title=title[:600], link=link, category=category,
                source=item.findtext('source', 'Источник'), date=date, key=key(title)))
        except (ValueError, TypeError, OverflowError):
            continue
    return articles

def collect(now):
    from newsroom import collect as curated_collect
    return [dict(a, key=key(a['title'])) for a in curated_collect(now)]

def prepare_article(a):
    from newsroom import summarize
    return summarize(a)

def message(a):
    esc = html.escape
    text = f'🏍 <b>{esc(a["category"])}</b>\n\n<b>{esc(a["title"])}</b>\n\n'
    text += esc(a['summary']) + '\n\n'
    if a['category'] == 'Законодательство РФ':
        text += 'Сообщение СМИ. Статус и дату вступления нормы нужно проверить по официальному документу.\n\n'
    published = a['date'].strftime('%d.%m.%Y') if a.get('date_only') else a['date'].strftime('%d.%m.%Y %H:%M %Z')
    return text + f'Источник: {esc(a["source"])}\nОпубликовано: {published}\n<a href="{esc(a["link"], quote=True)}">Читать оригинал</a>'

def run_once(db, now, preview=False, persist=None):
    day = now.astimezone(ZoneInfo(os.getenv('TZ', 'Europe/Moscow'))).date().isoformat()
    if db.execute('SELECT 1 FROM days WHERE day=?', (day,)).fetchone() and not preview:
        return
    seen = {r[0] for r in db.execute('SELECT key FROM articles')}
    candidates = [a for a in collect(now) if a['key'] not in seen]
    if not candidates:
        LOG.info('Свежих новых материалов нет; повторим проверку позже')
        return
    # Rotate topics by calendar day, preferring the newest item in that topic.
    category = QUERIES[now.toordinal() % len(QUERIES)][0]
    ranked = sorted(candidates, key=lambda a: (a['category'] == category, a['date']), reverse=True)
    # Give different publishers a chance before trying several articles from
    # a single fast-moving sports feed.
    first, rest, publishers = [], [], set()
    for candidate in ranked:
        if candidate['source'] in publishers:
            rest.append(candidate)
        else:
            first.append(candidate)
            publishers.add(candidate['source'])
    ranked = first + rest
    a, errors = None, 0
    for candidate in ranked[:4]:
        try:
            a = prepare_article(candidate)
            if a:
                break
            LOG.info('Материал пропущен редакционным фильтром: %s', candidate['source'])
        except Exception as exc:
            errors += 1
            diagnostic = getattr(exc, 'safe_diagnostic', type(exc).__name__)
            LOG.warning('Не удалось подготовить пересказ: %s (%s)', candidate['source'], diagnostic)
            if getattr(exc, 'provider_failure', False):
                break
    if a is None:
        if errors:
            raise RuntimeError('Пересказ не подготовлен. Проверьте GROQ_API_KEY, доступность API и источников.')
        LOG.info('Подходящих материалов для пересказа нет')
        return
    if preview:
        print(message(a))
        return
    # Commit a reservation BEFORE the network call. Ambiguous sends are never
    # retried automatically, preventing duplicates after timeouts/restarts.
    db.execute('INSERT INTO days VALUES (?, ?, ?)', (day, 'pending', a['key']))
    db.execute('INSERT OR IGNORE INTO articles VALUES (?)', (a['key'],))
    db.commit()
    if persist:
        persist(db)  # A durable reservation must succeed before sending.
    try:
        api('sendMessage', dict(chat_id=os.environ['TELEGRAM_CHAT_ID'], text=message(a), parse_mode='HTML'))
    except Exception:
        db.execute('UPDATE days SET status=? WHERE day=?', ('uncertain', day))
        db.commit()
        if persist:
            persist(db)
        LOG.error('Отправка не подтверждена. Проверьте чат; автоматического повтора сегодня не будет.')
        raise RuntimeError('Telegram delivery uncertain')
    db.execute('UPDATE days SET status=? WHERE day=?', ('sent', day))
    db.commit()
    if persist:
        persist(db)
    LOG.info('Новость отправлена')

def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'once', 'preview', 'chats', 'status'])
    args = parser.parse_args()
    if args.command == 'chats':
        for u in api('getUpdates', {'timeout': 0}):
            m = u.get('message') or u.get('channel_post') or u.get('my_chat_member', {})
            c = m.get('chat', {})
            if c:
                print(c.get('id'), c.get('title', c.get('type')))
        return
    tz = ZoneInfo(os.getenv('TZ', 'Europe/Moscow'))
    hour, minute = map(int, os.getenv('POST_TIME', '10:00').split(':'))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('Неверное POST_TIME')
    if args.command in ('run', 'once'):
        for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
            if not os.getenv(name):
                raise ValueError('Заполните ' + name)
    path = Path(os.getenv('DATABASE_PATH', 'data/state.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, status TEXT, key TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS articles(key TEXT PRIMARY KEY)')
        db.commit()
        if args.command == 'status':
            print(db.execute('SELECT * FROM days ORDER BY day DESC LIMIT 14').fetchall())
            return
        while True:
            now = datetime.now(timezone.utc)
            local = now.astimezone(tz)
            try:
                if args.command != 'run' or (local.hour, local.minute) >= (hour, minute):
                    run_once(db, now, args.command == 'preview')
            except Exception as exc:
                LOG.error('Цикл не выполнен (%s)', type(exc).__name__)
                if args.command != 'run':
                    raise SystemExit(1)
            if args.command != 'run':
                return
            time.sleep(60)

if __name__ == '__main__':
    main()
