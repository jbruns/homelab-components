"""Subcommands. Each module here exposes `register(subparsers)`, which adds its parser and sets
`func=run` with `run(args, ctx) -> int`. Modules are discovered, so a new command is a new file.

A command works on Stacks unless its parser sets `stacks=False`. For a command on Stacks, hops picks only a Host
that runs Stacks and refuses one with `deployer: none` before the command runs.
"""

import importlib
import pkgutil


def modules() -> list:
    return [
        importlib.import_module(f"{__name__}.{m.name}")
        for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name)
        if not m.name.startswith("_")
    ]
