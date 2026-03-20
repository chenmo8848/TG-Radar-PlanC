from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "api_id": 1234567,
    "api_hash": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "global_alert_channel_id": None,
    "notify_channel_id": None,
    "cmd_prefix": "-",
    "service_name_prefix": "tgrc-radar",
    "sync_interval_seconds": 1800,
    "route_worker_interval_seconds": 4,
    "revision_poll_seconds": 10,
    "repo_url": "",
}


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


def _normalize_int(value: Any) -> int | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def read_config_data(work_dir: Path) -> dict[str, Any]:
    config_path = work_dir / "config.json"
    raw: dict[str, Any]
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        raw = {}

    data = dict(DEFAULT_CONFIG)
    data.update(raw)
    data["api_id"] = int(data.get("api_id") or 0)
    data["api_hash"] = str(data.get("api_hash") or "")
    data["global_alert_channel_id"] = _normalize_int(data.get("global_alert_channel_id"))
    data["notify_channel_id"] = _normalize_int(data.get("notify_channel_id"))
    data["cmd_prefix"] = str(data.get("cmd_prefix") or "-")
    data["service_name_prefix"] = str(data.get("service_name_prefix") or "tgrc-radar")
    data["sync_interval_seconds"] = int(data.get("sync_interval_seconds") or 1800)
    data["route_worker_interval_seconds"] = int(data.get("route_worker_interval_seconds") or 4)
    data["revision_poll_seconds"] = int(data.get("revision_poll_seconds") or 10)
    data["repo_url"] = str(data.get("repo_url") or "")
    return data


def save_config_data(work_dir: Path, data: dict[str, Any]) -> Path:
    config_path = work_dir / "config.json"
    normalized = dict(DEFAULT_CONFIG)
    normalized.update(data)
    config_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config_path


def update_config_data(work_dir: Path, updates: dict[str, Any]) -> Path:
    data = read_config_data(work_dir)
    data.update(updates)
    return save_config_data(work_dir, data)


def load_config(work_dir: Path) -> AppConfig:
    data = read_config_data(work_dir)
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
        service_name_prefix=str(data.get("service_name_prefix") or "tgrc-radar"),
        sync_interval_seconds=int(data.get("sync_interval_seconds") or 1800),
        route_worker_interval_seconds=int(data.get("route_worker_interval_seconds") or 4),
        revision_poll_seconds=int(data.get("revision_poll_seconds") or 10),
        repo_url=data.get("repo_url") or None,
    )
