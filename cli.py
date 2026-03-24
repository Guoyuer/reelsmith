"""CLI entry point (thin wrapper — real code lives in pipeline.cli)."""

# Re-export everything so ``from cli import X`` keeps working.
from pipeline.cli import *  # noqa: F401,F403
from pipeline.cli import cli  # explicit for type checkers

if __name__ == "__main__":
    try:
        from rich.traceback import install as _install_traceback

        import click

        _install_traceback(show_locals=False, width=120, suppress=[click])
    except ImportError:
        pass
    cli()
