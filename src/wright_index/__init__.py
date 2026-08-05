"""wright-index — a code intelligence layer for coding agents.

Day 1 scope (L0-L2 of REPOSITORY-INTELLIGENCE.md):

    L0  walker.py     what files exist, what language, what to skip
    L1  parsers.py    raw source -> concrete syntax tree (tree-sitter)
    L2  extract/      syntax tree -> symbols (functions, classes, methods...)
        db.py         symbols -> SQLite, queryable
        indexer.py    orchestrates L0 -> L1 -> L2 -> DB
        cli.py        the `wi` command a human (or script) runs

CALL FLOW, top to bottom
------------------------
    user runs `wi index <repo>`
      -> cli.index()                      (cli.py)
        -> indexer.index_repository()     (indexer.py)   THE orchestrator
          -> walker.iter_source_files()   (walker.py)    yields one SourceFile at a time
          -> parsers.get_parser()         (parsers.py)   cached tree-sitter parser
          -> extract.EXTRACTORS[lang]()   (extract/)     Symbol list per file
          -> Database.insert_file()       (db.py)
          -> Database.insert_symbols()    (db.py)

    user runs `wi symbols <repo> --file x.py`
      -> cli.symbols()                    (cli.py)
        -> Database.search_symbols()      (db.py)        pure SQL, no parsing
"""

__version__ = "0.1.0"
