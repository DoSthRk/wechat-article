"""GM-LAB one-time login tickets for the existing intranet article entry."""
import json
import os
from pathlib import Path
import secrets
import time
from urllib.request import Request, urlopen

from utils.blog_task_lock import version_lock

KEY_PATH = Path(__file__).resolve().parent.parent / 'runtime' / 'article_session_key'
LAB_URL = 'https://lab.genemedi.cn'


def session_secret():
    with version_lock('__article_auth__', 'session_key'):
        if not KEY_PATH.exists():
            with KEY_PATH.open('xb') as stream:
                os.chmod(KEY_PATH, 0o600)
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        value = KEY_PATH.read_bytes()
        if len(value) != 32:
            raise RuntimeError('Invalid article session key')
        return value


def username(session):
    identity = session.get('lab_identity')
    if (isinstance(identity, dict) and isinstance(identity.get('username'), str)
            and isinstance(identity.get('expires_at'), (int, float))
            and identity['expires_at'] > time.time()):
        return identity['username'].strip().lower()
    return ''


def safe_next(value):
    if (isinstance(value, str) and value.startswith('/') and not value.startswith('//')
            and '\\' not in value and not any(ord(c) < 32 for c in value)):
        return value
    return '/operator'


def exchange(ticket):
    request = Request(LAB_URL + '/sso/article/exchange',
                      data=json.dumps({'ticket': ticket}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=10) as response:
        value = json.loads(response.read(8192))
    if not isinstance(value, dict) or not isinstance(value.get('username'), str) or not value['username'].strip():
        raise ValueError('Invalid GM-LAB identity')
    return {'username': value['username'].strip().lower(), 'user_id': str(value.get('user_id', '')),
            'expires_at': time.time() + 3600}
