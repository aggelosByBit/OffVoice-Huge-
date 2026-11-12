"""Clinic-side attendance entry. This operation is intentionally not an AI tool/public route."""
import argparse

from calendar_service import BookingError, CalendarService
from settings import Settings


def record_attendance(calendar, code, status):
    if status not in {'attended', 'no_show', 'unknown'}:
        raise ValueError('Unknown attendance status')
    with calendar._lock, calendar.service() as service:
        event = calendar.lookup(service, code)
        props = calendar._metadata(event)
        if props.get('ov_state') == 'cancelled':
            raise BookingError('CANCELLED', 'Δεν καταγράφεται προσέλευση σε ακυρωμένο ραντεβού.')
        if calendar.read_time(event['end']['dateTime']) > calendar.now():
            raise BookingError('NOT_ENDED', 'Η καταγραφή γίνεται μετά τη λήξη του ραντεβού.')
        props.update(ov_attendance=status, ov_attendance_at=calendar.now().isoformat())
        calendar._patch(service, event, {'extendedProperties': {'private': props}})


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code', required=True)
    parser.add_argument('--attendance', choices=['attended', 'no_show', 'unknown'], required=True)
    parser.add_argument('--apply', action='store_true', help='Actually update the clinic calendar')
    args = parser.parse_args()
    if not args.apply:
        parser.error('Use --apply to confirm this calendar update')
    record_attendance(CalendarService(Settings.from_env()), args.code, args.attendance)
    print('Η κατάσταση προσέλευσης ενημερώθηκε.')
