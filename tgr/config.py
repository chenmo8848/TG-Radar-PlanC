from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    work_dir: Path
    api_id: int
    api_hash: str
    global_alert_channel_id: int | None
    notify_channel_id: int | None
    cmd_prefix: str
    service_name_prefix: str
    sync_interval_seconds: int
    route_worker_interval_seconds: int
    revision_poll_seconds: int
    repo_url: str | None

    @property
    def db_path(self) -> Path:
        return self.work_dir / "radar.db"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def sessions_dir(self) -> Path:
        return self.work_dir / "sessions"

    @property
    def admin_session(self) -> Path:
        return self.sessions_dir / "tg_radar_admin"

    @property
    def core_session(self) -> Path:
        return self.sessions_dir / "tg_radar_core"


def load_config(work_dir: Path) -> AppConfig:
    config_path = work_dir / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing config.json: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    api_id = int(data.get("api_id") or 0)
    api_hash = str(data.get("api_hash") or "")
    if not api_id or api_id == 1234567 or not api_hash or api_hash == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
        raise ValueError("config.json does not contain valid Telegram API credentials")

    return AppConfig(
        work_dir=work_dir,
        api_id=api_id,
        api_hash=api_hash,
        global_alert_channel_id=data.get("global_alert_channel_id"),
        notify_channel_id=data.get("notify_channel_id"),
        cmd_prefix=str(data.get("cmd_prefix") or "-"),
        service_name_prefix=str(data.get("service_name_prefix") or "tg-radar"),
        sync_interval_seconds=int(data.get("sync_interval_seconds") or 1800),
        route_worker_interval_seconds=int(data.get("route_worker_interval_seconds") or 4),
        revision_poll_seconds=int(data.get("revision_poll_seconds") or 10),
        repo_url=data.get("repo_url"),
    )
