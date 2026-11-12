"""Calendar-backed appointments. Personal information stays in the clinic calendar."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import string
import threading

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httplib2
from google_auth_httplib2 import AuthorizedHttp

from settings import Settings


class BookingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_phone(value: str) -> str:
    phone = re.sub(r'[\s().-]', '', value)
    if phone.startswith('00'):
        phone = '+' + phone[2:]
    if re.fullmatch(r'[2-9]\d{9}', phone):
        phone = '+30' + phone
    if not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise BookingError('INVALID_PHONE', 'Παρακαλώ δώστε έγκυρο τηλέφωνο, με κωδικό χώρας αν είναι εκτός Ελλάδας.')
    return phone


def private(event):
    return event.get('extendedProperties', {}).get('private', {})


class CalendarService:
    def __init__(self, settings: Settings, service_factory=None, now=None):
        self.settings = settings
        self.now = now or (lambda: datetime.now(settings.tz))
        self._factory = service_factory or self._google_service
        self._lock = threading.RLock()

    def _google_service(self):
        creds = Credentials.from_service_account_info(
            json.loads(self.settings.google_credentials_json),
            scopes=['https://www.googleapis.com/auth/calendar.events'],
        )
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=15))
        return build('calendar', 'v3', http=http, cache_discovery=False)

    @contextmanager
    def service(self):
        # A separate HTTP transport per operation; httplib2 clients are not shared across threads.
        service = self._factory()
        try:
            yield service
        finally:
            close = getattr(service, 'close', None)
            if close:
                close()

    def events(self, service, **kwargs):
        items, token = [], None
        while True:
            params = dict(calendarId=self.settings.calendar_id, singleEvents=True, maxResults=250, **kwargs)
            if token:
                params['pageToken'] = token
            page = service.events().list(**params).execute()
            items.extend(page.get('items', []))
            token = page.get('nextPageToken')
            if not token:
                return items

    def owns(self, event):
        props = private(event)
        if props.get('ov_clinic'):
            return props['ov_clinic'] == self.settings.clinic_id
        return bool(re.search(r'\[OffVoice AI\].*Κωδικός Κράτησης:\s*[A-Z0-9]{6}\b', event.get('description', '')))

    def booking_code(self, event):
        code = private(event).get('ov_code', '')
        if code:
            return code
        match = re.search(r'Κωδικός Κράτησης:\s*([A-Z0-9]{6})\b', event.get('description', ''))
        return match[1] if match else ''

    def lookup(self, service, code):
        code = code.strip().upper()
        if not re.fullmatch(r'[A-Z0-9]{6}', code):
            raise BookingError('INVALID_CODE', 'Χρειάζεται ο πλήρης κωδικός έξι χαρακτήρων. Για απώλειά του επικοινωνήστε με τη γραμματεία.')
        candidates = self.events(service, privateExtendedProperty=[f'ov_clinic={self.settings.clinic_id}', f'ov_code={code}'])
        # Legacy bookings have the code in their description, without private properties.
        candidates.extend(self.events(service, q=code))
        matches = {e['id']: e for e in candidates if e.get('status') != 'cancelled' and self.owns(e) and self.booking_code(e) == code}
        if len(matches) != 1:
            raise BookingError('NOT_FOUND' if not matches else 'AMBIGUOUS_CODE', 'Δεν βρέθηκε μοναδικό ραντεβού με αυτόν τον κωδικό. Ελέγξτε τον ή επικοινωνήστε με τη γραμματεία.')
        return next(iter(matches.values()))

    def parse_start(self, value):
        try:
            parsed = datetime.fromisoformat(value.strip().replace(' ', 'T').replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            raise BookingError('INVALID_DATE', 'Παρακαλώ δώστε συγκεκριμένη ημερομηνία και ώρα.')
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.settings.tz)
        else:
            parsed = parsed.astimezone(self.settings.tz)
        if parsed.second or parsed.microsecond:
            raise BookingError('INVALID_DATE', 'Η ώρα πρέπει να δίνεται με ακρίβεια λεπτού.')
        if parsed.astimezone(timezone.utc).astimezone(self.settings.tz).replace(fold=parsed.fold) != parsed:
            raise BookingError('INVALID_DATE', 'Η ώρα δεν είναι έγκυρη λόγω αλλαγής θερινής ώρας.')
        return parsed

    def read_time(self, value):
        """Provider timestamps may contain seconds/fractions; booking-input restrictions do not apply."""
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=self.settings.tz) if parsed.tzinfo is None else parsed.astimezone(self.settings.tz)

    def validate_slot(self, value):
        start = self.parse_start(value)
        end = start + timedelta(minutes=self.settings.appointment_minutes)
        now = self.now()
        if start < now + timedelta(minutes=self.settings.minimum_notice_minutes):
            raise BookingError('PAST_OR_TOO_SOON', 'Επιλέξτε μελλοντική ώρα με το απαραίτητο περιθώριο προετοιμασίας.')
        if start > now + timedelta(days=self.settings.booking_horizon_days):
            raise BookingError('TOO_FAR', 'Η ημερομηνία είναι πέρα από το διαθέσιμο διάστημα κρατήσεων.')
        if start.weekday() not in self.settings.work_days or start.date().isoformat() in self.settings.closed_dates:
            raise BookingError('CLOSED_DAY', 'Το ιατρείο δεν λειτουργεί την επιλεγμένη ημέρα.')
        if (start.date() != end.date() or start.time() < self.settings.open_time or end.time() > self.settings.close_time):
            raise BookingError('OUT_OF_HOURS', 'Το ραντεβού πρέπει να ολοκληρώνεται εντός του ωραρίου του ιατρείου.')
        return start, end

    def slot_free(self, service, start, end, exclude=None):
        events = self.events(service, timeMin=start.isoformat(), timeMax=end.isoformat())
        return not any(e.get('status') != 'cancelled' and e.get('transparency') != 'transparent'
                       and private(e).get('ov_state') != 'cancelled' and e['id'] != exclude for e in events)

    def _stamp(self):
        return self.now().isoformat()

    def _patch(self, service, event, changes):
        request = service.events().patch(calendarId=self.settings.calendar_id, eventId=event['id'], body=changes)
        if event.get('etag'):
            request.headers['If-Match'] = event['etag']
        return request.execute()

    def _metadata(self, event):
        return {**private(event), 'ov_clinic': self.settings.clinic_id, 'ov_code': self.booking_code(event)}

    def _pending(self, props, action, operation_id, start=''):
        queued = [key for key, value in props.items() if key.startswith('ov_mail_') and value]
        if len(queued) >= 20:
            raise BookingError('NOTIFICATION_BACKLOG', 'Υπάρχει προσωρινό πρόβλημα ενημέρωσης της γραμματείας. Παρακαλώ επικοινωνήστε με το ιατρείο.')
        key = 'ov_mail_' + operation_id[:24]
        payload = json.dumps({'kind': action, 'key': operation_id, 'at': self._stamp(), 'start': start}, separators=(',', ':'))
        return {**props, 'ov_notify_pending': 'yes', key: payload}

    def _result(self, event, message, action=None):
        return {'ok': True, 'message': message, 'booking_code': self.booking_code(event),
                'start': event.get('start', {}).get('dateTime'), 'state': private(event).get('ov_state', 'booked'),
                'action': action}

    def action_id(self, session_token, request_id, action, args):
        material = json.dumps([session_token, request_id, action, args], sort_keys=True, ensure_ascii=False)
        return hmac.new(self.settings.session_secret.encode(), material.encode(), hashlib.sha256).hexdigest()

    def find(self, code):
        with self.service() as service:
            event = self.lookup(service, code)
            # A lookup never returns name, phone, visit reason or another patient's data to the LLM.
            return self._result(event, 'Βρέθηκε το ραντεβού.')

    def available(self, date_iso):
        try:
            day = datetime.fromisoformat(date_iso).date()
        except ValueError:
            raise BookingError('INVALID_DATE', 'Χρειάζεται ημερομηνία στη μορφή YYYY-MM-DD.')
        slots = []
        cursor = datetime.combine(day, self.settings.open_time, self.settings.tz)
        with self.service() as service:
            events = self.events(service, timeMin=cursor.isoformat(), timeMax=datetime.combine(day + timedelta(days=1), self.settings.open_time, self.settings.tz).isoformat())
            while cursor.time() < self.settings.close_time and len(slots) < 8:
                try:
                    start, end = self.validate_slot(cursor.isoformat())
                    busy = False
                    for event in events:
                        if event.get('status') == 'cancelled' or event.get('transparency') == 'transparent' or private(event).get('ov_state') == 'cancelled':
                            continue
                        start_info, end_info = event['start'], event['end']
                        a = self.read_time(start_info.get('dateTime', start_info.get('date')))
                        b = self.read_time(end_info.get('dateTime', end_info.get('date')))
                        if a < end and b > start:
                            busy = True
                            break
                    if not busy:
                        slots.append(start.isoformat())
                except BookingError:
                    pass
                cursor += timedelta(minutes=self.settings.appointment_minutes)
        return {'ok': True, 'available_starts': slots, 'date': day.isoformat()}

    def create(self, name, phone, visit_type, start_iso, operation_id):
        name, visit_type = name.strip(), visit_type.strip()
        if not 2 <= len(name) <= 100 or not 2 <= len(visit_type) <= 100:
            raise BookingError('MISSING_DETAILS', 'Χρειάζονται ονοματεπώνυμο και σύντομος τύπος επίσκεψης.')
        phone = normalize_phone(phone)
        start, end = self.validate_slot(start_iso)
        with self._lock, self.service() as service:
            try:
                existing = service.events().get(calendarId=self.settings.calendar_id, eventId=operation_id).execute()
            except HttpError as error:
                if error.resp.status != 404:
                    raise
            else:
                if self.owns(existing):
                    return self._result(existing, 'Το αίτημα είχε ήδη ολοκληρωθεί.', 'CREATE')
                raise BookingError('CONFLICT', 'Το αίτημα δεν μπορεί να ολοκληρωθεί. Δοκιμάστε ξανά.')
            if not self.slot_free(service, start, end):
                raise BookingError('SLOT_TAKEN', 'Η ώρα δεν είναι διαθέσιμη. Επιλέξτε άλλη ώρα.')
            for _ in range(5):
                code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
                try:
                    self.lookup(service, code)
                except BookingError as error:
                    if error.code == 'NOT_FOUND':
                        break
                    raise
            else:
                raise BookingError('CODE_UNAVAILABLE', 'Προσωρινή αδυναμία παραγωγής κωδικού. Δοκιμάστε ξανά.')
            now = self.now()
            after_hours = now.weekday() not in self.settings.work_days or not self.settings.open_time <= now.time() < self.settings.close_time or now.date().isoformat() in self.settings.closed_dates
            props = self._pending({'ov_clinic': self.settings.clinic_id, 'ov_code': code, 'ov_state': 'booked',
                                   'ov_created_at': now.isoformat(), 'ov_after_hours': str(after_hours).lower(),
                                   'ov_reschedules': '0', 'ov_last_action': operation_id, 'ov_attendance': 'unknown'}, 'CREATE', operation_id, start.isoformat())
            body = {'id': operation_id, 'summary': f'{name} — {visit_type}',
                    'description': f'🤖 [OffVoice AI] Κωδικός Κράτησης: {code}\nΤηλέφωνο ασθενούς: {phone}',
                    'start': {'dateTime': start.isoformat(), 'timeZone': self.settings.timezone},
                    'end': {'dateTime': end.isoformat(), 'timeZone': self.settings.timezone},
                    'extendedProperties': {'private': props}}
            event = service.events().insert(calendarId=self.settings.calendar_id, body=body, sendUpdates='none').execute()
            return self._result(event, 'Το ραντεβού καταχωρήθηκε. Κρατήστε τον κωδικό σας.', 'CREATE')

    def reschedule(self, code, start_iso, operation_id):
        start, end = self.validate_slot(start_iso)
        with self._lock, self.service() as service:
            event = self.lookup(service, code)
            props = self._metadata(event)
            if props.get('ov_last_action') == operation_id:
                return self._result(event, 'Η αλλαγή είχε ήδη ολοκληρωθεί.', 'RESCHEDULE')
            if props.get('ov_state') == 'cancelled':
                raise BookingError('CANCELLED', 'Το ραντεβού έχει ήδη ακυρωθεί.')
            if self.read_time(event['start']['dateTime']) < self.now():
                raise BookingError('PAST_APPOINTMENT', 'Για παρελθοντικό ραντεβού επικοινωνήστε με τη γραμματεία.')
            if not self.slot_free(service, start, end, exclude=event['id']):
                raise BookingError('SLOT_TAKEN', 'Η νέα ώρα δεν είναι διαθέσιμη. Επιλέξτε άλλη ώρα.')
            props.update(ov_last_action=operation_id, ov_modified_at=self._stamp(),
                         ov_reschedules=str(int(props.get('ov_reschedules', '0')) + 1), ov_confirmed_at='',
                         ov_reminder_state='', ov_reminder_date='')
            props = self._pending(props, 'RESCHEDULE', operation_id, start.isoformat())
            event = self._patch(service, event, {'start': {'dateTime': start.isoformat(), 'timeZone': self.settings.timezone},
                                               'end': {'dateTime': end.isoformat(), 'timeZone': self.settings.timezone},
                                               'extendedProperties': {'private': props}})
            return self._result(event, 'Το ραντεβού μεταφέρθηκε.', 'RESCHEDULE')

    def cancel(self, code, operation_id):
        with self._lock, self.service() as service:
            event = self.lookup(service, code)
            props = self._metadata(event)
            if props.get('ov_state') == 'cancelled':
                return self._result(event, 'Το ραντεβού είναι ήδη ακυρωμένο.', 'CANCEL')
            if self.read_time(event['start']['dateTime']) < self.now():
                raise BookingError('PAST_APPOINTMENT', 'Για παρελθοντικό ραντεβού επικοινωνήστε με τη γραμματεία.')
            props.update(ov_state='cancelled', ov_cancelled_at=self._stamp(), ov_last_action=operation_id)
            props = self._pending(props, 'CANCEL', operation_id, event['start']['dateTime'])
            # Keep only technical cancellation evidence in Calendar, with no patient contact/reason.
            event = self._patch(service, event, {'summary': 'Ακυρωμένο ραντεβού · OffVoice AI',
                                               'description': 'Ακυρωμένη κράτηση OffVoice AI. Δεν δεσμεύει διαθεσιμότητα.',
                                               'transparency': 'transparent', 'extendedProperties': {'private': props}})
            return self._result(event, 'Το ραντεβού ακυρώθηκε.', 'CANCEL')

    def confirm(self, code):
        with self._lock, self.service() as service:
            event = self.lookup(service, code)
            props = self._metadata(event)
            if props.get('ov_state') == 'cancelled':
                raise BookingError('CANCELLED', 'Το ραντεβού έχει ακυρωθεί.')
            if self.read_time(event['start']['dateTime']) <= self.now():
                raise BookingError('PAST_APPOINTMENT', 'Το ραντεβού δεν μπορεί πλέον να επιβεβαιωθεί.')
            if not props.get('ov_confirmed_at'):
                props['ov_confirmed_at'] = self._stamp()
                event = self._patch(service, event, {'extendedProperties': {'private': props}})
            return self._result(event, 'Η προσέλευσή σας επιβεβαιώθηκε.')
