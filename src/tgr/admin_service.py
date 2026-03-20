from __future__ import annotations

import asyncio
import html
import os
import re
import shlex
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions, types, utils

from .compat import seed_db_from_legacy_config_if_needed
from .config import load_config, sync_snapshot_to_config, update_config_data
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
        seed_db_from_legacy_config_if_needed(work_dir, self.db)
        sync_snapshot_to_config(work_dir, self.db)
        self.started_at = datetime.now()
        self.stop_event = asyncio.Event()
        self.sync_lock = asyncio.Lock()
        self.client: TelegramClient | None = None

    async def run(self) -> None:
        self.config.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not (self.config.admin_session.with_suffix('.session')).exists():
            raise FileNotFoundError("Missing runtime/sessions/tg_radar_admin.session. Run bootstrap_session.py first.")

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
                await self.safe_reply(event, f"❌ <b>系统执行报错</b>\n<blockquote expandable>{html.escape(str(exc))}</blockquote>")

    async def dispatch(self, event: events.NewMessage.Event, command: str, args: str) -> None:
        prefix = html.escape(self.config.cmd_prefix)

        if command == "help":
            await self.safe_reply(
                event,
                f"""⚙️ <b>TG-Radar 管理菜单</b>
<i>请直接发送以下指令进行系统管理：</i>

<b>📊 运行状态查看</b>
<code>{prefix}status</code> - 详细的系统监控大屏
<code>{prefix}ping</code>   - 简单的系统心跳测试
<code>{prefix}log 30</code> - 查看最近 30 条运行日志
<code>{prefix}version</code> - 查看当前版本与架构

<b>📂 监听分组管理</b>
<code>{prefix}folders</code> - 查看当前有几个分组、分别监控了多少群
<code>{prefix}enable [名称]</code> - 开启某个分组的监控
<code>{prefix}disable [名称]</code> - 关闭某个分组的监控
<code>{prefix}rules [名称]</code> - 查看某个分组里加了什么监控词

<b>🛡️ 监控词管理</b>
<code>{prefix}addrule [分组名] [规则名] [关键词]</code>
（例如：<code>{prefix}addrule 业务群 核心词 苹果 华为</code>，直接用空格分隔关键词即可）
<code>{prefix}delrule [分组名] [规则名] [要删的词]</code>

<b>🔀 智能路由配置</b>
<code>{prefix}routes</code> - 查看现有的自动收纳规则
<code>{prefix}addroute [分组名] [群名匹配词]</code>
<code>{prefix}delroute [分组名]</code>

<b>🔧 系统维护指令</b>
<code>{prefix}sync</code> - 强制执行一次全盘数据比对与同步
<code>{prefix}config</code> - 查看关键配置
<code>{prefix}setnotify [ID/off]</code> - 修改系统通知频道
<code>{prefix}setalert [ID/off]</code> - 修改默认告警频道
<code>{prefix}setprefix [新前缀]</code> - 修改控制前缀
<code>{prefix}update</code> - git pull 更新代码并重启
<code>{prefix}restart</code> - 重启系统进程

<i>(提示：为了防止面板刷屏，本条消息会在 45 秒后自动删除)</i>""",
                auto_delete=45,
            )
            return

        if command == "ping":
            stats = self.db.get_runtime_stats()
            total_hits = stats.get("total_hits", "0")
            await self.safe_reply(
                event,
                f"⚡ <b>系统运行正常</b> | 已经运行了: <code>{html.escape(format_duration((datetime.now() - self.started_at).total_seconds()))}</code> | 历史总计拦截: <code>{html.escape(total_hits)}</code> 次",
                auto_delete=10,
            )
            return

        if command == "status":
            await self.safe_reply(event, self.render_status_message(), auto_delete=35)
            return

        if command == "version":
            await self.safe_reply(
                event,
                f"""🧩 <b>版本信息</b>

· 版本号：<code>{__version__}</code>
· 架构：<code>Plan C / Admin + Core / SQLite WAL</code>
· 热更新：<code>通过 revision watcher 自动生效</code>
· 自动同步：<code>后台轮询 + 手动强制 sync</code>""",
                auto_delete=30,
            )
            return

        if command == "config":
            cfg_text = (
                f"API_ID：<code>{self.config.api_id}</code>\n"
                f"默认告警频道：<code>{self.config.global_alert_channel_id if self.config.global_alert_channel_id is not None else '未设置'}</code>\n"
                f"系统通知频道：<code>{self.config.notify_channel_id if self.config.notify_channel_id is not None else 'Saved Messages'}</code>\n"
                f"命令前缀：<code>{html.escape(self.config.cmd_prefix)}</code>\n"
                f"同步间隔：<code>{self.config.sync_interval_seconds} 秒</code>\n"
                f"热更新轮询：<code>{self.config.revision_poll_seconds} 秒</code>"
            )
            await self.safe_reply(event, f"🧾 <b>关键配置</b>\n<blockquote>{cfg_text}</blockquote>", auto_delete=40)
            return

        if command == "setnotify":
            value = self.parse_int_or_none(args)
            update_config_data(self.config.work_dir, {"notify_channel_id": value})
            self.config = load_config(self.config.work_dir)
            self.db.log_event("INFO", "SET_NOTIFY", str(value))
            await self.safe_reply(event, f"✅ <b>系统通知频道已更新</b>\n<code>{value if value is not None else 'Saved Messages'}</code>")
            return

        if command == "setalert":
            value = self.parse_int_or_none(args)
            update_config_data(self.config.work_dir, {"global_alert_channel_id": value})
            self.config = load_config(self.config.work_dir)
            self.db.log_event("INFO", "SET_ALERT", str(value))
            await self.safe_reply(event, f"✅ <b>默认告警频道已更新</b>\n<code>{value if value is not None else '未设置'}</code>")
            return

        if command == "setprefix":
            value = args.strip()
            if not value or len(value) > 3 or " " in value:
                await self.safe_reply(event, "⚠️ <b>前缀格式无效</b>\n建议 1-3 个字符，且不能包含空格。")
                return
            update_config_data(self.config.work_dir, {"cmd_prefix": value})
            self.db.log_event("INFO", "SET_PREFIX", value)
            self.write_last_message(event.id, "restart")
            await self.safe_reply(event, f"✅ <b>命令前缀已更新</b>：<code>{html.escape(value)}</code>\n<i>即将重启 admin/core 使前缀生效。</i>", auto_delete=0)
            self.restart_services(delay=1.2)
            return

        if command == "log":
            limit = 30
            if args.isdigit():
                limit = min(200, max(1, int(args)))
            rows = self.db.recent_logs(limit)
            if not rows:
                await self.safe_reply(event, "📋 <b>暂无操作日志</b>")
                return
            body = "\n".join(
                f"[{row['created_at']}] {row['level']}/{row['action']} :: {row['detail']}"
                for row in rows
            )
            await self.safe_reply(event, f"📋 <b>最近 {len(rows)} 条运行日志</b>\n<blockquote expandable>{html.escape(body)}</blockquote>", auto_delete=60)
            return

        if command == "folders":
            rows = self.db.list_folders()
            if not rows:
                await self.safe_reply(event, "📂 <b>当前没有任何分组记录</b>")
                return
            blocks = []
            for row in rows:
                folder_name = row["folder_name"]
                group_count = self.db.count_cache_for_folder(folder_name)
                rule_count = self.db.count_rules_for_folder(folder_name)
                icon = "🟢" if int(row["enabled"]) == 1 else "⚪"
                blocks.append(f"{icon} <b>{html.escape(folder_name)}</b>\n  └ 群组 <code>{group_count}</code> · 规则 <code>{rule_count}</code>")
            await self.safe_reply(event, f"📂 <b>分组总览</b>\n\n" + "\n\n".join(blocks), auto_delete=60)
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
            if not rows:
                body = "<i>该分组还没有规则</i>"
            else:
                body = "\n".join(f"· <b>{html.escape(row['rule_name'])}</b>\n  <code>{html.escape(row['pattern'])}</code>" for row in rows)
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
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "TOGGLE_FOLDER", f"{folder} -> {command}")
            word = "开启" if command == "enable" else "关闭"
            await self.safe_reply(event, f"✅ <b>已{word}分组</b>：<code>{html.escape(folder)}</code>\n\n<i>✅ 设置已自动生效，无需重启。</i>")
            return

        if command == "addrule":
            tokens = shlex.split(args)
            if len(tokens) < 3:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}addrule 业务群 核心词 苹果 华为</code>")
                return
            folder = self.find_folder(tokens[0])
            if folder is None:
                await self.safe_reply(event, "⚠️ <b>找不到该分组</b>")
                return
            rule_name = tokens[1]
            pattern = normalize_pattern_from_terms(" ".join(tokens[2:]))
            self.db.upsert_rule(folder, rule_name, pattern)
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "ADD_RULE", f"{folder}/{rule_name} -> {pattern}")
            await self.safe_reply(event, f"✅ <b>规则已保存</b>\n分组：<code>{html.escape(folder)}</code>\n规则：<code>{html.escape(rule_name)}</code>\n表达式：<code>{html.escape(pattern)}</code>\n\n<i>✅ 设置已自动生效，无需重启。</i>")
            return

        if command == "delrule":
            tokens = shlex.split(args)
            if len(tokens) < 2:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}delrule 业务群 核心词 [要删的词]</code>")
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
                sync_snapshot_to_config(self.config.work_dir, self.db)
                self.db.log_event("INFO", "DELETE_RULE", f"{folder}/{rule_name}")
                await self.safe_reply(event, f"🗑️ <b>已删除规则</b>：<code>{html.escape(folder)} / {html.escape(rule_name)}</code>\n\n<i>✅ 设置已自动生效，无需重启。</i>")
                return
            current_rows = self.db.get_rules_for_folder(folder)
            current = next((row for row in current_rows if row["rule_name"] == rule_name), None)
            if current is None:
                await self.safe_reply(event, "⚠️ <b>没有找到这条规则</b>")
                return
            new_pattern = try_remove_terms_from_pattern(str(current["pattern"]), tokens[2:])
            if new_pattern is None:
                self.db.delete_rule(folder, rule_name)
                sync_snapshot_to_config(self.config.work_dir, self.db)
                self.db.log_event("INFO", "DELETE_RULE", f"{folder}/{rule_name} -> removed all terms")
                await self.safe_reply(event, f"🗑️ <b>规则已删除</b>：<code>{html.escape(folder)} / {html.escape(rule_name)}</code>\n\n<i>✅ 设置已自动生效，无需重启。</i>")
                return
            self.db.update_rule_pattern(folder, rule_name, new_pattern)
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "UPDATE_RULE", f"{folder}/{rule_name} -> {new_pattern}")
            await self.safe_reply(event, f"✅ <b>规则已更新</b>\n新表达式：<code>{html.escape(new_pattern)}</code>\n\n<i>✅ 设置已自动生效，无需重启。</i>")
            return

        if command == "routes":
            rows = self.db.list_routes()
            if not rows:
                await self.safe_reply(event, "🔀 <b>当前没有自动收纳规则</b>")
                return
            body = "\n".join(f"· <b>{html.escape(row['folder_name'])}</b>\n  <code>{html.escape(row['pattern'])}</code>" for row in rows)
            await self.safe_reply(event, f"🔀 <b>自动收纳规则</b>\n<blockquote>{body}</blockquote>", auto_delete=60)
            return

        if command == "addroute":
            tokens = shlex.split(args)
            if len(tokens) < 2:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}addroute 业务群 供需 担保</code>")
                return
            folder = self.find_folder(tokens[0]) or tokens[0]
            if self.db.get_folder(folder) is None:
                self.db.upsert_folder(folder, None, enabled=False)
                self.db.upsert_rule(folder, f"{folder}监控", "(示范词A|示范词B)")
            pattern = normalize_pattern_from_terms(" ".join(tokens[1:]))
            self.db.set_route(folder, pattern)
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "ADD_ROUTE", f"{folder} -> {pattern}")
            await self.safe_reply(event, f"✅ <b>自动收纳规则已保存</b>\n分组：<code>{html.escape(folder)}</code>\n表达式：<code>{html.escape(pattern)}</code>\n\n<i>后续自动同步会持续检查并补充新群。</i>")
            return

        if command == "delroute":
            if not args:
                await self.safe_reply(event, f"⚠️ <b>参数不足</b>\n示例：<code>{prefix}delroute 业务群</code>")
                return
            folder = self.find_folder(args) or args.strip()
            if not self.db.delete_route(folder):
                await self.safe_reply(event, "⚠️ <b>没有找到该自动收纳规则</b>")
                return
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "DELETE_ROUTE", folder)
            await self.safe_reply(event, f"🗑️ <b>自动收纳规则已删除</b>：<code>{html.escape(folder)}</code>")
            return

        if command == "sync":
            await self.run_sync_command(event)
            return

        if command == "restart":
            self.write_last_message(event.id, "restart")
            await self.safe_reply(event, "🔄 <b>系统即将重启...</b>\n<i>(未完路由任务仍会留在数据库里，重启后自动接管)</i>", auto_delete=0)
            self.db.log_event("INFO", "RESTART", "restart requested from Telegram")
            self.restart_services(delay=1.2)
            return

        if command == "update":
            self.write_last_message(event.id, "update")
            await self.run_update_command(event)
            return

        await self.safe_reply(event, f"⚠️ <b>未知命令</b>\n请发送 <code>{prefix}help</code> 查看可用指令")

    async def run_sync_command(self, event: events.NewMessage.Event) -> None:
        if self.sync_lock.locked():
            await self.safe_reply(event, "⚠️ <b>系统正忙</b>\n后台正在执行其他同步任务，请稍等一两秒后再试。")
            return
        async with self.sync_lock:
            await self.safe_reply(event, "⏳ <b>系统正在扫描全局差异...</b>", auto_delete=0)
            sync_report = await sync_dialog_folders(self.client, self.db)
            route_report = await scan_auto_routes(self.client, self.db)
            sync_snapshot_to_config(self.config.work_dir, self.db)
            self.db.log_event("INFO", "SYNC", f"sync changed={sync_report.has_changes} queued={sum(route_report.queued.values())}")
            await self.safe_reply(event, self.render_sync_message(sync_report, route_report), auto_delete=35)

    async def run_update_command(self, event: events.NewMessage.Event) -> None:
        if not (self.config.work_dir / ".git").exists():
            await self.safe_reply(event, "⚠️ <b>当前目录不是 git 仓库</b>，请使用 git 部署后再执行 update")
            return
        await self.safe_reply(event, "🔄 <b>正在执行 git pull --ff-only ...</b>", auto_delete=0)
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
        await self.safe_reply(event, f"✅ <b>代码已更新</b>\n<blockquote expandable>{html.escape(output or 'Already up to date.')}</blockquote>\n<i>即将重启服务使新代码生效。</i>", auto_delete=0)
        self.restart_services(delay=1.5)

    async def periodic_sync(self) -> None:
        await asyncio.sleep(5)
        while not self.stop_event.is_set():
            if not self.sync_lock.locked():
                try:
                    async with self.sync_lock:
                        sync_report = await sync_dialog_folders(self.client, self.db)
                        route_report = await scan_auto_routes(self.client, self.db)
                        sync_snapshot_to_config(self.config.work_dir, self.db)
                        self.db.log_event("INFO", "AUTO_SYNC", f"sync changed={sync_report.has_changes} queued={sum(route_report.queued.values())}")
                        if sync_report.has_changes or route_report.queued or route_report.created:
                            await self.send_sync_report(sync_report, route_report, automatic=True)
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
                sync_snapshot_to_config(self.config.work_dir, self.db)
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

    async def send_sync_report(self, sync_report: SyncReport, route_report: RouteReport, automatic: bool = False) -> None:
        assert self.client is not None
        target = self.config.notify_channel_id if self.config.notify_channel_id is not None else "me"
        title = "🔄 <b>TG 最新数据同步报告</b>" if automatic else "🔄 <b>TG 手动同步报告</b>"
        lines = [title, ""]
        status = "🔔 发现变动并已更新" if sync_report.has_changes or route_report.queued or route_report.created else "✅ 同步完成，数据无变动"
        lines += [
            "<b>⚙️ 同步执行概况</b>",
            f"· 执行结果：<code>{status}</code>",
            f"· 耗费时间：<code>{sync_report.elapsed_seconds:.1f} 秒</code>",
            f"· 同步时间：<code>{datetime.now().strftime('%m-%d %H:%M:%S')}</code>",
            "",
            "<b>🔄 分组变动详情</b>",
        ]
        if sync_report.discovered:
            lines += [f"· ✨ <b>发现新分组</b>: <code>{html.escape(name)}</code>" for name in sync_report.discovered]
        if sync_report.renamed:
            lines += [f"· 🔄 <b>分组已改名</b>: <code>{html.escape(old)}</code> -> <code>{html.escape(new)}</code>" for old, new in sync_report.renamed]
        if sync_report.deleted:
            lines += [f"· 🗑️ <b>删除了分组</b>: <code>{html.escape(name)}</code>" for name in sync_report.deleted]
        if not (sync_report.discovered or sync_report.renamed or sync_report.deleted):
            lines.append("· <i>(本次同步没有增删改任何分组)</i>")

        if route_report.created or route_report.queued or route_report.matched_zero:
            lines += ["", "<b>🔀 自动收纳扫描</b>"]
            for name in route_report.created:
                lines.append(f"· ✨ <b>自动新建了分组</b>: <code>{html.escape(name)}</code>")
            for name, cnt in route_report.queued.items():
                lines.append(f"· ⏳ <b>已排队补充群组</b>: <code>{html.escape(name)}</code> + <code>{cnt}</code>")
            for name in route_report.matched_zero:
                lines.append(f"· 🔕 <b>没有匹配到群</b>: <code>{html.escape(name)}</code>")

        lines += ["", "<b>🌐 当前存在的分组及群数</b>"]
        if sync_report.active:
            for name, cnt in sync_report.active.items():
                lines.append(f"· 🟢 <b>{html.escape(name)}</b> (已收纳 <code>{cnt}</code> 个群)")
        else:
            lines.append("· <i>(当前没有读取到任何分组的群组数据)</i>")
        lines += [
            "",
            f"💡 <i>提示: 如果发现了新的分组，记得发送 <code>{html.escape(self.config.cmd_prefix)}enable [分组名]</code> 开启监控。</i>"
        ]
        try:
            await self.client.send_message(target, "\n".join(lines), link_preview=False)
        except Exception as exc:
            self.logger.warning("send sync report failed: %s", exc)

    async def send_startup_notification(self) -> None:
        assert self.client is not None
        rows = self.db.list_folders()
        enabled = [row for row in rows if int(row["enabled"]) == 1]
        folder_lines = []
        for row in enabled:
            folder_name = row["folder_name"]
            folder_lines.append(
                f"🟢 <b>{html.escape(folder_name)}</b> (监听了 {self.db.count_cache_for_folder(folder_name)} 个群, 包含 {self.db.count_rules_for_folder(folder_name)} 条规则)"
            )
        folder_block = "\n".join(folder_lines) if folder_lines else "<i>(当前没有开启任何分组的监控)</i>"
        route_rows = self.db.list_routes()
        route_block = "\n".join(
            f"🔀 将名含 <code>{html.escape(row['pattern'])}</code> 的群拉入 <code>{html.escape(row['folder_name'])}</code>"
            for row in route_rows
        ) if route_rows else "<i>(当前没有设置自动路由)</i>"

        msg = f"""📊 <b>TG-Radar 监控系统已上线</b>

<b>⚙️ 运行概况</b>
· 进程架构：<code>Admin + Core / SQLite WAL</code>
· 启动时间：<code>{self.started_at.strftime('%Y-%m-%d %H:%M:%S')}</code>
· 自动同步：<code>每 {self.config.sync_interval_seconds} 秒轮询一次</code>
· 版本：<code>{__version__}</code>

<b>🌐 监控规模</b>
· 活跃分组：<code>{len(enabled)}</code> 个 (系统共记录 {len(rows)} 个)
· 正在监听：<code>{len(self.db.build_target_map(self.config.global_alert_channel_id)[0])}</code> 个活跃群组/频道
· 生效规则：<code>{self.db.build_target_map(self.config.global_alert_channel_id)[1]}</code> 条监控策略

<b>[ 正在监控的分组 ]</b>
<blockquote>{folder_block}</blockquote>

<b>[ 自动路由配置 ]</b>
<blockquote>{route_block}</blockquote>

💡 <i>需要管理系统？请发送 <code>{html.escape(self.config.cmd_prefix)}help</code> 查看所有指令。</i>"""

        last_msg_path = self.config.work_dir / ".last_msg"
        target = self.config.notify_channel_id if self.config.notify_channel_id is not None else "me"
        msg_obj = None

        if last_msg_path.exists():
            try:
                ctx = __import__("json").loads(last_msg_path.read_text(encoding="utf-8"))
                action = ctx.get("action", "restart")
                prefix_text = "✨ <b>[ 代码更新完毕 ]</b> 系统已加载最新版本。\n\n" if action == "update" else "🔄 <b>[ 重启任务完毕 ]</b> 系统进程已恢复。\n\n"
                msg_obj = await self.client.edit_message("me", int(ctx["msg_id"]), prefix_text + msg)
                last_msg_path.unlink(missing_ok=True)
                self.db.log_event("INFO", "RESTORE", f"system back online after {action}")
            except Exception:
                pass

        if msg_obj is None:
            try:
                msg_obj = await self.client.send_message(target, msg, link_preview=False)
            except Exception as exc:
                self.logger.warning("startup notification failed: %s", exc)

        if msg_obj:
            asyncio.create_task(self.delete_later(msg_obj, 60))

    async def delete_later(self, msg, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except Exception:
            pass

    async def safe_reply(self, event: events.NewMessage.Event, text: str, auto_delete: int = 20) -> None:
        msg = None
        try:
            msg = await event.edit(text)
        except Exception:
            msg = await event.reply(text)
        if msg and auto_delete > 0:
            asyncio.create_task(self.delete_later(msg, auto_delete))

    def render_status_message(self) -> str:
        stats = self.db.get_runtime_stats()
        last_folder = stats.get("last_hit_folder") or "暂无记录"
        last_time = stats.get("last_hit_time") or "暂无记录"
        rows = self.db.list_folders()
        enabled_cnt = sum(1 for row in rows if int(row["enabled"]) == 1)
        target_map, valid_rules = self.db.build_target_map(self.config.global_alert_channel_id)
        queue_size = self.db.pending_route_count()
        q_info = f"有 {queue_size} 个后台补充任务正在缓慢执行中" if queue_size > 0 else "全部执行完毕 (当前空闲)"
        return f"""📊 <b>TG-Radar 详细监控大屏</b>

<b>⚙️ 核心运行状态</b>
· 系统状态：<code>🟢 稳定监控中</code>
· 持续运行：<code>{html.escape(format_duration((datetime.now() - self.started_at).total_seconds()))}</code>
· 自动同步：<code>每 {self.config.sync_interval_seconds} 秒</code>

<b>🌐 当前监控规模</b>
· 活跃分组：<code>{enabled_cnt}</code> 个 (系统共记录 {len(rows)} 个)
· 正在监听：<code>{len(target_map)}</code> 个活跃群组/频道
· 生效规则：<code>{valid_rules}</code> 条
· 队列任务：<code>{html.escape(q_info)}</code>

<b>🎯 历史命中统计</b>
· 总计拦截：<code>{html.escape(stats.get('total_hits', '0'))}</code> 次
· 最近命中：<code>{html.escape(last_folder)}</code>
· 最近时间：<code>{html.escape(last_time)}</code>"""

    def render_sync_message(self, sync_report: SyncReport, route_report: RouteReport) -> str:
        msg = f"✅ <b>TG 最新数据已核准完毕</b>\n\n"
        msg += f"· 分组同步结果：<code>{'发现变动并已更新' if sync_report.has_changes else '数据无变动'}</code>\n"
        msg += f"· 耗费时间：<code>{sync_report.elapsed_seconds:.1f} 秒</code>\n"
        msg += f"· 新分组：<code>{len(sync_report.discovered)}</code> 个 | 改名：<code>{len(sync_report.renamed)}</code> 个 | 删除：<code>{len(sync_report.deleted)}</code> 个\n"
        if route_report.created or route_report.queued or route_report.matched_zero or route_report.errors:
            msg += "\n<b>[ 自动收纳任务扫描结果 ]</b>\n<blockquote>"
            for fn in route_report.created:
                msg += f"· {html.escape(fn)} : ✨ 为您自动新建了该分组\n"
            for fn, cnt in route_report.queued.items():
                msg += f"· {html.escape(fn)} : ⏳ 找到了 {cnt} 个缺失的群，已排队等待添加\n"
            for fn in route_report.matched_zero:
                msg += f"· {html.escape(fn)} : 🔕 没找到符合名字的群\n"
            for fn, err in route_report.errors.items():
                msg += f"· {html.escape(fn)} : ❌ {html.escape(err)}\n"
            msg += "</blockquote>\n"
            msg += "<i>(控制台已解除锁定，所有添加操作均在后台静默完成)</i>"
        return msg

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

    def parse_int_or_none(self, raw: str) -> int | None:
        raw = raw.strip()
        if raw.lower() in {"", "off", "none", "null", "me"}:
            return None
        return int(raw)

    def restart_services(self, delay: float = 0.0) -> None:
        cmd = [
            "bash",
            "-lc",
            f"sleep {delay}; systemctl restart {self.config.service_name_prefix}-core {self.config.service_name_prefix}-admin",
        ]
        subprocess.Popen(cmd)

    def write_last_message(self, msg_id: int, action: str) -> None:
        path = self.config.work_dir / ".last_msg"
        path.write_text(__import__("json").dumps({"chat_id": "me", "msg_id": msg_id, "action": action}, ensure_ascii=False), encoding="utf-8")


async def run(work_dir: Path) -> None:
    app = AdminApp(work_dir)
    await app.run()
