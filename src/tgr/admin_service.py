from __future__ import annotations

import asyncio
import html
import os
import shlex
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions, types, utils

from .config import load_config, update_config_data
from .db import RadarDB, RouteTask
from .logger import setup_logger
from .sync_logic import RouteReport, SyncReport, scan_auto_routes, sync_dialog_folders
from .telegram_utils import dialog_filter_title, format_duration, normalize_pattern_from_terms, try_remove_terms_from_pattern
from .version import __version__


class AdminApp:
    def __init__(self, work_dir: Path) -> None:
        self.config = load_config(work_dir)
        self.logger = setup_logger("tg-radar-admin", self.config.logs_dir / "admin.log")
        self.db = RadarDB(self.config.db_path)
        self.started_at = datetime.now()
        self.stop_event = asyncio.Event()
        self.sync_lock = asyncio.Lock()
        self.client: TelegramClient | None = None

    async def run(self) -> None:
        self.config.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not (self.config.admin_session.with_suffix('.session')).exists():
            raise FileNotFoundError("Missing sessions/tg_radar_admin.session. Run bootstrap_session.py first.")

        lock_file = self.config.work_dir / ".admin.lock"
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        try:
            if os.name != "nt":
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            raise RuntimeError("tg-radar-admin is already running")

        async with TelegramClient(str(self.config.admin_session), self.config.api_id, self.config.api_hash) as client:
            self.client = client
            client.parse_mode = "html"
            self.register_handlers(client)
            self.db.log_event("INFO", "ADMIN", f"TGRC admin started v{__version__}")
            await self.send_startup_notification()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.stop_event.set)
                except NotImplementedError:
                    pass

            tasks = [
                asyncio.create_task(self.periodic_sync()),
                asyncio.create_task(self.route_worker()),
                asyncio.create_task(client.run_until_disconnected()),
                asyncio.create_task(self.stop_event.wait()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            self.stop_event.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self.db.log_event("INFO", "ADMIN", "admin service stopping")
            self.logger.info("TGRC admin service stopping")

    def register_handlers(self, client: TelegramClient) -> None:
        prefix = self.config.cmd_prefix
        import re

        cmd_regex = re.compile(rf"^{re.escape(prefix)}(\w+)[ \t]*([\s\S]*)", re.IGNORECASE)

        @client.on(events.NewMessage(chats=["me"], pattern=cmd_regex))
        async def control_panel(event: events.NewMessage.Event) -> None:
            command = event.pattern_match.group(1).lower()
            args = (event.pattern_match.group(2) or "").strip()
            try:
                await self.dispatch(event, command, args)
            except Exception as exc:
                self.logger.exception("command failed: %s", exc)
                self.db.log_event("ERROR", "COMMAND", str(exc))
                await self.safe_reply(event, f"❌ <b>执行失败</b>\n<blockquote expandable>{html.escape(str(exc))}</blockquote>")

    async def dispatch(self, event: events.NewMessage.Event, command: str, args: str) -> None:
        prefix = html.escape(self.config.cmd_prefix)

        if command == "help":
            await self.safe_reply(
                event,
                f"""⚙️ <b>TGRC 管理菜单</b>

<b>运行与状态</b>
<code>{prefix}ping</code> - 心跳测试
<code>{prefix}status</code> - 运行状态总览
<code>{prefix}log 30</code> - 最近 30 条操作日志
<code>{prefix}version</code> - 查看当前版本

<b>分组与规则</b>
<code>{prefix}folders</code> - 分组列表
<code>{prefix}rules 分组名</code> - 查看该分组规则
<code>{prefix}enable 分组名</code> - 开启分组
<code>{prefix}disable 分组名</code> - 关闭分组
<code>{prefix}addrule 分组名 规则名 关键词...</code> - 添加规则
<code>{prefix}delrule 分组名 规则名</code> - 删除规则

<b>路由与配置</b>
<code>{prefix}routes</code> - 查看自动路由
<code>{prefix}addroute 分组名 关键词...</code> - 添加自动路由
<code>{prefix}delroute 分组名</code> - 删除自动路由
<code>{prefix}config</code> - 查看关键配置
<code>{prefix}setnotify 频道ID/off</code> - 设置系统通知频道
<code>{prefix}setalert 频道ID/off</code> - 设置默认告警频道
<code>{prefix}setprefix 新前缀</code> - 修改命令前缀（需重启 admin）

<b>维护操作</b>
<code>{prefix}sync</code> - 执行同步
<code>{prefix}restart</code> - 重启核心与管理服务
<code>{prefix}update</code> - git pull 后重启
""",
                auto_delete=60,
            )
            return

        if command == "ping":
            stats = self.db.get_runtime_stats()
            total_hits = stats.get("total_hits", "0")
            await self.safe_reply(
                event,
                f"⚡ <b>系统运行正常</b> | 已运行 <code>{format_duration((datetime.now() - self.started_at).total_seconds())}</code> | 累计命中 <code>{total_hits}</code>",
            )
            return

        if command == "version":
            repo = html.escape(self.config.repo_url or "未配置")
            await self.safe_reply(
                event,
                f"🧬 <b>TGRC 版本信息</b>\n\n· 当前版本：<code>{__version__}</code>\n· 服务前缀：<code>{html.escape(self.config.service_name_prefix)}</code>\n· 仓库地址：<code>{repo}</code>",
                auto_delete=45,
            )
            return

        if command == "config":
            await self.safe_reply(
                event,
                f"🧩 <b>关键配置</b>\n\n· 命令前缀：<code>{html.escape(self.config.cmd_prefix)}</code>\n· 默认告警频道：<code>{self.config.global_alert_channel_id if self.config.global_alert_channel_id is not None else '未设置'}</code>\n· 系统通知频道：<code>{self.config.notify_channel_id if self.config.notify_channel_id is not None else 'Saved Messages'}</code>\n· 服务名前缀：<code>{html.escape(self.config.service_name_prefix)}</code>",
                auto_delete=45,
            )
            return

        if command == "setnotify":
            raw = args.strip()
            value = None if raw.lower() in {"", "off", "none", "null"} else int(raw)
            update_config_data(self.config.work_dir, {"notify_channel_id": value})
            self.config = load_config(self.config.work_dir)
            await self.safe_reply(event, f"✅ <b>系统通知频道已更新</b>\n<code>{value if value is not None else 'Saved Messages'}</code>")
            return

        if command == "setalert":
            raw = args.strip()
            value = None if raw.lower() in {"", "off", "none", "null"} else int(raw)
            update_config_data(self.config.work_dir, {"global_alert_channel_id": value})
            self.config = load_config(self.config.work_dir)
            self.db.bump_revision()
            await self.safe_reply(event, f"✅ <b>默认告警频道已更新</b>\n<code>{value if value is not None else '未设置'}</code>")
            return

        if command == "setprefix":
            value = args.strip()
            if not value or len(value) > 3 or " " in value:
                await self.safe_reply(event, "⚠️ <b>前缀格式无效</b>\n建议 1-3 个字符，且不能包含空格。")
                return
            update_config_data(self.config.work_dir, {"cmd_prefix": value})
            await self.safe_reply(event, f"✅ <b>命令前缀已更新</b>：<code>{html.escape(value)}</code>\n<i>即将重启服务使前缀生效。</i>")
            self.restart_services(delay=1.5)
            return

        if command == "status":
            folders = self.db.list_folders()
            enabled = sum(1 for row in folders if int(row["enabled"]) == 1)
            stats = self.db.get_runtime_stats()
            total_hits = stats.get("total_hits", "0")
            last_hit_folder = stats.get("last_hit_folder", "") or "暂无记录"
            last_hit_time = stats.get("last_hit_time", "") or "暂无记录"
            target_map, valid_rules = self.db.build_target_map(self.config.global_alert_channel_id)
            queue_count = self.db.pending_route_count()
            await self.safe_reply(
                event,
                f"""📊 <b>TGRC 运行状态</b>

<b>⚙️ 服务概况</b>
· 管理进程：<code>在线</code>
· 运行时长：<code>{format_duration((datetime.now() - self.started_at).total_seconds())}</code>
· 当前版本：<code>{__version__}</code>

<b>🌐 监控规模</b>
· 分组总数：<code>{len(folders)}</code>
· 已启用分组：<code>{enabled}</code>
· 活跃监听对象：<code>{len(target_map)}</code>
· 生效规则：<code>{valid_rules}</code>

<b>🔀 路由队列</b>
· 队列任务：<code>{queue_count}</code>

<b>🛡️ 命中统计</b>
· 累计命中：<code>{total_hits}</code>
· 最近命中：<code>{html.escape(last_hit_folder)}</code> / <code>{html.escape(last_hit_time)}</code>
""",
                auto_delete=30,
            )
            return

        if command == "log":
            limit = 20
            if args.isdigit():
                limit = max(1, min(100, int(args)))
            rows = self.db.recent_logs(limit)
            if not rows:
                await self.safe_reply(event, "📋 <b>暂无操作日志</b>")
                return
            body = "\n".join(f"[{row['created_at']}] {row['level']}/{row['action']} - {row['detail']}" for row in rows)
            await self.safe_reply(event, f"📋 <b>最近 {len(rows)} 条操作日志</b>\n<blockquote expandable>{html.escape(body)}</blockquote>", auto_delete=60)
            return

        if command == "folders":
            rows = self.db.list_folders()
            if not rows:
                await self.safe_reply(event, "📂 <b>当前没有任何分组记录</b>")
                return
            parts = []
            for row in rows:
                folder_name = row["folder_name"]
                group_count = self.db.count_cache_for_folder(folder_name)
                rule_count = self.db.count_rules_for_folder(folder_name)
                icon = "🟢" if int(row["enabled"]) == 1 else "⚪"
                parts.append(f"{icon} <b>{html.escape(folder_name)}</b>\n  └ 群组 <code>{group_count}</code> · 规则 <code>{rule_count}</code>")
            body = "\n\n".join(parts)
            await self.safe_reply(event, f"📂 <b>分组总览</b>\n\n{body}", auto_delete=60)
            return

        if command == "rules":
            if not args:
                await self.safe_reply(event, f"⚠️ <b>请指定分组名</b>\n示例：<code>{prefix}rules 业务群</code>")
                return
            folder = self.find_folder(args)
            if folder is None:
                await self.safe_reply(event, "⚠️ <b>找不到该分组</b>")
                return
            rows = self.db.get_rules_for_folder(folder)
            body = "\n".join(f"· <b>{html.escape(row['rule_name'])}</b>\n  <code>{html.escape(row['pattern'])}</code>" for row in rows) if rows else "<i>该分组还没有规则</i>"
            await self.safe_reply(event, f"🛡️ <b>{html.escape(folder)} 的规则</b>\n<blockquote>{body}</blockquote>", auto_delete=60)
            return

        if command in {"enable", "disable"}:
            if not args:
                await self.safe_reply(event, f"⚠️ <b>请指定分组名</b>\n示例：<code>{prefix}{command} 业务群</code>")
                return
            folder = self.find_folder(args)
            if folder is None:
                await self.safe_reply(event, "⚠️ <b>找不到该分组</b>")
                return
            self.db.set_folder_enabled(folder, command == "enable")
            self.db.log_event("INFO", "TOGGLE_FOLDER", f"{folder} -> {command}")
            word = "开启" if command == "enable" else "关闭"
            await self.safe_reply(event, f"✅ <b>已{word}分组</b>：<code>{html.escape(folder)}</code>")
            return

        if command == "addrule":
            tokens = shlex.split(args)
            if len(tokens) < 3:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}addrule 业务群 核心词 苹果 华为</code>")
                return
            folder_raw, rule_name = tokens[0], tokens[1]
            folder = self.find_folder(folder_raw)
            if folder is None:
                await self.safe_reply(event, "⚠️ <b>找不到该分组</b>")
                return
            pattern = normalize_pattern_from_terms(" ".join(tokens[2:]))
            self.db.upsert_rule(folder, rule_name, pattern)
            self.db.log_event("INFO", "ADD_RULE", f"{folder}/{rule_name} -> {pattern}")
            await self.safe_reply(event, f"✅ <b>规则已保存</b>\n分组：<code>{html.escape(folder)}</code>\n规则：<code>{html.escape(rule_name)}</code>\n表达式：<code>{html.escape(pattern)}</code>")
            return

        if command == "delrule":
            tokens = shlex.split(args)
            if len(tokens) < 2:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}delrule 业务群 核心词</code>")
                return
            folder = self.find_folder(tokens[0])
            if folder is None:
                await self.safe_reply(event, "⚠️ <b>找不到该分组</b>")
                return
            rule_name = tokens[1]
            if len(tokens) == 2:
                if not self.db.delete_rule(folder, rule_name):
                    await self.safe_reply(event, "⚠️ <b>没有找到这条规则</b>")
                    return
                self.db.log_event("INFO", "DELETE_RULE", f"{folder}/{rule_name}")
                await self.safe_reply(event, f"🗑️ <b>已删除规则</b>：<code>{html.escape(folder)} / {html.escape(rule_name)}</code>")
                return
            current_rows = self.db.get_rules_for_folder(folder)
            current = next((row for row in current_rows if row["rule_name"] == rule_name), None)
            if current is None:
                await self.safe_reply(event, "⚠️ <b>没有找到这条规则</b>")
                return
            new_pattern = try_remove_terms_from_pattern(str(current["pattern"]), tokens[2:])
            if new_pattern is None:
                self.db.delete_rule(folder, rule_name)
                self.db.log_event("INFO", "DELETE_RULE", f"{folder}/{rule_name} -> removed all terms")
                await self.safe_reply(event, f"🗑️ <b>规则已删除</b>：<code>{html.escape(folder)} / {html.escape(rule_name)}</code>")
                return
            self.db.update_rule_pattern(folder, rule_name, new_pattern)
            self.db.log_event("INFO", "UPDATE_RULE", f"{folder}/{rule_name} -> {new_pattern}")
            await self.safe_reply(event, f"✅ <b>规则已更新</b>\n新表达式：<code>{html.escape(new_pattern)}</code>")
            return

        if command == "routes":
            rows = self.db.list_routes()
            if not rows:
                await self.safe_reply(event, "🔀 <b>当前没有自动路由规则</b>")
                return
            body = "\n".join(f"· <b>{html.escape(row['folder_name'])}</b>\n  <code>{html.escape(row['pattern'])}</code>" for row in rows)
            await self.safe_reply(event, f"🔀 <b>自动路由规则</b>\n<blockquote>{body}</blockquote>", auto_delete=60)
            return

        if command == "addroute":
            tokens = shlex.split(args)
            if len(tokens) < 2:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}addroute 业务群 供需 担保</code>")
                return
            folder = self.find_folder(tokens[0]) or tokens[0]
            if self.db.get_folder(folder) is None:
                self.db.upsert_folder(folder, None, enabled=False)
            pattern = normalize_pattern_from_terms(" ".join(tokens[1:]))
            self.db.set_route(folder, pattern)
            self.db.log_event("INFO", "ADD_ROUTE", f"{folder} -> {pattern}")
            await self.safe_reply(event, f"✅ <b>自动路由已保存</b>\n分组：<code>{html.escape(folder)}</code>\n表达式：<code>{html.escape(pattern)}</code>")
            return

        if command == "delroute":
            if not args:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}delroute 业务群</code>")
                return
            folder = self.find_folder(args) or args.strip()
            if not self.db.delete_route(folder):
                await self.safe_reply(event, "⚠️ <b>没有找到该自动路由规则</b>")
                return
            self.db.log_event("INFO", "DELETE_ROUTE", folder)
            await self.safe_reply(event, f"🗑️ <b>自动路由已删除</b>：<code>{html.escape(folder)}</code>")
            return

        if command == "sync":
            await self.run_sync_command(event)
            return

        if command == "restart":
            await event.reply("🔄 <b>正在重启 tg-radar-core 与 tg-radar-admin ...</b>")
            self.db.log_event("INFO", "RESTART", "restart requested from Telegram")
            self.restart_services()
            return

        if command == "update":
            await self.run_update_command(event)
            return

        await self.safe_reply(event, f"⚠️ <b>未知命令</b>\n请发送 <code>{prefix}help</code> 查看可用指令")

    async def run_sync_command(self, event: events.NewMessage.Event) -> None:
        if self.sync_lock.locked():
            await self.safe_reply(event, "⚠️ <b>系统正忙</b>，当前已有同步任务在执行")
            return
        async with self.sync_lock:
            await event.reply("⏳ <b>正在执行分组同步与路由扫描...</b>")
            sync_report = await sync_dialog_folders(self.client, self.db)
            route_report = await scan_auto_routes(self.client, self.db)
            self.db.log_event("INFO", "SYNC", f"sync changed={sync_report.has_changes} queued={sum(route_report.queued.values())}")
            await self.safe_reply(event, self.render_sync_message(sync_report, route_report), auto_delete=60)

    async def run_update_command(self, event: events.NewMessage.Event) -> None:
        if not (self.config.work_dir / '.git').exists():
            await self.safe_reply(event, "⚠️ <b>当前目录不是 git 仓库</b>，请使用 git 部署后再执行 update")
            return
        await event.reply("🔄 <b>正在执行 git pull --ff-only ...</b>")
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(self.config.work_dir), "pull", "--ff-only",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            self.db.log_event("ERROR", "UPDATE", output or f"git pull failed: {proc.returncode}")
            await self.safe_reply(event, f"❌ <b>更新失败</b>\n<blockquote expandable>{html.escape(output or 'git pull failed')}</blockquote>")
            return
        self.db.log_event("INFO", "UPDATE", output or "git pull ok")
        await self.safe_reply(event, f"✅ <b>代码已更新</b>\n<blockquote expandable>{html.escape(output or 'Already up to date.')}</blockquote>\n<i>即将重启服务使新代码生效。</i>")
        self.restart_services(delay=1.5)

    async def periodic_sync(self) -> None:
        await asyncio.sleep(5)
        while not self.stop_event.is_set():
            if not self.sync_lock.locked():
                try:
                    async with self.sync_lock:
                        sync_report = await sync_dialog_folders(self.client, self.db)
                        route_report = await scan_auto_routes(self.client, self.db)
                        self.db.log_event("INFO", "AUTO_SYNC", f"sync changed={sync_report.has_changes} queued={sum(route_report.queued.values())}")
                except Exception as exc:
                    self.logger.exception("periodic sync failed: %s", exc)
                    self.db.log_event("ERROR", "AUTO_SYNC", str(exc))
            await asyncio.sleep(self.config.sync_interval_seconds)

    async def route_worker(self) -> None:
        while not self.stop_event.is_set():
            task = self.db.get_next_route_task()
            if task is None:
                await asyncio.sleep(2)
                continue
            try:
                await self.apply_route_task(task)
                self.db.complete_route_task(task.id)
                self.db.log_event("INFO", "ROUTE_TASK", f"{task.folder_name} +{len(task.peer_ids)}")
            except Exception as exc:
                self.logger.exception("route task failed: %s", exc)
                retry = task.retries < 3
                self.db.fail_route_task(task.id, str(exc), retry=retry)
                self.db.log_event("ERROR", "ROUTE_TASK", f"{task.folder_name}: {exc}")
            await asyncio.sleep(self.config.route_worker_interval_seconds)

    async def apply_route_task(self, task: RouteTask) -> None:
        assert self.client is not None
        req = await self.client(functions.messages.GetDialogFiltersRequest())
        folders = [f for f in getattr(req, "filters", []) if isinstance(f, types.DialogFilter)]
        target = None
        for folder in folders:
            title = dialog_filter_title(folder)
            if (task.folder_id is not None and int(folder.id) == int(task.folder_id)) or title == task.folder_name:
                target = folder
                break

        peers = []
        for peer_id in task.peer_ids:
            try:
                peers.append(await self.client.get_input_entity(peer_id))
            except Exception:
                continue
        if not peers:
            return

        if target is None:
            folder_id = task.folder_id or 2
            used_ids = {int(f.id) for f in folders}
            while folder_id in used_ids:
                folder_id += 1
            new_filter = types.DialogFilter(
                id=folder_id,
                title=task.folder_name,
                pinned_peers=[],
                include_peers=peers[:100],
                exclude_peers=[],
                contacts=False,
                non_contacts=False,
                groups=False,
                broadcasts=False,
                bots=False,
                exclude_muted=False,
                exclude_read=False,
                exclude_archived=False,
            )
            await self.client(functions.messages.UpdateDialogFilterRequest(id=folder_id, filter=new_filter))
            self.db.upsert_folder(task.folder_name, folder_id)
            return

        current_ids = set()
        for peer in getattr(target, "include_peers", []):
            try:
                current_ids.add(int(utils.get_peer_id(peer)))
            except Exception:
                continue
        existing = list(getattr(target, "include_peers", []))
        for peer in peers:
            try:
                pid = int(utils.get_peer_id(peer))
            except Exception:
                continue
            if pid in current_ids:
                continue
            existing.append(peer)
            current_ids.add(pid)
            if len(existing) >= 100:
                break
        target.include_peers = existing[:100]
        await self.client(functions.messages.UpdateDialogFilterRequest(id=target.id, filter=target))

    async def send_startup_notification(self) -> None:
        rows = self.db.list_folders()
        enabled = [row for row in rows if int(row["enabled"]) == 1]
        route_count = len(self.db.list_routes())
        target = self.config.notify_channel_id if self.config.notify_channel_id is not None else "me"
        msg = f"""📊 <b>TGRC 已上线</b>

<b>服务拆分</b>
· <code>radar_admin.py</code> 已启动
· <code>radar_core.py</code> 通过数据库 revision 热加载规则

<b>当前概况</b>
· 分组总数：<code>{len(rows)}</code>
· 已启用分组：<code>{len(enabled)}</code>
· 自动路由：<code>{route_count}</code>
· 版本：<code>{__version__}</code>
"""
        try:
            await self.client.send_message(target, msg, link_preview=False)
        except Exception as exc:
            self.logger.warning("startup notification failed: %s", exc)

    async def safe_reply(self, event: events.NewMessage.Event, text: str, auto_delete: int = 20) -> None:
        msg = None
        try:
            msg = await event.edit(text)
        except Exception:
            msg = await event.reply(text)
        if msg and auto_delete > 0:
            async def _delete_later() -> None:
                await asyncio.sleep(auto_delete)
                try:
                    await msg.delete()
                except Exception:
                    pass
            asyncio.create_task(_delete_later())

    def render_sync_message(self, sync_report: SyncReport, route_report: RouteReport) -> str:
        lines = ["✅ <b>同步完成</b>", ""]
        lines.append(f"· 分组变更：<code>{'有变化' if sync_report.has_changes else '无变化'}</code>")
        lines.append(f"· 耗时：<code>{sync_report.elapsed_seconds:.1f} 秒</code>")
        lines.append(f"· 新发现：<code>{len(sync_report.discovered)}</code>")
        lines.append(f"· 重命名：<code>{len(sync_report.renamed)}</code>")
        lines.append(f"· 删除：<code>{len(sync_report.deleted)}</code>")
        if route_report.created or route_report.queued or route_report.matched_zero:
            lines.append("")
            lines.append("<b>自动路由扫描</b>")
            for name in route_report.created:
                lines.append(f"· ✨ 新建分组：<code>{html.escape(name)}</code>")
            for name, count in route_report.queued.items():
                lines.append(f"· ⏳ 已排队：<code>{html.escape(name)}</code> + <code>{count}</code>")
            for name in route_report.matched_zero:
                lines.append(f"· 🔕 未匹配：<code>{html.escape(name)}</code>")
        return "\n".join(lines)

    def find_folder(self, query: str) -> str | None:
        rows = self.db.list_folders()
        names = [row["folder_name"] for row in rows]
        if query in names:
            return query
        lower = query.lower()
        for name in names:
            if name.lower() == lower:
                return name
        candidates = [name for name in names if lower in name.lower()]
        return candidates[0] if len(candidates) == 1 else None

    def restart_services(self, delay: float = 0.0) -> None:
        cmd = [
            "bash",
            "-lc",
            f"sleep {delay}; systemctl restart {self.config.service_name_prefix}-core {self.config.service_name_prefix}-admin",
        ]
        subprocess.Popen(cmd)


async def run(work_dir: Path) -> None:
    app = AdminApp(work_dir)
    await app.run()
