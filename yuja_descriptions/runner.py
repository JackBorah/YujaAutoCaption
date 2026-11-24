from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .checkpoint import Checkpoint
from .policy import precheck
from .workbook import Workbook

LOG = logging.getLogger(__name__)
PRESERVE = {'requested', 'requesting', 'unconfirmed', 'generated', 'existing_description', 'youtube', 'untouched'}


def previous_result(checkpoint, record):
    node = parse_qs(urlsplit(record.link).query).get('node', [''])[0]
    # Node history from the all-user workflow takes precedence over older row reviews.
    candidates = [checkpoint.get('node:' + node) if node else None, checkpoint.get(record.key)]
    return next((r for r in candidates if r and r['status'] in PRESERVE), None)


def restore_scan_log(book, checkpoint):
    """Keep the earlier all-user audit sheet when switching to row-only processing."""
    records = checkpoint.data.get('scan_records', {})
    if records:
        book.scan_rows = [[r['user'], r['email'], r['title'], r['node'], r['link'], 'JB',
                           checkpoint.get(k)['status'], checkpoint.get(k)['note'], checkpoint.get(k)['at']]
                          for k, r in records.items() if checkpoint.get(k)]


def run_records(book: Workbook, output: Path, checkpoint: Checkpoint, client, limit: int | None = None) -> dict[str, int]:
    """Checkpoint first, then workbook; replay safely if the workbook was locked."""
    counts: dict[str, int] = {}
    processed = 0
    restore_scan_log(book, checkpoint)
    book.save(output)  # Prove the output is writable before any website mutation.
    for record in reversed(book.records):
        if record.initials:
            counts['untouched'] = counts.get('untouched', 0) + 1
            continue
        previous = previous_result(checkpoint, record)
        if previous and previous['status'] != 'requesting':
            result = previous
        else:
            if limit is not None and processed >= limit:
                continue
            processed += 1
            LOG.info('Checking %s / %s (row %s)', record.user, record.title, record.row)
            check = precheck(record)
            if check:
                status, note = check
            elif previous:  # Never resubmit a possibly completed request after interruption.
                status, note = client.recover(record)
            else:
                def before_request():
                    checkpoint.put(record.key, 'requesting', 'AI request may be in flight; inspect before retrying')
                status, note = client.process(record, before_request)
            result = checkpoint.put(record.key, status, note)
        book.mark(record, f"[{result['at']}] {result['note']}")
        node = parse_qs(urlsplit(record.link).query).get('node', [''])[0]
        if node and 'node:' + node in checkpoint.data.get('scan_records', {}):
            checkpoint.data['results']['node:' + node] = dict(result)
            checkpoint.save()
            restore_scan_log(book, checkpoint)
        book.save(output)  # Failure propagates: do not continue making remote changes.
        counts[result['status']] = counts.get(result['status'], 0) + 1
        LOG.info('%s: %s', result['status'], result['note'])
    return counts
