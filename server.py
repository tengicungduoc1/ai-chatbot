"""Single-user local timetable, Gemini tools, and Xiaozhi voice server."""
import asyncio
from collections import deque
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from dotenv import load_dotenv
from calendar_store import ValidationError, ConflictError
from storage_backend import create_store
from ai_providers import create_provider
from gemini_agent import GeminiAgent
from voice import Speech, device_socket
from preferences import Preferences, DEFAULTS
from auth import Auth
from google_oauth import GoogleOAuth
from storage_backend import create_auth_store


SESSION_COOKIE = 'chatbot_session'


# Legacy alias stays the safe fallback; only documented IDs are selectable.
from preferences import MODELS as PREFERENCE_MODELS


def _safe_model(model, fallback='gemini-flash-latest'):
    return model if isinstance(model, str) and (model in PREFERENCE_MODELS or model == fallback) else fallback


def _apply_preferences(app, values):
    app['config']['model'] = values['model']
    app['agent'].assistant_name = values['assistant_name']
    app['agent'].style_prompt = values['style_prompt']
    app['agent'].model = values['model']
    if app['config'].get('ai_provider', 'gemini') == 'gemini':
        app['speech'].model = values['model']
    app['speech'].voice = values['voice']
    app['speech'].style_prompt = values['style_prompt']


async def preferences_api(request):
    prefs = request.app['preferences']
    user_id = request['user_id']
    if user_id != 'default':
        store = request.app['auth_store'].store
        if request.method != 'GET':
            await store_call(store, 'save_user_preferences', user_id, await body(request))
        values = await store_call(store, 'get_user_preferences', user_id)
        return web.json_response({**values, 'models': list(PREFERENCE_MODELS), 'voices': prefs.response()['voices']})
    if request.method == 'GET':
        return web.json_response(prefs.response())
    saved = prefs.save(await body(request))
    _apply_preferences(request.app, saved)
    return web.json_response(prefs.response())


ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=7))
LOG = logging.getLogger('chatbot')


def now():
    return datetime.now(TZ)


async def store_call(store, method, *args, **kwargs):
    call = getattr(store, method)
    if getattr(store, 'blocks_event_loop', False):
        return await asyncio.to_thread(call, *args, **kwargs)
    return call(*args, **kwargs)


def monday(value=None):
    try:
        d = date.fromisoformat(value) if value else now().date()
    except (ValueError, TypeError):
        raise ValidationError('Ngày phải có dạng YYYY-MM-DD.')
    return (d-timedelta(days=d.weekday())).isoformat()


def _api_key_pool(primary, backups):
    return tuple(dict.fromkeys(key.strip() for key in (primary, *(backups or '').split(',')) if key.strip()))


def configure_voice_file_logging(path):
    logger = logging.getLogger('chatbot.voice')
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(handler, logging.FileHandler)
               and Path(handler.baseFilename) == path for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger




def settings():
    load_dotenv(ROOT/'.env')
    folder = ROOT/'data'
    backend = os.getenv('DB_BACKEND', 'sqlite').lower()
    if backend == 'sqlite':
        folder.mkdir(exist_ok=True)
        secret_file = folder/'local_tokens.json'
        if secret_file.exists():
            tokens = json.loads(secret_file.read_text(encoding='utf-8'))
        else:
            tokens = {'web_token':secrets.token_urlsafe(32),'device_token':secrets.token_urlsafe(32)}
            with secret_file.open('x',encoding='utf-8') as f:
                json.dump(tokens,f)
    else:
        tokens = {'web_token':secrets.token_urlsafe(32),'device_token':secrets.token_urlsafe(32)}
    api_keys = _api_key_pool(os.getenv('GEMINI_API_KEY',''), os.getenv('GEMINI_API_KEYS',''))
    provider = os.getenv('AI_PROVIDER', 'gemini')
    return {'database':str(folder/'calendar.db'), 'db_backend':backend,
        'supabase_db_url':os.getenv('SUPABASE_DB_URL',''),
        'google_client_id':os.getenv('GOOGLE_CLIENT_ID',''),
        'google_client_secret':os.getenv('GOOGLE_CLIENT_SECRET',''),
        'google_redirect_uri':os.getenv('GOOGLE_REDIRECT_URI',''),
        'ai_provider':provider,'ai_base_url':os.getenv('AI_BASE_URL',''),
        'api_key':(os.getenv('AI_API_KEY','') if provider == 'openai_compatible' else (api_keys[0] if api_keys else '')),
        'api_keys':api_keys if provider == 'gemini' else (),
        'gemini_api_key':api_keys[0] if api_keys else '', 'gemini_api_keys':api_keys,
        'gemini_model':os.getenv('GEMINI_MODEL','gemini-flash-latest'),
        'model':os.getenv('AI_MODEL','') if provider == 'openai_compatible' else os.getenv('GEMINI_MODEL','gemini-flash-latest'),
        'tts_model':os.getenv('GEMINI_TTS_MODEL','gemini-3.1-flash-tts-preview'),
        'web_token':os.getenv('WEB_TOKEN') or tokens['web_token'],
        'device_token':os.getenv('DEVICE_TOKEN') or tokens['device_token'],
        'public_base_url':os.getenv('PUBLIC_BASE_URL','http://127.0.0.1:8000').rstrip('/'),
        'music_enabled':os.getenv('MUSIC_ENABLED', '1') == '1',
        'host':os.getenv('HOST','0.0.0.0'),'port':int(os.getenv('PORT','8000')),'background':True,
        'preferences_path':str(folder/'assistant_preferences.json')}


def _equal(a,b):
    return isinstance(a,str) and isinstance(b,str) and bool(a) and hmac.compare_digest(a,b)


def _web_auth(request):
    return _session_user(request) is not None


def _session_user(request):
    token = request.cookies.get(SESSION_COOKIE, '')
    return request.app['auth_store'].get_session(token)


def _csrf_ok(request):
    cookie = request.cookies.get('chatbot_csrf', '')
    header = request.headers.get('X-CSRF-Token', '')
    return _equal(cookie, header)


def _set_auth_cookies(request, response, token):
    csrf = secrets.token_urlsafe(24)
    secure = request.secure or request.app['config'].get('secure_cookies', False) or request.app['config']['public_base_url'].startswith('https://')
    # OAuth returns from Google with a top-level cross-site navigation. Lax is
    # required for the session cookie to reach the redirected GET /; CSRF stays Strict.
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite='Lax', secure=secure, max_age=86400, path='/')
    response.set_cookie('chatbot_csrf', csrf, httponly=False, samesite='Strict', secure=secure, max_age=86400, path='/')


def _clear_auth_cookies(request, response):
    secure = request.secure or request.app['config'].get('secure_cookies', False) or request.app['config']['public_base_url'].startswith('https://')
    response.del_cookie(SESSION_COOKIE, path='/', secure=secure)
    response.del_cookie('chatbot_csrf', path='/', secure=secure)

def _device_origin(request):
    # A configured HTTPS origin is authoritative behind a TLS-terminating proxy.
    public = request.app['config']['public_base_url']
    if public.startswith('https://'):
        return public
    # For local HTTP, use the accepted socket, never the client-controlled Host.
    host, port = request.transport.get_extra_info('sockname')[:2]
    if ':' in host:
        host = f'[{host}]'
    return f'{request.scheme}://{host}:{port}'


@web.middleware
async def boundary(request, handler):
    try:
        cfg = request.app['config']
        path = request.path
        if cfg['db_backend'] == 'supabase' and path in ('/login', '/api/auth/login', '/api/auth/register'):
            raise web.HTTPNotFound()
        if path.startswith('/api/'):
            user = _session_user(request)
            request['user_id'] = user['id'] if user else None
            request['user'] = user
            request['legacy_auth'] = bool(user and user['id'] == 'default')
            public = path in ('/api/auth/register', '/api/auth/login')
            if public and request.method == 'POST':
                peer = request.remote or 'unknown'  # Ignore spoofable forwarded headers.
                attempts = request.app['auth_attempts']
                stamp = time.monotonic()
                for address in list(attempts):
                    queue = attempts[address]
                    while queue and queue[0] <= stamp - 60:
                        queue.popleft()
                    if not queue:
                        del attempts[address]
                if peer not in attempts and len(attempts) >= 1024:
                    return web.json_response({'error':'Quá nhiều lượt đăng nhập.'}, status=429,
                                             headers={'Retry-After':'60'})
                queue = attempts.setdefault(peer, deque())
                if len(queue) >= 10:
                    return web.json_response({'error':'Thử lại sau một phút.'}, status=429,
                                             headers={'Retry-After':'60'})
                queue.append(stamp)
            if not user and not public:
                return web.json_response({'error':'Mở link đăng nhập được in khi chạy server.'},status=401)
            if request.method not in ('GET','HEAD'):
                origin = request.headers.get('Origin')
                expected_origin = (cfg['public_base_url'] if cfg['public_base_url'].startswith('https://')
                                   else f'{request.scheme}://{request.host}')
                if request.headers.get('X-Requested-With') != 'chatbot' or (origin and origin != expected_origin):
                    return web.json_response({'error':'Yêu cầu khác nguồn bị từ chối.'},status=403)
                if request.content_type != 'application/json':
                    raise ValidationError('Yêu cầu JSON.')
                if user and path not in ('/api/auth/register', '/api/auth/login'):
                    if not request.get('legacy_auth') and not _csrf_ok(request):
                        return web.json_response({'error':'Thiếu hoặc sai mã CSRF.'},status=403)
        if path.startswith('/device/') or path.startswith('/xiaozhi/'):
            token = request.headers.get('Authorization','').removeprefix('Bearer ')
            if path.rstrip('/') == '/xiaozhi/ota' and not token:
                token = request.query.get('token','')
            device = None
            if cfg['db_backend'] == 'sqlite' and _equal(token,cfg['device_token']):
                request['user_id'] = 'default'
                request['device_id'] = None
            elif token and len(token) <= 256:
                device = await store_call(request.app['auth_store'].store, 'resolve_device_token', token)
                if device:
                    request['user_id'] = device['user_id']
                    request['device_id'] = device['device_id']
            if request.get('user_id') is None:
                return web.json_response({'error':'Device token không hợp lệ.'},status=401)
            supplied_id = request.headers.get('Device-Id') or request.query.get('device_id')
            if not supplied_id and not device and (path.startswith('/xiaozhi/v1') or path == '/device/reminders'):
                supplied_id = 'esp32'
            if path.startswith('/xiaozhi/v1') and not device and supplied_id != (request.headers.get('Device-Id') or 'esp32'):
                return web.json_response({'error':'Device ID không khớp WebSocket.'},status=403)
            if device and ((supplied_id and supplied_id != device['device_id']) or
                           (path.startswith('/xiaozhi/v1') and request.headers.get('Device-Id') != device['device_id'])):
                return web.json_response({'error':'Device ID không thuộc token.'},status=403)
            if not device and supplied_id and await store_call(
                    request.app['auth_store'].store, 'device_id_exists', supplied_id):
                return web.json_response({'error':'Device ID không thuộc token.'},status=403)
            request['device_token'] = token
        response = await handler(request)
    except ConflictError as e:
        response = web.json_response({'error':str(e)},status=409)
    except (ValidationError,ValueError,TypeError,KeyError) as e:
        response = web.json_response({'error':str(e)[:400]},status=400)
    except web.HTTPException as e:
        response = e
    except Exception as e:
        LOG.error('Request failed (%s sqlstate=%s)', type(e).__name__, getattr(e, 'sqlstate', None))
        response = web.json_response({'error':'Server gặp lỗi. Kiểm tra cấu hình và nhật ký.'},status=500)
    if not isinstance(response,web.WebSocketResponse):
        response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Cache-Control':'no-store','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
    return response


async def body(request):
    data = await request.json()
    if not isinstance(data,dict):
        raise ValidationError('Nội dung phải là JSON object.')
    return data


async def index(request):
    if not _web_auth(request):
        if request.app['config']['db_backend'] == 'supabase':
            return web.FileResponse(ROOT/'static/google-login.html')
        return web.Response(text='''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Đăng nhập Chatbot</title><link rel="stylesheet" href="/static/style.css"><body><main class="auth-page"><h1>Thời khóa biểu AI</h1><form id="auth-form"><label>Tên đăng nhập <input name="username" required autocomplete="username" maxlength="80"></label><label>Mật khẩu <input name="password" type="password" required autocomplete="current-password" minlength="12" maxlength="1024"></label><div><button type="submit">Đăng nhập</button> <button type="button" id="register">Đăng ký</button></div><p id="auth-error" role="alert"></p></form><details><summary>Đăng nhập bằng mã cũ</summary><form action="/login"><label>Mã truy cập web <input name="token" type="password" required autocomplete="off"></label><button>Đăng nhập</button></form></details></main><script src="/static/app.js" defer></script></body></html>''',content_type='text/html')
    return web.FileResponse(ROOT/'static/index.html')


async def google_start(request):
    oauth = request.app['google_oauth']
    attempts = request.app['oauth_attempts']
    peers = request.app['oauth_start_attempts']
    stamp = time.monotonic()
    for key, item in list(attempts.items()):
        if item[3] < stamp:
            attempts.pop(key, None)
    for peer, queue in list(peers.items()):
        while queue and queue[0] <= stamp - 60:
            queue.popleft()
        if not queue:
            del peers[peer]
    peer = request.remote or 'unknown'  # Do not trust X-Forwarded-For.
    if len(attempts) >= 1024 or (peer not in peers and len(peers) >= 1024):
        raise web.HTTPTooManyRequests(headers={'Retry-After':'60'})
    queue = peers.setdefault(peer, deque())
    if len(queue) >= 10:
        raise web.HTTPTooManyRequests(headers={'Retry-After':'60'})
    url, state, nonce, verifier = oauth.authorization_url()
    queue.append(stamp)
    attempts[state] = (nonce, verifier, peer, stamp + 300)
    response = web.HTTPFound(url)
    response.set_cookie('chatbot_oauth_state', state, httponly=True, secure=oauth.redirect_uri.startswith('https://'),
                        samesite='Lax', max_age=300, path='/auth/google/callback')
    return response


async def google_callback(request):
    state = request.query.get('state', '')
    cookie = request.cookies.get('chatbot_oauth_state', '')
    attempts = request.app['oauth_attempts']
    pending = attempts.pop(state, None) if _equal(state, cookie) else None
    if not pending or pending[3] < time.monotonic() or request.query.get('error') or not request.query.get('code'):
        raise web.HTTPBadRequest(text='Google OAuth state không hợp lệ hoặc đã hết hạn.')
    nonce, verifier, _, _ = pending
    identity = await request.app['google_oauth'].exchange(request.query['code'], verifier, nonce)
    user = await store_call(request.app['auth_store'].store, 'upsert_google_user',
                            identity['sub'], identity['email'], identity['name'])
    auth = request.app['auth_store']
    auth.revoke_session(request.cookies.get(SESSION_COOKIE, ''))
    response = web.HTTPFound('/')
    _set_auth_cookies(request, response, auth.create_session(user))
    response.del_cookie('chatbot_oauth_state', path='/auth/google/callback')
    return response


async def login(request):
    if not _equal(request.query.get('token',''),request.app['config']['web_token']):
        return web.Response(text='Mã truy cập không đúng.',status=401)
    response = web.HTTPFound('/')
    token = request.app['auth_store'].create_session({'id':'default','username':'default'})
    _set_auth_cookies(request, response, token)
    return response


async def account(request):
    action = request.match_info.get('action', 'me')
    auth = request.app['auth_store']
    if action == 'me' and request.method == 'GET':
        return web.json_response({'user': request['user']})
    if action == 'logout' and request.method == 'POST':
        auth.revoke_session(request.cookies.get(SESSION_COOKIE, ''))
        response = web.json_response({'ok': True})
        _clear_auth_cookies(request, response)
        return response
    if request.app['config']['db_backend'] == 'supabase' or action not in ('register', 'login') or request.method != 'POST':
        raise web.HTTPNotFound()
    data = await body(request)
    if set(data) != {'username', 'password'}:
        raise ValidationError('Cần tên đăng nhập và mật khẩu.')
    username, password = data['username'], data['password']
    if not isinstance(username, str) or not 1 <= len(username.strip()) <= 80 or username.strip().lower() == 'default':
        raise ValidationError('Tên đăng nhập không hợp lệ.')
    if not isinstance(password, str) or not 1 <= len(password) <= 1024 or (action == 'register' and len(password) < 12):
        raise ValidationError('Mật khẩu cần 12–1024 ký tự khi đăng ký.')
    user = (auth.store.create_user(username, password) if action == 'register'
            else auth.store.authenticate_user(username, password))
    if not user:
        return web.json_response({'error':'Tên đăng nhập hoặc mật khẩu không đúng.'},status=401)
    auth.revoke_session(request.cookies.get(SESSION_COOKIE, ''))
    response = web.json_response({'user':user},status=201 if action == 'register' else 200)
    _set_auth_cookies(request, response, auth.create_session(user))
    return response


async def devices_api(request):
    user_id = request['user_id']
    if user_id == 'default':
        return web.json_response({'error':'Đăng nhập tài khoản để quản lý thiết bị.'},status=400)
    store = request.app['auth_store'].store
    if request.method == 'GET':
        return web.json_response({'devices':await store_call(store, 'list_devices', user_id)})
    if request.method == 'DELETE':
        if not await store_call(store, 'revoke_device', user_id, request.match_info['id']):
            raise web.HTTPNotFound()
        socket = request.app['voice_sessions'].get(request.match_info['id'])
        if socket:
            await socket.close(code=1008, message=b'Device revoked')
        return web.json_response({'ok':True})
    data = await body(request)
    if set(data) - {'device_id','name'} or 'device_id' not in data:
        raise ValidationError('Device ID không hợp lệ.')
    return web.json_response(await store_call(store, 'register_device', user_id, data['device_id'], data.get('name','Device')),status=201)


async def status(request):
    app = request.app
    user_id = request['user_id']
    auth_store = app['auth_store'].store
    registered = None if user_id == 'default' else await store_call(auth_store, 'list_devices', user_id)
    devices = []
    if registered is not None:
        for device in registered:
            activity = app['devices'].get(device['device_id'], {})
            devices.append({'id':device['device_id'], 'name':device['name'],
                'online':time.monotonic()-activity.get('last_seen',0)<30,
                'busy':activity.get('busy',False)})
    else:
        for k, v in app['devices'].items():
            if not await store_call(auth_store, 'device_id_exists', k):
                devices.append({'id':k,'online':time.monotonic()-v.get('last_seen',0)<30,'busy':v.get('busy',False)})
    return web.json_response({'ai_configured':bool(app['config']['api_key']),'model':app['config']['model'],
        'timezone':'Asia/Ho_Chi_Minh','now':now().isoformat(),'devices':devices,'voice_mode':'button-turn','last_audio_mode':app['last_audio_mode']})


async def events(request):
    store = request.app['store']
    if request.method == 'GET':
        week = monday(request.query.get('week'))
        end = (date.fromisoformat(week)+timedelta(days=6)).isoformat()
        return web.json_response({'week':week,'events':await store_call(store, 'list_events', week, end, user_id=request['user_id'])})
    return web.json_response(await store_call(store, 'create_event', await body(request), user_id=request['user_id']),status=201)


async def event(request):
    data = await body(request)
    revision = data.pop('revision',None)
    if type(revision) is not int:
        raise ValidationError('Thiếu revision hợp lệ. Tải lại lịch trước khi sửa.')
    store = request.app['store']
    if request.method == 'DELETE':
        if data:
            raise ValidationError('DELETE chỉ nhận revision.')
        await store_call(store, 'delete_event', request.match_info['id'], revision, user_id=request['user_id'])
        return web.json_response({'ok':True})
    return web.json_response(await store_call(store, 'update_event', request.match_info['id'], data, revision, user_id=request['user_id']))


async def copy_week(request):
    data = await body(request)
    if set(data) != {'source_week','target_week'}:
        raise ValidationError('Cần source_week và target_week.')
    return web.json_response({'events':await store_call(request.app['store'], 'copy_week', monday(data['source_week']), monday(data['target_week']), user_id=request['user_id'])})


async def agenda(request):
    current = now()
    all_events = await store_call(request.app['store'], 'list_events', current.date().isoformat(), (current.date()+timedelta(days=30)).isoformat(), user_id=request['user_id'])
    return web.json_response({'today':[e for e in all_events if e['date']==current.date().isoformat()],
        'upcoming':[e for e in all_events if e['date']+'T'+e['end'] > current.strftime('%Y-%m-%dT%H:%M')][:12]})


async def chat(request):
    data = await body(request)
    session = data.get('session_id','web')
    message = data.get('message','')
    if not isinstance(session,str) or not 1<=len(session)<=80 or not isinstance(message,str) or len(message)>4000:
        raise ValidationError('Phiên chat hoặc tin nhắn không hợp lệ.')
    if not request.app['config']['api_key']:
        return web.json_response({'error':'Chưa cấu hình GEMINI_API_KEY trong .env. Lịch thủ công vẫn dùng bình thường.'},status=503)
    try:
        kwargs = {}
        if request['user_id'] != 'default':
            kwargs['preferences'] = await store_call(request.app['auth_store'].store,
                'get_user_preferences', request['user_id'])
        result = await asyncio.wait_for(request.app['agent'].chat('web:'+session,message,
            selected_week=data.get('week'),confirmation=data.get('confirmation'),user_id=request['user_id'], **kwargs),90)
        return web.json_response(result)
    except (ValidationError,ConflictError):
        raise
    except Exception as e:
        LOG.warning('Gemini request failed (%s)',type(e).__name__)
        return web.json_response({'error':'Không gọi được Gemini. Kiểm tra key, model, hạn mức và Internet.'},status=503)


async def reminders(request):
    return web.json_response({'reminders':await store_call(request.app['store'], 'reminder_history', user_id=request['user_id'])})


async def ota(request):
    cfg = request.app['config']
    base = _device_origin(request)
    wsbase = ('wss://' if base.startswith('https://') else 'ws://')+base.split('://',1)[1]
    return web.json_response({'server_time':{'timestamp':int(time.time()*1000),'timezone_offset':420},
        'firmware':{'version':'0.0.0','url':''},
        'websocket':{'url':wsbase+'/xiaozhi/v1/','token':request['device_token'],'version':1}})


async def refresh_reminders(app):
    async with app['reminder_lock']:
        due = []
        auth_ids = await store_call(app['auth_store'].store, 'list_user_ids')
        for user_id in (['default'] if app['config']['db_backend'] == 'sqlite' else []) + auth_ids:
            due.extend({**item, 'user_id':user_id} for item in await store_call(app['store'], 'due_reminders', now(), user_id=user_id))
        valid = {d['id'] for d in due}
        for key in list(app['ready']):
            if key not in valid:
                app['ready'].pop(key,None)
        for item in due[:20]:
            if item['id'] in app['ready']:
                continue
            audio,mode = await app['speech'].reminder(item['text'])
            # Recheck after remote TTS: the event may have changed while awaiting the network.
            if not any(d['id']==item['id'] for d in await store_call(app['store'], 'due_reminders', now(), user_id=item['user_id'])):
                continue
            app['ready'][item['id']] = {**item,'audio':audio,'mode':mode}
            app['last_audio_mode'] = mode
        stamp = time.monotonic()
        for cap in list(app['media']):
            if app['media'][cap]['expires']<stamp or app['media'][cap]['reminder_id'] not in valid:
                app['media'].pop(cap,None)
        for identifier in list(app['issued']):
            if app['issued'][identifier]['expires']<stamp or identifier not in valid:
                app['issued'].pop(identifier,None)


async def reminder_loop(app):
    while True:
        try:
            await refresh_reminders(app)
        except Exception as e:
            LOG.warning('Reminder worker failed (%s)',type(e).__name__)
        await asyncio.sleep(3)


async def poll_reminder(request):
    app=request.app
    device_id=request['device_id'] or request.query.get('device_id','esp32')
    if not 1<=len(device_id)<=80:
        raise ValidationError('Device ID không hợp lệ.')
    device=app['devices'].setdefault(device_id,{})
    device.update(last_seen=time.monotonic())
    if device.get('busy'):
        return web.json_response({'notification':None})
    valid={d['id'] for d in await store_call(app['store'], 'due_reminders', now(), user_id=request['user_id'])}
    for identifier,item in list(app['ready'].items()):
        if identifier not in valid:
            continue
        previous=app['issued'].get(identifier)
        if previous and previous['expires']>time.monotonic():
            continue
        capability=secrets.token_urlsafe(32)
        app['media'][capability]={'audio':item['audio'],'expires':time.monotonic()+300,'reminder_id':identifier,'user_id':request['user_id']}
        app['issued'][identifier]={'device_id':device_id,'user_id':request['user_id'],'expires':time.monotonic()+120}
        return web.json_response({'notification':{'id':identifier,'text':item['text'],
            'audio_url':_device_origin(request)+'/media/'+capability+'.ogg','mode':item['mode']}})
    return web.json_response({'notification':None})


async def ack(request):
    app=request.app
    data=await body(request)
    identifier, state=data.get('id'),data.get('status')
    if not isinstance(identifier,str) or state not in ('played','failed','cancelled'):
        raise ValidationError('ACK không hợp lệ.')
    issued=app['issued'].get(identifier)
    if not issued or issued['expires']<time.monotonic() or issued['user_id'] != request['user_id'] or (request['device_id'] and issued['device_id'] != request['device_id']):
        raise ValidationError('Thông báo chưa được cấp hoặc ACK đã hết hạn.')
    if not any(d['id']==identifier for d in await store_call(app['store'], 'due_reminders', now(), user_id=request['user_id'])):
        app['issued'].pop(identifier,None)
        raise ValidationError('Lịch đã thay đổi hoặc thông báo hết hạn.')
    await store_call(app['store'], 'mark_reminder', identifier, state, user_id=request['user_id'])
    if state=='played':
        app['ready'].pop(identifier,None)
        app['issued'].pop(identifier,None)
    else:
        issued['expires']=time.monotonic()+15
    return web.json_response({'ok':True})


async def media(request):
    entry=request.app['media'].get(request.match_info['token'])
    if not entry or entry['expires']<time.monotonic():
        raise web.HTTPNotFound()
    valid = {item['id'] for item in await store_call(request.app['store'], 'due_reminders', now(), user_id=entry['user_id'])}
    if entry['reminder_id'] not in valid:
        request.app['media'].pop(request.match_info['token'], None)
        raise web.HTTPNotFound()
    return web.Response(body=entry['audio'],content_type='audio/ogg')


async def lifecycle(app):
    from speech_vad import _session
    await asyncio.to_thread(_session)  # Load/check once before accepting captures.
    LOG.info('Local speech VAD ready (Silero CPU 16kHz)')
    task=asyncio.create_task(reminder_loop(app)) if app['config']['background'] else None
    yield
    if task:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    for ws in list(app['voice_sessions'].values()):
        await ws.close()
    await app['speech'].close()
    # Agent may own a GenAI client; do not assume a private implementation.
    close=getattr(app['agent'],'close',None)
    if close:
        result=close()
        if asyncio.iscoroutine(result):
            await result
    app['store'].close()
    app['auth_store'].close()


def create_app(config=None):
    cfg={'model':'gemini-flash-latest','tts_model':'gemini-3.1-flash-tts-preview','background':True,
         'public_base_url':'http://127.0.0.1:8000',**(config or settings())}
    cfg['db_backend'] = cfg.get('db_backend', os.environ.get('DB_BACKEND', 'sqlite')).lower()
    if cfg['db_backend'] not in ('sqlite', 'supabase'):
        raise ValueError('Unknown database backend')
    if cfg['db_backend'] == 'supabase':
        for key in ('google_client_id', 'google_client_secret', 'google_redirect_uri'):
            if not cfg.get(key):
                raise ValueError('GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET và GOOGLE_REDIRECT_URI là bắt buộc.')
        if cfg['google_redirect_uri'] != cfg['public_base_url'] + '/auth/google/callback':
            raise ValueError('GOOGLE_REDIRECT_URI phải khớp PUBLIC_BASE_URL + /auth/google/callback.')
        oauth = GoogleOAuth(cfg['google_client_id'], cfg['google_client_secret'], cfg['google_redirect_uri'])
    provider_name = cfg.get('ai_provider', 'gemini')
    if provider_name == 'gemini':
        cfg['model'] = _safe_model(cfg['model'])
    if cfg['db_backend'] == 'supabase':
        # Cloud mode never reads or creates the local preferences file.
        initial = Preferences.__new__(Preferences)
        initial.path = Path(cfg.get('preferences_path', ROOT/'data'/'assistant_preferences.json'))
        initial.defaults = {**DEFAULTS, 'model': cfg['model']}
        initial.values = initial.defaults.copy()
    else:
        initial = Preferences(cfg.get('preferences_path', ROOT/'data'/'assistant_preferences.json'),
                              {'model': cfg['model']})
    if provider_name == 'gemini' and initial.values['model'] not in PREFERENCE_MODELS:
        initial.values['model'] = cfg['model']
    if provider_name == 'openai_compatible':
        initial.values['model'] = cfg['model']
    cfg['model'] = initial.values['model']
    cfg['assistant_name'] = initial.values['assistant_name']
    cfg['style_prompt'] = initial.values['style_prompt']
    cfg['voice'] = initial.values['voice']
    cfg['preferences_path'] = str(initial.path)
    parsed=urlsplit(cfg['public_base_url'])
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query:
        raise ValueError('PUBLIC_BASE_URL phải là http(s)://IP:port, không chứa đường dẫn hay thông tin đăng nhập.')
    for key in ('web_token','device_token'):
        if not isinstance(cfg.get(key),str) or len(cfg[key])<24:
            raise ValueError(key+' cần tối thiểu 24 ký tự.')
    if cfg['db_backend'] == 'sqlite':
        Path(cfg['database']).parent.mkdir(parents=True,exist_ok=True)
    app=web.Application(middlewares=[boundary],client_max_size=32768)
    app['config']=cfg
    app['auth_attempts']={}
    if cfg['db_backend'] == 'supabase':
        app['google_oauth'] = oauth
        app['oauth_attempts'] = {}
        app['oauth_start_attempts'] = {}

    provider = create_provider(cfg)
    app['store']=create_store(cfg)
    try:
        app['auth_store']=Auth(store=create_auth_store(cfg))
    except BaseException:
        app['store'].close()
        raise
    app['agent']=GeminiAgent(app['store'],api_key=cfg['api_key'],model=cfg['model'],
        api_keys=cfg.get('api_keys',()),provider=provider if provider_name == 'openai_compatible' else None)
    app['agent'].assistant_name = cfg['assistant_name']
    app['agent'].style_prompt = cfg['style_prompt']
    speech_key = cfg.get('gemini_api_key', cfg['api_key'] if provider_name == 'gemini' else '')
    speech_keys = cfg.get('gemini_api_keys', cfg.get('api_keys', ()) if provider_name == 'gemini' else ())
    speech_model = cfg.get('gemini_model', cfg['model'] if provider_name == 'gemini' else 'gemini-flash-latest')
    app['speech']=Speech(speech_key,speech_model,cfg['tts_model'],api_keys=speech_keys)
    app['speech'].voice = cfg['voice']
    app['preferences'] = initial
    app['preferences'].values.update({key: cfg[key] for key in ('assistant_name', 'style_prompt', 'model', 'voice')})
    app['preferences'].defaults.update({'model': cfg['model']})
    _apply_preferences(app, app['preferences'].values)
    for name in ('devices','voice_sessions','ready','issued','media'):
        app[name]={}
    app['reminder_lock']=asyncio.Lock()
    app['last_audio_mode']='none'
    app.cleanup_ctx.append(lifecycle)
    routes = [web.get('/',index), web.get('/api/status',status), web.get('/api/auth/me',account),
        web.post('/api/auth/{action}',account),
        web.get('/api/devices',devices_api),web.post('/api/devices',devices_api),web.delete('/api/devices/{id}',devices_api),
        web.get('/api/preferences',preferences_api),web.put('/api/preferences',preferences_api),
        web.get('/api/events',events),web.post('/api/events',events),web.put('/api/events/{id}',event),
        web.delete('/api/events/{id}',event),web.post('/api/weeks/copy',copy_week),
        web.get('/api/agenda',agenda),web.get('/api/reminders',reminders),web.post('/api/chat',chat),
        web.post('/xiaozhi/ota/',ota),web.post('/xiaozhi/ota',ota),web.get('/xiaozhi/v1/',device_socket),
        web.get('/device/reminders',poll_reminder),web.post('/device/ack',ack),web.get('/media/{token}.ogg',media)]
    if cfg['db_backend'] == 'supabase':
        routes.extend((web.get('/auth/google/start', google_start),
                       web.get('/auth/google/callback', google_callback)))
    else:
        routes.append(web.get('/login', login))
    app.add_routes(routes)
    static=ROOT/'static'
    static.mkdir(exist_ok=True)
    app.router.add_static('/static/',static,show_index=False)
    return app


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    cfg=settings()
    configure_voice_file_logging(ROOT/'data'/'voice.log')
    print(f"Web: http://127.0.0.1:{cfg['port']}/")
    print('Dữ liệu: '+cfg['database'])
    print('Gemini: '+('đã cấu hình (chưa kiểm tra kết nối)' if cfg['api_key'] else 'chưa có key; lịch thủ công vẫn hoạt động'))
    print('Đặt PUBLIC_BASE_URL thành IP LAN của máy để ESP truy cập. Không mở cổng router ra Internet.')
    web.run_app(create_app(cfg),host=cfg['host'],port=cfg['port'],access_log=None)
