from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from telethon import TelegramClient

from tgr.compat import seed_db_from_legacy_config_if_needed
from tgr.config import load_config, sync_snapshot_to_config
from tgr.db import RadarDB
from tgr.logger import setup_logger


async def main() -> None:
    work_dir = Path(__file__).resolve().parent.parent
    config = load_config(work_dir)
    logger = setup_logger("tg-radar-bootstrap", config.logs_dir / "bootstrap.log")
    db = RadarDB(config.db_path)
    seed_db_from_legacy_config_if_needed(work_dir, db)
    sync_snapshot_to_config(work_dir, db)

    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    temp_session = config.sessions_dir / "tg_radar_bootstrap"

    print("\nTelegram 首次授权")
    print("-" * 56)
    print("接下来 Telethon 会要求输入手机号、验证码，以及二步验证密码（如果已开启）。")
    print("完成一次后，admin/core 双进程都会复用生成的 session。\n")

    async with TelegramClient(str(temp_session), config.api_id, config.api_hash) as client:
        await client.start()
        me = await client.get_me()
        display_name = getattr(me, "username", None) or getattr(me, "first_name", "unknown")
        logger.info("authorized as %s", display_name)
        print(f"已授权账号：{display_name}\n")

    source = temp_session.with_suffix(".session")
    for target in [config.admin_session.with_suffix(".session"), config.core_session.with_suffix(".session")]:
        shutil.copy2(source, target)

    for leftover in [
        source,
        temp_session.with_suffix(".session-journal"),
        temp_session.with_suffix(".session-shm"),
        temp_session.with_suffix(".session-wal"),
    ]:
        try:
            leftover.unlink(missing_ok=True)
        except Exception:
            pass

    print("Session 已就位：")
    print(f"- {config.admin_session.with_suffix('.session')}")
    print(f"- {config.core_session.with_suffix('.session')}")
    print("\n授权完成。\n")


if __name__ == "__main__":
    asyncio.run(main())
