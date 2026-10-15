"""Make the source package available to unittest discovery."""

from pathlib import Path
import os
import sys

source = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, source)
os.environ["PYTHONPATH"] = os.pathsep.join([source, os.environ.get("PYTHONPATH", "")])
