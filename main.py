from pathlib import Path
import os
import re
import base64
import asyncio
import shutil
from datetime import datetime
import traceback
from functools import partial
from gradio_client import Client

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.message_components import Node, Nodes, Plain, File, Record, Image as CompImage
from astrbot.core.utils.session_waiter import session_waiter, SessionController
from astrbot.api import logger

# 分隔符常量
MODEL_ALIAS_SEPARATOR = "|||"

# 人声分离模型标准名称（与 RVCSVC-API gradio 选项完全一致）
UVR_VR_HP5 = "VR-HP5 (默认)"
UVR_MSST_ROFORMER = "MSST BS-Roformer"
UVR_MSST_PIPELINE = "MSST 串联管线 (becruily_deux + 去混响)"

UVR_CHOICES_LIST = [
    UVR_VR_HP5,
    UVR_MSST_ROFORMER,
    UVR_MSST_PIPELINE,
]

# 常见别名映射表
UVR_ALIAS_MAP = {
    # UVR5 / VR-HP5 别名
    "1": UVR_VR_HP5,
    "uvr": UVR_VR_HP5,
    "uvr5": UVR_VR_HP5,
    "vr": UVR_VR_HP5,
    "hp5": UVR_VR_HP5,
    "vr-hp5": UVR_VR_HP5,
    "vr_hp5": UVR_VR_HP5,
    "默认": UVR_VR_HP5,
    UVR_VR_HP5.lower(): UVR_VR_HP5,

    # MSST 单模型别名
    "2": UVR_MSST_ROFORMER,
    "msst": UVR_MSST_ROFORMER,
    "roformer": UVR_MSST_ROFORMER,
    "bs-roformer": UVR_MSST_ROFORMER,
    "bs_roformer": UVR_MSST_ROFORMER,
    UVR_MSST_ROFORMER.lower(): UVR_MSST_ROFORMER,

    # MSST 串联管线别名
    "3": UVR_MSST_PIPELINE,
    "串联": UVR_MSST_PIPELINE,
    "串联管线": UVR_MSST_PIPELINE,
    "pipeline": UVR_MSST_PIPELINE,
    "becruily": UVR_MSST_PIPELINE,
    UVR_MSST_PIPELINE.lower(): UVR_MSST_PIPELINE,
}

@register(
    "astrbot_plugin_rvc_svc",
    "ABCwewe",
    "RVC/SVC翻唱网易云歌曲",
    "1.1.2",
    "https://github.com/ABCwewe/astrbot_plugin_rvc_svc",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # === 后端服务地址 ===
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:7860/")
        self.svc_base_url = config.get("svc_base_url", "http://127.0.0.1:7866/")
        
        # === 音乐 API 配置 ===
        self.default_api = config.get("default_api", "netease")
        self.nodejs_base_url = config.get("nodejs_base_url", "http://127.0.0.1:3000")
        self.timeout = config.get("timeout", 60)
        
        # === 模型列表配置 ===
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        self.svc_models_keywords = config.get("svc_models_keywords", [])
        
        self.inference_timeout = config.get("inference_timeout", 300)

        # === 人声分离与发送配置 ===
        self.uvr_model = self._normalize_uvr_model(config.get("uvr_model", UVR_VR_HP5))
        self.ask_uvr_model = config.get("ask_uvr_model", False)
        self.send_mode = config.get("send_mode", "record")
        
        if self.default_api == "netease":
            try:
                from .api import NetEaseMusicAPI
            except ImportError:
                from api import NetEaseMusicAPI
            self.api = NetEaseMusicAPI()
        elif self.default_api == "netease_nodejs":
            try:
                from .api import NetEaseMusicAPINodeJs
            except ImportError:
                from api import NetEaseMusicAPINodeJs
            self.api = NetEaseMusicAPINodeJs(base_url=self.nodejs_base_url)

    def _normalize_uvr_model(self, user_val: str) -> str:
        """将用户输入或配置中的别名转换为标准模型名称"""
        if not user_val:
            return UVR_VR_HP5
        val_clean = str(user_val).strip().lower()
        if val_clean in UVR_ALIAS_MAP:
            return UVR_ALIAS_MAP[val_clean]
        for opt in UVR_CHOICES_LIST:
            if val_clean in opt.lower():
                return opt
        return UVR_VR_HP5

    async def _async_predict(self, client, *args, timeout=300, **kwargs):
        """将同步的 predict 变为异步，并接受超时参数"""
        loop = asyncio.get_running_loop()
        job = client.submit(*args, **kwargs)
        fn = partial(job.result, timeout=timeout)
        return await loop.run_in_executor(None, fn)

    def get_models_display_list(self, api_type="rvc"):
        """获取指定 API 类型的模型显示列表"""
        models_keywords = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = alias or os.path.splitext(model_name)[0]
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    async def _update_models_from_api(self, api_type="rvc"):
        """从指定的 API 更新模型列表"""
        base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
        client = Client(base_url)
        try:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")
        except Exception:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")

        if not isinstance(model_list_from_api, list):
            raise ValueError(f"获取模型列表失败: {model_list_from_api}")

        # 获取当前配置的模型列表
        current_config_list = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        old_aliases = {}
        for item_str in current_config_list:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [f"{m}{MODEL_ALIAS_SEPARATOR}{old_aliases.get(m, '')}" for m in model_list_from_api]
        
        # 保存模型列表
        if api_type == "svc":
            self.svc_models_keywords = new_models_list
            self.config["svc_models_keywords"] = new_models_list
        else:
            self.rvc_models_keywords = new_models_list
            self.config["rvc_models_keywords"] = new_models_list
        
        self.config.save_config()
        logger.info(f"{api_type.upper()} 模型列表已更新并成功保存，共 {len(new_models_list)} 个模型")

    # ==================== RVC 命令 ====================
    
    @filter.command("刷新rvc模型")
    async def refresh_rvc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 RVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="rvc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="rvc")
            display_str = display_str or "未发现任何模型。"
            chain = [Plain(f"当前 RVC 可用模型：\n{display_str}")]
            node = Node(
                uin=3974507586,
                name="玖玖瑠",
                content=chain
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 RVC 模型出错了: {e}")
            
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置rvc后端链接")
    async def set_rvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置rvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 RVC 后端: {self.rvc_base_url}\n用法: /设置rvc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.rvc_base_url = _url
        self.config["rvc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"RVC 后端链接已设置为: {_url}")

    @filter.command("rvc")
    async def rvc(self, event: AstrMessageEvent):
        """RVC 翻唱命令"""
        async for result in self._handle_cover(event, api_type="rvc"):
            yield result

    # ==================== SVC 命令 ====================
    
    @filter.command("刷新svc模型")
    async def refresh_svc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 SVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="svc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="svc")
            display_str = display_str or "未发现任何模型。"
            chain = [Plain(f"当前 SVC 可用模型：\n{display_str}")]
            node = Node(
                uin=3974507586,
                name="玖玖瑠",
                content=chain
            )
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 SVC 模型出错了: {e}")
            
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置svc后端链接")
    async def set_svc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置svc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 SVC 后端: {self.svc_base_url}\n用法: /设置svc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.svc_base_url = _url
        self.config["svc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"SVC 后端链接已设置为: {_url}")

    @filter.command("svc")
    async def svc(self, event: AstrMessageEvent):
        """SVC 翻唱命令"""
        async for result in self._handle_cover(event, api_type="svc"):
            yield result

    # ==================== 人声分离方案管理 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置分离方案")
    async def set_uvr_model_cmd(self, event: AstrMessageEvent):
        """设置默认人声分离方案（管理员可用）"""
        args = event.message_str.replace("设置分离方案", "").strip().split()
        if not args:
            opts_str = "\n".join([f"{i}. {opt}" for i, opt in enumerate(UVR_CHOICES_LIST, start=1)])
            yield event.plain_result(
                f"当前默认人声分离方案: {self.uvr_model}\n\n"
                f"可选方案:\n{opts_str}\n\n"
                f"快捷别名: uvr5, msst, 串联\n"
                f"用法: /设置分离方案 <方案名或别名>"
            )
            return

        target_model = self._normalize_uvr_model(args[0])
        self.uvr_model = target_model
        self.config["uvr_model"] = target_model
        self.config.save_config()
        yield event.plain_result(f"人声分离方案已设置为: {target_model}")

    @filter.command("查看分离方案")
    async def view_uvr_model_cmd(self, event: AstrMessageEvent):
        """查看当前生效的人声分离方案及可选列表"""
        opts_str = "\n".join([f"{i}. {opt}" for i, opt in enumerate(UVR_CHOICES_LIST, start=1)])
        yield event.plain_result(
            f"【当前生效人声分离方案】\n{self.uvr_model}\n\n"
            f"【所有可选方案】\n{opts_str}\n\n"
            f"💡 提示：可在翻唱指令末尾直接指定，如: /{ 'rvc' } 晴天 +2 msst"
        )

    # ==================== 通用翻唱处理逻辑 ====================

    async def _handle_cover(self, event: AstrMessageEvent, api_type="rvc"):
        """统一的翻唱处理逻辑"""
        cmd = api_type  # "rvc" 或 "svc"
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调] [分离方案]\n例如: /{cmd} 晴天 +2 msst")
            return

        # 智能解析参数：提取升降调、分离方案、歌名
        key_shift = 0
        specified_uvr = None
        remaining_args = []

        for arg in args:
            arg_lower = arg.lower()
            # 检查是否为升降调数字
            if (arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()) or (arg.startswith('+') and arg[1:].isdigit())):
                try:
                    val = int(arg)
                    if -12 <= val <= 12 and key_shift == 0:
                        key_shift = val
                        continue
                except ValueError:
                    pass
            # 检查是否为分离模型别名
            if arg_lower in UVR_ALIAS_MAP and specified_uvr is None:
                specified_uvr = UVR_ALIAS_MAP[arg_lower]
                continue
            
            remaining_args.append(arg)

        song_name = " ".join(remaining_args).strip()
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        songs = await self.api.fetch_data(keyword=song_name, limit=10)
        if not songs:
            yield event.plain_result("没能找到这首歌喵~")
            return
        
        # 跟踪交互过程中的临时消息 ID，用于超时撤回或交互完成后延迟撤回
        interactive_msg_ids = []

        # --- 步骤 1: 等待用户选择歌曲 ---
        song_card_id = await self._send_selection(event, songs)
        if song_card_id:
            interactive_msg_ids.append(song_card_id)

        selected_song_index = None
        user_id = event.get_sender_id()
        
        @session_waiter(timeout=self.timeout)
        async def song_waiter(controller: SessionController, waiter_event: AstrMessageEvent):
            if waiter_event.get_sender_id() != user_id:
                return            
            nonlocal selected_song_index
            u_input = waiter_event.message_str.strip()
            if u_input.isdigit() and 1 <= int(u_input) <= len(songs):
                selected_song_index = int(u_input) - 1
                u_mid = self._extract_event_msg_id(waiter_event)
                if u_mid:
                    interactive_msg_ids.append(u_mid)
                controller.stop()

        try:
            await song_waiter(event)
        except TimeoutError:
            await self._recall_messages(event, interactive_msg_ids)
            yield event.plain_result("选择超时，操作已取消。")
            return
        
        if selected_song_index is None:
            await self._recall_messages(event, interactive_msg_ids)
            return
             
        selected_song = songs[selected_song_index]

        # --- 步骤 2: 等待用户选择模型 ---
        display_str, keys = self.get_models_display_list(api_type=api_type)
        if not keys:
            await self._recall_messages(event, interactive_msg_ids)
            yield event.plain_result(f"当前没有可用的 {api_type.upper()} 模型，请先使用 /刷新{api_type}模型。")
            return
        
        chain = [
            Plain(
                f"已选歌曲: {selected_song['name']}\n"
                f"使用引擎: {api_type.upper()}\n\n"
                f"可用模型：\n{display_str}\n\n"
                f"👉 请在 {self.timeout} 秒内输入模型序号进行选择"
            )
        ]
        node = Node(
            uin=3974507586,
            name="玖玖瑠",
            content=chain
        )
        model_card_id = await self._send_interactive_msg(event, [node])
        if model_card_id:
            interactive_msg_ids.append(model_card_id)
        
        # 优化：已去除单独发送的"请在30秒内输入模型序号："，提示已直接整合于卡片中
        
        selected_model_index = None

        @session_waiter(timeout=self.timeout)
        async def model_waiter(controller: SessionController, waiter_event: AstrMessageEvent):
            if waiter_event.get_sender_id() != user_id:
                return    
            nonlocal selected_model_index
            u_input = waiter_event.message_str.strip()
            if u_input.isdigit() and 1 <= int(u_input) <= len(keys):
                selected_model_index = int(u_input) - 1
                u_mid = self._extract_event_msg_id(waiter_event)
                if u_mid:
                    interactive_msg_ids.append(u_mid)
                controller.stop()

        try:
            await model_waiter(event)
        except TimeoutError:
            await self._recall_messages(event, interactive_msg_ids)
            yield event.plain_result("选择超时，操作已取消。")
            return

        if selected_model_index is None:
            await self._recall_messages(event, interactive_msg_ids)
            return

        selected_model = keys[selected_model_index]

        # --- 可选步骤: 用户交互选择分离方案 (若开启且未在命令中指定) ---
        if not specified_uvr and self.ask_uvr_model:
            uvr_opts = "\n".join([f"{i}. {opt}" for i, opt in enumerate(UVR_CHOICES_LIST, start=1)])
            chain_uvr = [
                Plain(
                    f"请选择人声分离方案：\n{uvr_opts}\n\n"
                    f"👉 请在 {self.timeout} 秒内输入分离方案序号（超时将默认使用当前配置）"
                )
            ]
            node_uvr = Node(
                uin=3974507586,
                name="玖玖瑠",
                content=chain_uvr
            )
            uvr_card_id = await self._send_interactive_msg(event, [node_uvr])
            if uvr_card_id:
                interactive_msg_ids.append(uvr_card_id)

            selected_uvr_idx = None

            @session_waiter(timeout=self.timeout)
            async def uvr_waiter(controller: SessionController, waiter_event: AstrMessageEvent):
                if waiter_event.get_sender_id() != user_id:
                    return
                nonlocal selected_uvr_idx
                u_input = waiter_event.message_str.strip()
                if u_input.isdigit() and 1 <= int(u_input) <= len(UVR_CHOICES_LIST):
                    selected_uvr_idx = int(u_input) - 1
                    u_mid = self._extract_event_msg_id(waiter_event)
                    if u_mid:
                        interactive_msg_ids.append(u_mid)
                    controller.stop()

            try:
                await uvr_waiter(event)
            except TimeoutError:
                pass

            if selected_uvr_idx is not None:
                specified_uvr = UVR_CHOICES_LIST[selected_uvr_idx]
            else:
                specified_uvr = self.uvr_model
        elif not specified_uvr:
            specified_uvr = self.uvr_model

        # --- 步骤 3: 执行翻唱 ---
        yield event.plain_result(
            f"好的！正在使用 {api_type.upper()} 模型【{selected_model}】为您生成《{selected_song['name']}》\n"
            f"🔹 升降调: {key_shift:+d}\n"
            f"🔹 人声分离方案: {specified_uvr}\n"
            f"请耐心等待..."
        )

        # 交互完成，5秒后自动撤回之前的交互消息
        if interactive_msg_ids:
            asyncio.create_task(
                self._delayed_recall_messages(event, list(interactive_msg_ids), delay_seconds=5)
            )

        async for res in self._send_song(
            event=event,
            song=selected_song,
            model_name=selected_model,
            key_shift=key_shift,
            uvr_choice=specified_uvr,
            api_type=api_type
        ):
            yield res

    # ==================== 交互辅助及消息撤回方法 ====================

    def _extract_message_id(self, result: any) -> int | None:
        """从各种格式的 API 返回值中提取 message_id"""
        if result is None:
            return None
        if isinstance(result, int):
            return result
        if isinstance(result, str) and result.strip().isdigit():
            return int(result.strip())
        if isinstance(result, dict):
            for k in ("message_id", "msg_id", "id"):
                if k in result and result[k] is not None:
                    mid = self._extract_message_id(result[k])
                    if mid:
                        return mid
            if "data" in result:
                mid = self._extract_message_id(result["data"])
                if mid:
                    return mid
        return None

    def _extract_event_msg_id(self, event: AstrMessageEvent) -> int | None:
        """从 AstrMessageEvent 事件对象中提取消息 ID"""
        if not event:
            return None
        for attr in ("message_id", "msg_id"):
            val = getattr(event, attr, None)
            mid = self._extract_message_id(val)
            if mid:
                return mid
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            val = getattr(msg_obj, "message_id", None)
            mid = self._extract_message_id(val)
            if mid:
                return mid
            raw = getattr(msg_obj, "raw_message", None)
            if isinstance(raw, dict):
                mid = self._extract_message_id(raw.get("message_id"))
                if mid:
                    return mid
        return None

    async def _send_interactive_msg(self, event: AstrMessageEvent, components: list) -> int | None:
        """
        发送交互临时消息，并尽量捕获并返回其 message_id，方便后续自动撤回。
        """
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)

        if callable(call_action):
            try:
                has_node = any(isinstance(c, (Node, Nodes)) for c in components)
                if has_node:
                    for comp in components:
                        if isinstance(comp, Node):
                            nodes = Nodes([comp])
                        elif isinstance(comp, Nodes):
                            nodes = comp
                        else:
                            continue

                        payload = await nodes.to_dict()
                        if group_id:
                            payload["group_id"] = int(group_id) if str(group_id).isdigit() else group_id
                            if isinstance(raw_event, dict) and raw_event.get("self_id"):
                                payload["self_id"] = raw_event["self_id"]
                            res = await call_action("send_group_forward_msg", **payload)
                            msg_id = self._extract_message_id(res)
                            if msg_id:
                                return msg_id
                        elif sender_id:
                            payload["user_id"] = int(sender_id) if str(sender_id).isdigit() else sender_id
                            if isinstance(raw_event, dict) and raw_event.get("self_id"):
                                payload["self_id"] = raw_event["self_id"]
                            res = await call_action("send_private_forward_msg", **payload)
                            msg_id = self._extract_message_id(res)
                            if msg_id:
                                return msg_id
                else:
                    # 普通文本等消息
                    from astrbot.api.event import MessageChain
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                    parsed_msgs = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(components))
                    if parsed_msgs:
                        routing = {}
                        if isinstance(raw_event, dict) and raw_event.get("self_id"):
                            routing["self_id"] = raw_event["self_id"]
                        if group_id:
                            res = await call_action(
                                "send_group_msg",
                                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                                message=parsed_msgs,
                                **routing
                            )
                            msg_id = self._extract_message_id(res)
                            if msg_id:
                                return msg_id
                        elif sender_id:
                            res = await call_action(
                                "send_private_msg",
                                user_id=int(sender_id) if str(sender_id).isdigit() else sender_id,
                                message=parsed_msgs,
                                **routing
                            )
                            msg_id = self._extract_message_id(res)
                            if msg_id:
                                return msg_id
            except Exception as e:
                logger.debug(f"通过 OneBot call_action 发送交互消息异常: {e}，将回退标准发送")

        # 兜底回退 AstrBot 原生 event.send
        try:
            res = await event.send(event.chain_result(components))
            return self._extract_message_id(res)
        except Exception as e:
            logger.warning(f"发送交互消息兜底失败: {e}")
            return None

    async def _recall_messages(self, event: AstrMessageEvent, message_ids: list):
        """立即撤回指定的若干消息"""
        if not message_ids:
            return
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        for mid in set(message_ids):
            if not mid:
                continue
            try:
                numeric_mid = int(mid) if str(mid).isdigit() else mid
                if callable(call_action):
                    await call_action("delete_msg", message_id=numeric_mid)
                elif hasattr(event, "delete_msg") and callable(event.delete_msg):
                    await event.delete_msg(numeric_mid)
                elif hasattr(bot, "delete_msg") and callable(getattr(bot, "delete_msg", None)):
                    await bot.delete_msg(message_id=numeric_mid)
                logger.debug(f"已请求撤回交互消息 ID: {numeric_mid}")
            except Exception as e:
                logger.debug(f"撤回消息 {mid} 忽略异常 (可能无权限、已撤回或协议端限制): {e}")

    async def _delayed_recall_messages(self, event: AstrMessageEvent, message_ids: list, delay_seconds: int = 5):
        """延迟指定秒数后撤回交互消息"""
        if not message_ids:
            return
        try:
            await asyncio.sleep(delay_seconds)
            await self._recall_messages(event, message_ids)
        except Exception as e:
            logger.debug(f"延迟撤回交互消息异常: {e}")

    async def _send_selection(self, event: AstrMessageEvent, songs: list) -> int | None:
        formatted_songs = [f"{i + 1}. {s['name']} - {s['artists']}" for i, s in enumerate(songs[:10])]
        chain = [
            Plain(
                "为您找到以下歌曲：\n"
                + "\n".join(formatted_songs)
                + f"\n\n👉 请在 {self.timeout} 秒内输入歌曲序号进行选择"
            )
        ]
        node = Node(
            uin=3974507586,
            name="玖玖瑠",
            content=chain
        )
        return await self._send_interactive_msg(event, [node])

    @staticmethod
    def _to_record(file_path: str = "", data: bytes | None = None) -> Record:
        """
        参考 astrbot_plugin_GPT_SoVITS_multi_speaker 的语音实现:
        1. 优先通过 Record.fromFileSystem 读取本地文件
        2. 若失败或环境受限，读取二进制并转为 Base64，通过 Record.fromBase64 发送
        """
        if file_path and os.path.exists(file_path):
            try:
                return Record.fromFileSystem(file_path)
            except Exception as e:
                logger.warning(f"从文件系统加载 Record 失败 ({e})，尝试以 base64 读取")

        if not data and file_path and os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
            except Exception as e:
                logger.warning(f"读取音频文件二进制失败: {e}")

        if not data:
            raise ValueError(f"无法获取有效音频数据: {file_path}")

        b64 = base64.urlsafe_b64encode(data).decode()
        return Record.fromBase64(b64)

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: int, uvr_choice: str, api_type="rvc"):
        """根据 API 类型调用对应后端进行翻唱，并安全发送音频文件/语音条"""
        result_path = None
        try:
            base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
            client = Client(base_url)
            logger.info(
                f"[{api_type.upper()}] 正在请求后端 /convert, "
                f"歌曲ID={song['id']}, key_shift={key_shift}, 模型={model_name}, 分离方案={uvr_choice}"
            )
            
            # app_rvc 和 app_svc 的 /convert inputs 参数列表：
            # 0: inp1 (song_id/url)
            # 1: inp5 (key_shift)
            # 2: inp6 (vocal_vol)
            # 3: inp7 (inst_vol)
            # 4: api_model_name (model_name)
            # 5: inp_reverb (混响强度, 默认4)
            # 6: inp_delay (回声延迟, 默认0)
            # 7: uvr_model_choice (分离模型名称)
            try:
                result_path = await self._async_predict(
                    client,
                    str(song["id"]),
                    key_shift,
                    0,
                    0,
                    model_name,
                    4,
                    0,
                    uvr_choice,
                    api_name="/convert",
                    timeout=self.inference_timeout
                )
            except Exception as e_pos:
                logger.warning(f"位置参数调用 /convert 失败: {e_pos}，尝试关键字参数重试...")
                result_path = await self._async_predict(
                    client,
                    song_name_src=str(song["id"]),
                    key_shift=key_shift,
                    vocal_vol=0,
                    inst_vol=0,
                    model_dropdown=model_name,
                    reverb_intensity=4,
                    delay_intensity=0,
                    uvr_choice=uvr_choice,
                    api_name="/convert",
                    timeout=self.inference_timeout
                )

            if result_path and os.path.exists(result_path):
                async for res in self._send_audio_result(event, result_path, song["name"], api_type):
                    yield res
            else:
                yield event.plain_result("生成失败，后端未返回有效文件路径。")
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                yield event.plain_result(f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。如果需要，请在配置文件中调高 'inference_timeout' 的值。")
            else:
                yield event.plain_result(f"生成时发生严重错误: {e}")

    async def _safe_cleanup_file(self, file_path: str, delay_seconds: int = 60):
        """异步延迟清理临时文件，确保协议端异步读取和上传完成后再删除"""
        if not file_path or not os.path.isfile(file_path):
            return
        await asyncio.sleep(delay_seconds)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.debug(f"已清理临时翻唱文件: {file_path}")
        except OSError as e:
            logger.warning(f"删除临时文件失败: {e}")

    async def _send_audio_result(self, event: AstrMessageEvent, result_path: str, song_name: str, api_type: str):
        """
        发送音频文件/语音条：
        参考 astrbot_plugin_GPT_SoVITS_multi_speaker 的语音发送实现，默认优先发送 QQ 语音条 (Record)。
        具备异常降级机制，若语音条受限可自动回退群文件/通用文件发送。
        """
        file_path = os.path.abspath(result_path)
        safe_name = re.sub(r'[\\/:*?"<>|]', '', song_name).strip() or "翻唱"
        _, ext = os.path.splitext(file_path)
        if not ext:
            ext = ".mp3"
        send_name = f"{safe_name}_{api_type.upper()}翻唱{ext}"

        # 获取平台及 OneBot 特性
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        send_mode = str(self.config.get("send_mode", "record")).lower()

        record_sent = False
        file_sent = False
        last_error = None

        # 辅助发送语音条 (Record) - 参考 GPT_SoVITS 的 _to_record 实现
        async def do_send_record() -> bool:
            nonlocal record_sent, last_error
            try:
                record_seg = self._to_record(file_path)
                await event.send(event.chain_result([record_seg]))
                record_sent = True
                logger.info(f"QQ语音消息 (Record) 发送成功: {file_path}")
                return True
            except Exception as e_rec:
                logger.warning(f"QQ语音消息 (Record) 发送失败 (常见原因: 歌曲超过60秒/协议端限制): {e_rec}")
                last_error = e_rec
                return False

        # 辅助发送文件
        async def do_send_file() -> bool:
            nonlocal file_sent, last_error
            # 1. 如果是 OneBot (aiocqhttp) 且有群号，尝试调用 upload_group_file
            if group_id and callable(call_action):
                try:
                    await call_action(
                        "upload_group_file",
                        group_id=int(group_id),
                        file=file_path,
                        name=send_name
                    )
                    file_sent = True
                    logger.info(f"OneBot QQ群文件上传成功: {send_name}")
                    return True
                except Exception as e_grp:
                    logger.warning(f"OneBot upload_group_file 失败，尝试回退普通文件发送: {e_grp}")
                    last_error = e_grp

            # 2. 如果是 OneBot 私聊，尝试调用 upload_private_file
            if not group_id and sender_id and callable(call_action):
                try:
                    await call_action(
                        "upload_private_file",
                        user_id=int(sender_id),
                        file=file_path,
                        name=send_name
                    )
                    file_sent = True
                    logger.info(f"OneBot QQ私聊文件上传成功: {send_name}")
                    return True
                except Exception as e_priv:
                    logger.warning(f"OneBot upload_private_file 失败，尝试回退普通文件发送: {e_priv}")
                    last_error = e_priv

            # 3. 回退为 AstrBot 通用 File 消息组件
            try:
                await event.send(event.chain_result([File(file=file_path, name=send_name)]))
                file_sent = True
                logger.info(f"以 AstrBot File 组件发送成功: {send_name}")
                return True
            except Exception as e_file:
                logger.error(f"以 AstrBot File 组件发送失败: {e_file}")
                last_error = e_file
                return False

        # 根据配置模式执行发送
        if send_mode == "record":
            ok = await do_send_record()
            if not ok:
                yield event.plain_result("⚠️ QQ语音条发送失败（可能因歌曲超长或协议端限制），正在自动改发音频文件...")
                await do_send_file()
        elif send_mode == "file":
            ok = await do_send_file()
            if not ok:
                yield event.plain_result("⚠️ 音频文件发送失败，尝试改为发送语音条...")
                await do_send_record()
        elif send_mode == "both":
            await do_send_record()
            await do_send_file()
        else:
            # 默认 record
            ok = await do_send_record()
            if not ok:
                await do_send_file()

        if not file_sent and not record_sent:
            yield event.plain_result(f"音频发送失败: {last_error or '未知错误'}，请检查网络或协议端权限。")
        
        # 异步延迟清理本地临时文件，避免立即删除导致协议端异步读取失败
        asyncio.create_task(self._safe_cleanup_file(file_path, delay_seconds=60))
