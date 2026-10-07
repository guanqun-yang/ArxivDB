import json
from pathlib import Path

import pytest

from arxivdb import harvest, index

PAGE = (Path(__file__).parent / 'fixtures' / 'oai_page.xml').read_bytes()


def snapshot(data_dir):
    return {p: p.read_bytes() for p in sorted(Path(data_dir).rglob('*.jsonl'))}


def write(records, data_dir):
    for path in harvest.append_records(records, data_dir):
        harvest.normalize(path)


def test_parse_page():
    records, token, datestamp = harvest.parse_page(PAGE)
    assert len(records) == 32
    assert token.startswith('verb%3DListRecords') and token.endswith('skip%3D1300')
    assert datestamp == '2026-10-05'
    paper = next(r for r in records if r['id'] == '1503.06914')
    assert paper['title'].startswith('A Fundamental Inequality for Lower-bounding the Error Probability')
    assert paper['authors'] == ['Takuya Kubo', 'Hiroshi Nagaoka']
    assert paper['abstract'].startswith('In the study of the capacity problem for multiple access channels')
    assert '\n' not in paper['abstract'] and '  ' not in paper['abstract']
    assert {'id': '2610.99999', 'deleted': True} in records


def test_parse_page_without_records():
    body = (b'<?xml version="1.0" encoding="UTF-8"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            b'<error code="noRecordsMatch">No records</error></OAI-PMH>')
    assert harvest.parse_page(body) == ([], None, '')


def test_shard_path(tmp_path):
    assert harvest.shard_path('2410.12345', tmp_path) == tmp_path / '2024' / '2410.1xxxx.jsonl'
    assert harvest.shard_path('1501.00001', tmp_path) == tmp_path / '2015' / '1501.0xxxx.jsonl'
    assert harvest.shard_path('1203.4567', tmp_path) == tmp_path / '2012' / '1203.xxxx.jsonl'
    assert harvest.shard_path('1012.1234', tmp_path) is None
    assert harvest.shard_path('hep-th/9901001', tmp_path) is None


def test_write_is_idempotent_sorted_and_skips_old_papers(tmp_path):
    records, _, _ = harvest.parse_page(PAGE)
    write(records, tmp_path)
    first = snapshot(tmp_path)
    write(records, tmp_path)
    assert snapshot(tmp_path) == first
    ids = [json.loads(line)['id'] for content in first.values() for line in content.splitlines()]
    assert '0811.0186' not in ids and '2610.99999' not in ids
    assert len(ids) == 30 == len(set(ids))
    for content in first.values():
        shard_ids = [json.loads(line)['id'] for line in content.splitlines()]
        assert shard_ids == sorted(shard_ids)


def test_update_replaces_and_deletion_removes(tmp_path):
    records, _, _ = harvest.parse_page(PAGE)
    write(records, tmp_path)
    write([{**records[1], 'title': 'Revised title'}, {'id': records[2]['id'], 'deleted': True}], tmp_path)
    papers = {p['id']: p for p in index.get([records[1]['id'], records[2]['id']], tmp_path)}
    assert papers[records[1]['id']]['title'] == 'Revised title'
    assert records[2]['id'] not in papers


def test_normalize_rejects_files_over_limit(tmp_path, monkeypatch):
    records, _, _ = harvest.parse_page(PAGE)
    monkeypatch.setattr(harvest, 'MAX_FILE_BYTES', 1000)
    with pytest.raises(RuntimeError, match='over the 1,000 byte limit'):
        write(records, tmp_path)


def test_get_accepts_versions_and_prefix(tmp_path):
    records, _, _ = harvest.parse_page(PAGE)
    write(records, tmp_path)
    assert [p['id'] for p in index.get(['arXiv:1503.06914v2'], tmp_path)] == ['1503.06914']


@pytest.fixture(scope='module', params=['bm25s', 'sqlite'])
def built(tmp_path_factory, request):
    data_dir = tmp_path_factory.mktemp('data')
    index_dir = tmp_path_factory.mktemp('index')
    records, _, _ = harvest.parse_page(PAGE)
    write(records, data_dir)
    harvest.write_state('2026-10-05', data_dir)
    assert index.build(backend=request.param, data_dir=data_dir, index_dir=index_dir)['papers'] == 30
    return data_dir, index_dir


def test_search_finds_paper_by_title_words(built):
    data_dir, index_dir = built
    results = index.search('lower-bounding error probability multiple access channels',
                           data_dir=data_dir, index_dir=index_dir)
    assert results[0]['id'] == '1503.06914'
    assert results[0]['authors'] == ['Takuya Kubo', 'Hiroshi Nagaoka']
    assert results[0]['year'] == 2015


def test_search_filters(built):
    data_dir, index_dir = built
    query = 'lower-bounding error probability multiple access channels'
    kwargs = dict(data_dir=data_dir, index_dir=index_dir)
    assert '1503.06914' not in [r['id'] for r in index.search(query, years='2016-', **kwargs)]
    assert '1503.06914' not in [r['id'] for r in index.search(query, categories=['hep-th'], **kwargs)]
    results = index.search(query, years='2015', categories=['cs'], **kwargs)
    assert [r['id'] for r in results][:1] == ['1503.06914']
    assert all(r['year'] == 2015 and any(c.startswith('cs.') for c in r['categories']) for r in results)


def test_search_warns_when_index_is_stale(built, capsys):
    data_dir, index_dir = built
    harvest.write_state('2026-10-06', data_dir)
    index.search('channels', data_dir=data_dir, index_dir=index_dir)
    harvest.write_state('2026-10-05', data_dir)
    assert 'run `python -m arxivdb build`' in capsys.readouterr().err
