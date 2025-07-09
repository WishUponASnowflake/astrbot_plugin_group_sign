from pathlib import Path
from datetime import datetime, time, timedelta, timezone
import aiohttp
import aiofiles
import json
import asyncio
import os
from typing import List, Union, Optional, AsyncGenerator
from urllib.parse import urlparse
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api import logger

# ============= 可配置参数 =============
PLUGIN_ROOT = Path(__file__).parent
CONFIG = {
    "sign_time": time(0, 0, 5),  # 包含5秒延迟
    "timezone": 8,
    "storage_file": str(PLUGIN_ROOT / "group_sign_data.json"),
    "request_timeout": 10,
    "retry_delay": 60,
    "host":"192.168.1.50:3000"
}

class GroupSignPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.task: Optional[asyncio.Task] = None
        self.group_ids = None
        self.is_active = None 
        asyncio.create_task(self._async_init())
        self.base_url = "http://"+CONFIG["host"]+"/send_group_sign"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.debug_mode = False
        self._stop_event = asyncio.Event()
        self.timezone = timezone(timedelta(hours=CONFIG["timezone"]))
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _async_init(self):
        """异步初始化"""
        await self._load_config()
        if self.group_ids is None:
            self.group_ids = []
        if self.is_active is None:
            self.is_active = False
        logger.info(f"插件初始化完成，当前配置: {CONFIG}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建ClientSession"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _load_config(self):
        """异步加载配置（安全增强版）"""
        default_config = {
            "group_ids": [],
            "is_active": False  # 默认值建议与初始化一致
        }
        
        try:
            # 确保配置目录存在（异步友好方式）
            try:
                await asyncio.to_thread(os.makedirs, PLUGIN_ROOT, exist_ok=True)
            except Exception as e:
                logger.error(f"创建目录失败: {e}")
    
            # 异步读取配置文件
            if await asyncio.to_thread(os.path.exists, CONFIG["storage_file"]):
                async with aiofiles.open(CONFIG["storage_file"], mode='r', encoding='utf-8') as f:
                    try:
                        file_config = json.loads(await f.read())
                        # 安全更新属性（仅覆盖存在的配置项）
                        for key in default_config:
                            if key in file_config:
                                setattr(self, key, file_config[key])
                                logger.debug(f"从配置加载: {key}={file_config[key]}")
                    except json.JSONDecodeError as e:
                        logger.error(f"配置文件解析失败，使用默认值: {e}")
                        # 创建备份防止配置丢失
                        corrupted_file = f"{CONFIG['storage_file']}.corrupted"
                        await asyncio.to_thread(os.rename, CONFIG["storage_file"], corrupted_file)
                        logger.warning(f"已备份损坏文件到: {corrupted_file}")
        except Exception as e:
            logger.error(f"配置加载异常: {e}")
        
        # 确保所有关键属性都有值（双重保障）
        for key, default in default_config.items():
            if getattr(self, key, None) is None:
                setattr(self, key, default)
                logger.debug(f"设置默认值: {key}={default}")
    
        # 调试用日志
        logger.info(f"最终加载配置: group_ids={self.group_ids}, is_active={self.is_active}")
    

    async def _save_config(self):
        """异步保存配置"""
        try:
            os.makedirs(PLUGIN_ROOT, exist_ok=True)
            data = {
                "group_ids": self.group_ids,
                "is_active": self.is_active
            }
            async with aiofiles.open(CONFIG["storage_file"], "w", encoding="utf-8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            logger.info(f"配置已保存到: {CONFIG['storage_file']}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    async def _start_sign_task(self):
        """启动签到任务"""
        if self.is_active and (self.task is None or self.task.done()):
            self._stop_event.clear()
            self.task = asyncio.create_task(self._daily_sign_task())
            logger.info("自动签到任务已启动")

    def _get_local_time(self) -> datetime:
        return datetime.now(self.timezone)

    async def _send_sign_request(self, group_id: Union[str, int]) -> dict:
        """发送异步签到请求"""
        post_data = {"group_id": str(group_id)}
        logger.debug(f"发送签到请求到 {self.base_url}，数据: {json.dumps(post_data)}")
        
        try:
            session = await self._get_session()
            async with session.post(
                url=self.base_url,
                json=post_data,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=CONFIG["request_timeout"])
            ) as response:
                raw_content = await response.text()
                
                if response.status != 200:
                    error_msg = f"HTTP状态码异常: {response.status} {response.reason}"
                    return self._format_error_response(response, error_msg, raw_content)
                
                try:
                    json_data = json.loads(raw_content)
                    if not all(field in json_data for field in ["status", "retcode"]):
                        raise ValueError("缺少必要字段")
                        
                    return self._format_success_response(response, json_data)
                    
                except (json.JSONDecodeError, ValueError) as e:
                    error_msg = f"响应解析失败: {str(e)}"
                    return self._format_error_response(response, error_msg, raw_content)
                    
        except aiohttp.ClientError as e:
            error_msg = f"网络请求失败: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"请求处理异常: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _format_success_response(self, response, json_data):
        """格式化成功响应"""
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
        """格式化错误响应"""
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
        """并发签到所有群组"""
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
        """优化的每日定时任务"""
        logger.info("每日签到任务已启动")
        
        while not self._stop_event.is_set():
            try:
                now = self._get_local_time()
                target_time = now.replace(
                    hour=CONFIG["sign_time"].hour,
                    minute=CONFIG["sign_time"].minute,
                    second=CONFIG["sign_time"].second,
                    microsecond=0
                )
                
                if now >= target_time:
                    target_time += timedelta(days=1)
                
                wait_seconds = (target_time - now).total_seconds()
                if wait_seconds > 86400:
                    logger.warning(f"等待时间异常长: {wait_seconds}秒，重置为明天")
                    target_time = now.replace(
                        hour=CONFIG["sign_time"].hour,
                        minute=CONFIG["sign_time"].minute,
                        second=CONFIG["sign_time"].second,
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
                
                await asyncio.sleep(1)  # 防止CPU占用过高
                
            except Exception as e:
                logger.error(f"自动签到任务出错: {e}")
                await asyncio.sleep(CONFIG["retry_delay"])
    
    @filter.command("debug_sign")
    async def toggle_debug_mode(self, event: AstrMessageEvent, mode: str = None):
        """开启/关闭debug模式"""
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
        """启动自动签到服务"""
        try:
            # 如果有提供群号，则更新群号列表
            if group_ids:
                new_groups = []
                for gid in group_ids.split(","):
                    gid = gid.strip()
                    if gid:
                        try:
                            new_groups.append(int(gid))
                        except ValueError:
                            new_groups.append(gid)
                
                if not new_groups:
                    yield event.chain_result([Plain("❌ 提供的群号无效")])
                    return
                    
                self.group_ids = new_groups
            
            # 如果没有提供群号且当前没有配置群号
            elif not self.group_ids:
                yield event.chain_result([Plain("ℹ️ 当前没有配置任何群号，请先添加群号")])
                return
            
            # 更新状态并保存
            self.is_active = True
            self._save_config()
            
            # 启动任务
            await self._start_sign_task()
            
            next_run = (self._get_local_time().replace(
                hour=CONFIG["sign_time"].hour,
                minute=CONFIG["sign_time"].minute,
                second=CONFIG["sign_time"].second,
                microsecond=0
            ) + (timedelta(days=1) if self._get_local_time().time() > CONFIG["sign_time"] else timedelta(0)))
            
            message = [
                Plain("✅ 自动签到服务已启动\n"),
                Plain(f"⏰ 签到时间: 每天 {CONFIG['sign_time'].strftime('%H:%M')}\n"),
                Plain(f"👥 当前群号: {', '.join(map(str, self.group_ids)) if self.group_ids else '全局模式'}\n"),
                Plain(f"⏱ 下次执行: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
            ]
            yield event.chain_result(message)
            
        except Exception as e:
            logger.error(f"启动失败: {e}")
            yield event.chain_result([Plain(f"❌ 服务启动失败: {str(e)}")])
    
    @filter.command("sign_stop")
    async def stop_auto_sign(self, event: AstrMessageEvent):
        """停止自动签到服务"""
        if self.is_active:
            self._stop_event.set()
            self.is_active = False
            self._save_config()
            
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
        """查看签到服务状态"""
        status = "🟢 运行中" if self.is_active else "🔴 已停止"
        
        # 计算下次签到时间
        now = self._get_local_time()
        target_time = now.replace(
            hour=CONFIG["sign_time"].hour,
            minute=CONFIG["sign_time"].minute,
            second=CONFIG["sign_time"].second,
            microsecond=0
        )
        if now >= target_time:
            target_time += timedelta(days=1)
        wait_seconds = (target_time - now).total_seconds()
        
        # 确保所有群号都转为字符串
        group_ids_str = ', '.join(str(gid) for gid in self.group_ids) if self.group_ids else '无'
        
        message = [
            Plain(f"{status}\n"),
            Plain(f"⏰ 签到时间: 每天 {CONFIG['sign_time'].strftime('%H:%M:%S')} (UTC+{CONFIG['timezone']})\n"),
            Plain(f"🔗 目标URL: {self.base_url}\n"),
            Plain(f"👥 群号列表: {group_ids_str}\n"),
            Plain(f"⏱ 下次执行: {target_time.strftime('%Y-%m-%d %H:%M:%S')}\n"),
            Plain(f"⏳ 距离下次签到还有 {wait_seconds:.1f} 秒\n"),  # 这是修复后的正确写法
            Plain(f"🔧 Debug模式: {'开启' if self.debug_mode else '关闭'}")
        ]
        yield event.chain_result(message)
    
    

    @filter.command("sign_add")
    async def add_group(self, event: AstrMessageEvent, group_id: str):
        """添加群号到签到列表"""
        try:
            try:
                group_id = int(group_id)
            except ValueError:
                pass
                
            if group_id not in self.group_ids:
                self.group_ids.append(group_id)
                self._save_config()  # 改为调用 _save_config
                yield event.chain_result([Plain(
                    f"✅ 已添加群号: {group_id}\n"
                    f"👥 当前群号列表: {', '.join(map(str, self.group_ids))}"
                )])
            else:
                yield event.chain_result([Plain(f"ℹ️ 群号 {group_id} 已存在")])
        except Exception as e:
            yield event.chain_result([Plain(f"❌ 添加失败: {e}")])

    @filter.command("sign_remove")
    async def remove_group(self, event: AstrMessageEvent, group_id: str):
        """从签到列表中移除群号"""
        try:
            try:
                group_id = int(group_id)
            except ValueError:
                pass
                
            if group_id in self.group_ids:
                self.group_ids.remove(group_id)
                self._save_config()  # 改为调用 _save_config
                yield event.chain_result([Plain(
                    f"✅ 已移除群号: {group_id}\n"
                    f"👥 当前群号列表: {', '.join(map(str, self.group_ids)) if self.group_ids else '无'}"
                )])
            else:
                yield event.chain_result([Plain(f"ℹ️ 群号 {group_id} 不存在")])
        except Exception as e:
            yield event.chain_result([Plain(f"❌ 移除失败: {e}")])

    @filter.command("sign_now", aliases=["签到"])
    async def trigger_sign_now(self, event: AstrMessageEvent, group_ids: str = None):
        """立即执行签到（使用原生消息接口）"""
        try:
            # 1. 处理群号列表
            target_groups = []
            if group_ids:
                target_groups = [str(gid.strip()) for gid in group_ids.split(",") if gid.strip()]
            else:
                target_groups = [str(gid) for gid in self.group_ids]
            
            if not target_groups:
                yield event.chain_result([Plain("❌ 没有可用的群号配置")])
                return
    
            # 2. 开始处理提示
            yield event.chain_result([Plain("🔄 正在处理签到请求...")])
    
            # 3. 发送每个结果
            for group_id in target_groups:
                result = await self._send_sign_request(group_id)
                status = "✅ 成功" if result["success"] else f"❌ 失败: {result['message']}"
                yield event.chain_result([Plain(f"群 {group_id} 签到{status}")])
    
        except Exception as e:
            error_msg = f"❌ 处理异常: {str(e)}"
            logger.error(error_msg)
            yield event.chain_result([Plain(error_msg)])
    
    
    async def terminate(self):
        """异步清理资源"""
        if self.is_active:
            self._stop_event.set()
            self.is_active = False
            await self._save_config()
            
            if self.task and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                    
        if self._session and not self._session.closed:
            await self._session.close()
            
        logger.info("群签到插件已终止")