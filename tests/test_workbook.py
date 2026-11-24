from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from yuja_descriptions.workbook import Workbook, NS, video_id
from yuja_descriptions.policy import default_title, precheck
from yuja_descriptions.checkpoint import Checkpoint, RunLock


def fixture(path):
    headers = {'A':'Folder Path', 'B':'Title', 'C':'Owner', 'O':'Course/Academic Unit Association', 'R':'Direct Link', 'S':'Initials for DL Employee', 'T':'Notes'}
    def cell(address, value):
        from xml.sax.saxutils import escape
        return f'<c r="{address}" s="1" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    rows = '<row r="5">' + ''.join(cell(c+'5', v) for c,v in headers.items()) + '</row>'
    rows += '<row r="6">' + ''.join(cell(c+'6', v) for c,v in {
        'A':'Course Channels/Test Course', 'B':'У лукоморья дуб зелёный', 'C':'Test User',
        'O':'Test Course', 'R':'https://farmingdale.video.yuja.com/V/Video?v=123&a=4', 'S':'', 'T':'Existing note'}.items()) + '</row>'
    rows += '<row r="7">' + cell('A7','User Collections/Test User/My Media Collections') + '</row>'
    sheet = f'<worksheet xmlns="{NS["s"]}" xmlns:xr="urn:test"><sheetData>{rows}</sheetData><mergeCells><mergeCell ref="B6:B7"/><mergeCell ref="S6:S7"/><mergeCell ref="T6:T7"/></mergeCells><extLst><ext uri="test"><xr:keep/></ext></extLst></worksheet>'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr('xl/workbook.xml','<workbook xmlns="'+NS['s']+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',sheet)
        z.writestr('xl/preserved.bin',b'unchanged data')


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'input.xlsx'
        fixture(self.path)
        self.book = Workbook(self.path)

    def test_merged_records_and_identity(self):
        self.assertEqual(len(self.book.records),1)
        r = self.book.records[0]
        self.assertEqual((r.row,r.end_row,r.user),(6,7,'Test User'))
        self.assertEqual(r.key,'video:123')
        self.assertIsNone(precheck(r))

    def test_users_start_at_bottom_and_deduplicate(self):
        from dataclasses import replace
        record = self.book.records[0]
        self.book.records = [replace(record, owner=name, paths=(), row=index)
                             for index, name in enumerate(['Zoe', 'Amy', 'Pat', 'AMY'], 6)]
        self.assertEqual(self.book.users, ['AMY', 'Pat', 'Zoe'])

    def test_patch_preserves_all_other_parts_and_merges(self):
        r = self.book.records[0]
        self.book.mark(r,'Generated: A & B <test>')
        out = self.path.with_name('out.xlsx')
        self.book.save(out)
        result = Workbook(out).records[0]
        self.assertEqual(result.initials,'JB')
        self.assertEqual(result.notes,'Existing note\nGenerated: A & B <test>')
        with zipfile.ZipFile(out) as z:
            for name, data in self.book.parts.items():
                if name != r.part:
                    self.assertEqual(z.read(name), data)
            xml = z.read(r.part).decode()
            self.assertIn('<xr:keep/>',xml)
            self.assertIn('ref="B6:B7"',xml)
            self.assertIn('r="S6" s="1"',xml)
        self.assertEqual(Workbook(self.path).digest, self.book.digest)

    def test_missing_cell_inserted_in_order(self):
        xml = '<worksheet><row r="6"><c r="A6"/><c r="U6"/></row></worksheet>'
        patched = Workbook.patch_cell(xml,'S6','JB')
        self.assertLess(patched.index('r="S6"'), patched.index('r="U6"'))
        ET.fromstring(patched)

    def test_scan_sheet_addition_preserves_source_sheet(self):
        self.book.scan_rows = [['Test User', 'a@example.edu', 'New video', '999', 'https://example.edu/video', 'JB', 'requested', 'Requested; not verified', 'now']]
        output = self.path.with_name('output.xlsx')
        self.book.save(output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('xl/worksheets/sheet1.xml'), self.book.parts['xl/worksheets/sheet1.xml'])
            root = ET.fromstring(archive.read('xl/workbook.xml'))
            sheets = root.findall('s:sheets/s:sheet', NS)
            self.assertEqual(sheets[-1].get('name'), 'User scan results')
            scan = next(n for n in archive.namelist() if '/yuja_scan_' in n)
            xml = archive.read(scan).decode()
            self.assertIn('New video', xml)
            self.assertIn('JB', xml)
            self.assertIn('autoFilter', xml)
        self.assertEqual(len(Workbook(output).records), 1)

    def test_source_cannot_be_overwritten(self):
        with self.assertRaises(ValueError):
            self.book.save(self.path)

    def test_locked_output_has_actionable_error_and_is_preserved(self):
        from unittest.mock import patch
        out = self.path.with_name('out.xlsx')
        self.book.save(out)
        before = out.read_bytes()
        self.book.mark(self.book.records[0], 'Requested')
        with patch('yuja_descriptions.workbook.os.replace', side_effect=PermissionError('Access denied')):
            with self.assertRaisesRegex(PermissionError, 'Close this OUTPUT workbook'):
                self.book.save(out)
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in out.parent.glob('*.xlsx')), ['input.xlsx', 'out.xlsx'])

    def test_policy(self):
        from dataclasses import replace
        r = self.book.records[0]
        for title in ['My Event on Monday, October 12, 2026', 'My Event on Tuesday, November 17, 2026_edited',
                      'Movie on 9-11-26 at 10.55\u202fPM', 'Movie on 9-11-26 at 10.55 PM (2)']:
            self.assertTrue(default_title(title),title)
        self.assertTrue(default_title('[Original Copy]My Event on Monday, October 12, 2026'))
        self.assertFalse(default_title('Movie on Italian politics'))
        self.assertFalse(default_title('My Event on historical timelines'))
        self.assertEqual(precheck(replace(r,initials='AB'))[0],'untouched')
        self.assertEqual(precheck(replace(r,course_associations=()))[0],'unpublished')
        self.assertEqual(precheck(replace(r,owner='Other Person'))[0],'review')

    def test_video_id_rejects_other_hosts(self):
        self.assertEqual(video_id('https://evil.example/V/Video?v=123'),'')

    def test_checkpoint_recovery_and_input_guard(self):
        path = self.path.with_suffix('.json')
        checkpoint = Checkpoint(path,self.book.digest)
        checkpoint.put('video:123','requesting','Request may be in flight')
        self.assertEqual(Checkpoint(path,self.book.digest).get('video:123')['status'],'requesting')
        with self.assertRaises(ValueError):
            Checkpoint(path,'changed')

    def test_resume_after_resave_and_added_videos(self):
        path = self.path.with_suffix('.json')
        records = self.book.record_signatures
        state = Checkpoint(path, self.book.digest, records)
        state.put('video:123', 'requested', 'Requested once')
        new_records = {**records, 'video:456': '["New title", "other user", "other user"]'}
        resumed = Checkpoint(path, 'resaved bytes', new_records)
        self.assertEqual(resumed.get('video:123')['status'], 'requested')
        self.assertIsNone(resumed.get('video:456'))
        resumed.save()
        self.assertEqual(Checkpoint(path, 'resaved again', new_records).get('video:123')['note'], 'Requested once')

    def test_legacy_checkpoint_migrates_using_processed_records(self):
        path = self.path.with_suffix('.json')
        state = Checkpoint(path, self.book.digest)
        state.put('video:123', 'requested', 'Requested once')
        records = self.book.record_signatures
        resumed = Checkpoint(path, 'new bytes', {**records, 'video:456': 'new'}, records)
        self.assertEqual(resumed.get('video:123')['note'], 'Requested once')
        self.assertIn('video:456', resumed.data['records'])

    def test_changed_checked_identity_is_rejected_without_losing_checkpoint(self):
        path = self.path.with_suffix('.json')
        state = Checkpoint(path, self.book.digest, self.book.record_signatures)
        state.put('video:123', 'requested', 'Requested once')
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Title or owner changed'):
            Checkpoint(path, 'new bytes', {'video:123': 'different owner'})
        self.assertEqual(path.read_bytes(), before)

    def test_multiline_folder_paths(self):
        with zipfile.ZipFile(self.path) as archive:
            parts = {n: archive.read(n) for n in archive.namelist()}
        part = 'xl/worksheets/sheet1.xml'
        parts[part] = parts[part].replace(b'Course Channels/Test Course',
            b'Course Channels/Test Course\nUser Collections/Test User/My Media Collections\nCourse Channels/Second Course')
        with zipfile.ZipFile(self.path, 'w') as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        record = Workbook(self.path).records[0]
        self.assertEqual(record.collection_users, ['Test User'])
        self.assertEqual(record.courses, ('Second Course', 'Test Course'))

    def test_lock_excludes_concurrent_runs(self):
        path = self.path.with_suffix('.lock')
        with RunLock(path):
            with self.assertRaises(RuntimeError):
                with RunLock(path):
                    pass
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
