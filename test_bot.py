import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import bot

class BotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE days(day TEXT PRIMARY KEY,status TEXT,key TEXT)')
        self.db.execute('CREATE TABLE articles(key TEXT PRIMARY KEY)')
        self.now = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
        self.a = dict(title='Мото новинка', link='https://example.org/a', category='Мотоновости', source='Test', date=self.now, key='abc')
        self.preparer = patch.object(bot, 'prepare_article', side_effect=lambda a: dict(a, summary='Проверочный пересказ статьи.'))
        self.preparer.start()

    def tearDown(self):
        self.db.close()
        self.preparer.stop()

    def test_freshness(self):
        xml = b'<rss><channel><item><title>Old</title><link>https://example.org</link><pubDate>Thu, 12 Nov 2026 10:00:00 GMT</pubDate></item><item><title>New</title><link>https://example.org</link><pubDate>Sun, 15 Nov 2026 09:00:00 GMT</pubDate></item></channel></rss>'
        self.assertEqual([a['title'] for a in bot.parse_feed(xml, 'x', self.now)], ['New'])

    @patch.dict('os.environ', {'TELEGRAM_CHAT_ID': '-123'})
    def test_one_per_day_and_uncertain(self):
        with patch.object(bot, 'collect', return_value=[self.a]), patch.object(bot, 'api', side_effect=TimeoutError) as api:
            with self.assertRaises(RuntimeError):
                bot.run_once(self.db, self.now)
            bot.run_once(self.db, self.now)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(self.db.execute('SELECT status FROM days').fetchone()[0], 'uncertain')

    def test_preview_does_not_reserve(self):
        with patch.object(bot, 'collect', return_value=[self.a]), patch('builtins.print'):
            bot.run_once(self.db, self.now, True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM days').fetchone()[0], 0)

    def test_headline_duplicate(self):
        self.assertEqual(bot.key('Мото: Новинка! - Издание A'), bot.key('мото новинка - Издание B'))

    @patch.dict('os.environ', {'TELEGRAM_CHAT_ID': '-123'})
    def test_failed_remote_reservation_blocks_send(self):
        with patch.object(bot, 'collect', return_value=[self.a]), patch.object(bot, 'api') as api:
            with self.assertRaises(OSError):
                bot.run_once(self.db, self.now, persist=lambda db: (_ for _ in ()).throw(OSError()))
            api.assert_not_called()

    @patch.dict('os.environ', {'TELEGRAM_CHAT_ID': '-123'})
    def test_remote_reservation_precedes_send(self):
        events = []
        def persist(db):
            events.append(db.execute('SELECT status FROM days').fetchone()[0])
        with patch.object(bot, 'collect', return_value=[self.a]), patch.object(bot, 'api', side_effect=lambda *a: events.append('send')):
            bot.run_once(self.db, self.now, persist=persist)
        self.assertEqual(events, ['pending', 'send', 'sent'])

    def test_summary_failure_does_not_reserve_or_send(self):
        with patch.object(bot, 'collect', return_value=[self.a]), patch.object(bot, 'prepare_article', side_effect=ValueError), patch.object(bot, 'api') as api:
            with self.assertRaises(RuntimeError):
                bot.run_once(self.db, self.now)
            api.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM days').fetchone()[0], 0)

if __name__ == '__main__':
    unittest.main()
