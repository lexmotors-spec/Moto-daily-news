import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import newsroom

class NewsroomTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(name='Test', domains=['example.org'], url='https://example.org/', category='Мотоновости')
        self.now = datetime(2026,11,26,12,tzinfo=timezone.utc)
        self.article = dict(self.source, source='Test', title='New bike', link='https://example.org/bike', date=self.now)

    def test_listing_filters_dates_and_deduplicates(self):
        raw = b'''<a href="/news/26-11-2026_bike.html">New bike</a><a href="/news/26-11-2026_bike.html">Again</a><a href="/news/20-11-2026_old.html">Old</a><a href="https://evil.org/news/26-11-2026_bike.html">Evil</a>'''
        self.assertEqual(len(newsroom.listing_items(raw,self.source,self.now)),1)

    def test_url_allowlist(self):
        for url in ['http://example.org/a','https://example.org.evil.org/a','https://example.org:444/a','https://me@example.org/a']:
            self.assertFalse(newsroom.allowed(url,['example.org']))
        self.assertTrue(newsroom.allowed('https://example.org/a',['example.org']))

    def test_short_article_rejected(self):
        with self.assertRaises(ValueError):
            newsroom.article_text('<html><body><p>Only headline</p></body></html>')

    def test_cards_use_own_publication_dates(self):
        raw = b'''<article><span class="tile-date">26.11.2026</span><h3><a href="/news/new">New</a></h3></article><article><time datetime="2026-09-01">date</time><h2><a href="/news/old">Old</a></h2></article><article><h2><a href="/news/undated">Undated</a></h2></article>'''
        result = newsroom.card_items(raw,self.source,self.now)
        self.assertEqual([a['title'] for a in result],['New'])
        self.assertTrue(result[0]['date_only'])

    def test_official_sport_urls_have_explicit_publication_dates(self):
        source=dict(self.source,article_pattern=r'^/en/news/(20\d\d)/(\d{2})/(\d{2})/[^/]+/\d+$')
        raw=b'<a href="/en/news/2026/11/26/test/123">Race update</a><a href="/en/news/2026/11/01/old/456">Old</a><a href="/en/news/2026/11/26/test/123">Duplicate</a>'
        result=newsroom.dated_listing(raw,source,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['title'],'Race update')

    def test_publication_metadata_never_uses_modified_date(self):
        raw=b'<h1>Bike launch</h1><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-25T10:00:00Z","dateModified":"2026-11-26T10:00:00Z"}</script>'
        title,date,dayonly=newsroom.published_metadata(raw)
        self.assertEqual(title,'Bike launch')
        self.assertEqual(date.day,25)
        self.assertFalse(dayonly)
        with self.assertRaises(ValueError):
            newsroom.published_metadata(b'<script type="application/ld+json">{"dateModified":"2026-11-26"}</script>')

    def test_subscriber_article_skipped(self):
        raw=b'<script type="application/ld+json">{"datePublished":"2026-11-26","isAccessibleForFree":false}</script>'
        with self.assertRaises(ValueError):
            newsroom.published_metadata(raw)

    def test_empty_feed_fails_instead_of_silent_success(self):
        with self.assertRaises(ValueError):
            newsroom.rss_items(b'<rss><channel/></rss>',self.source,self.now)

    def test_metadata_listing_requires_real_article_date(self):
        source=dict(self.source,article_pattern=r'^/news/[^/]+$')
        raw=b'<a href="/news/bike">Bike</a><a href="https://evil.org/news/bike">Other</a>'
        page=b'<h1>Bike launch</h1><meta property="article:published_time" content="2026-11-26T10:00:00Z">'
        with patch.object(newsroom,'download',return_value=page) as download,patch.object(newsroom,'article_text',return_value='Full article'):
            result=newsroom.metadata_listing(raw,source,self.now)
        self.assertEqual(len(result),1)
        self.assertEqual(download.call_count,1)
        self.assertEqual(result[0]['_body'],'Full article')

    def response(self, result, finish='stop'):
        return io.BytesIO(json.dumps({'choices':[{'finish_reason':finish,'message':{'content':json.dumps(result)}}]}).encode())

    @patch.dict('os.environ', {'GROQ_API_KEY':'test-key'})
    def test_summary_uses_article_and_checks_russian(self):
        result = dict(relevant=True,title='Представлен новый мотоцикл',summary='Производитель представил новый мотоцикл для дальних поездок. В сообщении описаны изменения конструкции и возможности модели, которые могут заинтересовать любителей путешествий.',category='Мотоновости')
        with patch.object(newsroom,'download',return_value=b'html'), patch.object(newsroom,'article_text',return_value='FULL ARTICLE TEXT'), patch('urllib.request.urlopen',return_value=self.response(result)) as call:
            prepared = newsroom.summarize(self.article)
        self.assertEqual(prepared['summary'],result['summary'])
        self.assertIn('FULL ARTICLE TEXT',call.call_args.args[0].data.decode())

    @patch.dict('os.environ', {'GROQ_API_KEY':'test-key'})
    def test_irrelevant_and_truncated_results(self):
        with patch.object(newsroom,'download',return_value=b'html'), patch.object(newsroom,'article_text',return_value='Article'), patch('urllib.request.urlopen',return_value=self.response({'relevant':False})):
            self.assertIsNone(newsroom.summarize(self.article))
        with patch.object(newsroom,'download',return_value=b'html'), patch.object(newsroom,'article_text',return_value='Article'), patch('urllib.request.urlopen',return_value=self.response({},'length')):
            with self.assertRaises(ValueError):
                newsroom.summarize(self.article)
