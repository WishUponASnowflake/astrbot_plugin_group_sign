from pathlib import Path
from datetime import datetime, time, timedelta, timezone
import aiohttp
import aiofiles
import json
import asyncio
import os
from typing import List, Optional, Union
from urllib.parse import urlparse
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Plain
from astrbot.api import logger

# ============= 可配置参数 =============
CONFIG = {
    "timezone": 8,
    "request_timeout": 10,
    "retry_delay": 60,
    "host": "192.168.1.50:3000"
}

class GroupSignPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_data_dir = StarTools.get_data_dir()
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file = self.plugin_data_dir / "group_sign_data.json"
        
        self.task: Optional[asyncio.Task] = None
        self.group_ids: List[str] = []
        self.is_active = False
        self._stop_event = asyncio.Event()
        self.timezone = timezone(timedelta(hours=CONFIG["timezone"]))
        self._session: Optional[aiohttp.ClientSession] = None
        self.debug_mode = False
        self.sign_time: time = time(0, 45, 5)  # 初始化默认值
        
        self.base_url = f"http://{CONFIG['host']}/send_group_sign"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        asyncio.create_task(self._async_init())
    
    async def _async_init(self):
        await self._load_config()
        logger.info(
            f"初始化完成 | is_active={self.is_active} "
            f"来源={getattr(self, '_config_source', '未设置')}"
        )
        if self.is_active:
            await self._start_sign_task()

    def _get_next_run_time(self) -> datetime:
        now = self._get_local_time()
        target_time = now.replace(
            hour=self.sign_time.hour,
            minute=self.sign_time.minute,
            second=self.sign_time.second,
            microsecond=0
        )
        if now >= target_time:
            target_time += timedelta(days=1)
        return target_time

    async def _load_config(self):
        """异步加载配置文件，并自动补充 sign_time 字段"""
        default_values = {
            "group_ids": [],
            "is_active": False,
            "sign_time": time(0, 45, 5).strftime("%H:%M:%S")  # 用默认值
        }
        self._config_source = "default"
        need_save = False

        try:
            if not await asyncio.to_thread(os.path.exists, self.storage_file):
                logger.debug("配置文件不存在，使用默认值")
                for key, value in default_values.items():
                    if key == "sign_time":
                        self.sign_time = datetime.strptime(value, "%H:%M:%S").time()
                    else:
                        setattr(self, key, value)
                await self._save_config()  # 新建配置文件
                return True, "default"

            async with aiofiles.open(self.storage_file, 'r', encoding='utf-8') as f:
                try:
                    file_content = await f.read()
                    loaded_data = json.loads(file_content)
                    if not isinstance(loaded_data, dict):
                        raise ValueError("配置文件根节点不是一个JSON对象")
                    # 补充缺失字段
                    for key, value in default_values.items():
                        if key not in loaded_data:
                            loaded_data[key] = value
                            need_save = True
                    # 群号统一为字符串
                    if "group_ids" in loaded_data:
                        loaded_data["group_ids"] = [str(gid) for gid in loaded_data["group_ids"]]
                    # 赋值
                    for key in default_values:
                        if key == "sign_time":
                            try:
                                self.sign_time = datetime.strptime(loaded_data[key], "%H:%M:%S").time()
                            except Exception:
                                self.sign_time = time(0, 45, 5)
                                loaded_data[key] = time(0, 45, 5).strftime("%H:%M:%S")
                                need_save = True
                        else:
                            setattr(self, key, loaded_data[key])
                    self._config_source = "file"
                    if need_save:
                        async with aiofiles.open(self.storage_file, 'w', encoding='utf-8') as wf:
                            await wf.write(json.dumps(loaded_data, ensure_ascii=False, indent=2))
                        logger.info("配置文件已自动补充默认字段")
                    return True, "file"
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"配置文件解析失败: {e}")
                    corrupted_file = f"{self.storage_file}.corrupted"
                    await asyncio.to_thread(os.rename, self.storage_file, corrupted_file)
                    logger.warning(f"已备份损坏文件到: {corrupted_file}")
        except Exception as e:
            logger.error(f"加载配置异常: {str(e)}", exc_info=True)
        # 降级处理
        for key, value in default_values.items():
            if getattr(self, key, None) is None:
                if key == "sign_time":
                    self.sign_time = datetime.strptime(value, "%H:%M:%S").time()
                else:
                    setattr(self, key, value)
        logger.warning(f"使用默认配置 | is_active={self.is_active}")
        await self._save_config()
        return False, "default"

    async def _save_config(self) -> bool:
        temp_path = f"{self.storage_file}.tmp"
        data = {
            "group_ids": self.group_ids,
            "is_active": self.is_active,
            "sign_time": self.sign_time.strftime("%H:%M:%S")
        }
        try:
            async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            await asyncio.to_thread(os.replace, temp_path, self.storage_file)
            logger.info(f"配置已保存 | is_active={self.is_active}")
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            try:
                await asyncio.to_thread(os.unlink, temp_path)
            except:
                pass
            return False

    async def _start_sign_task(self):
        if self.is_active and (self.task is None or self.task.done()):
            self._stop_event.clear()
            self.task = asyncio.create_task(self._daily_sign_task())
            logger.info("自动签到任务已启动")

    def _get_local_time(self) -> datetime:
        return datetime.now(self.timezone)

    async def _send_sign_request(self, group_id: Union[str, int]) -> dict:
        post_data = {"group_id": str(group_id)}
        logger.debug(f"发送签到请求到 {self.base_url}，数据: {json.dumps(post_data)}")
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            logger.debug(f"准备发送请求到: {self.base_url}")
            logger.debug(f"请求头: {self.headers}")
            logger.debug(f"请求体: {post_data}")
            async with self._session.post(
                url=self.base_url,
                json=post_data,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=CONFIG["request_timeout"])
            ) as response:
                raw_content = await response.text()
                logger.debug(f"收到响应: {response.status} {raw_content}")
                if response.status != 200:
                    error_msg = f"HTTP状态码异常: {response.status} {response.reason}"
                    logger.error(f"{error_msg}, 响应内容: {raw_content}")
                    return self._format_error_response(response, error_msg, raw_content)
                try:
                    json_data = json.loads(raw_content)
                    logger.debug(f"解析后的JSON: {json_data}")
                    if not all(field in json_data for field in ["status", "retcode"]):
                        raise ValueError("响应缺少必要字段(status/retcode)")
                    return self._format_success_response(response, json_data)
                except (json.JSONDecodeError, ValueError) as e:
                    error_msg = f"响应解析失败: {str(e)}"
                    logger.error(f"{error_msg}, 原始响应: {raw_content}")
                    return self._format_error_response(response, error_msg, raw_content)
        except aiohttp.ClientError as e:
            error_msg = f"网络请求失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"请求处理异常: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"success": False, "message": error_msg}

    def _format_success_response(self, response, json_data):
        result = {
            "success": True,
            "status_code": response.status,
            "data": json_data
        }
        if self.debug_mode:
            headers_str = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
            result["raw_response"] = (
                f"HTTP/{response.version.major}.{response.version.minor} {response.status} {response.reason}\n"
                f"{headers_str}\n\n"
                f"{json.dumps(json_data, indent=2, ensure_ascii=False)}"
            )
        return result

    def _format_error_response(self, response, error_msg, raw_content):
        result = {
            "success": False,
            "status_code": response.status,
            "message": error_msg
        }
        if self.debug_mode:
            headers_str = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
            result["raw_response"] = (
                f"HTTP/{response.version.major}.{response.version.minor} {response.status} {response.reason}\n"
                f"{headers_str}\n\n"
                f"{raw_content}"
            )
        return result

    async def _sign_all_groups(self) -> str:
        if not self.group_ids:
            return "❌ 没有配置群号"
        tasks = [self._send_sign_request(group_id) for group_id in self.group_ids]
        results = await asyncio.gather(*tasks)
        messages = []
        for group_id, result in zip(self.group_ids, results):
            status = "✅ 成功" if result["success"] else f"❌ 失败: {result['message']}"
            msg = f"群 {group_id} 签到{status}"
            if self.debug_mode and result.get("raw_response"):
                msg += f"\n🔍 完整响应:\n{result['raw_response']}"
            messages.append(msg)
        return "\n".join(messages)

    async def _daily_sign_task(self):
        while not self._stop_event.is_set():
            try:
                now = self._get_local_time()
                target_time = now.replace(
                    hour=self.sign_time.hour,
                    minute=self.sign_time.minute,
                    second=self.sign_time.second,
                    microsecond=0
                )
                if now >= target_time:
                    target_time += timedelta(days=1)
                wait_seconds = (target_time - now).total_seconds()
                if wait_seconds > 86400:
                    logger.warning(f"等待时间异常长: {wait_seconds}秒，重置为明天")
                    target_time = now.replace(
                        hour=self.sign_time.hour,
                        minute=self.sign_time.minute,
                        second=self.sign_time.second,
                        microsecond=0
                    ) + timedelta(days=1)
                    wait_seconds = (target_time - now).total_seconds()
                logger.info(f"距离下次签到还有 {wait_seconds:.1f}秒 (将在 {target_time} 执行)")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                    if self._stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass
                logger.info("开始执行每日签到...")
                result = await self._sign_all_groups()
                logger.info(f"签到完成: {result}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"自动签到任务出错: {e}")
                await asyncio.sleep(CONFIG["retry_delay"])

    @filter.command("debug_sign")
    async def toggle_debug_mode(self, event: AstrMessageEvent, mode: str = None):
        if mode:
            if mode.lower() == "on":
                self.debug_mode = True
                yield event.chain_result([Plain("🔧 Debug模式已开启")])
            elif mode.lower() == "off":
                self.debug_mode = False
                yield event.chain_result([Plain("🔧 Debug模式已关闭")])
            else:
                yield event.chain_result([Plain("❌ 参数错误，请使用 on/off")])
        else:
            self.debug_mode = not self.debug_mode
            yield event.chain_result([Plain(f"🔧 Debug模式已{'开启' if self.debug_mode else '关闭'}")])

    @filter.command("sign_start")
    async def start_auto_sign(self, event: AstrMessageEvent, group_ids: str = None):
        try:
            if group_ids:
                new_groups = []
                for gid in group_ids.split(","):
                    gid = gid.strip()
                    if gid:
                        try:
                            new_groups.append(str(int(gid)))
                        except ValueError:
                            new_groups.append(gid)
                if not new_groups:
                    yield event.chain_result([Plain("❌ 提供的群号无效")])
                    return
                self.group_ids = new_groups
            elif not self.group_ids:
                yield event.chain_result([Plain("ℹ️ 当前没有配置任何群号，请先添加群号")])
                return
            self.is_active = True
            if not await self._save_config():
                yield event.chain_result([Plain("❌ 状态保存失败，请检查日志")])
                return
            await self._start_sign_task()
            next_run = (self._get_local_time().replace(
                hour=self.sign_time.hour,
                minute=self.sign_time.minute,
                second=self.sign_time.second,
                microsecond=0
            ) + (timedelta(days=1) if self._get_local_time().time() > self.sign_time else timedelta(0)))
            message = [
                Plain("✅ 自动签到服务已启动\n"),
                Plain(f"⏰ 签到时间: 每天 {self.sign_time.strftime('%H:%M')}\n"),
                Plain(f"👥 当前群号: {', '.join(map(str, self.group_ids)) if self.group_ids else '全局模式'}\n"),
                Plain(f"⏱ 下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
            ]
            yield event.chain_result(message)
        except Exception as e:
            logger.error(f"启动失败: {e}")
            yield event.chain_result([Plain(f"❌ 服务启动失败: {str(e)}")])

    @filter.command("sign_stop")
    async def stop_auto_sign(self, event: AstrMessageEvent):
        if self.is_active:
            self._stop_event.set()
            self.is_active = False
            await self._save_config()
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    logger.info("自动签到任务已取消")
                except Exception as e:
                    logger.error(f"取消任务时出错: {e}")
                finally:
                    self.task = None
            yield event.chain_result([Plain("🛑 已停止自动签到服务")])
        else:
            yield event.chain_result([Plain("ℹ️ 自动签到服务未在运行中")])

    @filter.command("sign_status")
    async def sign_status(self, event: AstrMessageEvent):
        status = "🟢 运行中" if self.is_active else "🔴 已停止"
        target_time = self._get_next_run_time()
        wait_seconds = (target_time - self._get_local_time()).total_seconds()
        group_ids_str = ', '.join(self.group_ids) if self.group_ids else '无'
        message = [
            Plain(f"{status}\n"),
            Plain(f"⏰ 签到时间: 每天 {self.sign_time.strftime('%H:%M:%S')} (UTC+{CONFIG['timezone']})\n"),
            Plain(f"🔗 目标URL: {self.base_url}\n"),
            Plain(f"👥 群号列表: {group_ids_str}\n"),
            Plain(f"⏱ 下次执行: {target_time.strftime('%Y-%m-%d %H:%M:%S')}\n"),
            Plain(f"⏳ 距离下次签到还有 {wait_seconds:.1f} 秒\n"),
            Plain(f"🔧 Debug模式: {'开启' if self.debug_mode else '关闭'}")
        ]
        yield event.chain_result(message)

    @filter.command("sign_add")
    async def add_group(self, event: AstrMessageEvent, group_id: str):
        try:
            group_id = group_id.strip()
            if group_id not in self.group_ids:
                self.group_ids.append(group_id)
                if not await self._save_config():
                    yield event.chain_result([Plain("❌ 保存配置失败")])
                    return
                yield event.chain_result([Plain(
                    f"✅ 已添加群号: {group_id}\n"
                    f"👥 当前群号列表: {', '.join(self.group_ids)}"
                )])
            else:
                yield event.chain_result([Plain(f"ℹ️ 群号 {group_id} 已存在")])
        except Exception as e:
            yield event.chain_result([Plain(f"❌ 添加失败: {e}")])

    @filter.command("sign_remove")
    async def remove_group(self, event: AstrMessageEvent, group_id: str):
        try:
            group_id = group_id.strip()
            if group_id in self.group_ids:
                self.group_ids.remove(group_id)
                if not await self._save_config():
                    yield event.chain_result([Plain("❌ 保存配置失败")])
                    return
                yield event.chain_result([Plain(
                    f"✅ 已移除群号: {group_id}\n"
                    f"👥 当前群号列表: {', '.join(self.group_ids) if self.group_ids else '无'}"
                )])
            else:
                yield event.chain_result([Plain(f"ℹ️ 群号 {group_id} 不存在")])
        except Exception as e:
            yield event.chain_result([Plain(f"❌ 移除失败: {e}")])

    @filter.command("sign_now", aliases=["签到"])
    async def trigger_sign_now(self, event: AstrMessageEvent, group_ids: str = None):
        try:
            logger.info(f"收到立即签到请求，参数: {group_ids}")
            target_groups = []
            if group_ids:
                target_groups = [str(gid.strip()) for gid in group_ids.split(",") if gid.strip()]
            else:
                target_groups = [str(gid) for gid in self.group_ids]
            if not target_groups:
                logger.warning("没有可用的群号配置")
                yield event.chain_result([Plain("❌ 没有可用的群号配置")])
                return
            logger.info(f"开始处理签到请求，目标群号: {target_groups}")
            yield event.chain_result([Plain("🔄 正在处理签到请求...")])
            for group_id in target_groups:
                result = await self._send_sign_request(group_id)
                status = "✅ 成功" if result["success"] else f"❌ 失败: {result['message']}"
                logger.info(f"群 {group_id} 签到结果: {status}")
                yield event.chain_result([Plain(f"群 {group_id} 签到{status}")])
        except Exception as e:
            error_msg = f"❌ 处理异常: {str(e)}"
            logger.error(error_msg, exc_info=True)
            yield event.chain_result([Plain(error_msg)])

    async def terminate(self):
        self._stop_event.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("自动签到插件已终止")