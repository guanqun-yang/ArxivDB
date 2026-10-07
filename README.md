# ArxivDB

Titles, authors, abstracts and categories of arXiv papers submitted since January 2011, one JSON object per line in `data/`. A daily workflow adds new and revised papers from arXiv's OAI-PMH interface.

```bash
pip install -e .
python -m arxivdb build
python -m arxivdb search "retrieval augmented generation" --years 2023- --cat cs.CL
```

arXiv metadata is available under [CC0 1.0](https://info.arxiv.org/help/api/tou.html).
