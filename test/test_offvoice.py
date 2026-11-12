import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import httpx
import pytest

from assistant_service import GroqLLM, safety_reply
from calendar_service import BookingError, CalendarService, normalize_phone, private
from communications import DeliveryUnknown, DemoMailer, Mailer, SMSSender
from demo import MemoryCalendar
from jobs import Jobs
from main import COOKIE, create_app
from manage_appointment import record_attendance
from monthly_report import build_report
from settings import Settings


class ScriptedLLM:
    def __init__(self):
        self.queue, self.calls = [], []

    async def complete(self, messages):
        self.calls.append(copy.deepcopy(messages))
        item = self.queue.pop(0) if self.queue else {'role': 'assistant', 'content': 'Πώς μπορώ να σας εξυπηρετήσω;'}
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


def calls(*items):
    return {'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': str(uuid4()),
            'function': {'name': name, 'arguments': json.dumps(args)}} for name, args in items]}


@pytest.fixture
def env():
    now = [datetime(2026, 11, 12, 12, tzinfo=ZoneInfo('Europe/Athens'))]
    monotonic = [1000.0]
    cfg = Settings(demo=True, secure_cookie=False, session_secret='test-only-' * 8,
                   calendar_id='synthetic-calendar', public_origin='http://testserver', minimum_notice_minutes=0)
    memory = MemoryCalendar()
    calendar = CalendarService(cfg, lambda: memory, lambda: now[0])
    llm, mailer = ScriptedLLM(), DemoMailer()
    app = create_app(cfg, calendar=calendar, llm=llm, mailer=mailer, start_jobs=False)
    app.state.sessions.now = lambda: monotonic[0]
    with TestClient(app) as client:
        client.get('/')
        yield SimpleNamespace(cfg=cfg, now=now, monotonic=monotonic, memory=memory, calendar=calendar,
                              llm=llm, mailer=mailer, app=app, client=client, jobs=app.state.jobs)


def book(env, at='2026-11-16T10:00', op=None):
    return env.calendar.create('Synthetic Patient', '6900000000', 'Έλεγχος', at, op or uuid4().hex)


def current_session(env, client=None):
    return env.app.state.sessions.get((client or env.client).cookies.get(COOKIE))


def post(env, text='Δοκιμή', **kwargs):
    return env.client.post('/chat', json={'message': text, **kwargs})


def test_visitors_have_separate_context_and_reset(env):
    other = TestClient(env.app)
    other.get('/')
    post(env, 'PRIVATE_A')
    other.post('/chat', json={'message': 'PRIVATE_B'})
    assert 'PRIVATE_A' not in json.dumps(env.llm.calls[-1])
    token_a, token_b = env.client.cookies.get(COOKIE), other.cookies.get(COOKIE)
    assert token_a != token_b
    other.post('/reset', json={})
    assert env.app.state.sessions.get(token_a).turns
    assert env.app.state.sessions.get(token_b) is None


def test_shared_calendar_busy_event_is_respected(env):
    env.memory.items['manual'] = {'id': 'manual', 'status': 'confirmed', 'summary': 'Secretary booking',
        'start': {'dateTime': '2026-11-16T10:00:00+02:00'}, 'end': {'dateTime': '2026-11-16T10:30:00+02:00'}}
    with pytest.raises(BookingError, match='δεν είναι διαθέσιμη'):
        book(env)
    assert len(env.memory.items) == 1


def test_reschedule_rejects_overlap_and_keeps_original(env):
    a = book(env)
    book(env, '2026-11-16T11:00')
    with pytest.raises(BookingError) as error:
        env.calendar.reschedule(a['booking_code'], '2026-11-16T11:00', uuid4().hex)
    assert error.value.code == 'SLOT_TAKEN'
    assert env.calendar.find(a['booking_code'])['start'].startswith('2026-11-16T10:00')


def test_reschedule_can_keep_own_slot(env):
    a = book(env)
    result = env.calendar.reschedule(a['booking_code'], a['start'], uuid4().hex)
    assert result['ok']


@pytest.mark.parametrize('date,code', [
    ('2026-11-11T10:00', 'PAST_OR_TOO_SOON'), ('2026-11-14T10:00', 'CLOSED_DAY'),
    ('2026-11-16T16:45', 'OUT_OF_HOURS'), ('2026-11-16T08:45', 'OUT_OF_HOURS'),
    ('2028-01-03T10:00', 'TOO_FAR'), ('not-a-date', 'INVALID_DATE'),
    ('2026-11-16T10:00:30', 'INVALID_DATE'),
])
def test_booking_boundaries(env, date, code):
    with pytest.raises(BookingError) as error:
        book(env, date)
    assert error.value.code == code
    assert not env.memory.items


def test_last_valid_slot_and_authoritative_duration(env):
    result = book(env, '2026-11-16T16:30')
    event = next(iter(env.memory.items.values()))
    assert result['ok'] and event['end']['dateTime'].startswith('2026-11-16T17:00')


def test_explicit_utc_offset_is_converted(env):
    result = book(env, '2026-11-16T09:00:00+00:00')
    assert result['start'].startswith('2026-11-16T11:00:00+02:00')


def test_exact_code_only_and_no_other_patient_data(env):
    a = book(env)
    result = env.calendar.find(a['booking_code'].lower())
    assert 'Synthetic Patient' not in json.dumps(result)
    assert '6900000000' not in json.dumps(result)
    for query in ('6900000000', a['booking_code'][:3], 'OFFVOICE', 'Patient'):
        with pytest.raises(BookingError):
            env.calendar.find(query)


def test_unrelated_event_cannot_be_cancelled(env):
    env.memory.items['unrelated'] = {'id': 'unrelated', 'summary': 'ABC123 synthetic private meeting',
        'start': {'dateTime': '2026-11-16T10:00:00+02:00'}, 'end': {'dateTime': '2026-11-16T10:30:00+02:00'}}
    with pytest.raises(BookingError):
        env.calendar.cancel('ABC123', uuid4().hex)
    assert 'unrelated' in env.memory.items


def test_legacy_exact_code_and_pagination(env):
    for i, code in enumerate(('AAAAAA', 'BBBBBB', 'CCCCCC')):
        env.memory.items[str(i)] = {'id': str(i), 'etag': '"1"', 'summary': 'Legacy patient',
            'description': f'🤖 [OffVoice AI] Κωδικός Κράτησης: {code}\nΤηλέφωνο ασθενούς: 6900000000',
            'start': {'dateTime': f'2026-11-16T{10+i:02d}:00:00+02:00'},
            'end': {'dateTime': f'2026-11-16T{10+i:02d}:30:00+02:00'}}
    env.memory.page_size = 1
    assert env.calendar.find('CCCCCC')['ok']
    with env.calendar.service() as service:
        assert len(env.calendar.events(service)) == 3


def test_idempotent_create_and_reschedule(env):
    op = uuid4().hex
    first = book(env, op=op)
    second = book(env, op=op)
    assert first['booking_code'] == second['booking_code'] and len(env.memory.items) == 1
    operation = uuid4().hex
    env.calendar.reschedule(first['booking_code'], '2026-11-16T11:00', operation)
    env.calendar.reschedule(first['booking_code'], '2026-11-16T11:00', operation)
    assert private(next(iter(env.memory.items.values())))['ov_reschedules'] == '1'


def test_concurrent_creates_only_one_succeeds(env):
    from concurrent.futures import ThreadPoolExecutor
    def attempt(_):
        try:
            return book(env)['ok']
        except BookingError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert sorted(results) == [False, True]
    assert len(env.memory.items) == 1


def test_cancel_scrubs_personal_data_and_releases_slot(env):
    original = book(env)
    env.calendar.cancel(original['booking_code'], uuid4().hex)
    cancelled = next(iter(env.memory.items.values()))
    assert private(cancelled)['ov_state'] == 'cancelled'
    assert 'Synthetic Patient' not in json.dumps(cancelled)
    assert '6900000000' not in json.dumps(cancelled)
    assert 'Έλεγχος' not in json.dumps(cancelled, ensure_ascii=False)
    assert cancelled['transparency'] == 'transparent'
    assert book(env)['ok']


def test_availability_hides_busy_patient_details(env):
    book(env)
    result = env.calendar.available('2026-11-16')
    assert not any(value.startswith('2026-11-16T10:00') for value in result['available_starts'])
    assert 'Synthetic Patient' not in json.dumps(result)


def test_lookup_then_cancel_in_one_message(env):
    booking = book(env)
    code = booking['booking_code']
    env.llm.queue = [calls(('find_calendar_event', {'booking_code': code})), calls(('cancel_calendar_event', {'booking_code': code}))]
    response = post(env, f'Ακυρώστε το ραντεβού με κωδικό {code}')
    assert response.json()['action_completed']
    assert env.calendar.find(code)['state'] == 'cancelled'
    assert len(env.llm.calls) == 2


def test_multiple_read_tools_all_receive_results(env):
    booking = book(env)
    env.llm.queue = [calls(('find_calendar_event', {'booking_code': booking['booking_code']}), ('check_availability', {'date_iso': '2026-11-16'}))]
    post(env)
    results = [m for m in env.llm.calls[-1] if m['role'] == 'tool']
    assert len(results) == 2


def test_http_request_id_deduplicates_and_rejects_changed_body(env):
    identifier = str(uuid4())
    a = post(env, 'ONE', request_id=identifier).json()
    b = post(env, 'ONE', request_id=identifier).json()
    assert a == b and len(env.llm.calls) == 1
    assert post(env, 'TWO', request_id=identifier).status_code == 409


def test_no_false_success_and_no_raw_provider_error(env):
    env.llm.queue = [{'content': 'Το ραντεβού καταχωρήθηκε.'}]
    assert 'Το ραντεβού καταχωρήθηκε.' != post(env).json()['response']
    env.llm.queue = [RuntimeError('SECRET_KEY patient@example.test')]
    result = post(env).json()['response']
    assert 'SECRET_KEY' not in result and 'patient@example.test' not in result


def test_email_outbox_preserves_multiple_actions_and_deduplicates(env):
    booking = book(env)
    env.calendar.reschedule(booking['booking_code'], '2026-11-16T11:00', uuid4().hex)
    env.jobs.flush_notifications()
    assert len(env.mailer.sent) == 2
    env.jobs.flush_notifications()
    assert len(env.mailer.sent) == 2
    assert private(next(iter(env.memory.items.values())))['ov_notify_pending'] == 'no'


def test_failed_booking_does_not_queue_new_booking_email(env):
    booking = book(env)
    env.jobs.flush_notifications()
    assert len(env.mailer.sent) == 1
    with pytest.raises(BookingError):
        book(env)
    env.jobs.flush_notifications()
    assert len(env.mailer.sent) == 1


def test_callback_requires_permission_and_actual_contact(env):
    args = {'patient_name': 'Synthetic', 'patient_phone': '6900000000'}
    env.llm.queue = [calls(('record_callback_contact', args))]
    post(env, 'Synthetic 6900000000')
    assert not current_session(env).pending
    env.llm.queue = [calls(('record_callback_contact', args))]
    post(env, 'Synthetic 6900000000', callback_allowed=True)
    assert current_session(env).pending


def test_abandoned_callback_once_and_transcript_expiry(env):
    session = current_session(env)
    session.name, session.phone, session.pending, session.callback_consent = 'Synthetic', '+306900000000', True, True
    session.turns = [[{'role': 'user', 'content': 'PRIVATE'}]]
    env.monotonic[0] += env.cfg.abandoned_seconds + 1
    asyncio.run(env.jobs.sweep_sessions())
    asyncio.run(env.jobs.sweep_sessions())
    assert len(env.mailer.sent) == 1 and 'Ημιτελές' in env.mailer.sent[0]['subject']
    assert not session.phone
    env.monotonic[0] += env.cfg.session_seconds
    asyncio.run(env.jobs.sweep_sessions())
    assert not env.app.state.sessions.sessions and not session.turns


def test_callback_withdrawal_and_reset_stop_pending_contact(env):
    session = current_session(env)
    session.phone, session.pending, session.callback_consent = '+306900000000', True, True
    env.client.post('/callback-consent', json={'callback_allowed': False})
    assert not session.phone and not session.pending
    env.client.post('/reset', json={})
    assert not env.app.state.sessions.sessions


def test_report_does_not_invent_attendance(env):
    a, b = book(env), book(env, '2026-11-16T11:00')
    env.calendar.cancel(b['booking_code'], uuid4().hex)
    env.now[0] = datetime(2026, 11, 17, 12, tzinfo=env.cfg.tz)
    report = build_report(env.calendar, '2026-11')
    assert report['elapsed_scheduled_appointments'] == 1
    assert report['recorded_attended'] == 0 and report['elapsed_attendance_unknown'] == 1
    assert report['cancelled_appointments_in_month'] == 1
    assert report['billable_count_estimate'] is None
    record_attendance(env.calendar, a['booking_code'], 'attended')
    assert build_report(env.calendar, '2026-11')['recorded_attended'] == 1
    assert 'Synthetic Patient' not in json.dumps(report)


def test_confirmation_is_separate_from_attendance(env):
    booking = book(env)
    env.calendar.confirm(booking['booking_code'])
    report = build_report(env.calendar, '2026-11')
    assert report['patient_confirmations_recorded_in_month'] == 1
    assert report['recorded_attended'] == 0


def test_reminders_disabled_by_default(env):
    class NeverSend:
        def send(self, *args):
            pytest.fail('Disabled reminder attempted delivery')
    env.jobs.sms = NeverSend()
    env.jobs.reminders()


def test_reminder_at_22_once_and_unknown_outcome_not_retried(env):
    book(env)
    cfg = replace(env.cfg, reminders_enabled=True)
    sent = []
    class UnknownSMS:
        def send(self, phone, text):
            sent.append((phone, text))
            raise DeliveryUnknown()
    jobs = Jobs(cfg, env.calendar, env.app.state.sessions, env.mailer, UnknownSMS())
    env.now[0] = datetime(2026, 11, 15, 21, 59, tzinfo=cfg.tz)
    jobs.reminders()
    assert not sent
    env.now[0] += timedelta(minutes=1)
    jobs.reminders()
    jobs.reminders()
    assert len(sent) == 1
    assert private(next(iter(env.memory.items.values())))['ov_reminder_state'] == 'unknown'


def test_origin_body_limit_and_session_cookie(env):
    response = env.client.post('/chat', json={'message': 'test'}, headers={'origin': 'https://evil.example'})
    assert response.status_code == 403
    assert env.client.post('/chat', content='x' * 17000).status_code == 413
    cookie = env.client.get('/').headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=lax' in cookie and 'max-age' not in cookie
    assert 'PRIVATE' not in env.client.get('/config').text


def test_clinic_credentials_alias_and_working_hours(monkeypatch):
    monkeypatch.setenv('GOOGLE_CREDENTIALS', '{"legacy":true}')
    monkeypatch.delenv('GOOGLE_CREDENTIALS_JSON', raising=False)
    monkeypatch.setenv('CLINIC_OPEN_TIME', '10:30')
    cfg = Settings.from_env()
    assert cfg.google_credentials_json == '{"legacy":true}' and cfg.open_time.hour == 10 and cfg.open_time.minute == 30


def test_configuration_rejects_incomplete_production():
    with pytest.raises(ValueError, match='Missing configuration'):
        Settings().validate()


@pytest.mark.parametrize('value,expected', [('690 000 0000', '+306900000000'), ('00306900000000', '+306900000000'), ('+44 7700 900123', '+447700900123')])
def test_phone_normalization(value, expected):
    assert normalize_phone(value) == expected


@pytest.mark.parametrize('text', ['Δεν μπορώ να αναπνεύσω', 'Τι φάρμακο να πάρω;', 'Ποια δοσολογία χρειάζεται;'])
def test_administrative_safety_boundaries(text):
    assert safety_reply(text)


def test_email_html_is_escaped_and_idempotency_header(env):
    captured = []
    def responder(request):
        captured.append(request)
        return httpx.Response(200, json={'id': 'synthetic'})
    cfg = replace(env.cfg, resend_key='fake', email_from='clinic@example.test', doctor_email='doctor@example.test')
    mailer = Mailer(cfg, httpx.MockTransport(responder))
    session = SimpleNamespace(name='<img src=x onerror=alert(1)>', phone='+306900000000', token='fake-token', generation=0)
    mailer.abandoned(session)
    body = json.loads(captured[0].content)
    assert '<img' not in body['html'] and '&lt;img' in body['html']
    assert captured[0].headers['Idempotency-Key'].startswith('abandoned-')
    assert 'fake-token' not in captured[0].headers['Idempotency-Key']


@pytest.mark.asyncio
async def test_real_groq_sdk_uses_limits_and_schema(env):
    from groq import AsyncGroq
    captured = []
    def responder(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion', 'created': 1,
            'model': env.cfg.model, 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Γεια σας'}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}})
    adapter = GroqLLM(replace(env.cfg, groq_key='fake'))
    await adapter.close()
    adapter.client = AsyncGroq(api_key='fake', http_client=httpx.AsyncClient(transport=httpx.MockTransport(responder)))
    result = await adapter.complete([{'role': 'user', 'content': 'Γεια'}])
    await adapter.close()
    assert result['content'] == 'Γεια σας'
    assert captured[0]['max_tokens'] == env.cfg.max_output_tokens
    assert captured[0]['tools'] and adapter.input_tokens == 20 and adapter.output_tokens == 5
