"""Read XLSX records and patch only result cells without reserializing the workbook.

Using the standard ZIP/XML formats keeps drawings, formatting, merged cells,
extensions and unrelated package parts intact. No Excel installation is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import posixpath
import re
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEADERS = ("Folder Path", "Title", "Owner", "Direct Link", "Initials for DL Employee", "Notes")


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def video_id(link: str) -> str:
    url = urlsplit(link)
    if url.scheme != "https" or url.hostname != "farmingdale.video.yuja.com":
        return ""
    value = parse_qs(url.query).get("v", [""])[0]
    return value if value.isdigit() else ""


@dataclass(frozen=True)
class Record:
    sheet: str
    part: str
    row: int
    end_row: int
    title: str
    owner: str
    paths: tuple[str, ...]
    courses: tuple[str, ...]
    link: str
    initials: str
    notes: str
    initials_cell: str
    notes_cell: str
    collection_url: str = ''
    node: str = ''
    account_email: str = ''
    course_associations: tuple[str, ...] | None = None

    @property
    def user(self) -> str:
        names = self.collection_users
        return names[0] if len(names) == 1 else self.owner

    @property
    def collection_users(self) -> list[str]:
        return sorted({normalized(m.group(1)) for path in self.paths
                       if (m := re.search(r"(?:^|/)User Collections?/([^/]+)(?:/|$)", path, re.I))})

    @property
    def identity_issue(self) -> str:
        names = self.collection_users
        if len(names) > 1:
            return "Conflicting user collections"
        if names and self.owner and normalized(names[0]).casefold() != normalized(self.owner).casefold():
            return "Owner conflicts with User Collections path"
        return "" if self.user else "Missing user name"

    @property
    def key(self) -> str:
        if self.node:
            return 'node:' + self.node
        return "video:" + video_id(self.link) if video_id(self.link) else "record:" + hashlib.sha256(
            f"{self.sheet}|{self.row}|{self.user}|{self.title}".encode()).hexdigest()


class Workbook:
    @property
    def users(self) -> list[str]:
        """Scan bottom-to-top, keeping each user's last occurrence first."""
        names, seen = [], set()
        for record in reversed(self.records):
            for name in record.collection_users or ([record.owner] if record.owner else []):
                if name.casefold() not in seen:
                    seen.add(name.casefold())
                    names.append(name)
        return names

    @property
    def record_signatures(self) -> dict[str, str]:
        """Stable resume identity, independent of file formatting and row order."""
        signatures = {}
        for record in self.records:
            signature = json.dumps([record.title, record.user.casefold(), record.owner.casefold()], ensure_ascii=False)
            if record.key in signatures and signatures[record.key] != signature:
                raise ValueError(f'Conflicting title/owner records for {record.key}')
            signatures[record.key] = signature
        return signatures

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.parts: dict[str, bytes] = {}
        self.records: list[Record] = []
        self.updates: dict[str, dict[str, str]] = {}
        self.scan_rows: list[list[str]] | None = None
        with zipfile.ZipFile(self.path) as z:
            self.parts = {name: z.read(name) for name in z.namelist()}
        strings = []
        if "xl/sharedStrings.xml" in self.parts:
            strings = ["".join(e.itertext()) for e in ET.fromstring(self.parts["xl/sharedStrings.xml"])]
        relations = {e.attrib["Id"]: e.attrib["Target"] for e in ET.fromstring(self.parts["xl/_rels/workbook.xml.rels"])}
        book = ET.fromstring(self.parts["xl/workbook.xml"])
        for sheet in book.findall("s:sheets/s:sheet", NS):
            target = relations[sheet.attrib[f"{{{REL}}}id"]]
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            self._read_sheet(sheet.attrib["name"], part, strings)
        if not self.records:
            raise ValueError("No video records found beneath the required workbook headers.")

    def _read_sheet(self, name: str, part: str, strings: list[str]) -> None:
        root = ET.fromstring(self.parts[part])
        cells: dict[str, str] = {}
        for c in root.findall("s:sheetData/s:row/s:c", NS):
            value = c.find("s:v", NS)
            text = value.text or "" if value is not None else ""
            if c.get("t") == "s":
                text = strings[int(text)]
            elif c.get("t") == "inlineStr":
                text = "".join(c.find("s:is", NS).itertext())
            cells[c.attrib["r"]] = text
        rows: dict[int, dict[str, str]] = {}
        for address, value in cells.items():
            col, number = re.fullmatch(r"([A-Z]+)(\d+)", address).groups()
            rows.setdefault(int(number), {})[col] = value
        header_row, cols = 0, {}
        for number, values in sorted(rows.items()):
            mapping = {normalized(v): c for c, v in values.items()}
            if all(h in mapping for h in HEADERS):
                header_row, cols = number, mapping
                break
        if not cols:
            return
        merges = [e.attrib["ref"].split(":") for e in root.findall("s:mergeCells/s:mergeCell", NS)]
        def value(header: str, row: int) -> str:
            return cells.get(f"{cols[header]}{row}", "")
        title_rows = [n for n in sorted(rows) if n > header_row and value("Title", n).strip()]
        for index, start in enumerate(title_rows):
            address = f"{cols['Title']}{start}"
            merged_end = next((int(re.search(r"\d+", b)[0]) for a, b in merges if a == address), start)
            end = merged_end
            # Some exports use blank continuation rows without merging the title.
            next_start = title_rows[index + 1] if index + 1 < len(title_rows) else max(rows) + 1
            while end + 1 < next_start and value("Folder Path", end + 1).strip():
                end += 1
            paths = tuple(path.strip() for n in range(start, end + 1)
                          for path in value("Folder Path", n).splitlines() if path.strip())
            courses = {p.split("/", 1)[1] for p in paths if p.lower().startswith("course channels/")}
            associations = None
            if "Course/Academic Unit Association" in cols:
                associations = tuple(sorted({normalized(item) for n in range(start, end + 1)
                    for item in value("Course/Academic Unit Association", n).splitlines()
                    if normalized(item) and normalized(item).casefold() not in {'n/a', 'none', 'not available'}}))
                courses.update(associations)
            self.records.append(Record(name, part, start, end, normalized(value("Title", start)),
                normalized(value("Owner", start)), paths, tuple(sorted(courses)), value("Direct Link", start).strip(),
                value("Initials for DL Employee", start).strip(), value("Notes", start),
                f"{cols['Initials for DL Employee']}{start}", f"{cols['Notes']}{start}",
                course_associations=associations))

    def mark(self, record: Record, note: str) -> None:
        updates = self.updates.setdefault(record.part, {})
        old = updates.get(record.notes_cell, record.notes)
        updates[record.initials_cell] = "JB"
        updates[record.notes_cell] = old + ("\n" if old else "") + note

    @staticmethod
    def patch_cell(xml: str, address: str, text: str) -> str:
        if len(text) > 32767:
            raise ValueError(f"Notes exceed Excel cell limit at {address}")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        pattern = re.compile(r'<c\b(?=[^>]*\br="' + re.escape(address) + r'")(?:[^>]*?/>|[^>]*>.*?</c>)', re.S)
        def replacement(match):
            attributes = re.match(r"<c\b(.*?)/?>", match[0], re.S)[1]
            attributes = re.sub(r'\s+t="[^"]*"', '', attributes).rstrip("/ ")
            return f'<c{attributes} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
        if pattern.search(xml):
            return pattern.sub(replacement, xml, count=1)
        number = re.search(r"\d+", address)[0]
        row_pattern = re.compile(r'(<row\b(?=[^>]*\br="' + number + r'")[^>]*>)(.*?)(</row>)', re.S)
        def insert(match):
            cell = f'<c r="{address}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
            def colnum(ref):
                total = 0
                for letter in re.match(r"[A-Z]+", ref)[0]:
                    total = total * 26 + ord(letter) - 64
                return total
            body = match[2]
            for existing in re.finditer(r'<c\b[^>]*\br="([A-Z]+\d+)"', body):
                if colnum(existing[1]) > colnum(address):
                    return match[1] + body[:existing.start()] + cell + body[existing.start():] + match[3]
            return match[1] + body + cell + match[3]
        result, count = row_pattern.subn(insert, xml, count=1)
        if not count:
            raise ValueError(f"Cannot locate row for result cell {address}")
        return result

    def save(self, output: Path) -> None:
        output = Path(output)
        if output.resolve() == self.path.resolve():
            raise ValueError("Output must differ from the source workbook")
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(suffix=".xlsx", dir=output.parent)
        os.close(fd)
        try:
            extra = self._scan_parts() if self.scan_rows is not None else {}
            with zipfile.ZipFile(self.path) as source, zipfile.ZipFile(temporary, "w") as dest:
                for info in source.infolist():
                    data = extra.pop(info.filename, self.parts[info.filename])
                    if info.filename in self.updates:
                        xml = data.decode("utf-8")
                        for address, text in self.updates[info.filename].items():
                            xml = self.patch_cell(xml, address, text)
                        ET.fromstring(xml)  # Validate before replacing a valid output file.
                        data = xml.encode("utf-8")
                    dest.writestr(info, data)
                for name, data in extra.items():
                    dest.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
            try:
                os.replace(temporary, output)
            except PermissionError as exc:
                raise PermissionError(
                    f'Cannot replace output workbook: {output.resolve()}. '
                    'Close this OUTPUT workbook in Excel or its preview, then rerun the same command. '
                    'Keep the checkpoint file: saved requests will not be repeated. '
                    'If the workbook is already closed, check its write permissions or OneDrive file lock.'
                ) from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _scan_parts(self) -> dict[str, bytes]:
        """Add a dedicated result sheet; preserve source XML namespace declarations."""
        book = self.parts['xl/workbook.xml'].decode('utf-8')
        rels = self.parts['xl/_rels/workbook.xml.rels'].decode('utf-8')
        types = self.parts['[Content_Types].xml'].decode('utf-8')
        root = ET.fromstring(book)
        sheets = root.findall('s:sheets/s:sheet', NS)
        name = 'User scan results'
        if any(s.get('name') == name for s in sheets):
            raise ValueError('Use the original input report, not a processed report containing User scan results.')
        sheet_id = max((int(s.get('sheetId', '0')) for s in sheets), default=0) + 1
        part = f'xl/worksheets/yuja_scan_{sheet_id}.xml'
        if part in self.parts:
            raise ValueError('Result sheet part already exists in source workbook')
        ids = {e.get('Id') for e in ET.fromstring(rels)}
        rel_id = 'rIdYujaScan'
        while rel_id in ids:
            rel_id += '_'
        book = book.replace('</sheets>', f'<sheet name="{name}" sheetId="{sheet_id}" '
            f'xmlns:r="{REL}" r:id="{rel_id}"/></sheets>')
        rels = rels.replace('</Relationships>', f'<Relationship Id="{rel_id}" '
            f'Type="{REL}/worksheet" Target="worksheets/yuja_scan_{sheet_id}.xml"/></Relationships>')
        types = types.replace('</Types>', f'<Override PartName="/{part}" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        headers = ['User', 'Account email', 'Title', 'Media node', 'Media URL', 'Initials', 'Status', 'Notes', 'Checked at (UTC)']
        rows = []
        for index, values in enumerate([headers, *(self.scan_rows or [])], 1):
            cells = []
            for column, value in enumerate(values, 65):
                text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))[:32767]
                cells.append(f'<c r="{chr(column)}{index}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
            rows.append(f'<row r="{index}">' + ''.join(cells) + '</row>')
        sheet = f'<worksheet xmlns="{NS["s"]}"><sheetViews><sheetView workbookViewId="0">' \
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' \
            '<cols><col min="1" max="2" width="28" customWidth="1"/><col min="3" max="3" width="50" customWidth="1"/>' \
            '<col min="4" max="4" width="14" customWidth="1"/><col min="5" max="5" width="45" customWidth="1"/>' \
            '<col min="6" max="6" width="10" customWidth="1"/><col min="7" max="7" width="24" customWidth="1"/>' \
            '<col min="8" max="8" width="75" customWidth="1"/><col min="9" max="9" width="28" customWidth="1"/></cols>' \
            '<sheetData>' + ''.join(rows) + f'</sheetData><autoFilter ref="A1:I{len(rows)}"/></worksheet>'
        result = {'xl/workbook.xml': book.encode(), 'xl/_rels/workbook.xml.rels': rels.encode(),
                  '[Content_Types].xml': types.encode(), part: sheet.encode()}
        for value in result.values():
            ET.fromstring(value)
        return result
