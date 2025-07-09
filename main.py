from pathlib import Path
from datetime import datetime, time, timedelta, timezone
import aiohttp
import json
import asyncio
import os
from typing import List, Union, Optional
from urllib.parse import urlparse
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
from astrbot.api import logger

# ============= 可配置参数 =============
PLUGIN_ROOT = Path(__file__).parent
CONFIG = {
    # 每日签到时间 (24小时制)
    "sign_time": time(0, 0, 5),  # 默认中午12点
    
    # 时区配置 (东八区为+8)
    "timezone": 8,
    
    # 持久化存储文件路径
    "storage_file": str(PLUGIN_ROOT / "group_sign_data.json")
}

# ============= 插件主类 =============
@register("group_sign", "mmyddd", "群签到插件", "1.1.0")
class GroupSignPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.task = None
        self.base_url = "http://192.168.1.50:3000/send_group_sign"
        self.group_ids = []  # 初始化空列表，将在_load_config中填充
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.is_active = False  # 初始状态，将在_load_config中覆盖
        self.debug_mode = False
        self._stop_event = asyncio.Event()
        
        # 初始化时区
        self.timezone = timezone(timedelta(hours=CONFIG["timezone"]))
        
        # 加载配置
        self._load_config()
        
        logger.info(f"插件初始化完成，当前配置: {CONFIG}")

    def _load_config(self):
        """从文件加载持久化的配置"""
        try:
            # 确保目录存在
            os.makedirs(PLUGIN_ROOT, exist_ok=True)
            
            if os.path.exists(CONFIG["storage_file"]):
                try:
                    with open(CONFIG["storage_file"], "r", encoding="utf-8") as f:
                        data = json.load(f)
                        # 确保只更新存在的字段，保留其他字段不变
                        if "group_ids" in data:
                            self.group_ids = data["group_ids"]
                        if "is_active" in data:  # 只有配置中存在时才更新
                            self.is_active = data["is_active"]
                        # 如果文件存在但缺少字段，不重置为默认值
                except json.JSONDecodeError:
                    logger.warning("配置文件格式错误，将使用默认值")
                    # 这里可以选择不重置is_active，或者创建备份
                    self._save_config()  # 重新保存当前配置
            else:
                # 文件不存在时才初始化默认值
                self.group_ids = []
                self.is_active = True
                self._save_config()
                
            logger.info(f"加载配置完成: group_ids={self.group_ids}, is_active={self.is_active}")
                
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            # 这里不再重置默认值，保持当前状态
    
    
    def _save_config(self):
        """保存配置到文件"""
        try:
            # 确保目录存在
            os.makedirs(PLUGIN_ROOT, exist_ok=True)
            
            data = {
                "group_ids": self.group_ids,
                "is_active": self.is_active
            }
            
            with open(CONFIG["storage_file"], "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存到: {CONFIG['storage_file']}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    async def _start_sign_task(self):
        """启动签到任务"""
        if self.is_active:
            self._stop_event.clear()
            self.task = asyncio.create_task(self._daily_sign_task())
            logger.info("自动签到任务已启动")

    def _get_local_time(self) -> datetime:
        """获取带时区的当前时间"""
        return datetime.now(self.timezone)
    
    
    async def _send_sign_request(self, group_id: Union[str, int]):
        """发送签到请求的核心方法"""
        try:
            post_data = {"group_id": str(group_id)}
            
            logger.debug(f"发送签到请求到 {self.base_url}，数据: {json.dumps(post_data)}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url=self.base_url,
                    json=post_data,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    raw_headers = "\n".join([f"{k}: {v}" for k, v in response.headers.items()])
                    raw_content = await response.text()
                    
                    if response.status != 200:
                        error_msg = f"HTTP状态码异常: {response.status} {response.reason}"
                        return {
                            "success": False,
                            "status_code": response.status,
                            "message": error_msg,
                            "raw_response": f"HTTP/{response.version.major}.{response.version.minor} {response.status} {response.reason}\n{raw_headers}\n\n{raw_content}" if self.debug_mode else None
                        }
                    
                    try:
                        json_data = json.loads(raw_content)
                        if not isinstance(json_data, dict):
                            raise ValueError("响应不是JSON对象")
                            
                        required_fields = ["status", "retcode"]
                        for field in required_fields:
                            if field not in json_data:
                                raise ValueError(f"缺少必要字段: {field}")
                                
                        formatted_response = (
                            f"HTTP/{response.version.major}.{response.version.minor} {response.status} {response.reason}\n"
                            f"{raw_headers}\n\n"
                            f"{json.dumps(json_data, indent=2, ensure_ascii=False)}"
                        )
                        
                        return {
                            "success": True,
                            "status_code": response.status,
                            "data": json_data,
                            "raw_response": formatted_response if self.debug_mode else None
                        }
                        
                    except (json.JSONDecodeError, ValueError) as e:
                        error_msg = f"响应解析失败: {str(e)}"
                        return {
                            "success": False,
                            "status_code": response.status,
                            "message": error_msg,
                            "raw_response": f"HTTP/{response.version.major}.{response.version.minor} {response.status} {response.reason}\n{raw_headers}\n\n{raw_content}" if self.debug_mode else None
                        }
        
        except aiohttp.ClientError as e:
            error_msg = f"网络请求失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "raw_response": None
            }
        except Exception as e:
            error_msg = f"请求处理异常: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "raw_response": None
            }

    async def _sign_all_groups(self) -> str:
        """为所有群号发送签到请求并返回结果"""
        if not self.group_ids:
            return "❌ 没有配置群号"
            
        results = []
        for group_id in self.group_ids:
            result = await self._send_sign_request(group_id)
            if result["success"]:
                msg = f"✅ 群 {group_id} 签到成功"
                if self.debug_mode and result.get("raw_response"):
                    msg += f"\n🔍 完整响应:\n{result['raw_response']}"
                results.append(msg)
            else:
                msg = f"❌ 群 {group_id} 签到失败: {result['message']}"
                if self.debug_mode and result.get("raw_response"):
                    msg += f"\n🔍 错误响应:\n{result['raw_response']}"
                results.append(msg)
        
        return "\n".join(results)

    async def _daily_sign_task(self):
        """每日定时签到任务"""
        logger.info("每日签到任务已启动")
        
        while not self._stop_event.is_set():
            try:
                now = self._get_local_time()
                
                # 计算今天的签到时间
                target_time = now.replace(
                    hour=CONFIG["sign_time"].hour,
                    minute=CONFIG["sign_time"].minute,
                    second=CONFIG["sign_time"].second,
                    microsecond=0
                )
                
                # 如果今天的时间已过，计算明天的时间
                if now >= target_time:
                    target_time += timedelta(days=1)
                
                wait_seconds = (target_time - now).total_seconds()
                logger.info(f"距离下次签到还有 {wait_seconds:.1f}秒 (将在 {target_time} 执行)")
                
                # 如果等待时间超过1天，可能是计算错误
                if wait_seconds > 86400:
                    logger.warning(f"等待时间异常长: {wait_seconds}秒，重置为明天")
                    target_time = now.replace(
                        hour=CONFIG["sign_time"].hour,
                        minute=CONFIG["sign_time"].minute,
                        second=CONFIG["sign_time"].second,
                        microsecond=0
                    ) + timedelta(days=1)
                    wait_seconds = (target_time - now).total_seconds()
                
                # 等待到目标时间或收到停止信号
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                    if self._stop_event.is_set():
                        break
                except asyncio.TimeoutError:
                    pass  # 正常到达目标时间
                
                # 执行签到
                logger.info("开始执行每日签到...")
                result = await self._sign_all_groups()
                logger.info(f"签到完成: {result}")
                
                # 短暂延迟防止CPU占用过高
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"自动签到任务出错: {e}")
                # 出错后等待一段时间再重试
                await asyncio.sleep(60)
    
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
        next_run = (self._get_local_time().replace(
            hour=CONFIG["sign_time"].hour,
            minute=CONFIG["sign_time"].minute,
            second=CONFIG["sign_time"].second,
            microsecond=0
        ) + (timedelta(days=1) if self._get_local_time().time() > CONFIG["sign_time"] else timedelta(0))).strftime('%Y-%m-%d %H:%M:%S')
        
        # 确保所有群号都转为字符串
        group_ids_str = ', '.join(str(gid) for gid in self.group_ids) if self.group_ids else '无'
        
        message = [
            Plain(f"{status}\n"),
            Plain(f"⏰ 签到时间: 每天 {CONFIG['sign_time'].strftime('%H:%M')} (UTC+{CONFIG['timezone']})\n"),
            Plain(f"🔗 目标URL: {self.base_url}\n"),
            Plain(f"👥 群号列表: {group_ids_str}\n"),
            Plain(f"⏱ 下次执行: {next_run}\n"),
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
        """插件终止时清理资源"""
        if self.is_active:
            self._stop_event.set()
            self.is_active = False
            self._save_config()  # 终止前保存状态
            
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
        logger.info("群签到插件已终止")