#!/usr/bin/env python3
"""Remove legacy cross-calendar copies only when a managed destination UID exists."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from academic_assistant.calendar_sync import GoogleCalendarSync


def managed_events(service, calendar_id):
    events = []
    token = None
    while True:
        page = service.events().list(calendarId=calendar_id,
            privateExtendedProperty='attendr_source=canvas', showDeleted=False,
            maxResults=2500, pageToken=token).execute()
        events.extend(e for e in page.get('items', []) if
            e.get('extendedProperties', {}).get('private', {}).get('attendr_source') == 'canvas')
        token = page.get('nextPageToken')
        if not token:
            return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-calendar', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    calendar = GoogleCalendarSync.from_env(ROOT / '.env')
    target, _ = calendar.resolve_calendar()
    service = calendar._service
    source = service.calendars().get(calendarId=args.source_calendar).execute()['id']
    if source == target:
        raise SystemExit('Source and destination must differ.')
    uid = lambda e: e.get('extendedProperties', {}).get('private', {}).get('canvas_uid')
    destinations = {uid(e): e for e in managed_events(service, target) if uid(e)}
    duplicates = [e for e in managed_events(service, source) if uid(e) in destinations]
    print(f'{len(duplicates)} legacy managed copies have a destination counterpart.')
    if not args.apply:
        return
    backup = ROOT / 'data' / ('calendar-duplicates-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    backup.write_text(json.dumps({'source': source, 'destination': target, 'events': duplicates}, indent=2))
    backup.chmod(0o600)
    for event in duplicates:
        current = service.events().get(calendarId=target, eventId=destinations[uid(event)]['id']).execute()
        if current.get('status') == 'cancelled' or uid(current) != uid(event):
            raise SystemExit('Destination changed; cleanup stopped.')
        service.events().delete(calendarId=source, eventId=event['id'], sendUpdates='none').execute()
    print(f'Removed {len(duplicates)} copies; backup: {backup}')


if __name__ == '__main__':
    main()
