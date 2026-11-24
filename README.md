# YuJa AI descriptions: spreadsheet-filtered title search

The default workflow processes **only the videos listed in Excel**, starting at
the **bottom of the file**. It does not scan whole user libraries or subfolders.

Before any browser search, the script skips records with existing initials,
default titles, conflicting/missing owners, or a blank **Course/Academic Unit
Association**. That column is the publication filter: **no live publication check**
is performed. Use a current report because changes after export will not be detected.

For each remaining record, the script searches **All Media** by video title,
selects the spreadsheet's node ID when available, and checks the title and owner
in Media Details. It preserves existing descriptions. Otherwise, it clicks
**Generate Description with AI**, closes the dialog, and moves on without waiting
or verifying completion. It generates descriptions, not captions or tags.

## Run (PowerShell)

The existing project `venv` already has Playwright. For a fresh installation:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
```

Run from the project folder:

```powershell
.\venv\Scripts\python.exe run.py "C:\Users\borah\Downloads\yuja caption example.xlsx"
```

Sign in yourself in Chromium using the account with **User Collections** access.
SSO/MFA remain manual. **No Enter is needed**; the script detects access automatically.
It recognizes YuJa in a frame or a new login tab, waits up to 15 minutes, and pauses
for sign-in again after session expiry. Credentials and login state are not saved.

Results appear in **Initials for DL Employee** and **Notes** on the original report
rows in `outputs/<input-name>_processed.xlsx`. The source file is preserved. Existing
all-user scan history is retained in the output when switching from the older mode.
Keep the output closed in Excel while running.

```powershell
# Preview row eligibility, bottom-to-top; no browser or file writes
.\venv\Scripts\python.exe run.py "C:\path\to\report.xlsx" --dry-run

# Limit new checks (including spreadsheet-filtered skips)
.\venv\Scripts\python.exe run.py "C:\path\to\report.xlsx" --limit 5

# Search eligible titles/read descriptions, without generating or marking Excel
.\venv\Scripts\python.exe run.py "C:\path\to\report.xlsx" --inspect --limit 5

# Choose a separate output path
.\venv\Scripts\python.exe run.py "C:\path\to\report.xlsx" --output "outputs\batch1.xlsx"
```

Do not append a trailing `\` to PowerShell commands.

## Spreadsheet rules

- Required headers: Folder Path, Title, Owner, Direct Link, Initials for DL Employee,
  Notes, **Course/Academic Unit Association**. Extra whitespace in headers is normalized.
- Read merged video groups, continuation rows, and multiline folder paths. A course
  association on any row belonging to the video qualifies it. Blank/whitespace,
  `N/A`, `None`, and `Not Available` do not qualify. A Course Channels folder path
  alone does not qualify when the association field is blank.
- Read rows bottom-to-top, including reverse worksheet order. There is no alphabetical
  regrouping by user. Search results must match the title and owner. A node ID selects
  the intended video when titles repeat; otherwise ambiguous title matches are logged
  for review. User account search is no longer necessary.
- Skip `My Event on <weekday>, <month> <day>, <year>` and `Movie on <date> at <time>`
  titles without searching or opening them, including edited/numbered suffixes and
  `[Original Copy]` prefixes.
- Preserve existing descriptions and initials. Newly checked or excluded rows get
  `JB` and an appended note. Existing notes and formatting are retained.
- Per the requested fast rule, no Generate Description with AI button means YouTube.
  Disabled buttons are handled separately as pending/unavailable.
- Successful clicks are logged as **requested; completion not verified**. The script
  does not click Save or re-open videos to verify generation.

## Resume and recovery

Keep the processed workbook and `.checkpoint.json` together. Rerun the same command.
Previous request history from both video-row and all-user/node workflows prevents
duplicate requests. Do not delete the checkpoint to switch to the faster workflow.
Old navigation/review/publication failures can be reconsidered using the new search
and spreadsheet rules; prior requested/uncertain generation is not resubmitted.

Checkpoint before each AI click; save Excel after each result. Interrupted or
uncertain clicks remain unconfirmed without resubmission. Navigation errors get
two retries; generation clicks do not. A file-save failure stops the run. Close
the output workbook or resolve the disk issue, then rerun to restore saved results.

Rows may be added/reordered and the input may be resaved/reformatted between runs.
Do not edit it during a run. A changed title/owner on a previously checked record
requires review. Older checkpoints migrate using the existing output workbook.
Use the original report as input, not an output containing the User scan results tab.

A `.lock` prevents concurrent runs using the same output. Only remove a stale lock
after confirming its process has ended. Stop any running old script with Ctrl+C
before restarting to load code changes.

Workbook I/O uses Python's standard ZIP/XML libraries and preserves source package
parts except the requested result cells and any retained audit sheet. Excel is not
required. Output files contain names, video links, and results; this project is in
OneDrive, so those files may sync according to your OneDrive settings.

## Tests

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover spreadsheet eligibility, blank associations despite course folder paths,
bottom-to-top order, title search, identity checks, no live publication requirement,
existing descriptions, default-title skips, resume across modes, and file preservation.
Browser fixtures intercept traffic locally and do not request live YuJa generation.
Locators use the official [Playwright API](https://playwright.dev/python/docs/locators).
