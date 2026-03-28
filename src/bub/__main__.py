"""Bub framework CLI bootstrap."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import typer

from bub.framework import BubFramework


def _bootstrap_framework_from_argv(framework: BubFramework, argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--workspace", "-w"}:
            if index + 1 < len(args):
                framework.workspace = Path(args[index + 1]).resolve()
                index += 2
                continue
        elif arg.startswith("--workspace="):
            framework.workspace = Path(arg.split("=", 1)[1]).resolve()
        elif arg == "--home":
            if index + 1 < len(args):
                framework.home = Path(args[index + 1]).expanduser().resolve()
                index += 2
                continue
        elif arg.startswith("--home="):
            framework.home = Path(arg.split("=", 1)[1]).expanduser().resolve()
        index += 1


def create_cli_app(argv: Sequence[str] | None = None) -> typer.Typer:
    framework = BubFramework()
    _bootstrap_framework_from_argv(framework, argv)
    framework.load_hooks()
    app = framework.create_cli_app()

    if not app.registered_commands:

        @app.command("help")
        def _help() -> None:
            typer.echo("No CLI command loaded.")

    return app


app = create_cli_app()

if __name__ == "__main__":
    app()
