"""Anonymous aggregate report. Run without --send to preview without sending email."""
import argparse
from datetime import datetime, timedelta
from html import escape
import json

from calendar_service import CalendarService, private
from communications import Mailer
from settings import Settings


def month_range(month, tz):
    start = datetime.strptime(month, '%Y-%m').replace(tzinfo=tz)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def in_period(value, start, end):
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=start.tzinfo)
        return start <= parsed < end
    except (ValueError, TypeError):
        return False


def build_report(calendar, month):
    settings = calendar.settings
    begin, end = month_range(month, settings.tz)
    with calendar.service() as service:
        # Read every page, including legacy appointments. Deduplicate the two sources.
        tagged = calendar.events(service, privateExtendedProperty=[f'ov_clinic={settings.clinic_id}'])
        legacy = calendar.events(service, q='OffVoice AI')
    events = {e['id']: e for e in tagged + legacy if calendar.owns(e) and e.get('status') != 'cancelled'}
    scheduled, created, confirmed = [], [], []
    for event in events.values():
        props = private(event)
        start = event.get('start', {}).get('dateTime')
        if in_period(start, begin, end):
            scheduled.append(event)
        if in_period(props.get('ov_created_at') or event.get('created'), begin, end):
            created.append(event)
        if props.get('ov_state') != 'cancelled' and in_period(props.get('ov_confirmed_at'), begin, end):
            confirmed.append(event)
    active = [e for e in scheduled if private(e).get('ov_state') != 'cancelled']
    elapsed = [e for e in active if calendar.read_time(e['end']['dateTime']) <= calendar.now()]
    attended = [e for e in active if private(e).get('ov_attendance') == 'attended']
    no_show = [e for e in active if private(e).get('ov_attendance') == 'no_show']
    unknown = [e for e in elapsed if private(e).get('ov_attendance', 'unknown') == 'unknown']
    after_hours = 0
    for event in created:
        props = private(event)
        if 'ov_after_hours' in props:
            after_hours += props['ov_after_hours'] == 'true'
        else:
            when = calendar.read_time(event['created'])
            after_hours += when.weekday() not in settings.work_days or not settings.open_time <= when.time() < settings.close_time
    billable = None
    if settings.billing_basis == 'created':
        billable = sum(private(e).get('ov_state') != 'cancelled' for e in created)
    elif settings.billing_basis == 'confirmed':
        billable = len(confirmed)
    elif settings.billing_basis == 'attended':
        billable = len(attended)
    return {
        'month': month, 'created_in_month': len(created), 'created_after_hours': after_hours,
        'active_appointments_in_month': len(active), 'cancelled_appointments_in_month': len(scheduled) - len(active),
        'reschedules_total_for_month_appointments': sum(int(private(e).get('ov_reschedules', '0')) for e in scheduled),
        'elapsed_scheduled_appointments': len(elapsed), 'recorded_attended': len(attended),
        'recorded_no_show': len(no_show), 'elapsed_attendance_unknown': len(unknown),
        'patient_confirmations_recorded_in_month': len(confirmed),
        'estimated_minutes_saved_range': [2 * len(created), 3 * len(created)],
        'billing_basis': settings.billing_basis, 'billable_count_estimate': billable,
        'notes': ['Η εξοικονόμηση χρόνου είναι εκτίμηση βάσει 2–3 λεπτών ανά νέα καταχώρηση.',
                  'Παλαιές ή εξωτερικές οριστικές διαγραφές δεν καταμετρώνται αξιόπιστα ως ακυρώσεις.',
                  'Οι μεταφορές αφορούν το συνολικό ιστορικό των ραντεβού του επιλεγμένου μήνα.',
                  'Οι μετρήσεις δείχνουν την τρέχουσα καταγεγραμμένη κατάσταση και δεν αποτελούν αυτόματη τιμολόγηση.'],
    }


def send_report(calendar, mailer, month):
    data = build_report(calendar, month)
    labels = {
        'created_in_month': 'Νέες καταχωρήσεις μέσα στον μήνα',
        'created_after_hours': 'Καταχωρήσεις εκτός ωραρίου',
        'active_appointments_in_month': 'Ενεργά ραντεβού με ημερομηνία μέσα στον μήνα',
        'cancelled_appointments_in_month': 'Ακυρωμένα ραντεβού με ημερομηνία μέσα στον μήνα',
        'elapsed_scheduled_appointments': 'Ραντεβού των οποίων πέρασε η ώρα λήξης',
        'recorded_attended': 'Καταγεγραμμένες προσελεύσεις',
        'recorded_no_show': 'Καταγεγραμμένες μη προσελεύσεις',
        'elapsed_attendance_unknown': 'Παρελθοντικά ραντεβού χωρίς καταγραφή προσέλευσης',
        'patient_confirmations_recorded_in_month': 'Καταγεγραμμένες επιβεβαιώσεις ασθενών μέσα στον μήνα',
    }
    rows = ''.join(f'<li>{escape(label)}: <strong>{data[key]}</strong></li>' for key, label in labels.items())
    notes = ''.join(f'<li>{escape(note)}</li>' for note in data['notes'])
    html = f'<h2>OffVoice AI · Αναφορά {escape(month)}</h2><p>{escape(calendar.settings.doctor_name)}</p><ul>{rows}</ul><h3>Διευκρινίσεις</h3><ul>{notes}</ul>'
    mailer.send(f'OffVoice AI · Μηνιαία αναφορά {month}', html, f'report-{calendar.settings.clinic_id}-{month}')
    return data


def main():
    from dotenv import load_dotenv
    load_dotenv()
    settings = Settings.from_env()
    previous = datetime.now(settings.tz).replace(day=1) - timedelta(days=1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month', default=previous.strftime('%Y-%m'))
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    if not settings.calendar_id or not settings.google_credentials_json:
        parser.error('CALENDAR_ID and GOOGLE_CREDENTIALS_JSON are required')
    calendar = CalendarService(settings)
    data = send_report(calendar, Mailer(settings), args.month) if args.send else build_report(calendar, args.month)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
