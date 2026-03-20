from __future__ import annotations

import asyncio
import html
import os
import re
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events

from .config import load_config
from .db import RadarDB
from .logger import setup_logger
from .telegram_utils import build_message_link
from .version import __version__


@dataclass
class RuntimeState:
    target_map: dict[int, list[dict]]
    valid_rules_count: int
    revision: int
    started_at: datetime


async def run(work_dir: Path) -> None:
    config = load_config(work_dir)
    logger = setup_logger("tg-radar-core", config.logs_dir / "core.log")
    db = RadarDB(config.db_path)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)

    if not (config.core_session.with_suffix('.session')).exists():
        raise FileNotFoundError("Missing runtime/sessions/tg_radar_core.session. Run bootstrap_session.py first.")

    lock_file = work_dir / ".core.lock"
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform != "win32":
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        raise RuntimeError("tg-radar-core is already running")

    raw_target_map, valid_rules_count = db.build_target_map(config.global_alert_channel_id)
    state = RuntimeState(
        target_map=compile_target_map(raw_target_map, logger),
        valid_rules_count=valid_rules_count,
        revision=db.get_revision(),
        started_at=datetime.now(),
    )

    stop_event = asyncio.Event()

    async with TelegramClient(str(config.core_session), config.api_id, config.api_hash) as client:
        client.parse_mode = "html"
        logger.info("core service started, version=%s, revision=%s, chats=%s, rules=%s", __version__, state.revision, len(state.target_map), state.valid_rules_count)
        db.log_event("INFO", "CORE", f"core service started v{__version__}")

        @client.on(events.NewMessage)
        async def message_handler(event: events.NewMessage.Event) -> None:
            try:
                if not (event.is_group or event.is_channel):
                    return
                tasks = state.target_map.get(int(event.chat_id))
                if not tasks:
                    return
                msg_text = event.raw_text or ""
                if not msg_text:
                    return

                chat = None
                chat_title = "未知聊天"
                sender_name = "隐藏用户"
                sender_loaded = False
                sent_keys: set[tuple[int, str, str]] = set()

                for task in tasks:
                    for rule_name, pattern in task["rules"]:
                        match = pattern.search(msg_text)
                        if not match:
                            continue
                        route_key = (int(task["alert_channel"]), str(task["folder_name"]), str(rule_name))
                        if route_key in sent_keys:
                            continue
                        sent_keys.add(route_key)

                        if not sender_loaded:
                            sender_loaded = True
                            chat = await event.get_chat()
                            chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or "未知聊天"
                            try:
                                sender = await event.get_sender()
                                if getattr(sender, "bot", False):
                                    return
                                sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or "隐藏用户"
                            except Exception:
                                sender_name = "广播系统"

                        preview = html.escape(msg_text[:1200])
                        msg_link = build_message_link(chat, int(event.chat_id), int(event.id)) if chat is not None else ""
                        now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        alert_text = f"""🚨 <b>监控情报触发通知</b>

⏰ <b>捕获时间</b>：<code>{now_time}</code>
🎯 <b>命中词汇</b>：<code>{html.escape(match.group(0))}</code>
📁 <b>命中规则</b>：<code>{html.escape(rule_name)}</code> ({html.escape(task['folder_name'])})
📡 <b>消息来源</b>：<code>{html.escape(chat_title)}</code>
👤 <b>发送人员</b>：@{html.escape(sender_name)}

<b>💬 原始消息快照</b>：
<blockquote expandable>{preview}</blockquote>"""
                        if msg_link:
                            alert_text += f'\n🔗 <a href="{msg_link}">点击跳转直达现场</a>'

                        try:
                            await client.send_message(int(task["alert_channel"]), alert_text, link_preview=False)
                            db.increment_hit(task["folder_name"])
                            db.log_event("HIT", "MATCH", f"{rule_name} <- {chat_title}")
                        except Exception as exc:
                            logger.exception("failed to send alert: %s", exc)
                            db.log_event("ERROR", "SEND_ALERT", str(exc))
            except Exception as exc:
                logger.exception("message handler error: %s", exc)
                db.log_event("ERROR", "CORE_HANDLER", str(exc))

        async def revision_watcher() -> None:
            while not stop_event.is_set():
                try:
                    latest = db.get_revision()
                    if latest != state.revision:
                        raw_map, valid_rules = db.build_target_map(config.global_alert_channel_id)
                        state.target_map = compile_target_map(raw_map, logger)
                        state.valid_rules_count = valid_rules
                        state.revision = latest
                        logger.info("core reloaded revision=%s, chats=%s, rules=%s", latest, len(state.target_map), valid_rules)
                        db.log_event("INFO", "CORE_RELOAD", f"reloaded revision {latest}")
                except Exception as exc:
                    logger.exception("revision watcher error: %s", exc)
                    db.log_event("ERROR", "CORE_WATCHER", str(exc))
                await asyncio.sleep(config.revision_poll_seconds)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        watcher_task = asyncio.create_task(revision_watcher())
        disconnect_task = asyncio.create_task(client.run_until_disconnected())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait({watcher_task, disconnect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        stop_event.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("core service stopping")
        db.log_event("INFO", "CORE", "core service stopping")


def compile_target_map(raw_target_map: dict[int, list[dict]], logger) -> dict[int, list[dict]]:
    compiled: dict[int, list[dict]] = {}
    for chat_id, tasks in raw_target_map.items():
        for task in tasks:
            compiled_rules: list[tuple[str, re.Pattern[str]]] = []
            for rule_name, pattern in task["rules"]:
                try:
                    compiled_rules.append((rule_name, re.compile(pattern, re.IGNORECASE)))
                except re.error as exc:
                    logger.warning("invalid regex skipped: folder=%s rule=%s err=%s", task["folder_name"], rule_name, exc)
            if not compiled_rules:
                continue
            compiled.setdefault(chat_id, []).append(
                {
                    "folder_name": task["folder_name"],
                    "alert_channel": task["alert_channel"],
                    "rules": compiled_rules,
                }
            )
    return compiled
