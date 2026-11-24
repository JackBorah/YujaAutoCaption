from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from playwright.sync_api import sync_playwright
from test_workbook import fixture
from test_browser import DIALOG
from yuja_descriptions.workbook import Workbook
from yuja_descriptions.title_search import SpreadsheetClient


class TitleSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)/'input.xlsx'
        fixture(path)
        self.record = replace(Workbook(path).records[0], link='https://farmingdale.video.yuja.com/V/Video?v=123&node=456')
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.route('**/*', lambda route: route.fulfill(body='<html></html>'))
        self.page.goto('https://farmingdale.video.yuja.com/P/VideoManagement/MediaLibrary')
        self.client = SpreadsheetClient(self.page)
        self.set_html()

    def set_html(self, existing_description=False):
        import json
        details = DIALOG.replace('<div id="publishLocationSection"><div class="FileParentLocationListElement">Course Channels/Test Course</div></div>', '')
        if existing_description:
            details = details.replace('<textarea aria-label="Description"></textarea>', '<textarea aria-label="Description">Already described</textarea>')
        self.page.set_content('''<button>User Collections</button>
        <button aria-label="Change the scope of your search" onclick="document.getElementById('scope').hidden=!document.getElementById('scope').hidden">Scope</button>
        <div id="scope" hidden><label><input type="radio" name="scope">All Media</label></div>
        <input role="combobox" aria-label="Search Box for Media Library" onkeydown="if(event.key==='Enter')searchTitle(this.value)">
        <main></main><script>
        var details = ''' + json.dumps(details) + ''';
        function openVideo(){document.body.innerHTML=details;}
        function searchTitle(title){window.searches=(window.searches||0)+1;
          history.pushState({},'', '/P/VideoManagement/MediaLibrary/search?query='+encodeURIComponent('('+title+')'));
          const main=document.querySelector('main');
          main.innerHTML='<div role="group" id="di_fileListEntry-456"><a role="link"></a><button role="menuitem" aria-label="More Menu Button" onclick="openVideo()">More</button></div>';
          main.querySelector('a').setAttribute('aria-label',title);main.querySelector('a').textContent=title;
        }</script>''')

    def test_searches_title_and_requests_without_live_publication(self):
        submitted=[]
        status,_ = self.client.process(self.record,lambda:submitted.append(True))
        self.assertEqual(status,'requested')
        self.assertEqual(submitted,[True])
        self.assertEqual(self.page.evaluate('window.searches'),1)
        self.assertEqual(self.page.evaluate('window.requests'),1)

    def test_blank_association_never_searches_even_with_course_path(self):
        status,_ = self.client.process(replace(self.record,course_associations=()), lambda:self.fail('No request allowed'))
        self.assertEqual(status,'unpublished')
        self.assertIsNone(self.page.evaluate('window.searches'))

    def test_default_title_never_searches(self):
        status,_ = self.client.process(replace(self.record,title='My Event on Monday, October 12, 2026'),lambda:self.fail('No request allowed'))
        self.assertEqual(status,'excluded_title')
        self.assertIsNone(self.page.evaluate('window.searches'))

    def test_search_preserves_existing_description(self):
        self.set_html(existing_description=True)
        status,_=self.client.process(self.record,lambda:self.fail('No request allowed'))
        self.assertEqual(status,'existing_description')
        self.assertIsNone(self.page.evaluate('window.requests'))
