"""Privacy notice must be available before chat and never expose configuration secrets."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient

from main import create_app
from settings import Settings
from test_offvoice import ScriptedLLM
from calendar_service import CalendarService
from demo import MemoryCalendar
from communications import DemoMailer


def privacy_client(**changes):
    cfg = replace(Settings(demo=True, secure_cookie=False, session_secret='synthetic-secret-' * 3), **changes)
    calendar = CalendarService(cfg, lambda: MemoryCalendar())
    return TestClient(create_app(cfg, calendar=calendar, llm=ScriptedLLM(), mailer=DemoMailer(), start_jobs=False))


def test_notice_without_session_and_config_values_are_html_escaped():
    with privacy_client(clinic_name='<script>clinic()</script>', privacy_notice='<img src=x onerror=alert(1)>', session_seconds=2400, abandoned_seconds=600) as client:
        response = client.get('/privacy')
        assert response.status_code == 200
        assert 'set-cookie' not in response.headers
        assert not client.app.state.sessions.sessions
        assert '<script>clinic()' not in response.text and '&lt;script&gt;' in response.text
        assert '<img src=x' not in response.text and '&lt;img' in response.text
        assert '40 λεπτά' in response.text and '10 λεπτά' in response.text
        assert '{{' not in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert "frame-ancestors 'self'" in response.headers['content-security-policy']


def test_configured_mode_uses_normal_privacy_notice_even_with_obsolete_flag(monkeypatch):
    monkeypatch.setenv('TEST_DATA_ONLY', 'true')
    assert not hasattr(Settings.from_env(), 'test_data_only')
    cfg = Settings(demo=False, clinic_name='Ιατρείο Παρουσίασης', secure_cookie=True, session_secret='synthetic-secret-' * 3,
                   public_origin='https://testserver', calendar_id='synthetic', groq_key='synthetic',
                   resend_key='synthetic', doctor_email='doctor@example.test', email_from='sender@example.test',
                   privacy_notice='Τα στοιχεία χρησιμοποιούνται για ραντεβού.',
                   google_credentials_json=json.dumps({'client_email':'service@example.test','private_key':'synthetic-private','token_uri':'https://example.test/token'}))
    app = create_app(cfg, calendar=CalendarService(cfg, lambda: MemoryCalendar()), llm=ScriptedLLM(), mailer=DemoMailer(), start_jobs=False)
    with TestClient(app, base_url='https://testserver') as client:
        config = client.get('/config').json()
        assert config['demo'] is False
        assert 'βοηθό AI' in config['privacy_summary']
        assert 'εικονικά' not in config['privacy_summary'] and 'Δοκιμ' not in config['privacy_summary']
        assert 'Δοκιμαστική λειτουργία' not in client.get('/privacy').text
        assert 'synthetic-private' not in client.get('/privacy').text
        assert 'session_secret' not in config and 'google_credentials_json' not in config


def test_actual_simulation_keeps_its_demo_notice():
    with privacy_client() as client:
        assert client.get('/config').json()['demo'] is True
        assert 'εικονικά' in client.get('/config').json()['privacy_summary']
        html = client.get('/').text
        assert html.index('privacySummary') < html.index('chatForm')
        assert '<details class="privacy">' in html and 'href="/privacy"' in html
