"""Module entry point for ``python -m athena_backup``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

