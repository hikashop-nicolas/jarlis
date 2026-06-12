"""Console entry point for the `jarlis` command.

`jarlis <command> [args...]` dispatches to the matching subcommand module, so
`jarlis setup --lang fr` behaves exactly like `python -m jarlis.setup --lang fr`.
With no command (or -h/--help) it prints the list of subcommands.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable

# command -> (module, entry-point function name)
_COMMANDS: dict[str, tuple[str, str]] = {
    "setup": ("jarlis.setup", "main"),
    "bootstrap": ("jarlis.bootstrap", "_cli_main"),
    "pipeline": ("jarlis.pipeline", "_cli_main"),
    "recap": ("jarlis.recap", "_cli_main"),
    "cleanup": ("jarlis.cleanup", "_cli_main"),
    "scheduler": ("jarlis.scheduler", "_cli_main"),
    "uninstall": ("jarlis.uninstall", "main"),
    "config": ("jarlis.config", "_cli_main"),
}


def _print_usage(stream=sys.stdout) -> None:
    stream.write("usage: jarlis <command> [args...]\n\ncommands:\n")
    for name in _COMMANDS:
        stream.write(f"  {name}\n")
    stream.write("\nRun 'jarlis <command> --help' for command-specific options.\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _print_usage()
        return 0

    command, rest = argv[0], argv[1:]
    entry = _COMMANDS.get(command)
    if entry is None:
        sys.stderr.write(f"jarlis: unknown command '{command}'\n\n")
        _print_usage(sys.stderr)
        return 2

    module_name, func_name = entry
    func: Callable[[list[str]], int] = getattr(importlib.import_module(module_name), func_name)
    return func(rest)


if __name__ == "__main__":
    sys.exit(main())
