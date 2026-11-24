"""User-driven scan runner; Excel is the user list, not the video boundary."""
from collections import Counter
from datetime import datetime, timezone
import logging
from urllib.parse import parse_qs, urlsplit

from .discovery import accounts_for, discover_videos
from .policy import default_title

LOG = logging.getLogger(__name__)
PROTECTED = {'requested', 'requesting', 'unconfirmed', 'generated', 'existing_description', 'youtube'}


def input_nodes(book):
    nodes = {}
    for record in book.records:
        node = parse_qs(urlsplit(record.link).query).get('node', [''])[0]
        if node.isdigit():
            nodes.setdefault(node, []).append(record)
    return nodes


def save_scan(book, output, state):
    book.updates.clear()
    rows = []
    sources = input_nodes(book)
    # Carry forward earlier row-based requests into the new complete result sheet.
    for node, originals in sources.items():
        key = 'node:' + node
        if key in state.data['scan_records']:
            continue
        original = next((r for r in originals if state.get(r.key) and
                         state.get(r.key)['status'] in PROTECTED), None)
        if original:
            state.data['scan_records'][key] = {'user': original.user, 'email': '', 'node': node,
                                               'title': original.title, 'link': original.link}
            state.data['results'][key] = dict(state.get(original.key))
    for key, record in state.data.get('scan_records', {}).items():
        result = state.get(key)
        if not result:
            continue
        note = result['note']
        rows.append([record['user'], record['email'], record['title'], record['node'], record['link'],
                     'JB', result['status'], note, result['at']])
        for original in sources.get(record['node'], []):
            if not original.initials:
                book.mark(original, f"[{result['at']}] {note}")
    for item in state.data.get('scan_errors', {}).values():
        rows.append([item['user'], item.get('email', ''), '', '', '', '', 'scan_failed', item['note'], item['at']])
    book.scan_rows = rows
    book.save(output)


def run_users(book, output, state, client, limit=None, inspect_only=False):
    state.data.setdefault('scan_records', {})
    state.data.setdefault('scan_errors', {})
    sources = input_nodes(book)
    seen_accounts, seen_nodes = set(), set()
    counts = Counter()
    checked = 0
    if not inspect_only:
        save_scan(book, output, state)  # Validate writes before any requests.
    for user in book.users:
        try:
            accounts = accounts_for(client, user)
            if not accounts:
                raise RuntimeError('No exact matching user accounts found')
        except Exception as exc:
            if client.page.is_closed():
                raise
            counts['scan_failed'] += 1
            LOG.error('Could not find accounts for %s: %s', user, exc)
            if not inspect_only:
                state.data['scan_errors'][user] = {'user': user, 'note': str(exc).splitlines()[0],
                    'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                state.save()
                save_scan(book, output, state)
            continue
        state.data['scan_errors'].pop(user, None)
        for account in accounts:
            if account.url in seen_accounts:
                continue
            seen_accounts.add(account.url)
            LOG.info('Scanning ALL videos: %s <%s>', account.name, account.email)
            try:
                videos = iter(discover_videos(client, account))
                while True:
                    # Only enumeration failures are account failures. Save failures must stop the run.
                    try:
                        video = next(videos)
                    except StopIteration:
                        break
                    if video.node in seen_nodes:
                        continue
                    seen_nodes.add(video.node)
                    if limit is not None and checked >= limit:
                        return dict(counts)
                    prior = state.get(video.key)
                    originals = sources.get(video.node, [])
                    if prior is None:
                        # Preserve earlier requests, but retry old ambiguous-user reviews now that
                        # all matching accounts are explicitly authorized.
                        prior = next((state.get(r.key) for r in originals
                                      if state.get(r.key) and state.get(r.key)['status'] in PROTECTED), None)
                    if inspect_only:
                        if default_title(video.title):
                            LOG.info('%s: skipped default title without opening', video.title)
                        else:
                            LOG.info('%s: %s', video.title, client.inspect(video))
                        checked += 1
                        counts['inspected'] += 1
                        continue
                    state.data['scan_records'][video.key] = {'user': video.user, 'email': video.account_email,
                        'node': video.node, 'title': video.title, 'link': video.link}
                    if prior:
                        status, note = prior['status'], prior['note']
                        if status == 'requesting':
                            status, note = client.recover(video)
                        counts['resumed'] += 1
                    elif default_title(video.title):
                        status, note = 'excluded_title', 'Skipped: default title; video not opened'
                        checked += 1
                    elif any(r.initials for r in originals):
                        status, note = 'untouched', 'Existing spreadsheet initials preserved'
                    else:
                        LOG.info('Checking %s <%s> / %s', video.user, video.account_email, video.title)
                        def before_request():
                            state.put(video.key, 'requesting', 'AI request may have been submitted; not yet recorded')
                        status, note = client.process(video, before_request)
                        checked += 1
                    if prior and prior['status'] == status:
                        state.data['results'][video.key] = dict(prior)
                        state.save()
                    else:
                        state.put(video.key, status, note)
                    save_scan(book, output, state)
                    counts[status] += 1
                    LOG.info('%s: %s — %s', status, video.title, note)
            except (OSError, KeyboardInterrupt):
                raise
            except Exception as exc:
                if client.page.is_closed():
                    raise
                counts['scan_failed'] += 1
                LOG.error('Incomplete scan for %s <%s>: %s', account.name, account.email, exc)
                if not inspect_only:
                    state.data['scan_errors'][account.url] = {'user': account.name, 'email': account.email,
                        'note': str(exc).splitlines()[0], 'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                    state.save()
                    save_scan(book, output, state)
                continue
            state.data['scan_errors'].pop(account.url, None)
            if not inspect_only:
                state.save()
                save_scan(book, output, state)
    return dict(counts)
