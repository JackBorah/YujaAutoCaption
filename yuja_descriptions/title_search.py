"""Search only spreadsheet-listed titles; never enumerate user libraries."""
import re
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import expect
from .browser import YuJaClient, ReviewRequired
from .policy import precheck
from .workbook import video_id


class SpreadsheetClient(YuJaClient):
    spreadsheet_publication = True

    def _open_once(self, record):
        self._ensure_session()
        scope = self.root.get_by_role('button', name='Change the scope of your search', exact=True)
        if not self.root.get_by_role('radio', name='All Media', exact=True).is_visible():
            scope.click()
        self.root.get_by_role('radio', name='All Media', exact=True).check()
        if self.root.get_by_role('radio', name='All Media', exact=True).is_visible():
            scope.click()
        search = self.root.get_by_role('combobox', name='Search Box for Media Library', exact=True)
        search.fill(record.title)
        search.press('Enter')
        self.root.wait_for_url(re.compile(r'/search\?'))
        self.root.wait_for_function('''title => (new URL(location.href)).searchParams.get('query')?.includes(title)''', arg=record.title)
        node = parse_qs(urlsplit(record.link).query).get('node', [''])[0] if video_id(record.link) else ''
        main = self.root.get_by_role('main')
        if node.isdigit():
            card = main.locator(f'[id="di_fileListEntry-{node}"], [id="di_fileGridEntry-{node}"]')
            expect(card).to_be_visible(timeout=20000)
            expect(card.get_by_role('link', name=record.title, exact=True)).to_be_visible()
        else:
            card = main.get_by_role('group').filter(has=self.root.get_by_role('link', name=record.title, exact=True))
            expect(card.first).to_be_visible(timeout=20000)
            if card.count() != 1:
                raise ReviewRequired('Duplicate title results; a Direct Link with node ID is required')
        # More opens the media view; it does not edit the video.
        card.get_by_role('menuitem', name='More Menu Button', exact=True).click()
        return self.read_details(record, verify_link=not node.isdigit())

    def process(self, record, before_request):
        check = precheck(record)
        if check:
            return check
        return super().process(record, before_request)

    def inspect(self, record):
        check = precheck(record)
        if check:
            return {'status': check[0], 'note': check[1]}
        try:
            return self.metadata(self.open_record(record), check_publication=False)
        finally:
            self.close_details()
