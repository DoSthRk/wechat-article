import stat
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import create_app
from utils import lab_sso


class LabSsoTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(lab_sso, 'KEY_PATH', self.root / 'key'),
                        patch('utils.blog_task_lock.LOCK_DIR', self.root / 'locks'),
                        patch('utils.auto_blog.kick')]
        for item in self.patches:
            item.start()
        self.app = create_app(testing=False)
        self.client = self.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def start(self, next='/image-review/paper'):
        response = self.client.get('/auth/start', query_string={'next': next})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith('https://lab.genemedi.cn/sso/article?'))
        return parse_qs(urlsplit(response.location).query)['state'][0]

    def test_real_session_uses_lab_identity_and_returns_to_review(self):
        state = self.start()
        identity = {'user_id': '7', 'username': 'jhh', 'expires_at': time.time() + 3600}
        with patch.object(lab_sso, 'exchange', return_value=identity) as exchange:
            response = self.client.get('/auth/callback', query_string={'state': state, 'ticket': 't'*43})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, '/image-review/paper')
            self.assertIn('HttpOnly', response.headers['Set-Cookie'])
            self.assertIn('当前用户：jhh', self.client.get('/operator').get_data(as_text=True))
            self.assertEqual(self.client.get('/auth/callback', query_string={'state': state, 'ticket': 't'*43}).status_code, 400)
            exchange.assert_called_once()

    def test_forged_header_does_not_create_production_identity(self):
        self.assertEqual(self.client.get('/api/sources', headers={'X-GM-LAB-Username': 'admin'}).status_code, 401)

    def test_invalid_state_never_exchanges_and_external_next_is_rejected(self):
        self.start('https://other.example/')
        with self.client.session_transaction() as session:
            self.assertEqual(session['lab_login']['next'], '/operator')
        with patch.object(lab_sso, 'exchange') as exchange:
            self.assertEqual(self.client.get('/auth/callback', query_string={'state': 'wrong', 'ticket': 't'*43}).status_code, 400)
            exchange.assert_not_called()
        for candidate in ('//other.example', '/\\other.example', '/\nLocation:evil'):
            self.assertEqual(lab_sso.safe_next(candidate), '/operator')

    def test_expired_state_and_session_are_rejected(self):
        state = self.start()
        with self.client.session_transaction() as session:
            session['lab_login'] = {**session['lab_login'], 'expires_at': 1}
            session['lab_identity'] = {'username': 'admin', 'expires_at': 1}
        with patch.object(lab_sso, 'exchange') as exchange:
            self.assertEqual(self.client.get('/auth/callback', query_string={'state': state, 'ticket': 't'*43}).status_code, 400)
            exchange.assert_not_called()
        self.assertEqual(self.client.get('/api/sources').status_code, 401)

    def test_persistent_signing_key_is_private_and_stable(self):
        self.assertEqual(lab_sso.session_secret(), lab_sso.session_secret())
        self.assertEqual(stat.S_IMODE(lab_sso.KEY_PATH.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
