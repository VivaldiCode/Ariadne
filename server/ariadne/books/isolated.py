"""Subprocesso de extração: `python -I -m ariadne.books.isolated check|extract FICHEIRO`."""

import sys

from ariadne.books.extract import child_main

if __name__ == "__main__":
    raise SystemExit(child_main(sys.argv[1:]))
