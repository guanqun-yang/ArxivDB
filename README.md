# ArxivDB

Titles, authors, abstracts and categories of arXiv papers submitted since January 2011, one JSON object per line in `data/`. A daily workflow adds new and revised papers from arXiv's OAI-PMH interface. The arXiv metadata is available under [CC0 1.0](https://info.arxiv.org/help/api/tou.html).

## Build the Index Once, Then Search Offline

```bash
pip install -e .
python -m arxivdb build
python -m arxivdb search "retrieval augmented generation" --years 2023- --cat cs.CL
```

`build` reads `data/` and writes the search index to `index/`. The default backend, bm25s, ranks papers with BM25, a keyword relevance score. `build --backend sqlite` writes a SQLite FTS5 index instead. FTS5 is the full-text search built into SQLite. It needs only the Python standard library. It returns only papers that contain every query word. `index/` holds one backend at a time. `search` uses the backend that `build` ran last.

To keep a SQLite FTS5 index next to the bm25s index, build it once into `index/sqlite/` through the Python API:

```bash
python -c "from arxivdb import index; index.build(backend='sqlite', index_dir='index/sqlite')"
```

The command-line `search` does not read `index/sqlite/`. Query it with `index.search(query, index_dir='index/sqlite')`.

## A Git Hook Rebuilds the Index After Each Pull

`hooks/post-merge` rebuilds the index in the background after every `git pull` that changes `data/`. Git does not enable hooks from a cloned repository by itself, because a cloned hook would run someone else's code on your machine. Enable the hook once in each clone:

```bash
git config core.hooksPath hooks
```

Git then runs `hooks/post-merge` after every pull that merges or fast-forwards. The hook takes these steps, in order:

1. It exits if the pull did not change any file in `data/`.
2. It exits if an `arxivdb build` is already running. Two bm25s builds at once would double the memory peak.
3. It rebuilds only the indexes that the clone already has. It rebuilds `index/` with the backend named in `index/meta.json`. It rebuilds `index/sqlite/` if that folder holds a `meta.json`. A clone with no index builds nothing.
4. It starts the builds one after the other in a background process, so `git pull` returns at once. The builds run with `.venv/bin/python` when that file exists, and with `python3` otherwise.
5. It writes the output of each build to `logs/<timestamp>-build/`.

In one test on a Mac with 16 GB of memory and 2.57 million papers, the bm25s build took 5.4 minutes with a peak resident memory of 5.8 GB. The SQLite build took 2.9 minutes with 45 MB. To rebuild without a pull, run the `build` commands above.

The hook has these limits:

- Git does not run it after `git pull --rebase`, or after `git fetch` followed by `git reset`.
- `build` deletes the old index before it writes the new one. Search fails until the build finishes.
- A failed build reports its error only in its log folder. It also leaves no `meta.json`, so later pulls skip that index until you run `build` by hand.
