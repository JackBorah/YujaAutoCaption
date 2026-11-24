import re
from .workbook import Record, normalized

EVENT = re.compile(r"^My Event on (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
                   r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
                   r"\s+\d{1,2},?\s+\d{4}(?=$|\W|_)", re.I)
MOVIE = re.compile(r"^Movie on \d{1,2}[-/]\d{1,2}[-/]\d{2,4} at \d{1,2}[.:]\d{2}"
                   r"(?:[.:]\d{2})?\s*[AP]M(?=$|\W|_)", re.I)


def default_title(title: str) -> bool:
    title = normalized(title)
    title = re.sub(r'^(?:\[Original Copy\]\s*)+', '', title, flags=re.I)
    return bool(EVENT.match(title) or MOVIE.match(title))


def precheck(record: Record) -> tuple[str, str] | None:
    if record.initials:
        return "untouched", "Existing initials preserved"
    if record.identity_issue:
        return "review", record.identity_issue
    if default_title(record.title):
        return 'excluded_title', 'Skipped: default title; video not opened'
    if record.course_associations is None:
        return 'review', 'Missing Course/Academic Unit Association column'
    if not record.course_associations:
        return "unpublished", "Skipped: Course/Academic Unit Association is blank"
    return None
