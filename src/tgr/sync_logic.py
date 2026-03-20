from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from telethon import TelegramClient, functions, types, utils

from .db import RadarDB
from .telegram_utils import dialog_filter_title, resolve_peer_id


@dataclass
class SyncReport:
    discovered: list[str]
    renamed: list[tuple[str, str]]
    deleted: list[str]
    active: dict[str, int]
    has_changes: bool
    elapsed_seconds: float


@dataclass
class RouteReport:
    created: list[str]
    queued: dict[str, int]
    matched_zero: list[str]
    already_in: dict[str, int]
    errors: dict[str, str]


DEFAULT_DEMO_PATTERN = "(示范词A|示范词B)"


async def sync_dialog_folders(client: TelegramClient, db: RadarDB) -> SyncReport:
    started = datetime.now()
    discovered: list[str] = []
    renamed: list[tuple[str, str]] = []
    deleted: list[str] = []
    active: dict[str, int] = {}
    changed = False

    result = await client(functions.messages.GetDialogFiltersRequest())
    tg_folders = [f for f in getattr(result, "filters", []) if isinstance(f, types.DialogFilter)]
    all_dialogs = await client.get_dialogs(limit=None)
    all_dialog_by_id = {int(d.id): d for d in all_dialogs if d.is_group or d.is_channel}

    db_folders = db.list_folders()
    id_to_name = {row["folder_id"]: row["folder_name"] for row in db_folders if row["folder_id"] is not None}
    existing_names = {row["folder_name"] for row in db_folders}
    current_ids: set[int] = set()

    with db.tx() as conn:
        for folder in tg_folders:
            folder_id = int(folder.id)
            title = dialog_filter_title(folder)
            current_ids.add(folder_id)

            if folder_id in id_to_name and id_to_name[folder_id] != title:
                old_name = id_to_name[folder_id]
                db.rename_folder(old_name, title, folder_id=folder_id, conn=conn)
                renamed.append((old_name, title))
                changed = True
            elif title not in existing_names:
                db.upsert_folder(title, folder_id, enabled=False, alert_channel_id=None, conn=conn)
                db.upsert_rule(title, f"{title}监控", DEFAULT_DEMO_PATTERN, conn=conn)
                discovered.append(title)
                changed = True
            else:
                db.upsert_folder(title, folder_id, conn=conn)

            target_ids: set[int] = set()
            exclude_ids: set[int] = set()

            for peer in getattr(folder, "exclude_peers", []):
                pid = resolve_peer_id(peer)
                if pid:
                    exclude_ids.add(pid)
            for peer in getattr(folder, "include_peers", []):
                pid = resolve_peer_id(peer)
                if pid:
                    target_ids.add(pid)

            if getattr(folder, "groups", False) or getattr(folder, "broadcasts", False):
                for dialog in all_dialogs:
                    if folder.groups and dialog.is_group:
                        target_ids.add(int(dialog.id))
                    elif folder.broadcasts and dialog.is_channel and not dialog.is_group:
                        target_ids.add(int(dialog.id))

            target_ids = target_ids - exclude_ids
            items: list[tuple[int, str | None]] = []
            for chat_id in sorted(target_ids):
                dialog = all_dialog_by_id.get(chat_id)
                chat_title = None
                if dialog is not None:
                    chat_title = getattr(dialog, "name", None) or getattr(dialog.entity, "title", None)
                items.append((chat_id, chat_title))

            old_rows = conn.execute(
                "SELECT chat_id FROM system_cache WHERE folder_name=? ORDER BY chat_id",
                (title,),
            ).fetchall()
            old_ids = [int(row["chat_id"]) for row in old_rows]
            new_ids = [int(chat_id) for chat_id, _ in items]
            if old_ids != new_ids:
                changed = True
            db.replace_folder_cache(title, items, conn=conn)
            active[title] = len(items)

        for row in db_folders:
            folder_id = row["folder_id"]
            if folder_id is not None and folder_id not in current_ids:
                deleted.append(row["folder_name"])
                db.delete_folder(row["folder_name"], conn=conn)
                changed = True

        if changed:
            db.bump_revision(conn)

    return SyncReport(
        discovered=discovered,
        renamed=renamed,
        deleted=deleted,
        active=active,
        has_changes=changed,
        elapsed_seconds=(datetime.now() - started).total_seconds(),
    )


async def scan_auto_routes(client: TelegramClient, db: RadarDB) -> RouteReport:
    report = RouteReport(created=[], queued={}, matched_zero=[], already_in={}, errors={})
    routes = db.list_routes()
    if not routes:
        return report

    req = await client(functions.messages.GetDialogFiltersRequest())
    folders = [f for f in getattr(req, "filters", []) if isinstance(f, types.DialogFilter)]
    folders_by_title = {dialog_filter_title(f): f for f in folders}
    used_ids = {int(f.id) for f in folders}

    all_dialogs = []
    async for d in client.iter_dialogs():
        if not (d.is_group or d.is_channel):
            continue
        name = utils.get_display_name(d.entity) or getattr(d, "name", "") or getattr(d, "title", "") or ""
        all_dialogs.append({"id": int(d.id), "name": name})

    for row in routes:
        folder_name = str(row["folder_name"])
        pattern_str = str(row["pattern"])
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            report.errors[folder_name] = f"invalid route regex: {exc}"
            continue

        matched_ids = [d["id"] for d in all_dialogs if pattern.search(d["name"])]
        if not matched_ids:
            report.matched_zero.append(folder_name)
            continue

        folder_obj = folders_by_title.get(folder_name)
        db_folder = db.get_folder(folder_name)
        folder_id = int(db_folder["folder_id"]) if db_folder and db_folder["folder_id"] is not None else None

        if folder_obj is None:
            new_id = 2
            while new_id in used_ids:
                new_id += 1
            used_ids.add(new_id)
            folder_id = new_id
            report.created.append(folder_name)
            db.upsert_folder(folder_name, folder_id)

        current_ids: set[int] = set()
        if folder_obj is not None:
            for peer in getattr(folder_obj, "include_peers", []):
                try:
                    current_ids.add(int(utils.get_peer_id(peer)))
                except Exception:
                    continue

        to_add = [pid for pid in matched_ids if pid not in current_ids]
        already = len(matched_ids) - len(to_add)
        if already:
            report.already_in[folder_name] = already
        if not to_add:
            continue
        if folder_obj is not None and len(current_ids) + len(to_add) > 100:
            allowed = max(0, 100 - len(current_ids))
            to_add = to_add[:allowed]
        if not to_add:
            continue
        db.upsert_route_task(folder_name, folder_id, to_add)
        report.queued[folder_name] = len(to_add)

    return report
