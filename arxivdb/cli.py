import argparse
import json

from . import harvest, index


def print_paper(rank, paper):
    authors = ', '.join(paper['authors'][:3]) + (', et al.' if len(paper['authors']) > 3 else '')
    abstract = paper['abstract'] if len(paper['abstract']) <= 300 else paper['abstract'][:300] + '...'
    print(f"{rank}. {paper['title']}")
    print(f"   {paper['id']} | {paper['year']} | {' '.join(paper['categories'])} | score {paper['score']}")
    print(f'   {authors}')
    print(f'   {abstract}')
    print(f"   https://arxiv.org/abs/{paper['id']}")


def main():
    parser = argparse.ArgumentParser(prog='arxivdb')
    commands = parser.add_subparsers(dest='command', required=True)

    p = commands.add_parser('harvest', help='download new and changed arXiv records into data/')
    p.add_argument('--from', dest='start', help='OAI-PMH datestamp to start from (default: data/state.json)')
    p.add_argument('--resume', help='resumption token from an interrupted harvest log')

    p = commands.add_parser('build', help='build the search index in index/ from data/')
    p.add_argument('--backend', choices=['bm25s', 'sqlite'], default='bm25s',
                   help='sqlite builds SQLite FTS5 instead, which needs no third-party packages and '
                        'matches only papers that contain every query word')

    p = commands.add_parser('search', help='search titles, authors and abstracts')
    p.add_argument('query')
    p.add_argument('-n', '--limit', type=int, default=10)
    p.add_argument('--years', help='2024, 2020-2023, 2022- or -2015')
    p.add_argument('--cat', action='append', dest='categories',
                   help='category such as cs.CL, or archive such as cs; repeat to match any of several')
    p.add_argument('--json', action='store_true', help='print one full JSON record per line')

    p = commands.add_parser('get', help='print full records by arXiv ID, read from data/')
    p.add_argument('ids', nargs='+')

    args = parser.parse_args()
    if args.command == 'harvest':
        harvest.harvest(start=args.start, resume=args.resume)
    elif args.command == 'build':
        print(json.dumps(index.build(backend=args.backend)))
    elif args.command == 'search':
        results = index.search(args.query, limit=args.limit, years=args.years,
                               categories=args.categories)
        for rank, paper in enumerate(results, 1):
            if args.json:
                print(json.dumps(paper, ensure_ascii=False))
            else:
                print_paper(rank, paper)
    elif args.command == 'get':
        for paper in index.get(args.ids):
            print(json.dumps(paper, ensure_ascii=False))


if __name__ == '__main__':
    main()
