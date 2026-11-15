import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
from html import escape
import json
import logging
from pathlib import Path
import re
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from assistant_service import Assistant, GroqLLM, safety_reply
from calendar_service import CalendarService
from communications import DemoMailer, Mailer, SMSSender
from demo import DemoLLM, MemoryCalendar
from jobs import Jobs
from session_store import RateLimiter, SessionStore
from settings import Settings

log = logging.getLogger('offvoice')
BASE_DIR = Path(__file__).parent
COOKIE = 'offvoice_session'


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=10000)
    request_id: UUID = Field(default_factory=uuid4)
    callback_allowed: bool = False


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    callback_allowed: bool


class BodyLimit:
    def __init__(self, app, maximum=16000):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH'}:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            item = await receive()
            if item['type'] == 'http.disconnect':
                return
            size += len(item.get('body', b''))
            if size > self.maximum:
                return await JSONResponse({'detail': 'Το μήνυμα είναι πολύ μεγάλο.'}, status_code=413)(scope, receive, send)
            chunks.append(item.get('body', b''))
            if not item.get('more_body'):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)


def create_app(settings=None, calendar=None, llm=None, mailer=None, sms=None, start_jobs=True):
    cfg = settings or Settings.from_env()
    store = SessionStore(cfg)
    limiter = RateLimiter(cfg.session_secret)
    memory = MemoryCalendar() if cfg.demo and calendar is None else None
    calendar = calendar or CalendarService(cfg, service_factory=(lambda: memory) if memory else None)
    if cfg.demo:
        mailer = mailer or DemoMailer()
        mailer.settings = cfg
    else:
        mailer = mailer or Mailer(cfg)
    sms = sms or SMSSender(cfg)

    @asynccontextmanager
    async def lifespan(app):
        cfg.validate()
        assistant_llm = llm or (DemoLLM() if cfg.demo else GroqLLM(cfg))
        app.state.assistant = Assistant(cfg, calendar, assistant_llm, limiter)
        app.state.jobs = Jobs(cfg, calendar, store, mailer, sms)
        task = asyncio.create_task(app.state.jobs.run()) if start_jobs else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            close = getattr(assistant_llm, 'close', None)
            if close:
                await close()
            store.sessions.clear()

    app = FastAPI(title='OffVoice AI', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    app.state.settings, app.state.sessions = cfg, store
    app.state.calendar, app.state.mailer = calendar, mailer

    @app.middleware('http')
    async def security(request, call_next):
        if request.method in {'POST', 'PUT', 'DELETE'}:
            expected = cfg.public_origin or str(request.base_url).rstrip('/')
            origin = request.headers.get('origin')
            if (origin and origin.rstrip('/') != expected) or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': 'Μη επιτρεπτή προέλευση.'}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'SAMEORIGIN',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'",
        })
        if cfg.secure_cookie:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def identity(request):
        # Do not trust arbitrary X-Forwarded-For headers; configure trusted proxies in Uvicorn.
        return request.client.host if request.client else 'unknown'

    def session_for(request, response):
        session = store.get(request.cookies.get(COOKIE))
        if not session:
            if not limiter.allow(identity(request), 'new-session', 15, 600):
                raise HTTPException(429, 'Πολλές νέες συνομιλίες. Παρακαλώ δοκιμάστε αργότερα.')
            try:
                session = store.create()
            except OverflowError:
                raise HTTPException(503, 'Η υπηρεσία εξυπηρετεί πολλά αιτήματα. Δοκιμάστε σε λίγο.') from None
        response.set_cookie(COOKIE, session.token, httponly=True, secure=cfg.secure_cookie, samesite='lax', path='/')
        return session

    @app.get('/', response_class=HTMLResponse)
    async def homepage(request: Request, response: Response):
        session_for(request, response)
        return (BASE_DIR / 'static' / 'index.html').read_text()

    @app.get('/config')
    async def public_config():
        summary = ('Δοκιμή με εικονικά στοιχεία. Συνομιλείτε με βοηθό AI που χρησιμοποιεί εξωτερικούς παρόχους. Μην καταχωρίζετε πραγματικά στοιχεία ασθενών.'
                   if cfg.demo else
                   f'Συνομιλείτε με τον βοηθό AI του {cfg.clinic_name}. Τα στοιχεία σας χρησιμοποιούνται για ραντεβού μέσω εξωτερικών παρόχων AI, ημερολογίου και email. Μην στέλνετε εξετάσεις ή αναλυτικό ιατρικό ιστορικό.')
        return {'clinic_name': cfg.clinic_name, 'clinic_phone': cfg.clinic_phone, 'demo': cfg.demo,
                'privacy_summary': summary,
                'privacy_notice': cfg.privacy_notice or ('Χρησιμοποιήστε αποκλειστικά εικονικά στοιχεία.' if cfg.demo else 'Τα απαραίτητα στοιχεία χρησιμοποιούνται για τη διαχείριση ραντεβού και την επικοινωνία με τη γραμματεία. Αναλυτικές πληροφορίες και δικαιώματα στη σελίδα προσωπικών δεδομένων.'),
                'session_minutes': cfg.session_seconds // 60, 'abandoned_minutes': cfg.abandoned_seconds // 60,
                'max_message_chars': cfg.max_message_chars}

    @app.get('/privacy', response_class=HTMLResponse)
    async def privacy():
        # Reading the notice must not require or create a chat session.
        values = {
            'CLINIC_NAME': cfg.clinic_name,
            'SESSION_MINUTES': str(cfg.session_seconds // 60),
            'ABANDONED_MINUTES': str(cfg.abandoned_seconds // 60),
            'CLINIC_NOTICE': cfg.privacy_notice or 'Δεν έχει συμπληρωθεί ειδική ενημέρωση ιατρείου.',
            'CONTEXT_NOTICE': ('Η λειτουργία Demo χρησιμοποιεί προσομοιωμένες υπηρεσίες και μόνο εικονικά στοιχεία.'
                               if cfg.demo else
                               'Η ενημέρωση αφορά τη διαχείριση αιτημάτων ραντεβού μέσω βοηθού AI. Για πληροφορίες και άσκηση δικαιωμάτων απευθυνθείτε στο ιατρείο.'),
        }
        template = (BASE_DIR / 'static' / 'privacy.html').read_text()
        return re.sub(r'\{\{([A-Z_]+)\}\}', lambda m: escape(values[m[1]]), template)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'mode': 'demo' if cfg.demo else 'configured', 'reminders_enabled': cfg.reminders_enabled}

    @app.post('/chat')
    async def chat(payload: ChatRequest, request: Request, response: Response, background_tasks: BackgroundTasks):
        text = payload.message.strip()
        if not text or len(text) > cfg.max_message_chars:
            raise HTTPException(422, f'Το μήνυμα πρέπει να έχει 1–{cfg.max_message_chars} χαρακτήρες.')
        if not limiter.allow(identity(request), 'chat', cfg.chat_per_minute):
            raise HTTPException(429, 'Πολλά μηνύματα. Περιμένετε λίγο πριν συνεχίσετε.')
        session = session_for(request, response)
        async with session.lock:
            request_id = str(payload.request_id)
            digest = hashlib.sha256(json.dumps([text, payload.callback_allowed]).encode()).hexdigest()
            if request_id in session.responses:
                original_digest, cached = session.responses[request_id]
                if digest != original_digest:
                    raise HTTPException(409, 'Ο ίδιος κωδικός αιτήματος χρησιμοποιήθηκε με διαφορετικό περιεχόμενο.')
                return cached
            session.last_seen = store.now()
            session.callback_consent = payload.callback_allowed
            if not payload.callback_allowed:
                session.clear_contact()
            safe = safety_reply(text)
            if safe:
                result = {'response': safe, 'action_completed': False}
            else:
                result = await app.state.assistant.run(session, text, request_id, identity(request), payload.callback_allowed)
            session.responses[request_id] = (digest, result)
            while len(session.responses) > 20:
                session.responses.pop(next(iter(session.responses)))
            if result.get('action_completed'):
                async def flush_safely():
                    try:
                        await asyncio.to_thread(app.state.jobs.flush_notifications)
                    except Exception:
                        log.warning('appointment_notification_remains_in_calendar_outbox')
                background_tasks.add_task(flush_safely)
            return result

    @app.post('/callback-consent')
    async def callback_consent(payload: ConsentRequest, request: Request, response: Response):
        session = session_for(request, response)
        async with session.lock:
            session.callback_consent = payload.callback_allowed
            if not payload.callback_allowed:
                session.clear_contact()
        return {'ok': True}

    @app.post('/reset')
    async def reset(request: Request, response: Response):
        session = store.get(request.cookies.get(COOKIE))
        if session:
            async with session.lock:
                session.clear_contact()
                session.turns.clear()
                session.responses.clear()
                store.discard(session.token)
        response.delete_cookie(COOKIE, path='/', secure=cfg.secure_cookie, httponly=True, samesite='lax')
        return {'status': 'ok'}

    app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
    return app


app = create_app()
