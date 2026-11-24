from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_workbook import fixture
from yuja_descriptions.workbook import Workbook
from yuja_descriptions.checkpoint import Checkpoint
from yuja_descriptions.discovery import Account
from yuja_descriptions.user_scan import run_users


class UserScanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'input.xlsx'
        fixture(self.path)
        self.book = Workbook(self.path)
        self.output = self.path.with_name('output.xlsx')
        self.state = Checkpoint(self.output.with_suffix('.json'), self.book.digest)
        self.client = Mock()
        self.client.page.is_closed.return_value = False
        self.client.process.return_value = ('requested', 'AI description requested; completion not verified')
        self.accounts = [Account('Test User', 'first@example.edu', 'https://example.edu/a'),
                         Account('Test User', 'second@example.edu', 'https://example.edu/b')]

    def video(self, account, node, title='New video absent from Excel'):
        return replace(self.book.records[0], title=title, node=node, collection_url=account.url,
                       account_email=account.email, link=account.url+'/WatchVideo/'+node,
                       courses=(), paths=(), row=0, end_row=0)

    def test_all_duplicate_accounts_and_unlisted_videos_processed(self):
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts), \
             patch('yuja_descriptions.user_scan.discover_videos', side_effect=lambda c,a: iter([self.video(a, '1' if a==self.accounts[0] else '2')])):
            counts = run_users(self.book,self.output,self.state,self.client)
        self.assertEqual(self.client.process.call_count,2)
        self.assertEqual(counts['requested'],2)
        self.assertEqual(len(self.book.scan_rows),2)
        self.assertEqual({r[1] for r in self.book.scan_rows},{'first@example.edu','second@example.edu'})

    def test_existing_node_checkpoint_not_resubmitted(self):
        self.state.put('node:1','requested','Requested earlier')
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts[:1]), \
             patch('yuja_descriptions.user_scan.discover_videos', return_value=iter([self.video(self.accounts[0],'1')])):
            run_users(self.book,self.output,self.state,self.client)
        self.client.process.assert_not_called()

    def test_default_title_skipped_without_opening(self):
        video = self.video(self.accounts[0],'1','My Event on Monday, October 12, 2026_edited')
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts[:1]), \
             patch('yuja_descriptions.user_scan.discover_videos', return_value=iter([video])):
            counts = run_users(self.book,self.output,self.state,self.client)
        self.client.process.assert_not_called()
        self.client.inspect.assert_not_called()
        self.assertEqual(counts['excluded_title'],1)
        self.assertEqual(self.book.scan_rows[0][5],'JB')

    def test_old_default_title_exclusion_stays_skipped(self):
        self.state.put('node:1','excluded_title','Skipped: default video title')
        video = self.video(self.accounts[0],'1','Movie on 9-11-26 at 10.55 PM')
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts[:1]), \
             patch('yuja_descriptions.user_scan.discover_videos', return_value=iter([video])):
            run_users(self.book,self.output,self.state,self.client)
        self.client.process.assert_not_called()
        self.assertEqual(self.state.get('node:1')['status'],'excluded_title')

    def test_default_title_not_opened_during_live_inspection(self):
        video = self.video(self.accounts[0],'1','My Event on Monday, October 12, 2026')
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts[:1]), \
             patch('yuja_descriptions.user_scan.discover_videos', return_value=iter([video])):
            run_users(self.book,self.output,self.state,self.client,inspect_only=True)
        self.client.inspect.assert_not_called()
        self.client.process.assert_not_called()

    def test_save_failure_stops_remote_processing(self):
        self.book.save = Mock(side_effect=PermissionError('locked'))
        with self.assertRaises(PermissionError):
            run_users(self.book,self.output,self.state,self.client)
        self.client.process.assert_not_called()

    def test_old_ambiguous_review_does_not_skip_account_scan(self):
        source = replace(self.book.records[0], link='https://farmingdale.video.yuja.com/V/Video?v=123&node=1')
        self.book.records = [source]
        self.state.put(source.key,'review','Two accounts found')
        with patch('yuja_descriptions.user_scan.accounts_for', return_value=self.accounts[:1]), \
             patch('yuja_descriptions.user_scan.discover_videos', return_value=iter([self.video(self.accounts[0],'1')])):
            run_users(self.book,self.output,self.state,self.client)
        self.client.process.assert_called_once()
        self.assertEqual(Workbook(self.output).records[0].initials,'JB')


if __name__ == '__main__':
    unittest.main()
