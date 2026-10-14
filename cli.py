"""Shepherdr's monitor and on-demand runtime commands."""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "runtime":
        from .runtime import main as runtime_main

        return runtime_main(argv[1:])
    from .bridge import main as bridge_main

    return bridge_main(argv)
