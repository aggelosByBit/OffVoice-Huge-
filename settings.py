"""One configuration source per clinic. Secrets are environment variables only."""
from dataclasses import dataclass, field
from datetime import time
from datetime import date
import json
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {'1', 'true', 'yes'}


def clock(value: str) -> time:
    return time.fromisoformat(value if ':' in value else f'{int(value):02d}:00')


@dataclass(frozen=True)
class Settings:
    clinic_id: str = 'offvoice-test'
    clinic_name: str = 'Δοκιμαστικό Ιατρείο'
    doctor_name: str = 'Γιατρέ'
    clinic_phone: str = ''
    timezone: str = 'Europe/Athens'
    calendar_id: str = ''
    google_credentials_json: str = field(default='', repr=False)
    groq_key: str = field(default='', repr=False)
    model: str = 'qwen/qwen3.8-27b'
    resend_key: str = field(default='', repr=False)
    doctor_email: str = ''
    email_from: str = ''
    session_secret: str = field(default='', repr=False)
    public_origin: str = ''
    secure_cookie: bool = True
    demo: bool = False
    open_time: time = time(9)
    close_time: time = time(17)
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    closed_dates: tuple[str, ...] = ()
    appointment_minutes: int = 30
    booking_horizon_days: int = 180
    minimum_notice_minutes: int = 15
    session_seconds: int = 1800
    abandoned_seconds: int = 900
    max_sessions: int = 500
    max_message_chars: int = 1500
    max_turns: int = 12
    max_output_tokens: int = 500
    chat_per_minute: int = 20
    tool_steps: int = 5
    reminder_time: time = time(22)
    reminders_enabled: bool = False
    sms_provider: str = 'disabled'
    twilio_sid: str = field(default='', repr=False)
    twilio_token: str = field(default='', repr=False)
    twilio_from: str = ''
    billing_basis: str = 'unset'
    privacy_notice: str = ''

    @property
    def tz(self):
        return ZoneInfo(self.timezone)

    def validate(self):
        if self.open_time >= self.close_time:
            raise ValueError('CLINIC_OPEN_TIME must precede CLINIC_CLOSE_TIME')
        if not self.work_days or any(d not in range(7) for d in self.work_days):
            raise ValueError('CLINIC_WORK_DAYS must contain weekday numbers 0..6')
        if self.appointment_minutes not in range(5, 241):
            raise ValueError('APPOINTMENT_MINUTES must be between 5 and 240')
        if not 60 <= self.abandoned_seconds < self.session_seconds <= 86400:
            raise ValueError('Use 60 <= abandoned timeout < session timeout <= 86400 seconds')
        if not 1 <= self.max_turns <= 30 or not 128 <= self.max_output_tokens <= 2000:
            raise ValueError('Invalid history or output token limit')
        if self.max_sessions < 1 or self.max_message_chars < 1 or self.chat_per_minute < 1:
            raise ValueError('Session, message and rate limits must be positive')
        if self.booking_horizon_days < 1 or self.minimum_notice_minutes < 0:
            raise ValueError('Invalid booking window')
        for value in self.closed_dates:
            date.fromisoformat(value)
        if self.billing_basis not in {'unset', 'created', 'confirmed', 'attended'}:
            raise ValueError('BILLING_BASIS must be unset, created, confirmed or attended')
        if self.sms_provider not in {'disabled', 'twilio'}:
            raise ValueError('Supported SMS_PROVIDER values: disabled, twilio')
        if self.reminders_enabled and (self.sms_provider != 'twilio' or not all((self.twilio_sid, self.twilio_token, self.twilio_from))):
            raise ValueError('Reminder delivery needs a configured SMS provider')
        self.tz
        if not self.demo:
            required = {
                'CALENDAR_ID': self.calendar_id, 'GOOGLE_CREDENTIALS_JSON': self.google_credentials_json,
                'GROQ_API_KEY': self.groq_key, 'SESSION_SECRET (at least 32 characters)': self.session_secret if len(self.session_secret) >= 32 else '',
                'RESEND_API_KEY': self.resend_key, 'DOCTOR_NOTIFICATION_EMAIL': self.doctor_email,
                'EMAIL_FROM (verified sender)': self.email_from, 'PUBLIC_ORIGIN': self.public_origin,
                'PRIVACY_NOTICE (reviewed clinic notice)': self.privacy_notice,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError('Missing configuration: ' + ', '.join(missing))
            if not self.secure_cookie or not self.public_origin.startswith('https://'):
                raise ValueError('Production requires HTTPS and COOKIE_SECURE=true')
            origin = urlsplit(self.public_origin)
            if not origin.hostname or origin.username or origin.password or origin.path not in {'', '/'} or origin.query or origin.fragment:
                raise ValueError('PUBLIC_ORIGIN must be only the HTTPS scheme and host')
            try:
                credentials = json.loads(self.google_credentials_json)
                if not all(credentials.get(key) for key in ('client_email', 'private_key', 'token_uri')):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('GOOGLE_CREDENTIALS_JSON must contain valid service-account JSON') from None

    @classmethod
    def from_env(cls):
        return cls(
            clinic_id=os.getenv('CLINIC_ID', 'offvoice-test'),
            clinic_name=os.getenv('CLINIC_NAME', 'Δοκιμαστικό Ιατρείο'),
            doctor_name=os.getenv('DOCTOR_NAME', 'Γιατρέ'), clinic_phone=os.getenv('CLINIC_PHONE', ''),
            timezone=os.getenv('CLINIC_TIMEZONE', 'Europe/Athens'), calendar_id=os.getenv('CALENDAR_ID', ''),
            google_credentials_json=os.getenv('GOOGLE_CREDENTIALS_JSON') or os.getenv('GOOGLE_CREDENTIALS', ''),
            groq_key=os.getenv('GROQ_API_KEY', ''), model=os.getenv('GROQ_MODEL', 'qwen/qwen3.8-27b'),
            resend_key=os.getenv('RESEND_API_KEY', ''), doctor_email=os.getenv('DOCTOR_NOTIFICATION_EMAIL', ''),
            email_from=os.getenv('EMAIL_FROM', ''), session_secret=os.getenv('SESSION_SECRET', ''),
            public_origin=os.getenv('PUBLIC_ORIGIN', '').rstrip('/'), secure_cookie=flag('COOKIE_SECURE', True),
            demo=flag('DEMO_MODE'),
            open_time=clock(os.getenv('CLINIC_OPEN_TIME', os.getenv('CLINIC_OPEN_HOUR', '09:00'))),
            close_time=clock(os.getenv('CLINIC_CLOSE_TIME', os.getenv('CLINIC_CLOSE_HOUR', '17:00'))),
            work_days=tuple(int(d.strip()) for d in os.getenv('CLINIC_WORK_DAYS', '0,1,2,3,4').split(',')),
            closed_dates=tuple(d.strip() for d in os.getenv('CLINIC_CLOSED_DATES', '').split(',') if d.strip()),
            appointment_minutes=int(os.getenv('APPOINTMENT_MINUTES', '30')),
            booking_horizon_days=int(os.getenv('BOOKING_HORIZON_DAYS', '180')),
            minimum_notice_minutes=int(os.getenv('MINIMUM_NOTICE_MINUTES', '15')),
            session_seconds=int(os.getenv('SESSION_TTL_SECONDS', '1800')),
            abandoned_seconds=int(os.getenv('ABANDONED_AFTER_SECONDS', '900')),
            max_sessions=int(os.getenv('MAX_SESSIONS', '500')),
            max_message_chars=int(os.getenv('MAX_MESSAGE_CHARS', '1500')),
            max_turns=int(os.getenv('MAX_HISTORY_TURNS', '12')),
            max_output_tokens=int(os.getenv('MAX_OUTPUT_TOKENS', '500')),
            chat_per_minute=int(os.getenv('CHAT_REQUESTS_PER_MINUTE', '20')),
            reminder_time=clock(os.getenv('REMINDER_TIME', '22:00')),
            reminders_enabled=flag('REMINDERS_ENABLED'), sms_provider=os.getenv('SMS_PROVIDER', 'disabled'),
            twilio_sid=os.getenv('TWILIO_ACCOUNT_SID', ''), twilio_token=os.getenv('TWILIO_AUTH_TOKEN', ''),
            twilio_from=os.getenv('TWILIO_FROM_NUMBER', ''), billing_basis=os.getenv('BILLING_BASIS', 'unset'),
            privacy_notice=os.getenv('PRIVACY_NOTICE', ''),
        )
