from __future__ import annotations

import asyncio
from pathlib import Path

from tgr.core_service import run


if __name__ == "__main__":
    asyncio.run(run(Path(__file__).resolve().parent.parent))
