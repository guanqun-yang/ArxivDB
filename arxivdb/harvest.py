import gzip
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
OAI_URL = 'https://oaipmh.arxiv.org/oai'
USER_AGENT = 'ArxivDB (+https://github.com/guanqun-yang/ArxivDB)'
MIN_YYMM = '1101'
MAX_FILE_BYTES = 50_000_000
REQUEST_GAP_SECONDS = 3
NS = {'oai': 'http://www.openarchives.org/OAI/2.0/', 'arxiv': 'http://arxiv.org/OAI/arXiv/'}
ID_RE = re.compile(r'(\d{4})\.(\d{4,5})')


def shard_path(arxiv_id, data_dir=DATA_DIR):
    m = ID_RE.fullmatch(arxiv_id)
    if not m or m[1] < MIN_YYMM:
        return None
    yymm, num = m.groups()
    block = num[0] + 'xxxx' if len(num) == 5 else 'xxxx'
    return Path(data_dir) / f'20{yymm[:2]}' / f'{yymm}.{block}.jsonl'


def all_shards(data_dir=DATA_DIR):
    return sorted(Path(data_dir).glob('20*/*.jsonl'))


def clean(text):
    return ' '.join((text or '').split())


def parse_record(record):
    header = record.find('oai:header', NS)
    arxiv_id = header.findtext('oai:identifier', '', NS).removeprefix('oai:arXiv.org:')
    if header.get('status') == 'deleted':
        return {'id': arxiv_id, 'deleted': True}
    meta = record.find('oai:metadata/arxiv:arXiv', NS)
    authors = [
        clean(' '.join(a.findtext(f'arxiv:{part}', '', NS) for part in ('forenames', 'keyname', 'suffix')))
        for a in meta.iterfind('arxiv:authors/arxiv:author', NS)
    ]
    return {
        'id': arxiv_id,
        'title': clean(meta.findtext('arxiv:title', '', NS)),
        'authors': authors,
        'abstract': clean(meta.findtext('arxiv:abstract', '', NS)),
        'categories': meta.findtext('arxiv:categories', '', NS).split(),
    }


def parse_page(body):
    root = ET.fromstring(body)
    error = root.find('oai:error', NS)
    if error is not None:
        if error.get('code') == 'noRecordsMatch':
            return [], None, ''
        raise RuntimeError(f"OAI-PMH error {error.get('code')}: {error.text}")
    records = root.findall('oai:ListRecords/oai:record', NS)
    datestamp = max((r.findtext('oai:header/oai:datestamp', '', NS) for r in records), default='')
    token = root.findtext('oai:ListRecords/oai:resumptionToken', '', NS) or None
    return [parse_record(r) for r in records], token, datestamp


def fetch(params, log, attempts=6):
    url = OAI_URL + '?' + urllib.parse.urlencode(params)
    for attempt in range(1, attempts + 1):
        start = time.time()
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
            log({'event': 'response', 'url': url, 'attempt': attempt, 'status': 200,
                 'bytes': len(body), 'seconds': round(time.time() - start, 2)})
            return body
        except (OSError, http.client.HTTPException) as e:
            status = getattr(e, 'code', None)
            retry_after = getattr(e, 'headers', None) and e.headers.get('Retry-After')
            wait = int(retry_after) if retry_after else 60 * attempt
            log({'event': 'error', 'url': url, 'attempt': attempt, 'status': status,
                 'error': str(e), 'wait_seconds': wait})
            if attempt == attempts or status not in (None, 429, 500, 502, 503, 504):
                raise
            time.sleep(wait)


def append_records(records, data_dir=DATA_DIR):
    lines = {}
    for record in records:
        path = shard_path(record['id'], data_dir)
        if path:
            lines.setdefault(path, []).append(json.dumps(record, ensure_ascii=False) + '\n')
    for path, chunk in lines.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f:
            f.writelines(chunk)
    return set(lines)


def normalize(path):
    papers = {}
    with path.open(encoding='utf-8') as f:
        for line in f:
            record = json.loads(line)
            papers[record['id']] = record
    text = ''.join(json.dumps(r, ensure_ascii=False) + '\n'
                   for _, r in sorted(papers.items()) if not r.get('deleted'))
    if len(text.encode()) > MAX_FILE_BYTES:
        raise RuntimeError(f'{path} would be {len(text.encode()):,} bytes, over the {MAX_FILE_BYTES:,} byte limit')
    if not text:
        path.unlink()
    elif text != path.read_text(encoding='utf-8'):
        path.write_text(text, encoding='utf-8')


def read_state(data_dir=DATA_DIR):
    return json.loads((Path(data_dir) / 'state.json').read_text())['datestamp']


def write_state(datestamp, data_dir=DATA_DIR):
    (Path(data_dir) / 'state.json').write_text(json.dumps({'datestamp': datestamp}) + '\n')


def harvest(start=None, resume=None, data_dir=DATA_DIR, log_dir=None):
    log_dir = Path(log_dir or ROOT / 'logs' / f"{datetime.now():%Y%m%d-%H%M%S}-harvest")
    (log_dir / 'pages').mkdir(parents=True, exist_ok=True)
    log_file = (log_dir / 'requests.jsonl').open('a', encoding='utf-8')

    def log(entry):
        log_file.write(json.dumps({'time': datetime.now().isoformat(timespec='seconds'), **entry}) + '\n')
        log_file.flush()

    if resume:
        params = {'verb': 'ListRecords', 'resumptionToken': resume}
    else:
        params = {'verb': 'ListRecords', 'metadataPrefix': 'arXiv', 'from': start or read_state(data_dir)}
    touched, latest, page, total = set(), '', 0, 0
    while True:
        started = time.time()
        body = fetch(params, log)
        (log_dir / 'pages' / f'{page:05d}.xml.gz').write_bytes(gzip.compress(body))
        records, token, datestamp = parse_page(body)
        paths = append_records(records, data_dir)
        touched |= paths
        latest = max(latest, datestamp)
        total += len(records)
        log({'event': 'page', 'page': page, 'records': len(records),
             'deleted': sum(1 for r in records if r.get('deleted')),
             'kept': sum(1 for r in records if shard_path(r['id'], data_dir)),
             'datestamp': datestamp, 'token': token})
        print(f'page {page}: {len(records)} records, datestamp {datestamp}, total {total:,}', flush=True)
        if not token:
            break
        params = {'verb': 'ListRecords', 'resumptionToken': token}
        page += 1
        time.sleep(max(0, REQUEST_GAP_SECONDS - (time.time() - started)))

    for path in all_shards(data_dir) if resume else sorted(touched):
        if path.exists():
            normalize(path)
    if latest:
        write_state(latest, data_dir)
    log({'event': 'done', 'pages': page + 1, 'records': total, 'shards': len(touched), 'datestamp': latest})
    log_file.close()
    return total
