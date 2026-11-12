from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest

from calendar_service import BookingError, private
from test_offvoice import book, calls, current_session, env, post
from monthly_report import build_report


def test_successful_booking_cannot_become_an_abandoned_lead(env):
    env.llm.queue = [calls(
        ('create_calendar_event', {'patient_name': 'Synthetic Patient', 'patient_phone': '6900000000',
                                   'visit_type': 'Έλεγχος', 'start_iso': '2026-11-16T10:00'}),
        ('record_callback_contact', {'patient_name': 'Synthetic Patient', 'patient_phone': '6900000000'}),
    )]
    response = post(env, 'Synthetic Patient 6900000000 για έλεγχο Δευτέρα στις 10', callback_allowed=True)
    assert response.json()['action_completed']
    assert not current_session(env).pending and not current_session(env).phone


def test_unknown_write_outcome_stops_automatic_followup_actions(env, monkeypatch):
    count = []
    def uncertain(*args):
        count.append(1)
        raise TimeoutError('Synthetic timeout after possible commit')
    monkeypatch.setattr(env.calendar, 'create', uncertain)
    args = {'patient_name': 'Synthetic', 'patient_phone': '6900000000', 'visit_type': 'Έλεγχος', 'start_iso': '2026-11-16T10:00'}
    env.llm.queue = [calls(('create_calendar_event', args), ('create_calendar_event', args))]
    response = post(env)
    assert len(count) == 1 and len(env.llm.calls) == 1
    assert not response.json()['action_completed']


def test_old_completed_booking_contact_is_not_reused_as_new_abandoned_lead(env):
    args = {'patient_name': 'Synthetic Patient', 'patient_phone': '6900000000', 'visit_type': 'Έλεγχος', 'start_iso': '2026-11-16T10:00'}
    env.llm.queue = [calls(('create_calendar_event', args))]
    assert post(env, 'Synthetic Patient 6900000000 για έλεγχο Δευτέρα 10', callback_allowed=True).json()['action_completed']
    env.llm.queue = [calls(('record_callback_contact', {'patient_name': 'Synthetic Patient', 'patient_phone': '6900000000'}))]
    post(env, 'Ευχαριστώ', callback_allowed=True)
    assert not current_session(env).pending


def test_notification_retry_payload_is_stable_after_cancellation(env):
    from communications import Mailer
    import httpx
    payloads = []
    def capture(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic'})
    cfg = replace(env.cfg, resend_key='fake', email_from='clinic@example.test', doctor_email='doctor@example.test')
    mailer = Mailer(cfg, httpx.MockTransport(capture))
    booking = book(env)
    before = next(iter(env.memory.items.values()))
    queued = next(v for k, v in private(before).items() if k.startswith('ov_mail_'))
    payload = json.loads(queued)
    mailer.appointment(env.calendar, before, payload)
    env.calendar.cancel(booking['booking_code'], uuid4().hex)
    after = next(iter(env.memory.items.values()))
    mailer.appointment(env.calendar, after, payload)
    assert payloads[0] == payloads[1]


def test_old_email_is_flagged_for_review_not_blindly_resent(env):
    book(env)
    env.now[0] += timedelta(hours=24)
    env.jobs.flush_notifications()
    assert not env.mailer.sent
    props = private(next(iter(env.memory.items.values())))
    assert props['ov_review_needed'] == 'yes'
    assert not any(k.startswith('ov_mail_') for k in props)


def test_history_is_bounded_in_complete_turns(env):
    env.app.state.assistant.settings = replace(env.cfg, max_turns=2)
    for i in range(4):
        post(env, f'Synthetic message {i}')
    session = current_session(env)
    assert len(session.turns) == 2
    assert 'Synthetic message 0' not in json.dumps(session.turns)


def test_closed_dates_block_booking(env):
    env.calendar.settings = replace(env.cfg, closed_dates=('2026-11-16',))
    with pytest.raises(BookingError) as error:
        book(env)
    assert error.value.code == 'CLOSED_DAY'


def test_capacity_and_request_rate_are_enforced(env):
    env.app.state.sessions.settings = replace(env.cfg, max_sessions=1)
    with pytest.raises(OverflowError):
        env.app.state.sessions.create()
    for _ in range(env.cfg.chat_per_minute):
        assert post(env).status_code == 200
    assert post(env).status_code == 429


def test_legacy_google_fractional_timestamps_in_report(env):
    env.memory.items['legacy'] = {'id': 'legacy', 'etag': '"1"', 'summary': 'Legacy synthetic',
        'created': '2026-11-12T10:12:23.456Z',
        'description': '🤖 [OffVoice AI] Κωδικός Κράτησης: LEG123\nΤηλέφωνο ασθενούς: 6900000000',
        'start': {'dateTime': '2026-11-16T10:00:00+02:00'},
        'end': {'dateTime': '2026-11-16T10:30:12.345+02:00'}}
    report = build_report(env.calendar, '2026-11')
    assert report['created_in_month'] == 1 and report['recorded_attended'] == 0


def test_availability_accepts_existing_events_with_seconds(env):
    env.memory.items['manual'] = {'id': 'manual', 'status': 'confirmed', 'summary': 'Secretary booking',
        'start': {'dateTime': '2026-11-16T10:00:01+02:00'}, 'end': {'dateTime': '2026-11-16T10:30:01+02:00'}}
    result = env.calendar.available('2026-11-16')
    assert any(value.startswith('2026-11-16T09:00') for value in result['available_starts'])
    assert not any(value.startswith('2026-11-16T10:00') for value in result['available_starts'])
