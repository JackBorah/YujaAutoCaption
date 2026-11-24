from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_workbook import fixture
from yuja_descriptions.workbook import Workbook
from yuja_descriptions.checkpoint import Checkpoint
from yuja_descriptions.runner import run_records


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.input = Path(temp.name)/'in.xlsx'
        self.output = Path(temp.name)/'out.xlsx'
        fixture(self.input)
        self.book = Workbook(self.input)
        self.state = Checkpoint(self.output.with_suffix('.json'), self.book.digest)

    def test_pending_request_is_inspected_not_resubmitted(self):
        self.state.put('video:123','requesting','pending')
        client = Mock()
        client.recover.return_value = ('generated','Verified saved description after interruption')
        run_records(self.book,self.output,self.state,client)
        client.process.assert_not_called()
        client.recover.assert_called_once()
        self.assertEqual(Workbook(self.output).records[0].initials,'JB')

    def test_completed_checkpoint_replays_without_browser(self):
        self.state.put('video:123','youtube','YouTube: AI description unavailable')
        client = Mock()
        run_records(self.book,self.output,self.state,client)
        client.process.assert_not_called()
        self.assertIn('YouTube',Workbook(self.output).records[0].notes)

    def test_rows_run_bottom_to_top(self):
        from dataclasses import replace
        first = self.book.records[0]
        self.book.records = [first, replace(first,row=20,title='Last video',link='https://farmingdale.video.yuja.com/V/Video?v=999')]
        client=Mock()
        client.process.return_value=('requested','Requested')
        run_records(self.book,self.output,self.state,client)
        self.assertEqual([c.args[0].title for c in client.process.call_args_list],['Last video',first.title])

    def test_user_scan_checkpoint_prevents_repeat_after_switch(self):
        from dataclasses import replace
        record=replace(self.book.records[0],link='https://farmingdale.video.yuja.com/V/Video?v=123&node=456')
        self.book.records=[record]
        self.state.put(record.key,'review','Old ambiguous match')
        self.state.put('node:456','requested','Already requested during user scan')
        client=Mock()
        run_records(self.book,self.output,self.state,client)
        client.process.assert_not_called()
        self.assertIn('Already requested',Workbook(self.output).records[0].notes)

    def test_cannot_write_output_prevents_remote_action(self):
        client = Mock()
        self.book.save = Mock(side_effect=PermissionError('Workbook open in Excel'))
        with self.assertRaises(PermissionError):
            run_records(self.book,self.output,self.state,client)
        client.process.assert_not_called()

    def test_write_ahead_checkpoint_survives_post_action_save_failure(self):
        client = Mock()
        def process(record, before_request):
            before_request()
            self.assertEqual(self.state.get(record.key)['status'],'requesting')
            return 'generated','AI description generated and verified'
        client.process.side_effect = process
        self.book.save = Mock(side_effect=[None,PermissionError('locked')])
        with self.assertRaises(PermissionError):
            run_records(self.book,self.output,self.state,client)
        self.assertEqual(self.state.get('video:123')['status'],'generated')


if __name__ == '__main__':
    unittest.main()
