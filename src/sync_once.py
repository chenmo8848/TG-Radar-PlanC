from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient

from tgr.config import load_config
from tgr.db import RadarDB
from tgr.sync_logic import scan_auto_routes, sync_dialog_folders


async def main() -> None:
    work_dir = Path(__file__).resolve().parent.parent
    config = load_config(work_dir)
    db = RadarDB(config.db_path)
    async with TelegramClient(str(config.admin_session), config.api_id, config.api_hash) as client:
        sync_report = await sync_dialog_folders(client, db)
        route_report = await scan_auto_routes(client, db)
        print(f"sync changed={sync_report.has_changes} discovered={len(sync_report.discovered)} queued={sum(route_report.queued.values())}")


if __name__ == "__main__":
    asyncio.run(main())
