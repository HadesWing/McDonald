"""云端演示服务入口。

部署沙箱只给一个端口（`$PORT`）并要求绑 `0.0.0.0`，所以单独给一个入口文件，
免得部署脚本还要猜 `PYTHONPATH=src python -m mcd_solver.cli serve` 怎么拼。

本地也照样能跑：

    PORT=8090 python app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from mcd_solver.serve import serve  # noqa: E402  （要先插 path 再导入）


if __name__ == "__main__":
    serve(host=os.environ.get("APP_HOST") or "0.0.0.0", port=int(os.environ.get("PORT") or 8080))
