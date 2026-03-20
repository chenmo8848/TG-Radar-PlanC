from __future__ import annotations

import shutil
from pathlib import Path

from telethon import TelegramClient

from tgr.config import load_config
from tgr.db import RadarDB
from tgr.logger import setup_logger


async def main() -> None:
    work_dir = Path(__file__).resolve().parent
    config = load_config(work_dir)
    logger = setup_logger("tg-radar-bootstrap", config.logs_dir / "bootstrap.log")
    RadarDB(config.db_path)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    temp_session = config.sessions_dir / "tg_radar_bootstrap"

    async with TelegramClient(str(temp_session), config.api_id, config.api_hash) as client:
        await client.start()
        me = await client.get_me()
        logger.info("authorized as %s", getattr(me, 'username', None) or getattr(me, 'first_name', 'unknown'))

    source = temp_session.with_suffix('.session')
    for target in [config.admin_session.with_suffix('.session'), config.core_session.with_suffix('.session')]:
        shutil.copy2(source, target)
    print("\nSession bootstrap completed.")
    print(f"- {config.admin_session.with_suffix('.session')}")
    print(f"- {config.core_session.with_suffix('.session')}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
