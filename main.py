from pathlib import Path
import aiohttp
import json
import asyncio
import os
from datetime import datetime, time, timedelta
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
    "sign_time": time(0, 0),  # 默认中午12点
    
    # 时区配置 (东八区为+8)
    "timezone": 8,
    
    # 结果转发群号 (None表示不转发)
    "report_group_id": 838116737,  # 例如 "123456789"
    
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
        self.group_ids = self._load_group_ids()  # 从文件加载群号
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.is_active = False
        self.debug_mode = False
        self._stop_event = asyncio.Event()
        
        # 初始化时区
        self.timezone = timedelta(hours=CONFIG["timezone"])
        logger.info(f"插件初始化完成，当前配置: {CONFIG}")

    def _load_group_ids(self) -> List[Union[str, int]]:
        """从文件加载持久化的群号列表"""
        try:
            # 确保目录存在
            os.makedirs(PLUGIN_ROOT, exist_ok=True)
            
            if os.path.exists(CONFIG["storage_file"]):
                with open(CONFIG["storage_file"], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("group_ids", [])
            else:
                # 文件不存在时创建空文件
                with open(CONFIG["storage_file"], "w", encoding="utf-8") as f:
                    json.dump({"group_ids": []}, f)
                return []
        except Exception as e:
            logger.error(f"加载群号列表失败: {e}")
            return []
    
    def _save_group_ids(self):
        """保存群号列表到文件"""
        try:
            # 确保目录存在
            os.makedirs(PLUGIN_ROOT, exist_ok=True)
            
            with open(CONFIG["storage_file"], "w", encoding="utf-8") as f:
                json.dump({"group_ids": self.group_ids}, f, ensure_ascii=False, indent=2)
            logger.info(f"群号列表已保存到: {CONFIG['storage_file']}")
        except Exception as e:
            logger.error(f"保存群号列表失败: {e}")
    

    def _get_local_time(self) -> datetime:
        """获取带时区的当前时间"""
        return datetime.utcnow() + self.timezone

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

    async def _send_to_target_group(self, message: str):
        """直接发送消息到配置的目标群"""
        if not CONFIG["report_group_id"]:
            return
    
        try:
            # 使用最基础的API发送消息
            target_group = str(CONFIG["report_group_id"])
            await self.context.bot.send_group_msg(
                group_id=target_group,
                message=message
            )
        except Exception as e:
            logger.error(f"发送消息到群 {target_group} 失败: {e}")
    
    

    async def _daily_sign_task(self):
        """每日定时签到任务"""
        while not self._stop_event.is_set():
            now = self._get_local_time()
            target_time = now.replace(
                hour=CONFIG["sign_time"].hour,
                minute=CONFIG["sign_time"].minute,
                second=0,
                microsecond=0
            )
        
            if now > target_time:
                target_time += timedelta(days=1)

            wait_seconds = (target_time - now).total_seconds()
            logger.info(f"等待下次签到时间: {wait_seconds:.1f}秒")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                if self._stop_event.is_set():
                   break
                   
                logger.info("开始执行每日签到...")
                for group_id in self.group_ids:
                    result = await self._send_sign_request(group_id)
                    report_msg = (f"⏰ 每日签到\n群 {group_id}: " + 
                                ("成功" if result["success"] else f"失败 - {result['message']}"))
                await self._send_to_target_group(report_msg)

                await asyncio.sleep(1)

            except asyncio.TimeoutError:
               continue
            except Exception as e:
                logger.error(f"自动签到出错: {e}")
            await asyncio.sleep(300)
    
    

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
        """启动自动签到服务
        
        参数:
            group_ids - (可选)群号列表，用逗号分隔。不提供则使用已有群号或全局设置
        """
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
                self._save_group_ids()
            # 如果没有提供群号且当前没有配置群号
            elif not self.group_ids:
                yield event.chain_result([Plain("ℹ️ 当前没有配置任何群号，请先添加群号")])
                return
            
            # 启动/重启任务
            if self.is_active:
                self._stop_event.set()
                await asyncio.sleep(1)  # 等待任务停止
                
            self._stop_event.clear()
            self.task = asyncio.create_task(self._daily_sign_task())
            self.is_active = True
            
            next_run = (self._get_local_time().replace(
                hour=CONFIG["sign_time"].hour,
                minute=CONFIG["sign_time"].minute,
                second=0,
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
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
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
            second=0,
            microsecond=0
        ) + (timedelta(days=1) if self._get_local_time().time() > CONFIG["sign_time"] else timedelta(0))).strftime('%Y-%m-%d %H:%M:%S')
        
        # 确保所有群号都转为字符串
        group_ids_str = ', '.join(str(gid) for gid in self.group_ids) if self.group_ids else '无'
        
        # 确保转发群号是字符串
        report_group = f"群 {CONFIG['report_group_id']}" if CONFIG['report_group_id'] else '不转发'
        if CONFIG['report_group_id'] and isinstance(CONFIG['report_group_id'], int):
            report_group = f"群 {str(CONFIG['report_group_id'])}"
        
        message = [
            Plain(f"{status}\n"),
            Plain(f"⏰ 签到时间: 每天 {CONFIG['sign_time'].strftime('%H:%M')} (UTC+{CONFIG['timezone']})\n"),
            Plain(f"🔗 目标URL: {self.base_url}\n"),
            Plain(f"👥 群号列表: {group_ids_str}\n"),
            Plain(f"📨 结果转发: {report_group}\n"),
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
                self._save_group_ids()  # 持久化保存
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
                self._save_group_ids()  # 持久化保存
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
    
            # 3. 收集所有结果
            all_results = []
            for group_id in target_groups:
                result = await self._send_sign_request(group_id)
                status = "✅ 成功" if result["success"] else f"❌ 失败: {result['message']}"
                result_msg = f"群 {group_id} 签到{status}"
                all_results.append(result_msg)
                
                # 立即返回每个结果
                yield event.chain_result([Plain(result_msg)])
    
            # 4. 合并发送完整结果（可选）
            full_report = "签到完成:\n" + "\n".join(all_results)
            yield event.chain_result([Plain(full_report)])
    
        except Exception as e:
            error_msg = f"❌ 处理异常: {str(e)}"
            logger.error(error_msg)
            yield event.chain_result([Plain(error_msg)])
    
    
    async def terminate(self):
        """插件终止时清理资源"""
        if self.is_active:
            self._stop_event.set()
            self.is_active = False
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
        logger.info("群签到插件已终止")
