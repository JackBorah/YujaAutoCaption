"""Real Playwright on local HTML reproducing the inspected YuJa controls."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from playwright.sync_api import sync_playwright
from yuja_descriptions.browser import YuJaClient
from yuja_descriptions.workbook import Workbook
from test_workbook import fixture

DIALOG = '''<!doctype html><html><body>
<button>User Collections</button>
<button onclick="document.querySelector('[role=dialog]').hidden=false">View media details</button>
<div role="dialog" aria-label="Media Details" hidden>
<button onclick="this.parentElement.hidden=true">Close</button>
<button role="tab">General</button><button role="tab">Links</button>
<div role="tabpanel" aria-label="General">
<input aria-label="Enter media title" value="У лукоморья дуб зелёный">
<div><label>Owner</label><div><span>Test User</span></div></div>
<textarea aria-label="Description"></textarea>
<div id="publishLocationSection"><div class="FileParentLocationListElement">Course Channels/Test Course</div></div>
<button id="generateAIDescBtn" aria-label="Generate Description with AI" aria-disabled="false"
 onclick="window.requests=(window.requests||0)+1">Generate Description with AI</button>
</div>
<input aria-label="Direct Link" value="https://farmingdale.video.yuja.com/V/Video?v=123&node=456">
<button onclick="window.saves=(window.saves||0)+1">Save</button>
</div></body></html>'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'fixture.xlsx'
        fixture(path)
        self.record = replace(Workbook(path).records[0],link='https://farmingdale.video.yuja.com/V/Video?v=123&node=456')
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.html = DIALOG
        self.page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=self.html))
        self.base = 'https://farmingdale.video.yuja.com/P/VideoManagement/MediaLibrary/Users/u-test/MyMediaCollections'
        self.page.goto(self.base)
        self.client = YuJaClient(self.page)
        self.client.collection_urls['Test User'] = self.base

    def test_request_then_close_without_save_or_verification(self):
        writes = []
        status, note = self.client.process(self.record, lambda: writes.append('before'))
        self.assertEqual(writes,['before'])
        self.assertEqual(status,'requested')
        self.assertIn('not verified', note)
        self.assertEqual(self.page.evaluate('window.requests'),1)
        self.assertIsNone(self.page.evaluate('window.saves'))
        self.assertFalse(self.page.get_by_role('dialog').is_visible())

    def test_existing_description_is_not_replaced(self):
        self.html = DIALOG.replace('<textarea aria-label="Description"></textarea>', '<textarea aria-label="Description">Existing description</textarea>')
        status, _ = self.client.process(self.record, lambda: self.fail('Must not submit'))
        self.assertEqual(status,'existing_description')
        self.assertIsNone(self.page.evaluate('window.requests'))

    def test_mismatched_owner_is_review_only(self):
        self.html = DIALOG.replace('<span>Test User</span>','<span>Other User</span>')
        status, _ = self.client.process(self.record, lambda: self.fail('Must not submit'))
        self.assertEqual(status,'review')
        self.assertIsNone(self.page.evaluate('window.requests'))

    def test_no_class_is_excluded(self):
        self.html = DIALOG.replace('Course Channels/Test Course','User Collections/Test User/My Media Collections')
        status, _ = self.client.process(self.record, lambda: self.fail('Must not submit'))
        self.assertEqual(status,'unpublished')

    def test_youtube_is_not_submitted(self):
        import re
        self.html = re.sub(r'<button id="generateAIDescBtn".*?</button>', '', DIALOG, flags=re.S)
        status, _ = self.client.process(self.record, lambda: self.fail('Must not submit'))
        self.assertEqual(status,'youtube')

    def test_recovery_does_not_open_or_resubmit(self):
        self.client.open_record = lambda _: self.fail('No generation verification this pass')
        status, _ = self.client.recover(self.record)
        self.assertEqual(status,'unconfirmed')

    def test_login_detects_delayed_controls_without_enter(self):
        from unittest.mock import patch
        self.page.set_content('<html><body><script>setTimeout(()=>{document.body.innerHTML="<button>User Collections</button>"},80)</script></body></html>')
        with patch('builtins.input', side_effect=AssertionError('No Enter prompts allowed')):
            self.client.wait_for_login(timeout_seconds=2)
        self.assertEqual(self.client.page,self.page)

    def test_resolves_both_same_name_accounts(self):
        from yuja_descriptions.discovery import accounts_for
        self.page.set_content('''<button onclick="document.querySelector('[role=dialog]').hidden=false">User Collections</button>
        <div role="dialog" aria-label="User Collections" hidden>
        <input aria-label="Search Users"><button aria-label="Search">Search</button>
        <div class="user-table-row" onclick="history.pushState({},'', '/P/VideoManagement/MediaLibrary/Users/u-one/MyMediaCollections');this.parentElement.hidden=true">
        <div class="user-table-body-cell">Test</div><div class="user-table-body-cell">User</div><div class="user-table-body-cell">one@example.edu</div></div>
        <div class="user-table-row" onclick="history.pushState({},'', '/P/VideoManagement/MediaLibrary/Users/u-two/MyMediaCollections');this.parentElement.hidden=true">
        <div class="user-table-body-cell">Test</div><div class="user-table-body-cell">User</div><div class="user-table-body-cell">two@example.edu</div></div>
        <button onclick="this.parentElement.hidden=true">Close</button></div>''')
        accounts = accounts_for(self.client,'Test User')
        self.assertEqual([a.email for a in accounts],['one@example.edu','two@example.edu'])
        self.assertEqual(len({a.url for a in accounts}),2)

    def test_collects_all_load_more_videos_and_current_link_titles(self):
        from yuja_descriptions.discovery import collect_folder
        self.page.set_content('''<main>
        <div role="group" aria-label="Old title Video" id="di_fileGridEntry-1"><div role="link" aria-label="Updated title"></div></div>
        <button onclick="loadNext(this)">Load More</button>
        <script>function loadNext(b){b.insertAdjacentHTML('beforebegin', '<div role="group" aria-label="Second Video" id="di_fileGridEntry-2"><div role="link" aria-label="Second"></div></div>');b.remove();}</script>
        </main>''')
        entries = collect_folder(self.client)
        self.assertEqual([e['title'] for e in entries],['Updated title','Second'])

    def test_discovers_nested_folder_video(self):
        from unittest.mock import patch
        from yuja_descriptions.discovery import Account, discover_videos
        def route(request):
            if '/SubFolder' in request.request.url:
                body = '<main><div role="group" aria-label="Nested Video" id="di_fileGridEntry-2"><div role="link" aria-label="Nested"></div></div></main>'
            else:
                body = '<main><div role="group" aria-label="Lessons Folder" id="di_fileGridEntry-9" ondblclick="location.href=location.href+\'/SubFolder\'">Lessons</div></main>'
            request.fulfill(body='<button>User Collections</button>'+body, content_type='text/html')
        self.page.unroute('**/*')
        self.page.route('**/*',route)
        from yuja_descriptions.discovery import READ_ENTRIES, ENTRY_SELECTOR
        # Traversal is independent of the separately tested lazy-loading loop.
        with patch('yuja_descriptions.discovery.collect_folder', side_effect=lambda c:c.page.locator(ENTRY_SELECTOR).evaluate_all(READ_ENTRIES)):
            videos = list(discover_videos(self.client,Account('Test User','one@example.edu',self.base)))
        self.assertEqual([v.node for v in videos],['2'])
        self.assertEqual(videos[0].title,'Nested')


if __name__ == '__main__':
    unittest.main()
