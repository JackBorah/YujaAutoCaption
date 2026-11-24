"""YuJa UI adapter. Submit description requests without waiting for completion.

Selectors were inspected in the signed-in Farmingdale YuJa interface. Publication,
owner, title and ID are checked BEFORE submission. Nothing claims AI completion.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError, TimeoutError, expect

from .workbook import Record, normalized, video_id
from .policy import default_title

LOG = logging.getLogger(__name__)
LIBRARY = 'https://farmingdale.video.yuja.com/P/VideoManagement/MediaLibrary'


class ReviewRequired(Exception):
    pass


class YuJaClient:
    def __init__(self, page):
        self.page = page
        self.page.set_default_timeout(15000)
        self.page.set_default_navigation_timeout(45000)
        self.collection_urls: dict[str, str] = {}
        self.root = page

    def wait_for_login(self, timeout_seconds=900):
        """Credentials and MFA are entered by the operator, never by the script."""
        LOG.info('Sign in in Chromium using the account with User Collections access. '
                 'Waiting automatically; no Enter needed (Ctrl+C to stop).')
        deadline = time.monotonic() + timeout_seconds
        context = self.page.context
        while time.monotonic() < deadline:
            for candidate in context.pages:
                for frame in candidate.frames:
                    try:
                        if frame.get_by_role('button', name='User Collections', exact=True).is_visible():
                            self.page = candidate
                            self.root = frame
                            LOG.info('YuJa sign-in detected. Starting user scan.')
                            return
                    except PlaywrightError:
                        continue  # Frames may detach during SSO redirects.
            if not context.pages:
                raise RuntimeError('Browser was closed')
            context.pages[-1].wait_for_timeout(500)
        raise RuntimeError('Timed out waiting for User Collections access after 15 minutes')

    def _ensure_session(self):
        if self.page.is_closed():
            raise RuntimeError('Browser was closed')
        try:
            expect(self.root.get_by_role('button', name='User Collections', exact=True)).to_be_visible(timeout=15000)
        except AssertionError:
            self.wait_for_login()

    def _collection(self, user: str):
        self._ensure_session()
        if user in self.collection_urls:
            return
        self.root.get_by_role('button', name='User Collections', exact=True).click()
        dialog = self.root.get_by_role('dialog', name='User Collections', exact=True)
        dialog.get_by_role('textbox', name='Search Users', exact=True).fill(user)
        dialog.get_by_role('button', name='Search', exact=True).click()
        rows = dialog.locator('.user-table-row')
        # Wait for search results, then compare the separate first/last-name cells.
        expect(rows.filter(has_text=re.compile(re.escape(user.split()[0]), re.I)).first).to_be_visible(timeout=20000)
        matches = []
        for row in rows.all():
            cells = row.locator('.user-table-body-cell').all_text_contents()
            if len(cells) >= 2 and normalized(' '.join(cells[:2])).casefold() == normalized(user).casefold():
                matches.append(row)
        if len(matches) != 1:
            raise ReviewRequired(f'Expected one exact user match for {user}; found {len(matches)}')
        # Do not assume a partial list proves the account is unique.
        if dialog.get_by_role('button', name='Load More', exact=True).is_visible():
            raise ReviewRequired('User search has additional results; narrow the name or review manually')
        matches[0].click()
        expect(dialog).not_to_be_visible()
        self.root.wait_for_url(re.compile(r'/Users/[^/]+/MyMediaCollections(?:[?#].*)?$'))
        self.collection_urls[user] = self.root.url.split('?', 1)[0].split('#', 1)[0]

    def _open_once(self, record: Record):
        if record.collection_url:
            collection_url = record.collection_url
        else:
            self._collection(record.user)
            collection_url = self.collection_urls[record.user]
        query = parse_qs(urlsplit(record.link).query)
        node = record.node or (query.get('node', [''])[0] if video_id(record.link) else '')
        if node.isdigit():
            # Use the report's exact node under the selected owner's library.
            self.page.goto(collection_url + '/WatchVideo/' + node,
                           wait_until='domcontentloaded')
            self.root = self.page.main_frame
        else:
            self.page.goto(collection_url, wait_until='domcontentloaded')
            self.root = self.page.main_frame
            # Older reports without a node need an exact visible title match.
            links = self.root.get_by_role('main').get_by_role('link', name=record.title, exact=True)
            expect(links.first).to_be_visible(timeout=20000)
            if links.count() != 1:
                raise ReviewRequired('Multiple videos match this title; Direct Link with node ID required')
            links.click()
        return self.read_details(record, verify_link=not node.isdigit())

    def read_details(self, record: Record, verify_link=False):
        self.root.get_by_role('button', name='View media details', exact=True).click()
        dialog = self.root.get_by_role('dialog', name='Media Details', exact=True)
        expect(dialog).to_be_visible()
        dialog.get_by_role('tab', name='General', exact=True).click()
        expect(dialog.get_by_role('textbox', name='Enter media title', exact=True)).to_be_visible()
        title = dialog.get_by_role('textbox', name='Enter media title', exact=True).input_value()
        if normalized(title) != record.title:
            raise ReviewRequired(f'Live title differs from workbook: {title}')
        panel = dialog.get_by_role('tabpanel', name='General', exact=True)
        owner = panel.evaluate('''el => {
            const label = [...el.querySelectorAll('label')].find(x => x.textContent.trim() === 'Owner');
            return label?.parentElement.querySelector('span')?.textContent?.trim() || '';
        }''')
        if normalized(owner).casefold() != normalized(record.user).casefold():
            raise ReviewRequired('Live owner differs from workbook user')
        # With a report node ID we opened the exact node and checked its title/owner.
        # Only old reports lacking a node need the additional Links-tab lookup.
        if verify_link and video_id(record.link):
            dialog.get_by_role('tab', name='Links', exact=True).click()
            live_link = dialog.get_by_role('textbox', name='Direct Link', exact=True).input_value()
            if video_id(live_link) != video_id(record.link):
                raise ReviewRequired('Live video ID differs from workbook Direct Link')
            dialog.get_by_role('tab', name='General', exact=True).click()
        return dialog

    def open_record(self, record: Record):
        for attempt in range(3):
            try:
                return self._open_once(record)
            except (TimeoutError, PlaywrightError, AssertionError):
                if attempt == 2 or self.page.is_closed():
                    raise
                LOG.warning('Navigation failed; retry %s/2', attempt + 1)
                self.page.goto(LIBRARY, wait_until='domcontentloaded')
                self.root = self.page.main_frame
                self._ensure_session()
        raise AssertionError('Unreachable')

    def metadata(self, dialog, check_publication=True) -> dict:
        panel = dialog.get_by_role('tabpanel', name='General', exact=True)
        locations = panel.locator('#publishLocationSection .FileParentLocationListElement').all_text_contents() if check_publication else []
        courses = [normalized(s) for s in locations if normalized(s).startswith('Course Channels/')]
        description = panel.get_by_role('textbox', name='Description', exact=True).input_value().strip()
        generate = panel.locator('#generateAIDescBtn')
        present = generate.count() == 1 and generate.is_visible()
        label = (generate.get_attribute('aria-label') or '') if present else ''
        return {'courses': courses, 'description_present': bool(description), 'youtube': not present,
                'generation_available': present
                    and generate.is_enabled() and generate.get_attribute('aria-disabled') != 'true',
                'generation_pending': 'generating' in label.casefold()}

    def close_details(self):
        dialog = self.root.get_by_role('dialog', name='Media Details', exact=True)
        if dialog.is_visible():
            # Two equivalent Close buttons exist (header/footer); deliberately use header.
            dialog.get_by_role('button', name='Close', exact=True).first.click()
            expect(dialog).not_to_be_visible()
        search_dialog = self.root.get_by_role('dialog', name='User Collections', exact=True)
        if search_dialog.is_visible():
            search_dialog.get_by_role('button', name='Close', exact=True).first.click()

    def inspect(self, record: Record) -> dict:
        try:
            return self.metadata(self.open_record(record))
        finally:
            self.close_details()

    def process(self, record: Record, before_request) -> tuple[str, str]:
        if default_title(record.title):
            return 'excluded_title', 'Skipped: default title; video not opened'
        submitted = False
        try:
            dialog = self.open_record(record)
            info = self.metadata(dialog, check_publication=not getattr(self, 'spreadsheet_publication', False))
            if not getattr(self, 'spreadsheet_publication', False) and not info['courses']:
                return 'unpublished', 'Skipped: no live class publication'
            if info['youtube']:
                return 'youtube', 'YouTube: no Generate Description with AI button'
            if info['description_present']:
                return 'existing_description', 'Existing description preserved'
            if info['generation_pending']:
                return 'requested', 'AI description already generating; completion not verified'
            if not info['generation_available']:
                return 'unavailable', 'AI description generation unavailable; review media type/permissions'
            before_request()  # Durable checkpoint BEFORE the side effect.
            submitted = True  # A click timeout may still mean that the request was sent.
            dialog.get_by_role('button', name='Generate Description with AI', exact=True).click()
            return 'requested', 'AI description requested; completion not verified'
        except ReviewRequired as exc:
            return 'review', str(exc)
        except (PlaywrightError, AssertionError) as exc:
            if self.page.is_closed():
                raise RuntimeError('Browser closed; run stopped with checkpoint preserved') from exc
            if submitted:
                return 'unconfirmed', 'AI request may have been submitted; not retried or verified'
            return 'failed', 'Navigation or UI failure: ' + str(exc).splitlines()[0][:250]
        finally:
            # Cleanup must never turn a submitted request into a retryable failure.
            try:
                self.close_details()
            except (PlaywrightError, AssertionError):
                if not submitted:
                    LOG.warning('Could not close media details; next navigation will reset the view')

    def recover(self, record: Record) -> tuple[str, str]:
        # This pass explicitly does not verify generation. Leave the request for review.
        return 'unconfirmed', 'Interrupted AI request may have been submitted; not retried or verified'
