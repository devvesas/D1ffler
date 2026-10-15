"""Launch the D1ffler desktop app or headless comparison command."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from d1ffler.diff_tool import main


if __name__ == "__main__":
    main()
