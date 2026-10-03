"""Optional loopback-only HTTP adapter. Python standard library, no accounts."""

import configparser
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .config import config_model, load_config, update_config_text
from .io import Console, ConsoleError, command_lock, config_document, load_state, replace_config, save_state
from .policy import effective_rtt
from .service import LOG, operate

MAX_BODY = 65536
SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SECONDS = 7 * 24 * 60 * 60
MAX_SESSIONS = 128
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Session:
    origin: str
    csrf: str
    created_at: float
    expires_at: float
    deadline: float
    remember: bool

    def public(self):
        return {'authenticated': True, 'csrf': self.csrf, 'created_at': self.created_at,
                'expires_at': self.expires_at, 'remember': self.remember}


class Sessions:
    """One process owns bounded, non-renewing sessions; never hold this lock during I/O."""

    def __init__(self):
        self.lock = threading.Lock()
        self.records = {}
        self.failures = []

    @staticmethod
    def digest(identifier):
        return hashlib.sha256(identifier.encode('ascii')).digest()

    def clean(self):
        now = time.monotonic()
        self.records = {key: value for key, value in self.records.items() if value.deadline > now}

    def get(self, identifier, origin):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', identifier):
            return None
        with self.lock:
            self.clean()
            session = self.records.get(self.digest(identifier))
            return session if session and session.origin == origin else None

    def login(self, token, expected, origin, remember, previous=''):
        with self.lock:
            now = time.monotonic()
            self.failures = [stamp for stamp in self.failures if stamp > now - LOGIN_WINDOW_SECONDS]
            if len(self.failures) >= LOGIN_FAILURE_LIMIT:
                return 429, None, None
            if not hmac.compare_digest(token.encode('utf-8'), expected.encode('ascii')):
                self.failures.append(now)
                return 401, None, None
            self.clean()
            previous_key = self.digest(previous)
            old = self.records.get(previous_key)
            replacing = old is not None and old.origin == origin
            if len(self.records) >= MAX_SESSIONS and not replacing:
                return 429, None, None
            if replacing:
                del self.records[previous_key]
            seconds = REMEMBER_SECONDS if remember else SESSION_SECONDS
            identifier = secrets.token_urlsafe(32)
            created = time.time()
            session = Session(origin, secrets.token_urlsafe(32), created, created + seconds,
                              now + seconds, remember)
            self.records[self.digest(identifier)] = session
            self.failures.clear()
            return 200, identifier, session

    def revoke(self, identifier, origin):
        with self.lock:
            key = self.digest(identifier)
            session = self.records.get(key)
            if session and session.origin == origin:
                del self.records[key]


def load_token(path):
    """Create a durable random secret; refuse symlinks and permissive files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, os.O_RDONLY | flags)
    except FileNotFoundError:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=path.parent,
                                             prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(secrets.token_urlsafe(32) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publish complete contents atomically without replacing a concurrent winner.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                os.unlink(temporary)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        fd = os.open(path, os.O_RDONLY | flags)
    with os.fdopen(fd, 'r', encoding='ascii') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('token 文件必须属于当前用户，且权限为 0600')
        token = stream.read(130).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise ValueError('token 必须是 32–128 位字母、数字、下划线或连字符')
    return token


def snapshot(config, state):
    nodes = []
    for node in config.nodes:
        health = state['nodes'][node.id]
        nodes.append(dict(asdict(node), **{'health': health,
                     'effective_rtt_ms': effective_rtt(health, config.policy) if node.rtt_ranked else None}))
    return {'state': {key: value for key, value in state.items() if key != 'nodes'},
            'nodes': nodes, 'policy': asdict(config.policy)}


class Backend:
    def __init__(self, config_path, state_path):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)

    def run(self, command, node_id=None):
        with command_lock(str(self.state_path) + '.lock'):
            config = load_config(self.config_path)
            state = load_state(self.state_path, config)
            console = Console(config.nodes, config.policy.connect_timeout_seconds)
            error = None
            try:
                operate(config, state, console, command, node_id)
            except ConsoleError as exc:
                error = str(exc)
            finally:
                if command in ('probe', 'switch', 'auto'):
                    save_state(self.state_path, state)
            result = snapshot(config, state)
            return (502 if error else 200), {'ok': error is None, 'error': error, 'data': result}

    def config(self, draft=None):
        with command_lock(str(self.state_path) + '.lock'):
            if draft is None:
                return config_document(self.config_path)
            if set(draft) != {'text', 'revision'} or not all(isinstance(v, str) for v in draft.values()):
                raise ValueError('配置请求必须包含 text 和 revision 字符串')
            current = load_config(self.config_path)
            state = load_state(self.state_path, current)
            result = replace_config(self.config_path, draft['text'], draft['revision'], state)
            LOG.info('config updated via WebUI; previous version saved as .webui.bak')
            return result


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, token, backend):
        # Deliberately no configurable bind address.
        super().__init__(('127.0.0.1', port), Handler)
        self.token = token
        self.backend = backend
        self.sessions = Sessions()
        # Stable across restarts, distinct for separate tokens/listening ports. No secret in the name.
        scope = '{}:{}'.format(token, self.server_address[1]).encode('ascii')
        self.cookie_name = 'relay_helper_session_' + hashlib.sha256(scope).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    server_version = 'RelayHelper'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        # BaseHTTPRequestHandler logs the entire URL, including entry tokens.
        pass

    def reply(self, code, body, content_type='application/json; charset=utf-8', nonce=None, cookie=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        sources = "'nonce-{}'".format(nonce) if nonce else "'none'"
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src {0}; style-src {0}; "
                         "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'".format(sources))
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        hosts = self.headers.get_all('Host', [])
        if (len(hosts) != 1 or not re.fullmatch(r'(127\.0\.0\.1|localhost):[0-9]{1,5}', hosts[0])
                or not 1 <= int(hosts[0].rsplit(':', 1)[1]) <= 65535):
            self.reply(403, {'ok': False, 'error': '只接受本机 Host'})
            return False
        origins = self.headers.get_all('Origin', [])
        self.origin = 'http://' + hosts[0]
        if ((origins and origins != ['http://' + hosts[0]])
                or (self.command == 'POST' and origins != [self.origin])
                or self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin')):
            self.reply(403, {'ok': False, 'error': '请求来源不受信任'})
            return False
        if not self.path.startswith('/') or self.path.startswith('//'):
            self.reply(400, {'ok': False, 'error': '请求必须使用本机路径'})
            return False
        if '?' in self.path and self.path.partition('?')[0] != '/':
            self.reply(401, {'ok': False, 'error': '接口不接受查询参数'})
            return False
        return True

    def bearer(self):
        values = self.headers.get_all('Authorization', [])
        return values[0][7:] if len(values) == 1 and values[0].startswith('Bearer ') else ''

    def identifier(self):
        cookies = self.headers.get_all('Cookie', [])
        if len(cookies) != 1:
            return ''
        values = [part.strip().partition('=')[2] for part in cookies[0].split(';')
                  if part.strip().partition('=')[0] == self.server.cookie_name]
        if len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', values[0]):
            return ''
        return values[0]

    def authorized(self):
        self.session = self.server.sessions.get(self.identifier(), self.origin)
        if self.session is None:
            self.reply(401, {'ok': False, 'error': '登录已失效，请重新登录'})
            return False
        if self.command == 'POST':
            csrf = self.headers.get_all('X-CSRF-Token', [])
            if len(csrf) != 1 or not hmac.compare_digest(csrf[0].encode('utf-8'), self.session.csrf.encode('ascii')):
                self.reply(403, {'ok': False, 'error': 'CSRF 校验失败'})
                return False
        return True

    def cookie(self, identifier, remember=False):
        value = '{}={}; Path=/; HttpOnly; SameSite=Strict'.format(self.server.cookie_name, identifier)
        if not identifier:
            value += '; Max-Age=0'
        elif remember:
            value += '; Max-Age={}'.format(REMEMBER_SECONDS)
        return value

    def page(self, name):
        nonce = secrets.token_urlsafe(24)
        page = Path(__file__).with_name(name).read_text(encoding='utf-8')
        self.reply(200, page.replace('__NONCE__', nonce), 'text/html; charset=utf-8', nonce)

    def do_GET(self):
        path = self.path.partition('?')[0]
        if not self.trusted():
            return
        try:
            if path == '/':
                if '?' in self.path:
                    query = parse_qs(self.path.partition('?')[2], keep_blank_values=True)
                    values = query.get('token', [])
                    token = values[0] if len(values) == 1 and set(query) == {'token'} else ''
                    if not hmac.compare_digest(token.encode('utf-8'), self.server.token.encode('ascii')):
                        self.reply(401, {'ok': False, 'error': '缺少或无效访问 token'})
                        return
                    self.page('login.html')
                else:
                    session = self.server.sessions.get(self.identifier(), self.origin)
                    self.page('webui.html' if session else 'login.html')
                return
            if not self.authorized():
                return
            if path == '/api/session':
                self.reply(200, {'ok': True, 'data': self.session.public()})
            elif path in ('/api/status', '/api/nodes'):
                code, result = self.server.backend.run(path.rsplit('/', 1)[1])
                self.reply(code, result)
            elif path == '/api/config':
                self.reply(200, {'ok': True, 'data': self.server.backend.config()})
            else:
                self.reply(404, {'ok': False, 'error': '页面不存在'})
        except (OSError, ValueError, KeyError, configparser.Error, RuntimeError) as exc:
            self.failure(exc)

    def do_POST(self):
        if not self.trusted():
            return
        path = self.path.partition('?')[0]
        if path != '/api/session/login' and not self.authorized():
            return
        if path not in ('/api/session/login', '/api/session/logout', '/api/probe', '/api/switch', '/api/auto',
                        '/api/config', '/api/config/parse', '/api/config/render'):
            self.reply(404, {'ok': False, 'error': '操作不存在'})
            return
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError('请求必须包含单个 Content-Length')
            length = int(lengths[0])
            if length > MAX_BODY:
                self.reply(413, {'ok': False, 'error': '请求不得超过 64 KiB'})
                return
            if self.headers.get('Content-Type') != 'application/json':
                self.reply(415, {'ok': False, 'error': '仅接受 application/json'})
                return
            content = self.rfile.read(length)
            if len(content) != length:
                raise ValueError('请求内容不完整')
            body = json.loads(content)
            if not isinstance(body, dict):
                raise ValueError('请求必须是 JSON 对象')
            if path == '/api/session/login':
                if set(body) != {'remember'} or not isinstance(body['remember'], bool):
                    raise ValueError('登录请求必须包含 remember 布尔值')
                code, identifier, session = self.server.sessions.login(
                    self.bearer(), self.server.token, self.origin, body['remember'], self.identifier())
                if code != 200:
                    self.reply(code, {'ok': False, 'error': '登录尝试过多或会话已满，请稍后重试' if code == 429
                                     else '缺少或无效访问 token'})
                    return
                self.reply(200, {'ok': True, 'data': session.public()}, cookie=self.cookie(identifier, body['remember']))
            elif path == '/api/session/logout':
                if body:
                    raise ValueError('此操作不接受参数')
                self.server.sessions.revoke(self.identifier(), self.origin)
                self.reply(200, {'ok': True}, cookie=self.cookie(''))
            elif path == '/api/config/parse':
                if set(body) != {'text'} or not isinstance(body['text'], str):
                    raise ValueError('解析请求必须包含 text 字符串')
                self.reply(200, {'ok': True, 'data': config_model(body['text'])})
            elif path == '/api/config/render':
                if set(body) != {'text', 'model'} or not isinstance(body['text'], str):
                    raise ValueError('映射请求必须包含 text 字符串和 model')
                self.reply(200, {'ok': True, 'data': {'text': update_config_text(body['text'], body['model'])}})
            elif path == '/api/config':
                self.reply(200, {'ok': True, 'data': self.server.backend.config(body)})
            else:
                command = path.rsplit('/', 1)[1]
                if command == 'switch':
                    if set(body) != {'id'} or not isinstance(body['id'], str):
                        raise ValueError('switch 必须指定节点 id')
                elif body:
                    raise ValueError('此操作不接受参数')
                code, result = self.server.backend.run(command, body.get('id'))
                self.reply(code, result)
        except (OSError, ValueError, KeyError, configparser.Error, RuntimeError) as exc:
            self.failure(exc)

    def failure(self, exc):
        code = 409 if isinstance(exc, RuntimeError) else 400
        LOG.warning('WebUI operation failed: %s', exc)
        self.reply(code, {'ok': False, 'error': str(exc)})


def serve(config_path, state_path, port=8765, token_file=None):
    if not 1 <= port <= 65535:
        raise ValueError('WebUI 端口必须在 1–65535 之间')
    token_path = Path(token_file) if token_file else Path(state_path).parent / 'webui.token'
    reserved = [Path(config_path), Path(state_path), Path(str(state_path) + '.lock'),
                Path(str(config_path) + '.webui.bak')]
    if token_path.resolve() in [path.resolve() for path in reserved]:
        raise ValueError('token 文件不能与配置、备份、状态或锁文件共用路径')
    token = load_token(token_path)
    with LocalServer(port, token, Backend(config_path, state_path)) as server:
        print('WebUI 仅监听 http://127.0.0.1:{}；token 文件：{}'.format(port, token_path), flush=True)
        if sys.stdout.isatty():
            print('访问地址：http://127.0.0.1:{}/?token={}'.format(port, token), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nWebUI 已停止')
    return 0
