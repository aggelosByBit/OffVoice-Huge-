"""Provider adapters. No message bodies, phone numbers or names are logged."""
from datetime import datetime
from html import escape
import hashlib
import base64
import json
import re
from zoneinfo import ZoneInfo
import httpx


class DeliveryUnknown(Exception):
    """A timeout may have happened after the provider accepted a message."""


class DeliveryFailed(Exception):
    pass


class InvalidNotificationSnapshot(ValueError):
    pass


def snapshot_prefix(operation_id):
    return 'ov_snap_' + operation_id[:24] + '_'


def pack_notification_snapshot(operation_id, snapshot):
    """Google limits each property value to 1024 characters; also keep UTF-8 below that."""
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    chunks, current, size = [], '', 0
    for char in raw:
        length = len(char.encode('utf-8'))
        if size + length > 900:
            chunks.append(current)
            current, size = '', 0
        current += char
        size += length
    if current:
        chunks.append(current)
    if not 1 <= len(chunks) <= 8:
        raise ValueError('Notification snapshot is too large')
    prefix = snapshot_prefix(operation_id)
    return {prefix + str(i): chunk for i, chunk in enumerate(chunks)}, len(chunks)


def load_notification_snapshot(event, payload):
    if 'v' not in payload:
        return None  # Previously queued emails retain their original body/idempotency behavior.
    try:
        if payload['v'] != 1 or type(payload['snapshot_parts']) is not int or not 1 <= payload['snapshot_parts'] <= 8:
            raise ValueError()
        props = event.get('extendedProperties', {}).get('private', {})
        prefix = snapshot_prefix(payload['key'])
        data = json.loads(''.join(props[prefix + str(i)] for i in range(payload['snapshot_parts'])))
        required = {'patient_name', 'phone', 'visit_type', 'summary', 'start', 'end', 'previous_start',
                    'previous_end', 'booking_code', 'clinic_name', 'timezone', 'event_id', 'calendar_id'}
        if not isinstance(data, dict) or not required <= data.keys() or not all(isinstance(data[k], str) for k in required):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError):
        raise InvalidNotificationSnapshot('Missing or invalid appointment email snapshot') from None


def render_appointment_email(snapshot, kind):
    """Version 1 template: use only immutable snapshot fields so retries have identical bodies."""
    options = {
        'CREATE': ('🔔', 'Νέο ραντεβού', 'Νέο προγραμματισμένο ραντεβού', '#147d54', 'Καταχωρήθηκε',
                   'Η κράτηση ολοκληρώθηκε και καταχωρήθηκε στο ημερολόγιο του ιατρείου.'),
        'RESCHEDULE': ('🔄', 'Μεταφορά ραντεβού', 'Αλλαγή ημερομηνίας ή ώρας', '#a65b00', 'Μεταφέρθηκε',
                       'Το ραντεβού μεταφέρθηκε. Παρακάτω εμφανίζονται τα ενημερωμένα στοιχεία.'),
        'CANCEL': ('❌', 'Ακύρωση ραντεβού', 'Το ραντεβού ακυρώθηκε', '#b42335', 'Ακυρώθηκε',
                   'Το παρακάτω ραντεβού ακυρώθηκε και η ώρα του είναι ξανά διαθέσιμη.'),
    }
    icon, subject_label, title, color, status, intro = options[kind]
    tz = ZoneInfo(snapshot['timezone'])
    days = ('Δευτέρα', 'Τρίτη', 'Τετάρτη', 'Πέμπτη', 'Παρασκευή', 'Σάββατο', 'Κυριακή')

    def moment(value):
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)

    start, end = moment(snapshot['start']), moment(snapshot['end'])
    date_label = f'{days[start.weekday()]}, {start:%d/%m/%Y}' if start else 'Δεν δηλώθηκε'
    time_label = f'{start:%H:%M}' if start else 'Δεν δηλώθηκε'
    if start and end:
        time_label += f' – {end:%H:%M}' if start.date() == end.date() else f' – {end:%d/%m/%Y %H:%M}'
        minutes = int((end - start).total_seconds() // 60)
        if minutes > 0:
            time_label += f' ({minutes} λεπτά)'
    patient = snapshot['patient_name'] or snapshot['summary'] or 'Δεν δηλώθηκε'
    phone = snapshot['phone'] or 'Δεν δηλώθηκε'
    phone_html = escape(phone)
    if re.fullmatch(r'\+?\d{8,15}', phone):
        phone_html = f'<a href="tel:{escape(phone)}" style="color:#115c9a;text-decoration:none;">{escape(phone)}</a>'

    def row(label, value, html=False):
        content = value if html else escape(value)
        return f'<tr><td style="padding:12px 0;border-bottom:1px solid #e6edf3;"><div style="font-size:12px;color:#526777;margin-bottom:5px;">{escape(label)}</div><div style="font-size:16px;line-height:1.5;color:#18364d;overflow-wrap:anywhere;">{content}</div></td></tr>'

    rows = row('Ονοματεπώνυμο', patient)
    rows += row('Τηλέφωνο επικοινωνίας', phone_html, html=True)
    if snapshot['visit_type']:
        rows += row('Τύπος επίσκεψης / Αιτία', snapshot['visit_type'])
    elif snapshot['summary'] and snapshot['summary'] != patient:
        rows += row('Στοιχεία / Αιτία', snapshot['summary'])
    rows += row('Ημερομηνία', date_label) + row('Ώρα έναρξης – λήξης', time_label)
    previous = ''
    if kind == 'RESCHEDULE' and snapshot['previous_start']:
        old = moment(snapshot['previous_start'])
        old_end = moment(snapshot['previous_end'])
        old_label = f'{days[old.weekday()]}, {old:%d/%m/%Y %H:%M}'
        if old_end:
            old_label += f' – {old_end:%H:%M}'
        previous = f'<div style="margin:18px 0 0;padding:12px 14px;background:#fff6e8;border-radius:8px;color:#785019;font-size:13px;line-height:1.6;"><strong>Προηγούμενο ραντεβού</strong><br>{escape(old_label)}</div>'
    eid = base64.urlsafe_b64encode(f'{snapshot["event_id"]} {snapshot["calendar_id"]}'.encode()).decode().rstrip('=')
    link = 'https://calendar.google.com/calendar/event?eid=' + eid
    code = escape(snapshot['booking_code'])
    subject_name = ' '.join(patient.split())[:100]
    subject = f'{icon} {subject_label}: {subject_name} · {snapshot["booking_code"]}'
    html = f'''<!doctype html><html lang="el"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f6fa;font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f6fa;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #dbe5ee;border-radius:14px;overflow:hidden;">
<tr><td style="padding:24px;background:#0b548d;color:#ffffff;"><div style="font-size:13px;letter-spacing:1px;color:#ffffff;">OffVoice AI</div><h1 style="margin:12px 0 8px;font-size:25px;line-height:1.3;color:#ffffff;">{icon} {escape(title)}</h1><div style="font-size:13px;line-height:1.5;color:#ffffff;">{escape(snapshot['clinic_name'])}</div></td></tr>
<tr><td style="padding:24px;"><span style="display:inline-block;padding:6px 10px;border:1px solid {color};border-radius:20px;color:{color};font-size:12px;font-weight:bold;">{escape(status)}</span><p style="margin:16px 0 8px;color:#526777;font-size:14px;line-height:1.7;">{escape(intro)}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table>{previous}
<div style="margin:22px 0;padding:16px;background:#eef5fc;border:1px solid #d9e8f7;border-radius:10px;"><div style="font-size:12px;color:#526777;">Κωδικός κράτησης</div><div style="margin-top:7px;font-family:Consolas,monospace;font-size:26px;font-weight:bold;letter-spacing:3px;color:#0b548d;">{code}</div></div>
<a href="{escape(link)}" style="display:inline-block;padding:13px 18px;background:#0b548d;border-radius:8px;color:#ffffff;text-decoration:none;font-size:14px;font-weight:bold;line-height:1.5;">Άνοιγμα στο ημερολόγιο του ιατρείου</a>
</td></tr><tr><td style="padding:16px 24px;border-top:1px solid #e6edf3;color:#687e8f;font-size:11px;line-height:1.6;">OffVoice AI System · Αυτόματη ενημέρωση ιατρείου</td></tr>
</table></td></tr></table></body></html>'''
    return subject, html


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
        snapshot = load_notification_snapshot(event, payload)
        if snapshot is not None:
            subject, html = render_appointment_email(snapshot, payload['kind'])
            return self.send(subject, html, payload['key'])
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
