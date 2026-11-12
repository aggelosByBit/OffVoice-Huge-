"""Single-process background jobs; calendar metadata is the durable appointment outbox."""
import asyncio
from datetime import datetime, timedelta
import json
import logging
import re

from calendar_service import BookingError, private
from communications import (DeliveryFailed, DeliveryUnknown, InvalidNotificationSnapshot,
                            load_notification_snapshot)

log = logging.getLogger('offvoice.jobs')


class Jobs:
    def __init__(self, settings, calendar, sessions, mailer, sms):
        self.settings, self.calendar, self.sessions = settings, calendar, sessions
        self.mailer, self.sms = mailer, sms

    async def sweep_sessions(self):
        now = self.sessions.now()
        candidates = []
        for token, session in list(self.sessions.sessions.items()):
            if session.lock.locked():
                continue
            async with session.lock:
                age = now - session.last_seen
                if age >= self.settings.session_seconds:
                    session.clear_contact()
                    session.turns.clear()
                    session.responses.clear()
                    self.sessions.discard(token)
                    continue
                if (session.pending and session.callback_consent and session.phone and not session.abandoned_notified
                    and not session.callback_expired and age >= self.settings.abandoned_seconds
                    and now - session.callback_attempt >= 60 and age < self.settings.session_seconds):
                    candidates.append(session)
        async def deliver(session):
            async with session.lock:
                if not session.pending or not session.callback_consent or not session.phone:
                    return
                age = self.sessions.now() - session.last_seen
                if age < self.settings.abandoned_seconds or age >= self.settings.session_seconds:
                    return
                session.callback_attempt = self.sessions.now()
                try:
                    await asyncio.to_thread(self.mailer.abandoned, session)
                    session.abandoned_notified = True
                    session.clear_contact()
                except (DeliveryFailed, DeliveryUnknown):
                    log.warning('callback_delivery_pending')
        # Expire all sessions first. Bound concurrent email I/O independently of clinic size.
        await asyncio.gather(*(deliver(session) for session in candidates[:5]))

    def flush_notifications(self):
        with self.calendar.service() as service:
            events = self.calendar.events(service, privateExtendedProperty=[f'ov_clinic={self.settings.clinic_id}', 'ov_notify_pending=yes'])
            for event in events:
                if not self.calendar.owns(event):
                    continue
                props = dict(private(event))
                remove, needs_review = [], False
                for key, raw in list(props.items()):
                    if not key.startswith('ov_mail_') or not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                        if payload['kind'] not in {'CREATE', 'RESCHEDULE', 'CANCEL'} or not payload['key']:
                            raise ValueError('Invalid queued notification')
                        age = (self.calendar.now() - datetime.fromisoformat(payload['at'])).total_seconds()
                        load_notification_snapshot(event, payload)
                    except (ValueError, KeyError, TypeError):
                        remove.append(key)
                        needs_review = True
                        continue
                    # Resend idempotency expires after 24 hours. Do not blindly retry beyond it.
                    if age >= 23 * 3600:
                        remove.append(key)
                        needs_review = True
                        continue
                    try:
                        self.mailer.appointment(self.calendar, event, payload)
                    except (InvalidNotificationSnapshot, ValueError):
                        remove.append(key)
                        needs_review = True
                        log.warning('appointment_email_needs_review')
                        continue
                    except (DeliveryFailed, DeliveryUnknown):
                        log.warning('appointment_email_pending')
                        continue
                    remove.append(key)
                if remove:
                    # Re-read under the write lock so concurrently queued actions are preserved.
                    with self.calendar._lock:
                        latest = service.events().get(calendarId=self.settings.calendar_id, eventId=event['id']).execute()
                        props = dict(private(latest))
                        for key in remove:
                            props[key] = None
                            prefix = 'ov_snap_' + key[len('ov_mail_'):] + '_'
                            for snapshot_key in list(props):
                                if snapshot_key.startswith(prefix):
                                    props[snapshot_key] = None
                        if needs_review:
                            props['ov_review_needed'] = 'yes'
                        props['ov_notify_pending'] = 'yes' if any(k.startswith('ov_mail_') and v for k, v in props.items()) else 'no'
                        self.calendar._patch(service, latest, {'extendedProperties': {'private': props}})

    def reminders(self):
        if not self.settings.reminders_enabled:
            return
        now = self.calendar.now()
        if now.time() < self.settings.reminder_time:
            return
        tomorrow = (now + timedelta(days=1)).date()
        day_key = tomorrow.isoformat()
        begin = datetime.combine(tomorrow, datetime.min.time(), self.settings.tz)
        end = begin + timedelta(days=1)
        with self.calendar.service() as service:
            events = self.calendar.events(service, timeMin=begin.isoformat(), timeMax=end.isoformat())
            for event in events:
                # Hold the booking lock for one delivery only, never for the entire nightly batch.
                with self.calendar._lock:
                    event = service.events().get(calendarId=self.settings.calendar_id, eventId=event['id']).execute()
                    if not self.calendar.owns(event) or event.get('status') == 'cancelled' or private(event).get('ov_state') == 'cancelled':
                        continue
                    if not event.get('start', {}).get('dateTime') or datetime.fromisoformat(event['start']['dateTime']).astimezone(self.settings.tz).date() != tomorrow:
                        continue
                    props = self.calendar._metadata(event)
                    if props.get('ov_reminder_date') == day_key:
                        continue
                    match = re.search(r'Τηλέφωνο ασθενούς:\s*(\+?\d{10,15})\b', event.get('description', ''))
                    if not match:
                        continue
                    from calendar_service import normalize_phone
                    try:
                        phone = normalize_phone(match[1])
                    except BookingError:
                        continue
                    when = datetime.fromisoformat(event['start']['dateTime']).astimezone(self.settings.tz).strftime('%d/%m %H:%M')
                    text = f'Υπενθύμιση ραντεβού {when}, {self.settings.clinic_name}. Κωδικός: {self.calendar.booking_code(event)}. Για αλλαγή ή επιβεβαίωση: {self.settings.public_origin}'
                    props.update(ov_reminder_date=day_key, ov_reminder_state='sending')
                    claimed = self.calendar._patch(service, event, {'extendedProperties': {'private': props}})
                    try:
                        self.sms.send(phone, text)
                        props['ov_reminder_state'] = 'sent'
                    except DeliveryUnknown:
                        props['ov_reminder_state'] = 'unknown'
                        log.warning('reminder_delivery_unknown')
                    except DeliveryFailed:
                        props['ov_reminder_state'] = 'failed'
                        log.warning('reminder_delivery_failed')
                    self.calendar._patch(service, claimed, {'extendedProperties': {'private': props}})

    async def run(self):
        async def sessions():
            while True:
                try:
                    await self.sweep_sessions()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.error('session_sweep_failed')
                await asyncio.sleep(10)
        async def calendar_jobs():
            while True:
                try:
                    await asyncio.to_thread(self.flush_notifications)
                    await asyncio.to_thread(self.reminders)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.error('calendar_job_failed')
                await asyncio.sleep(60)
        async with asyncio.TaskGroup() as group:
            group.create_task(sessions())
            group.create_task(calendar_jobs())
