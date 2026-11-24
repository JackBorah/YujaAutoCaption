from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from .checkpoint import Checkpoint, RunLock
from .policy import precheck
from .workbook import Workbook

START_URL = 'https://farmingdale.video.yuja.com/P/VideoManagement/MediaLibrary/Users/u-nUUxUOeZQx/MyMediaCollections'


def main() -> int:
    parser = argparse.ArgumentParser(description='Search Excel-listed video titles with a populated Course/Academic Unit Association.')
    parser.add_argument('input', type=Path, help='Original .xlsx report')
    parser.add_argument('--output', type=Path, help='Separate results workbook; default: outputs/<name>_processed.xlsx')
    parser.add_argument('--dry-run', action='store_true', help='Print eligible rows, bottom-to-top; no browser or files written')
    parser.add_argument('--inspect', action='store_true', help='Search eligible titles and read descriptions without generating or marking results')
    parser.add_argument('--limit', type=int, help='Maximum new records to check in this run')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if args.dry_run and args.inspect:
        parser.error('Choose --dry-run or --inspect')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        book = Workbook(args.input)
        if any(r.course_associations is None for r in book.records):
            raise ValueError('The input report must contain the Course/Academic Unit Association column.')
        if args.dry_run:
            for record in reversed(book.records):
                status, note = precheck(record) or ('eligible', 'Search title; use spreadsheet course association')
                print(json.dumps({'row': record.row, 'user': record.user, 'title': record.title,
                    'status': status, 'note': note}, ensure_ascii=False))
            return 0
        from playwright.sync_api import sync_playwright
        from .title_search import SpreadsheetClient
        from .runner import run_records, previous_result
        output = args.output or Path('outputs') / (args.input.stem + '_processed.xlsx')
        if output.resolve() == args.input.resolve():
            raise ValueError('Output must differ from input')
        output.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path = output.with_suffix('.checkpoint.json')
        if output.exists() and not checkpoint_path.exists() and not args.inspect:
            raise ValueError('Output already exists without its checkpoint. Choose a new --output path.')
        with RunLock(output.with_suffix('.lock')):
            legacy_records = Workbook(output).record_signatures if output.exists() else None
            checkpoint = Checkpoint(checkpoint_path, book.digest, book.record_signatures, legacy_records)
            checkpoint.data['workflow'] = 'spreadsheet'
            if not args.inspect:
                checkpoint.save()
            logging.info('Mode: spreadsheet titles only, bottom-to-top; no user-library scans or live publication checks.')
            logging.info('Results go to %s (original rows, Initials and Notes). Input workbook is preserved.', output.resolve())
            if not args.inspect and all(precheck(r) or (previous_result(checkpoint,r) and
                    previous_result(checkpoint,r)['status'] != 'requesting') for r in book.records):
                counts = run_records(book,output,checkpoint,None,args.limit)
                print(json.dumps(counts,indent=2))
                print(f'Results: {output.resolve()}')
                return 0
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                try:
                    page = context.new_page()
                    page.goto(START_URL, wait_until='domcontentloaded')
                    client = SpreadsheetClient(page)
                    client.wait_for_login()
                    if args.inspect:
                        selected = [r for r in reversed(book.records) if precheck(r) is None]
                        for record in selected[:args.limit]:
                            print(json.dumps({'row':record.row, 'title':record.title, 'inspection':client.inspect(record)}, ensure_ascii=False))
                        counts = {'inspected':len(selected[:args.limit])}
                    else:
                        counts = run_records(book, output, checkpoint, client, args.limit)
                    print(json.dumps(counts, indent=2))
                    if not args.inspect:
                        print(f'Results: {output.resolve()}')
                    if counts.get('scan_failed'):
                        logging.error('Some accounts were not fully scanned. See scan_failed rows and rerun to retry discovery.')
                        return 2
                finally:
                    context.close()
                    browser.close()
        return 0
    except KeyboardInterrupt:
        print('\nStopped. Saved checkpoints will be recovered on the next run.', file=sys.stderr)
        return 130
    except Exception as exc:
        logging.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
