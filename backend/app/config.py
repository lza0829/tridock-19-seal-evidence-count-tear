from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "Tridock"
API_PORT = 10900
_default_data = Path(__file__).resolve().parents[2] / "data"
DATA_DIR = Path(os.getenv("TRIDOCK_DATA") or _default_data)
DB_PATH = DATA_DIR / "tridock.db"

# open 对账批的有效期。超过该时长后，结案必须重新跑三联对账。
BATCH_TTL_SECONDS = int(os.getenv("TRIDOCK_BATCH_TTL_SECONDS", "1800"))

# 结案证据来源：只允许绑定对账批，不允许无批直封。
SEALED_BY_BATCH = "batch"
