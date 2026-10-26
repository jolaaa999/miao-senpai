"""仓库根目录的干跑入口。

为什么需要这个薄壳：项目有两种 import 约定——
测试用 `dl_senpai.xxx`（src/plugins 在 path 上），插件加载用 `plugins.dl_senpai`。
脚本自己把两条路都铺好，省得每次手动设 PYTHONPATH。

同时必须 chdir 到仓库根：配置用 `env_file=(".env", ".env.dev")`，
那是相对**当前工作目录**解析的，在别的目录跑会静默用上默认值，
看起来像「配置没生效」，很难查。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from plugins.dl_senpai.dryrun import main  # noqa: E402 — 必须在 sys.path 调整之后

if __name__ == "__main__":
    raise SystemExit(main())
