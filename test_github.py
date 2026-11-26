import base64
import json
import os
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from github_run import State

class StateTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'STATE_BRANCH':'main', 'GITHUB_REPOSITORY':'owner/repo'})
        self.env.start()
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE days(day TEXT PRIMARY KEY,status TEXT,key TEXT)')
        self.db.execute('CREATE TABLE articles(key TEXT PRIMARY KEY)')

    def tearDown(self):
        self.db.close()
        self.env.stop()

    def test_state_roundtrip_and_sha_guard(self):
        initial = {'version':1, 'days':[['2026-11-26','pending','abc']], 'articles':['abc']}
        response = {'sha':'old-sha', 'content':base64.b64encode(json.dumps(initial).encode()).decode()}
        state = State()
        with patch.object(state, 'request', return_value=response):
            state.load(self.db)
        with patch.object(state, 'request', return_value={'content':{'sha':'new-sha'}}) as req:
            state.save(self.db)
        payload = req.call_args.args[2]
        self.assertEqual(payload['sha'], 'old-sha')
        self.assertEqual(json.loads(base64.b64decode(payload['content'])), initial)
        self.assertEqual(state.sha, 'new-sha')

    def test_missing_state_fails_closed(self):
        state = State()
        with patch.object(state, 'request', side_effect=HTTPError('url',404,'missing',{},None)):
            with self.assertRaises(HTTPError):
                state.load(self.db)
