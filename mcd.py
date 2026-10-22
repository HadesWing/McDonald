"""`python mcd.py ...` 的薄封装，免去 pip install。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mcd_solver.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
