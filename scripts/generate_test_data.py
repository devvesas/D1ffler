"""Create sample folders with intentional differences for manual testing."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1] / "test_data"
LEFT = ROOT / "left"
RIGHT = ROOT / "right"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    write(LEFT / "README.txt", "This file is the same on both sides.\n")
    write(RIGHT / "README.txt", "This file is the same on both sides.\n")
    write(LEFT / "notes.txt", "Line one\nLine two\nLine three\n")
    write(RIGHT / "notes.txt", "Line one\nLine TWO (changed)\nLine three\nA new line\n")
    write(
        LEFT / "middle_insertion.txt",
        "Shared opening line 1\nShared opening line 2\nShared closing line 1\nShared closing line 2\n",
    )
    write(
        RIGHT / "middle_insertion.txt",
        "Shared opening line 1\nShared opening line 2\n"
        "Only on the right: inserted line 1\nOnly on the right: inserted line 2\n"
        "Only on the right: inserted line 3\nShared closing line 1\nShared closing line 2\n",
    )
    write(
        LEFT / "multiple_changes.txt",
        "Shared header\n"
        "First section: unchanged\n"
        "First section: original value\n"
        "Shared spacer after first change\n"
        "Second section: unchanged\n"
        "Second section: line removed on right\n"
        "Shared spacer after second change\n"
        "Third section: unchanged\n"
        "Third section: original ending\n"
        "Shared footer\n",
    )
    write(
        RIGHT / "multiple_changes.txt",
        "Shared header\n"
        "First section: unchanged\n"
        "First section: updated value\n"
        "Shared spacer after first change\n"
        "Second section: unchanged\n"
        "Shared spacer after second change\n"
        "Third section: unchanged\n"
        "Third section: updated ending\n"
        "Third section: additional detail\n"
        "Shared footer\n",
    )
    write(LEFT / "only_left.txt", "This file exists only on the left.\n")
    write(RIGHT / "only_right.txt", "This file exists only on the right.\n")
    write(LEFT / "nested" / "config.ini", "[app]\nmode=development\nretry=3\n")
    write(RIGHT / "nested" / "config.ini", "[app]\nmode=production\nretry=5\n")
    write(LEFT / "nested" / "same.txt", "Nested identical file.\n")
    write(RIGHT / "nested" / "same.txt", "Nested identical file.\n")
    print(f"Created test folders:\n  {LEFT}\n  {RIGHT}")


if __name__ == "__main__":
    main()
