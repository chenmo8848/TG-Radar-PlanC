from __future__ import annotations

from pathlib import Path

from .config import read_config_data, sync_snapshot_to_config
from .db import RadarDB


def seed_db_from_legacy_config_if_needed(work_dir: Path, db: RadarDB) -> bool:
    data = read_config_data(work_dir)
    if not db.is_empty():
        return False
    if not (data.get("folder_rules") or data.get("auto_route_rules") or data.get("_system_cache")):
        return False
    changed = db.import_legacy_snapshot(data)
    if changed:
        sync_snapshot_to_config(work_dir, db)
    return changed
