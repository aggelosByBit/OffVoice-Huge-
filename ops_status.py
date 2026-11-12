"""Clinic-side aggregate delivery status, without names, numbers or transcripts."""
import json
from calendar_service import CalendarService, private
from settings import Settings


def status(calendar):
    with calendar.service() as service:
        events = calendar.events(service, privateExtendedProperty=[f'ov_clinic={calendar.settings.clinic_id}'])
    props = [private(event) for event in events if calendar.owns(event)]
    return {
        'appointments_with_pending_email': sum(p.get('ov_notify_pending') == 'yes' for p in props),
        'appointments_needing_email_review': sum(p.get('ov_review_needed') == 'yes' for p in props),
        'reminders_with_unknown_outcome': sum(p.get('ov_reminder_state') in {'unknown', 'sending'} for p in props),
        'failed_reminders': sum(p.get('ov_reminder_state') == 'failed' for p in props),
    }


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    print(json.dumps(status(CalendarService(Settings.from_env())), indent=2))
