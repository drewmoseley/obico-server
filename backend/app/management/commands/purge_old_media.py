import os
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.models import Print

RETENTION_DAYS = 30


class Command(BaseCommand):
    help = (
        'Purge capture media (snapshots, timelapses) per retention policy: keep only '
        'captures from prints Obico itself flagged as failed (Print.alerted_at set), '
        'delete everything else, and purge anything older than 30 days regardless. '
        'Prints still in progress (no finished_at/cancelled_at) are never touched. '
        'Dry-run by default; pass --commit to actually delete.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Actually delete files. Default is dry-run.')

    @staticmethod
    def _keep_print(p, cutoff):
        return bool(p.alerted_at) and p.started_at is not None and p.started_at >= cutoff

    def handle(self, *args, **options):
        commit = options['commit']
        cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)

        ended_prints = [p for p in Print.objects.all() if p.ended_at() is not None]
        by_id = {p.id: p for p in ended_prints}
        by_printer = {}
        for p in ended_prints:
            by_printer.setdefault(p.printer_id, []).append(p)

        to_delete = []
        to_keep = 0

        # tsd-timelapses/private/<print_id>{,_tagged}.mp4, <print_id>_p.json — keyed directly by print id
        timelapse_dir = os.path.join(settings.MEDIA_ROOT, settings.TIMELAPSE_CONTAINER, 'private')
        if os.path.isdir(timelapse_dir):
            for fname in os.listdir(timelapse_dir):
                m = re.match(r'^(\d+)[._]', fname)
                if not m:
                    continue
                p = by_id.get(int(m.group(1)))
                if p is None:
                    continue  # print still active, or row gone independently — leave alone
                path = os.path.join(timelapse_dir, fname)
                if self._keep_print(p, cutoff):
                    to_keep += 1
                else:
                    to_delete.append(path)

        # tsd-pics/{ff_printshots,p}/<printer_id>/<print_id>/... — also keyed directly by print id
        for subdir in ('ff_printshots', 'p'):
            base = os.path.join(settings.MEDIA_ROOT, settings.PICS_CONTAINER, subdir)
            if not os.path.isdir(base):
                continue
            for printer_id_name in os.listdir(base):
                printer_dir = os.path.join(base, printer_id_name)
                if not os.path.isdir(printer_dir):
                    continue
                for print_id_name in os.listdir(printer_dir):
                    print_dir = os.path.join(printer_dir, print_id_name)
                    if not os.path.isdir(print_dir):
                        continue
                    try:
                        p = by_id.get(int(print_id_name))
                    except ValueError:
                        continue
                    if p is None:
                        continue
                    keep = self._keep_print(p, cutoff)
                    for fname in os.listdir(print_dir):
                        path = os.path.join(print_dir, fname)
                        if keep:
                            to_keep += 1
                        else:
                            to_delete.append(path)

        # tsd-pics/{snapshots,raw,tagged}/<printer_id>/<epoch>[_rotated].jpg — keyed by printer id +
        # timestamp only, so match each file to whichever print's [started_at, ended_at] window it
        # falls inside. No match at all (e.g. routine snapshots outside any print) gets age-only purge.
        for subdir in ('snapshots', 'raw', 'tagged'):
            base = os.path.join(settings.MEDIA_ROOT, settings.PICS_CONTAINER, subdir)
            if not os.path.isdir(base):
                continue
            for printer_id_name in os.listdir(base):
                printer_dir = os.path.join(base, printer_id_name)
                if not os.path.isdir(printer_dir):
                    continue
                try:
                    printer_id = int(printer_id_name)
                except ValueError:
                    continue
                prints_for_printer = by_printer.get(printer_id, [])
                for fname in os.listdir(printer_dir):
                    if fname == 'latest_unrotated.jpg':
                        continue  # live cache file, not a retention target
                    m = re.match(r'^(\d+\.\d+)', fname)
                    if not m:
                        continue
                    ts = datetime.fromtimestamp(float(m.group(1)), tz=dt_timezone.utc)
                    path = os.path.join(printer_dir, fname)

                    match = next(
                        (p for p in prints_for_printer if p.started_at and p.started_at <= ts <= p.ended_at()),
                        None,
                    )
                    if match is not None:
                        if self._keep_print(match, cutoff):
                            to_keep += 1
                        else:
                            to_delete.append(path)
                    elif ts < cutoff:
                        to_delete.append(path)
                    else:
                        to_keep += 1

        self.stdout.write(f'{len(to_delete)} file(s) to delete, {to_keep} to keep.')
        for path in to_delete:
            self.stdout.write(f'  {"DELETE" if commit else "would delete"}: {path}')
            if commit:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

        if not commit:
            self.stdout.write(self.style.WARNING('Dry run only — re-run with --commit to actually delete.'))
