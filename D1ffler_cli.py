"""Console entry point; use the same comparison engine as the desktop app."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from d1ffler.headless_compare import run_compare
from d1ffler.app_version import APP_VERSION


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    prog = Path(sys.executable if getattr(sys, "frozen", False) else __file__).name
    if argv and argv[0] == "compare":
        import d1ffler.diff_tool as diff_tool
        return run_compare(argv[1:], diff_tool, prog=f"{prog} compare")
    parser = argparse.ArgumentParser(prog=prog, description="D1ffler command-line comparisons")
    parser.add_argument("--version", action="version", version=f"D1ffler {APP_VERSION}")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "compare", help="Compare two files or folders; use compare --help for options")
    parser.parse_args(argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
