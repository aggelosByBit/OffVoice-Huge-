"""Entirely local, synthetic demo adapters. Nothing is sent outside this process."""
import copy
from datetime import datetime, timezone
import json
import threading

import httplib2
from googleapiclient.errors import HttpError


class Request:
    def __init__(self, execute):
        self.execute = execute
        self.headers = {}


def api_error(status):
    return HttpError(httplib2.Response({'status': str(status)}), json.dumps({'error': {'message': 'Synthetic error'}}).encode())


class MemoryCalendar:
    def __init__(self):
        self.items = {}
        self.lock = threading.RLock()
        self.page_size = 250

    def close(self):
        pass

    def events(self):
        return self

    @staticmethod
    def moment(info):
        value = info.get('dateTime', info.get('date'))
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def list(self, **kwargs):
        def execute():
            with self.lock:
                items = copy.deepcopy(list(self.items.values()))
            if not kwargs.get('showDeleted'):
                items = [e for e in items if e.get('status') != 'cancelled']
            if kwargs.get('timeMin'):
                minimum = datetime.fromisoformat(kwargs['timeMin'].replace('Z', '+00:00'))
                items = [e for e in items if self.moment(e['end']) > minimum]
            if kwargs.get('timeMax'):
                maximum = datetime.fromisoformat(kwargs['timeMax'].replace('Z', '+00:00'))
                items = [e for e in items if self.moment(e['start']) < maximum]
            for item in kwargs.get('privateExtendedProperty', []):
                key, value = item.split('=', 1)
                items = [e for e in items if e.get('extendedProperties', {}).get('private', {}).get(key) == value]
            if kwargs.get('q'):
                query = kwargs['q'].casefold()
                items = [e for e in items if query in (e.get('summary', '') + ' ' + e.get('description', '')).casefold()]
            items.sort(key=lambda e: (e.get('start', {}).get('dateTime', ''), e['id']))
            offset = int(kwargs.get('pageToken', '0'))
            limit = min(kwargs.get('maxResults', 250), self.page_size)
            result = {'items': items[offset:offset + limit]}
            if len(items) > offset + limit:
                result['nextPageToken'] = str(offset + limit)
            return result
        return Request(execute)

    def get(self, **kwargs):
        def execute():
            with self.lock:
                if kwargs['eventId'] not in self.items:
                    raise api_error(404)
                return copy.deepcopy(self.items[kwargs['eventId']])
        return Request(execute)

    def insert(self, **kwargs):
        def execute():
            with self.lock:
                body = copy.deepcopy(kwargs['body'])
                if body['id'] in self.items:
                    raise api_error(409)
                body.update(etag='"1"', status='confirmed', created=datetime.now(timezone.utc).isoformat())
                self.items[body['id']] = body
                return copy.deepcopy(body)
        return Request(execute)

    def patch(self, **kwargs):
        def execute():
            with self.lock:
                if kwargs['eventId'] not in self.items:
                    raise api_error(404)
                current = self.items[kwargs['eventId']]
                if request.headers.get('If-Match', current['etag']) != current['etag']:
                    raise api_error(412)
                changes = copy.deepcopy(kwargs['body'])
                if 'extendedProperties' in changes:
                    props = current.setdefault('extendedProperties', {}).setdefault('private', {})
                    props.update(changes.pop('extendedProperties').get('private', {}))
                    for key in [k for k, v in props.items() if v is None]:
                        del props[key]
                current.update(changes)
                current['etag'] = '"' + str(int(current['etag'].strip('"')) + 1) + '"'
                return copy.deepcopy(current)
        request = Request(execute)
        return request


class DemoLLM:
    async def complete(self, messages):
        return {'role': 'assistant', 'content': 'Είστε σε δοκιμαστική λειτουργία με εικονικά στοιχεία. Η συνομιλία σας παραμένει ξεχωριστή· για δοκιμή πλήρους κράτησης συνδέστε το δοκιμαστικό ημερολόγιο και το AI στις ρυθμίσεις.'}

    async def close(self):
        pass
