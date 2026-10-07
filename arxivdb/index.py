import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from .harvest import DATA_DIR, ROOT, all_shards, read_state, shard_path

INDEX_DIR = ROOT / 'index'
FIELDS = ('id', 'year', 'title', 'authors', 'abstract', 'categories')


def iter_rows(data_dir):
    rowid = 0
    for shard in all_shards(data_dir):
        with shard.open(encoding='utf-8') as f:
            for line in f:
                p = json.loads(line)
                yield (rowid, p['id'], 2000 + int(p['id'][:2]), p['title'],
                       json.dumps(p['authors'], ensure_ascii=False), p['abstract'],
                       json.dumps(p['categories']))
                rowid += 1


def build_sqlite(data_dir, path, fts):
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE papers (rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, year INTEGER,
                             title TEXT, authors TEXT, abstract TEXT, categories TEXT);
        CREATE TABLE categories (category TEXT, paper INTEGER);
    """)
    db.executemany('INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)', iter_rows(data_dir))
    db.executescript("""
        INSERT INTO categories SELECT j.value, p.rowid FROM papers p, json_each(p.categories) j;
        CREATE INDEX categories_category ON categories (category, paper);
        CREATE INDEX papers_year ON papers (year);
    """)
    if fts:
        db.executescript("""
            CREATE VIRTUAL TABLE papers_fts USING fts5(title, authors, abstract, content='papers',
                                                       content_rowid='rowid', tokenize='porter unicode61');
            INSERT INTO papers_fts (papers_fts) VALUES ('rebuild');
        """)
    db.commit()
    return db


def build_bm25s(db, out_dir):
    import bm25s
    import Stemmer
    tokenizer = bm25s.tokenization.Tokenizer(stopwords='en', stemmer=Stemmer.Stemmer('english'))
    count = db.execute('SELECT count(*) FROM papers').fetchone()[0]
    # Three copies of the title lifted the mean reciprocal rank of 24 exact-title queries
    # for well-known papers from 0.50 (one copy) to 0.64.
    texts = (f'{t} {t} {t} {a} {b}' for t, a, b in
             db.execute('SELECT title, authors, abstract FROM papers ORDER BY rowid'))
    ids = tokenizer.tokenize(texts, length=count, update_vocab=True, show_progress=False)
    retriever = bm25s.BM25()
    retriever.index((ids, tokenizer.get_vocab_dict()), show_progress=False)
    retriever.save(out_dir)


def build(backend='bm25s', data_dir=DATA_DIR, index_dir=INDEX_DIR):
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / 'meta.json').unlink(missing_ok=True)
    shutil.rmtree(index_dir / 'bm25s', ignore_errors=True)
    db = build_sqlite(data_dir, index_dir / 'arxiv.sqlite', fts=backend == 'sqlite')
    if backend == 'bm25s':
        build_bm25s(db, index_dir / 'bm25s')
    count = db.execute('SELECT count(*) FROM papers').fetchone()[0]
    meta = {'datestamp': read_state(data_dir), 'papers': count, 'backend': backend}
    (index_dir / 'meta.json').write_text(json.dumps(meta) + '\n')
    return meta


def parse_years(spec):
    lo, _, hi = spec.partition('-') if '-' in spec else (spec, '', spec)
    return int(lo or 0), int(hi or 9999)


def category_condition(column, categories):
    # '/' follows '.' in ASCII, so the range holds exactly the categories that start with c + '.'
    sql = ' OR '.join([f'{column} = ? OR ({column} > ? AND {column} < ?)'] * len(categories))
    return sql, [v for c in categories for v in (c, c + '.', c + '/')]


def search_bm25s(db, query, limit, years, categories, index_dir):
    import bm25s
    import numpy as np
    import Stemmer
    retriever = bm25s.BM25.load(index_dir / 'bm25s', mmap=True)
    query_tokens = bm25s.tokenize(query, stopwords='en', stemmer=Stemmer.Stemmer('english'),
                                  return_ids=False, show_progress=False)
    # One query per filter: SQLite plans the combined query badly (8.6 s against 1.1 s).
    filters = []
    if years:
        filters.append(('SELECT rowid FROM papers WHERE year BETWEEN ? AND ?', parse_years(years)))
    if categories:
        condition, params = category_condition('category', categories)
        filters.append((f'SELECT paper FROM categories WHERE {condition}', params))
    mask = None
    for sql, params in filters:
        keep = np.zeros(retriever.scores['num_docs'], dtype=np.float32)
        keep[np.fromiter((r for r, in db.execute(sql, params)), dtype=np.int64)] = 1
        mask = keep if mask is None else mask * keep
    k = min(limit, retriever.scores['num_docs'])
    docs, scores = retriever.retrieve(query_tokens, k=k, weight_mask=mask, show_progress=False)
    return [(int(d), float(s)) for d, s in zip(docs[0], scores[0]) if s > 0]


def search_sqlite(db, query, limit, years, categories):
    words = re.findall(r'\w+', query)
    if not words:
        return []
    conditions, params = [], ['"' + '" "'.join(words) + '"']
    if years:
        conditions.append('p.year BETWEEN ? AND ?')
        params += parse_years(years)
    if categories:
        condition, category_params = category_condition('value', categories)
        conditions.append(f'EXISTS (SELECT 1 FROM json_each(p.categories) WHERE {condition})')
        params += category_params
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    # Filtering after the match: a rowid filter inside the FTS5 query took 16 to 132 s.
    rows = db.execute(f"""
        WITH hits AS MATERIALIZED (
            SELECT rowid, bm25(papers_fts) AS score FROM papers_fts WHERE papers_fts MATCH ?)
        SELECT hits.rowid, -hits.score FROM hits JOIN papers p ON p.rowid = hits.rowid
        {where} ORDER BY hits.score LIMIT ?
    """, [*params, limit])
    return rows.fetchall()


def search(query, limit=10, years=None, categories=None, data_dir=DATA_DIR, index_dir=INDEX_DIR):
    index_dir = Path(index_dir)
    meta = json.loads((index_dir / 'meta.json').read_text())
    backend = meta['backend']
    if meta['datestamp'] != read_state(data_dir):
        print(f"warning: index is from data of {meta['datestamp']}, data is now "
              f"{read_state(data_dir)}; run `python -m arxivdb build`", file=sys.stderr)
    db = sqlite3.connect(f"file:{index_dir / 'arxiv.sqlite'}?mode=ro", uri=True)
    if backend == 'bm25s':
        hits = search_bm25s(db, query, limit, years, categories, index_dir)
    else:
        hits = search_sqlite(db, query, limit, years, categories)
    results = []
    for rowid, score in hits:
        row = db.execute(f"SELECT {', '.join(FIELDS)} FROM papers WHERE rowid = ?", (rowid,)).fetchone()
        paper = dict(zip(FIELDS, row))
        paper['authors'] = json.loads(paper['authors'])
        paper['categories'] = json.loads(paper['categories'])
        results.append({**paper, 'score': round(score, 3)})
    return results


def get(arxiv_ids, data_dir=DATA_DIR):
    wanted = {re.sub(r'v\d+$', '', i.removeprefix('arXiv:')) for i in arxiv_ids}
    papers = []
    for path in sorted({shard_path(i, data_dir) for i in wanted} - {None}):
        if path.exists():
            with path.open(encoding='utf-8') as f:
                papers += [p for p in map(json.loads, f) if p['id'] in wanted]
    return papers
