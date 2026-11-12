"""Provider adapters. No message bodies, phone numbers or names are logged."""
from datetime import datetime
from html import escape
import hashlib
import base64
import httpx


class DeliveryUnknown(Exception):
    """A timeout may have happened after the provider accepted a message."""


class DeliveryFailed(Exception):
    pass


class Mailer:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def send(self, subject, html, key):
        if not self.settings.resend_key or not self.settings.email_from or not self.settings.doctor_email:
            raise DeliveryFailed('Email is not configured')
        try:
            with httpx.Client(timeout=12, transport=self.transport) as client:
                response = client.post('https://api.resend.com/emails',
                    headers={'Authorization': f'Bearer {self.settings.resend_key}', 'Idempotency-Key': key},
                    json={'from': self.settings.email_from, 'to': [self.settings.doctor_email], 'subject': subject, 'html': html})
        except httpx.TransportError:
            raise DeliveryUnknown('Email delivery outcome is unknown') from None
        if response.status_code >= 400:
            raise DeliveryFailed(f'Email provider status {response.status_code}')
        return response.json().get('id', '')

    def appointment(self, calendar, event, payload):
        labels = {'CREATE': 'Νέο ραντεβού', 'CANCEL': 'Ακύρωση ραντεβού', 'RESCHEDULE': 'Μεταφορά ραντεβού'}
        title = labels[payload['kind']]
        code = calendar.booking_code(event)
        when = payload.get('start') or event.get('start', {}).get('dateTime', '')
        try:
            when = datetime.fromisoformat(when).astimezone(self.settings.tz).strftime('%d/%m/%Y %H:%M')
        except ValueError:
            when = 'Δείτε το ημερολόγιο'
        # The immutable technical payload keeps retry bodies identical for provider idempotency.
        # Full patient details are available in the clinic's authenticated Calendar, not copied into every email.
        eid = base64.urlsafe_b64encode(f'{event["id"]} {self.settings.calendar_id}'.encode()).decode().rstrip('=')
        link = f'https://calendar.google.com/calendar/event?eid={eid}'
        html = f'<h2>{escape(title)}</h2><p>Κωδικός: {escape(code)}</p><p>Ώρα: {escape(when)}</p><p><a href="{escape(link)}">Άνοιγμα στο ημερολόγιο του ιατρείου</a></p>'
        return self.send(f'OffVoice AI · {title} · {code}', html, payload['key'])

    def abandoned(self, session):
        html = f'<h2>Ημιτελές αίτημα · Επανάκληση από γραμματεία</h2><p>Όνομα: {escape(session.name)}</p><p>Τηλέφωνο: {escape(session.phone)}</p><p>Ο επισκέπτης επέλεξε επικοινωνία από τη γραμματεία και το αίτημα έμεινε ανενεργό. Δεν έχει επιβεβαιωθεί κράτηση για αυτό το αίτημα.</p>'
        key = hashlib.sha256(f'{session.token}:{session.generation}'.encode()).hexdigest()
        return self.send('OffVoice AI · Ημιτελές αίτημα για επανάκληση', html, f'abandoned-{key}')


class SMSSender:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def send(self, phone, text):
        if not self.settings.reminders_enabled or self.settings.sms_provider != 'twilio':
            raise DeliveryFailed('Patient messages are disabled')
        try:
            with httpx.Client(timeout=12, transport=self.transport) as client:
                response = client.post(f'https://api.twilio.com/2010-04-01/Accounts/{self.settings.twilio_sid}/Messages.json',
                    auth=(self.settings.twilio_sid, self.settings.twilio_token),
                    data={'To': phone, 'From': self.settings.twilio_from, 'Body': text})
        except httpx.TransportError:
            raise DeliveryUnknown('SMS delivery outcome is unknown') from None
        if response.status_code >= 500:
            raise DeliveryUnknown('SMS provider outcome is unknown')
        if response.status_code >= 400:
            raise DeliveryFailed(f'SMS provider status {response.status_code}')
        return response.json().get('sid', '')


class DemoMailer:
    def __init__(self):
        self.sent = []

    def send(self, subject, html, key):
        # Only synthetic demo data is allowed in demo mode.
        if not any(item['key'] == key for item in self.sent):
            self.sent.append({'subject': subject, 'html': html, 'key': key})
        return 'demo-mail'

    appointment = Mailer.appointment
    abandoned = Mailer.abandoned
