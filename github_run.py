"""One-shot Actions entrypoint; saves reservations before Telegram sends."""
import base64
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

import bot


class State:
    def __init__(self):
        self.branch = os.environ['STATE_BRANCH']
        repo = os.environ['GITHUB_REPOSITORY']
        self.url = f'https://api.github.com/repos/{repo}/contents/.bot-state/state.json'
        self.sha = None

    def request(self, method, url, payload=None):
        req = urllib.request.Request(url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'User-Agent': 'MotoNewsBot'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def load(self, db):
        # The empty initial state file is shipped with the repository. A missing
        # or unreadable file is a hard error, never a silent history reset.
        result = self.request('GET', self.url + '?ref=' + quote(self.branch, safe=''))
        self.sha = result['sha']
        state = json.loads(base64.b64decode(result['content']))
        if state.get('version') != 1:
            raise ValueError('Unsupported state format')
        db.executemany('INSERT INTO days VALUES (?, ?, ?)', state['days'])
        db.executemany('INSERT INTO articles VALUES (?)', [(k,) for k in state['articles']])
        db.commit()

    def save(self, db):
        state = dict(version=1,
            days=db.execute('SELECT day,status,key FROM days ORDER BY day').fetchall(),
            articles=[r[0] for r in db.execute('SELECT key FROM articles ORDER BY key')])
        content = (json.dumps(state, indent=2) + '\n').encode()
        result = self.request('PUT', self.url, dict(
            message='Update moto publication state', branch=self.branch, sha=self.sha,
            content=base64.b64encode(content).decode()))
        self.sha = result['content']['sha']


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    mode = os.getenv('BOT_MODE', 'preview')
    if mode not in ('preview', 'send', 'status'):
        raise ValueError('Unknown mode')
    for name in ('GITHUB_TOKEN', 'GITHUB_REPOSITORY', 'STATE_BRANCH'):
        if not os.getenv(name):
            raise ValueError('Missing ' + name)
    if mode == 'send':
        for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
            if not os.getenv(name):
                raise ValueError('Missing ' + name)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE days(day TEXT PRIMARY KEY,status TEXT,key TEXT)')
        db.execute('CREATE TABLE articles(key TEXT PRIMARY KEY)')
        state = State()
        state.load(db)
        if mode == 'status':
            print(db.execute('SELECT day,status FROM days ORDER BY day DESC LIMIT 14').fetchall())
            return
        now = datetime.now(timezone.utc)
        if mode == 'send':
            day = now.astimezone(bot.ZoneInfo(os.getenv('TZ', 'Europe/Moscow'))).date().isoformat()
            row = db.execute('SELECT status FROM days WHERE day=?', (day,)).fetchone()
            if row:
                if row[0] != 'sent':
                    raise RuntimeError('Previous delivery unresolved; inspect chat and state')
                logging.info('Сегодня уже опубликовано')
                return
        bot.run_once(db, now, preview=(mode == 'preview'), persist=state.save if mode == 'send' else None)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No tracebacks or raw URLs that may contain the Telegram token.
        logging.error('Run failed (%s). Inspect Actions settings and publication state.', type(exc).__name__)
        raise SystemExit(1)
