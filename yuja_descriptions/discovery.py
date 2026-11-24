"""Enumerate every matching account and its media through the visible YuJa UI."""
from dataclasses import dataclass
import logging
import re
from playwright.sync_api import TimeoutError, expect
from .workbook import Record, normalized

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    url: str


def search_rows(client, name):
    client.close_details()
    client._ensure_session()
    client.root.get_by_role('button', name='User Collections', exact=True).click()
    dialog = client.root.get_by_role('dialog', name='User Collections', exact=True)
    dialog.get_by_role('textbox', name='Search Users', exact=True).fill(name)
    dialog.get_by_role('button', name='Search', exact=True).click()
    rows = dialog.locator('.user-table-row')
    # Wait for the requested name rather than consuming the old unfiltered list.
    try:
        expect(rows.filter(has_text=re.compile(re.escape(name.split()[0]), re.I)).first).to_be_visible(timeout=20000)
    except AssertionError:
        if rows.count() == 0:
            return dialog, []
        raise
    while dialog.get_by_role('button', name='Load More', exact=True).is_visible():
        count = rows.count()
        dialog.get_by_role('button', name='Load More', exact=True).click()
        client.root.wait_for_function('(n) => document.querySelectorAll(".user-table-row").length > n', arg=count)
    matches = []
    for index, row in enumerate(rows.all()):
        cells = row.locator('.user-table-body-cell').all_text_contents()
        if len(cells) >= 3 and normalized(' '.join(cells[:2])).casefold() == normalized(name).casefold():
            matches.append((index, normalized(cells[2])))
    return dialog, matches


def accounts_for(client, name):
    dialog, matches = search_rows(client, name)
    if not matches:
        client.close_details()
        return []
    count = len(matches)
    accounts = []
    for position in range(count):
        if position:
            dialog, matches = search_rows(client, name)
        if len(matches) != count:
            raise RuntimeError(f'User results changed while resolving {name}; run again')
        index, email = matches[position]
        dialog.locator('.user-table-row').nth(index).click()
        expect(dialog).not_to_be_visible()
        client.root.wait_for_url(re.compile(r'/Users/[^/]+/MyMediaCollections(?:[?#].*)?$'))
        url = client.root.url.split('?', 1)[0].split('#', 1)[0]
        if url not in {a.url for a in accounts}:
            accounts.append(Account(name, email, url))
    LOG.info('%s: found %s matching account(s)', name, len(accounts))
    return accounts


ENTRY_SELECTOR = '[role="group"][aria-label$=" Video"], [role="group"][aria-label$=" Folder"], [role="group"][aria-label$=" folder"]'
READ_ENTRIES = '''els => els.map(e => {
    const label = e.getAttribute('aria-label') || '';
    const link = e.querySelector('[role="link"][aria-label]');
    const title = link?.getAttribute('aria-label') || e.querySelector('.titleText')?.textContent || label.replace(/ (Video|Folder)$/i, '');
    return {id:e.id, label, title, href:link?.getAttribute('href') || '',
            folder:/folder$/i.test(label), video:/ video$/i.test(label)};
})'''


def collect_folder(client):
    """Collect rendered entries while scrolling and exhausting Load More/Next."""
    page = client.page
    page.get_by_role('main').wait_for(state='visible')
    list_view = page.get_by_role('button', name=re.compile(r'^list View enabled', re.I))
    if list_view.is_visible():
        list_view.click()
    # Allow an asynchronous initial media response; an explicit empty view also ends this wait.
    page.wait_for_function('''selector => document.querySelector(selector) ||
        /no (?:media|files|videos|results)|folder is empty|no items|drag.*drop|upload.*first/i.test(
        document.querySelector('main, [role="main"]')?.innerText || '')''', arg=ENTRY_SELECTOR, timeout=30000)
    found = {}
    quiet = 0
    for _ in range(2000):
        entries = page.locator(ENTRY_SELECTOR).evaluate_all(READ_ENTRIES)
        for entry in entries:
            found[entry['id']] = entry
        more = page.get_by_role('button', name=re.compile(r'^(Load More|Show More|Next(?: Page)?)$', re.I))
        visible = [b for b in more.all() if b.is_visible() and b.is_enabled() and b.get_attribute('aria-disabled') != 'true']
        if visible:
            before = [e['id'] for e in entries]
            visible[0].click()
            page.wait_for_function('''({selector,before}) => JSON.stringify([...document.querySelectorAll(selector)].map(e=>e.id)) !== JSON.stringify(before)''',
                                   arg={'selector': ENTRY_SELECTOR, 'before': before}, timeout=20000)
            quiet = 0
            continue
        # Scroll the actual scrolling surfaces, including a nested library scroller.
        moved = page.evaluate('''() => {
            const main = document.querySelector('main, [role="main"]');
            const candidates = [document.scrollingElement, ...main.querySelectorAll('*')];
            let p=main; while(p){candidates.push(p);p=p.parentElement;}
            let moved=false;
            for(const e of new Set(candidates)) {
                if(!e || e.clientHeight===0 || e.scrollHeight<=e.clientHeight+2) continue;
                if(e!==document.scrollingElement && !/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
                const old=e.scrollTop; e.scrollTop=Math.min(e.scrollTop+Math.max(300,e.clientHeight*0.8),e.scrollHeight);
                if(e.scrollTop>old+1)moved=true;
            } return moved;
        }''')
        before = [e['id'] for e in entries]
        try:
            page.wait_for_function('''({selector,before}) => JSON.stringify([...document.querySelectorAll(selector)].map(e=>e.id)) !== JSON.stringify(before)''',
                                   arg={'selector': ENTRY_SELECTOR, 'before': before}, timeout=300 if moved else 2000)
            quiet = 0
        except TimeoutError:
            quiet = 0 if moved else quiet + 1
            if quiet >= 2:
                return list(found.values())
    raise RuntimeError('Library pagination did not terminate; scan is incomplete')


def discover_videos(client, account):
    """Breadth-first traversal; yield each video node once, including subfolders."""
    pending = [account.url]
    folders_seen, videos_seen = set(), set()
    while pending:
        url = pending.pop(0)
        if url in folders_seen:
            continue
        folders_seen.add(url)
        client.page.goto(url, wait_until='domcontentloaded')
        client.root = client.page.main_frame
        client._ensure_session()
        entries = collect_folder(client)
        LOG.info('%s <%s>: %s entries in %s', account.name, account.email, len(entries), url)
        # Resolve folder destinations before yielding videos (processing navigates away).
        for entry in entries:
            if not entry['folder']:
                continue
            client.page.goto(url, wait_until='domcontentloaded')
            collect_folder(client)
            folder = client.page.locator('[id="' + entry['id'] + '"]')
            if not folder.count():
                raise RuntimeError('Folder disappeared during scan: ' + entry['title'])
            folder.dblclick()
            client.page.wait_for_function('(old) => location.href !== old', arg=url)
            pending.append(client.page.url)
        for entry in entries:
            if not entry['video']:
                continue
            match = re.search(r'-(\d+)$', entry['id'])
            node = match[1] if match else ''
            if not node.isdigit() or node in videos_seen:
                continue
            videos_seen.add(node)
            yield Record('', '', 0, 0, normalized(entry['title']), account.name,
                         (), (), account.url + '/WatchVideo/' + node, '', '', '', '',
                         account.url, node, account.email)
