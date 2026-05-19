import asyncio
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_niko_danmaku"
DATA_DIR = Path("data/plugin_data") / PLUGIN_NAME
DATA_FILE = DATA_DIR / "targets.json"


@register(
    "astrbot_plugin_niko_danmaku",
    "pasarano",
    "定时间隔推送一条 sb6657.cn 中与 NiKo/niko 有关的弹幕",
    "1.4.1",
)
class NikoDanmakuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.target_tasks: dict[str, asyncio.Task] = {}
        self.targets: set[str] = set()
        self.sent_ids_by_target: dict[str, set[str]] = {}
        self.sent_texts_by_target: dict[str, list[str]] = {}

        self.base_url = self.config.get("base_url", "https://hguofichp.cn:10086")
        self.keyword = self.config.get("keyword", "niko")
        self.extra_keywords_raw = self.config.get("extra_keywords", "尼扣,虾")

        self.page_size = int(self.config.get("page_size", 50))
        self.push_prefix = self.config.get("push_prefix", "【NiKo 弹幕播报】")
        self.show_prefix = self._to_bool(self.config.get("show_prefix", True))
        self.no_repeat_cache_size = int(self.config.get("no_repeat_cache_size", 200))

        self.interval_minutes = int(self.config.get("interval_minutes", 60))
        if self.interval_minutes < 1:
            self.interval_minutes = 1

        self.include_meta = self._to_bool(self.config.get("include_meta", True))
        self.push_on_start = self._to_bool(self.config.get("push_on_start", False))
        self.block_llm_in_bound_targets = self._to_bool(
            self.config.get("block_llm_in_bound_targets", True)
        )

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.targets = self._dedupe_targets(self._load_targets())
        self._save_targets()

        for umo in self.targets:
            self._ensure_target_task(umo)

        logger.info(
            f"{PLUGIN_NAME} initialized, "
            f"targets={len(self.targets)}, "
            f"interval_minutes={self.interval_minutes}, "
            f"include_meta={self.include_meta}, "
            f"show_prefix={self.show_prefix}, "
            f"block_llm_in_bound_targets={self.block_llm_in_bound_targets}, "
            f"extra_keywords={self.extra_keywords_raw}"
        )

    async def terminate(self):
        for task in list(self.target_tasks.values()):
            task.cancel()

        for task in list(self.target_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.target_tasks.clear()

        self._save_targets()
        logger.info(f"{PLUGIN_NAME} terminated")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕绑定")
    async def bind_target(self, event: AstrMessageEvent):
        """绑定当前会话为 NiKo 弹幕定时推送目标"""
        event.stop_event()
        replaced = self._replace_target_for_same_chat(event.unified_msg_origin)
        self._ensure_target_task(event.unified_msg_origin)
        self._save_targets()

        if replaced:
            yield event.plain_result(
                f"已更新当前会话绑定。之后会每隔 {self.interval_minutes} 分钟推送一条与 niko 有关的弹幕。"
            )
        else:
            yield event.plain_result(
                f"已绑定当前会话。之后会每隔 {self.interval_minutes} 分钟推送一条与 niko 有关的弹幕。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕解绑")
    async def unbind_target(self, event: AstrMessageEvent):
        """取消当前会话的 NiKo 弹幕定时推送"""
        event.stop_event()
        target_key = self._target_key(event.unified_msg_origin)
        matched = self._targets_for_key(target_key)

        if matched:
            for umo in matched:
                self.targets.remove(umo)

            self._cancel_target_task(target_key)
            self.sent_ids_by_target.pop(target_key, None)
            self.sent_texts_by_target.pop(target_key, None)
            self._save_targets()
            yield event.plain_result("已解绑当前会话。")
        else:
            yield event.plain_result("当前会话还没有绑定。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕测试")
    async def test_once(self, event: AstrMessageEvent):
        """立即获取并发送一条 NiKo 相关弹幕"""
        event.stop_event()
        target_key = self._target_key(event.unified_msg_origin)
        meme = await self._fetch_random_niko_meme(target_key)
        if not meme:
            yield event.plain_result(
                "没搜到 niko 相关弹幕，或接口暂时不可用。请查看 AstrBot 日志中的 sb6657 返回内容。"
            )
            return

        text = self._format_meme(meme)
        self._remember_sent_text(target_key, text)
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕状态")
    async def status(self, event: AstrMessageEvent):
        """查看插件绑定和配置状态"""
        event.stop_event()
        is_bound = self._target_key(event.unified_msg_origin) in self._target_keys()

        yield event.plain_result(
            f"当前会话绑定状态：{'已绑定' if is_bound else '未绑定'}\n"
            f"全局绑定会话数：{len(self._target_keys())}\n"
            f"主关键词：{self.keyword}\n"
            f"额外关键词：{self.extra_keywords_raw or '无'}\n"
            f"实际搜索关键词：{', '.join(self._get_search_keywords())}\n"
            f"后端：{self.base_url}\n"
            f"播报间隔：{self.interval_minutes} 分钟\n"
            f"是否显示前缀：{'是' if self.show_prefix else '否'}\n"
            f"是否带附加信息：{'是' if self.include_meta else '否'}\n"
            f"绑定会话阻止 LLM 回复：{'是' if self.block_llm_in_bound_targets else '否'}\n"
            f"启动后立即播报：{'是' if self.push_on_start else '否'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕设置间隔")
    async def set_interval(self, event: AstrMessageEvent):
        """临时设置播报间隔，格式：/niko弹幕设置间隔 30"""
        event.stop_event()
        message = event.message_str.strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("用法：/niko弹幕设置间隔 30")
            return

        try:
            minutes = int(parts[1])
        except ValueError:
            yield event.plain_result("间隔必须是数字，单位是分钟。")
            return

        if minutes < 1:
            yield event.plain_result("间隔不能小于 1 分钟。")
            return

        self.interval_minutes = minutes
        self._restart_target_tasks()
        yield event.plain_result(
            f"已临时设置播报间隔为 {minutes} 分钟。重启插件后会恢复为配置文件中的值。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕附加信息")
    async def set_include_meta(self, event: AstrMessageEvent):
        """临时切换是否显示标签、复制、投稿等信息，格式：/niko弹幕附加信息 开"""
        event.stop_event()
        message = event.message_str.strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("用法：/niko弹幕附加信息 开 或 /niko弹幕附加信息 关")
            return

        value = parts[1].strip().lower()

        if value in ("开", "开启", "true", "yes", "1", "on"):
            self.include_meta = True
            yield event.plain_result("已开启附加信息。之后会显示标签、复制、投稿等信息。")
        elif value in ("关", "关闭", "false", "no", "0", "off"):
            self.include_meta = False
            yield event.plain_result("已关闭附加信息。之后只显示弹幕正文。")
        else:
            yield event.plain_result("参数只能是：开 或 关。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕前缀")
    async def set_show_prefix(self, event: AstrMessageEvent):
        """临时切换是否显示推送前缀，格式：/niko弹幕前缀 开"""
        event.stop_event()
        message = event.message_str.strip()
        parts = message.split()

        if len(parts) < 2:
            yield event.plain_result("用法：/niko弹幕前缀 开 或 /niko弹幕前缀 关")
            return

        value = parts[1].strip().lower()

        if value in ("开", "开启", "true", "yes", "1", "on"):
            self.show_prefix = True
            yield event.plain_result("已开启推送前缀。")
        elif value in ("关", "关闭", "false", "no", "0", "off"):
            self.show_prefix = False
            yield event.plain_result("已关闭推送前缀。之后会直接发送弹幕正文。")
        else:
            yield event.plain_result("参数只能是：开 或 关。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("niko弹幕额外关键词")
    async def set_extra_keywords(self, event: AstrMessageEvent):
        """临时设置额外搜索关键词，格式：/niko弹幕额外关键词 尼扣,虾,三楼"""
        event.stop_event()
        message = event.message_str.strip()
        parts = message.split(maxsplit=1)

        if len(parts) < 2:
            yield event.plain_result(
                "用法：/niko弹幕额外关键词 尼扣,虾,三楼\n"
                f"当前额外关键词：{self.extra_keywords_raw or '无'}"
            )
            return

        raw = parts[1].strip()

        if raw in ("无", "空", "清空", "none", "None", "NULL", "null"):
            self.extra_keywords_raw = ""
            yield event.plain_result(
                "已临时清空额外关键词。重启插件后会恢复为配置文件中的值。"
            )
            return

        self.extra_keywords_raw = raw
        yield event.plain_result(
            f"已临时设置额外关键词：{self.extra_keywords_raw}\n"
            f"当前实际搜索关键词：{', '.join(self._get_search_keywords())}\n"
            "重启插件后会恢复为配置文件中的值。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def stop_unauthorized_plugin_command(self, event: AstrMessageEvent):
        """非管理员发送插件命令时立即停止传播，避免继续触发 LLM。"""
        if not self._is_plugin_command(event.message_str):
            return

        if self._is_admin_event(event):
            return

        event.stop_event()
        logger.warning(
            f"{PLUGIN_NAME} blocked non-admin command from {event.unified_msg_origin}"
        )
        yield event.plain_result("此命令仅限 AstrBot 管理员使用。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def stop_danmaku_quote_followup(self, event: AstrMessageEvent):
        """阻止已绑定会话中引用本插件弹幕后继续触发 LLM。"""
        if self._target_key(event.unified_msg_origin) not in self._target_keys():
            return

        if self._is_plugin_command(event.message_str):
            return

        if self._is_likely_danmaku_quote(event):
            event.stop_event()
            logger.info(f"{PLUGIN_NAME} stopped quoted danmaku follow-up in {event.unified_msg_origin}")

    @filter.on_llm_request(priority=100)
    async def stop_danmaku_quote_llm_request(self, event: AstrMessageEvent, req):
        """兜底阻止引用本插件弹幕产生的 LLM 请求。"""
        if self._target_key(event.unified_msg_origin) not in self._target_keys():
            return

        if self.block_llm_in_bound_targets and not self._is_plugin_command(event.message_str):
            event.stop_event()
            logger.info(f"{PLUGIN_NAME} blocked LLM request in bound target {event.unified_msg_origin}")
            return

        if self._is_likely_danmaku_quote(event):
            event.stop_event()
            logger.info(f"{PLUGIN_NAME} stopped quoted danmaku LLM request in {event.unified_msg_origin}")

    def _ensure_target_task(self, umo: str):
        target_key = self._target_key(umo)
        task = self.target_tasks.get(target_key)
        if task and not task.done():
            return

        task = asyncio.create_task(self._target_interval_loop(umo))

        def remove_done_task(done_task: asyncio.Task, target: str = target_key):
            if self.target_tasks.get(target) is done_task:
                self.target_tasks.pop(target, None)

        task.add_done_callback(remove_done_task)
        self.target_tasks[target_key] = task

    def _cancel_target_task(self, target_key: str):
        task = self.target_tasks.pop(target_key, None)
        if task and not task.done():
            task.cancel()

    def _restart_target_tasks(self):
        for target_key in list(self.target_tasks):
            self._cancel_target_task(target_key)

        for umo in self.targets:
            self._ensure_target_task(umo)

    async def _target_interval_loop(self, umo: str):
        if self.push_on_start:
            await self._push_once_to_target(umo)

        while True:
            try:
                await asyncio.sleep(self.interval_minutes * 60 + self._target_jitter_seconds(umo))
                await self._push_once_to_target(umo)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"{PLUGIN_NAME} interval loop error target={umo}: {e}")
                await asyncio.sleep(60)

    async def _broadcast_once(self):
        for umo in self._dedupe_targets(self.targets):
            await self._push_once_to_target(umo)

    async def _push_once_to_target(self, umo: str):
        target_key = self._target_key(umo)
        if target_key not in self._target_keys():
            return

        meme = await self._fetch_random_niko_meme(target_key)
        if not meme:
            logger.warning(f"未获取到 niko 相关弹幕，本次定时播报跳过 target={umo}")
            return

        text = self._format_meme(meme)
        chain = MessageChain().message(text)

        try:
            await self.context.send_message(umo, chain)
            self._remember_sent_text(target_key, text)
        except Exception as e:
            logger.warning(f"向 {umo} 推送 NiKo 弹幕失败: {e}")

    def _target_jitter_seconds(self, umo: str) -> int:
        interval_seconds = self.interval_minutes * 60
        max_jitter = max(0, min(300, interval_seconds // 5))
        if max_jitter <= 0:
            return 0

        digest = hashlib.sha256(self._target_key(umo).encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % (max_jitter + 1)

    def _target_key(self, umo: str) -> str:
        text = str(umo or "")
        tokens = [x for x in re.split(r"[^0-9A-Za-z]+", text) if x]
        lowered = [x.lower() for x in tokens]

        for marker in ("groupmessage", "group", "guildmessage"):
            if marker in lowered:
                marker_index = lowered.index(marker)
                numbers_after = [x for x in tokens[marker_index + 1 :] if x.isdigit()]
                if numbers_after:
                    platform = tokens[0] if tokens else "default"
                    return f"group:{platform}:{numbers_after[-1]}"

        for marker in ("privatemessage", "friendmessage", "private", "friend"):
            if marker in lowered:
                marker_index = lowered.index(marker)
                numbers_after = [x for x in tokens[marker_index + 1 :] if x.isdigit()]
                if numbers_after:
                    platform = tokens[0] if tokens else "default"
                    return f"private:{platform}:{numbers_after[-1]}"

        return f"raw:{text}"

    def _target_keys(self) -> set[str]:
        return {self._target_key(umo) for umo in self.targets}

    def _targets_for_key(self, target_key: str) -> list[str]:
        return [umo for umo in self.targets if self._target_key(umo) == target_key]

    def _dedupe_targets(self, targets: set[str]) -> set[str]:
        result: dict[str, str] = {}

        for umo in sorted(targets):
            result[self._target_key(umo)] = umo

        if len(result) != len(targets):
            logger.warning(
                f"{PLUGIN_NAME} deduped targets from {len(targets)} to {len(result)} by chat key"
            )

        return set(result.values())

    def _replace_target_for_same_chat(self, umo: str) -> bool:
        target_key = self._target_key(umo)
        matched = self._targets_for_key(target_key)
        replaced = bool(matched)

        for old_umo in matched:
            self.targets.remove(old_umo)

        if replaced:
            self._cancel_target_task(target_key)

        self.targets.add(umo)
        return replaced

    def _get_sent_ids(self, target_key: str | None) -> set[str]:
        key = target_key or "__manual__"
        if key not in self.sent_ids_by_target:
            self.sent_ids_by_target[key] = set()
        return self.sent_ids_by_target[key]

    def _remember_sent_text(self, target_key: str, text: str):
        values = self.sent_texts_by_target.setdefault(target_key, [])
        values.append(text)

        if len(values) > 50:
            del values[:-50]

    def _is_plugin_command(self, message: str) -> bool:
        text = message.strip()
        return text.startswith("/niko弹幕") or text.startswith("niko弹幕")

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                return bool(is_admin())
            except Exception as e:
                logger.warning(f"{PLUGIN_NAME} admin check failed: {e}")

        for obj in (event, getattr(event, "message_obj", None)):
            if obj is None:
                continue

            for attr in ("is_admin", "isAdmin"):
                value = getattr(obj, attr, None)
                if isinstance(value, bool):
                    return value

            for attr in ("role", "permission"):
                value = str(getattr(obj, attr, "")).lower()
                if value in ("admin", "administrator", "owner", "superuser"):
                    return True

        return False

    def _is_likely_danmaku_quote(self, event: AstrMessageEvent) -> bool:
        text = self._event_debug_text(event)
        lowered = text.lower()

        quote_markers = (
            "引用消息",
            "reply",
            "quote",
            "cq:reply",
            "componenttype.reply",
            "componenttype.node",
        )
        if not any(marker in lowered for marker in quote_markers):
            return False

        if self.push_prefix and self.push_prefix in text:
            return True

        if "NiKo 弹幕播报" in text or "niko 弹幕播报" in lowered:
            return True

        if "标签：" in text and ("复制：" in text or "投稿：" in text or "点赞：" in text):
            return any(keyword.lower() in lowered for keyword in self._get_match_keywords())

        target_key = self._target_key(event.unified_msg_origin)
        for sent_text in self.sent_texts_by_target.get(target_key, []):
            if sent_text and sent_text[:80] in text:
                return True

        return False

    def _event_debug_text(self, event: AstrMessageEvent) -> str:
        parts = [event.message_str or ""]
        message_obj = getattr(event, "message_obj", None)

        if message_obj is not None:
            for attr in ("message", "raw_message", "message_str"):
                value = getattr(message_obj, attr, None)
                if value is not None:
                    parts.append(str(value))

        return "\n".join(parts)

    async def _fetch_random_niko_meme(self, target_key: str | None = None) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []

        keywords = self._get_search_keywords()

        for kw in keywords:
            part = await self._search_meme(kw)
            candidates.extend(part)

        normalized: dict[str, dict[str, Any]] = {}
        match_keywords = self._get_match_keywords()

        for item in candidates:
            content = str(
                item.get("barrage")
                or item.get("content")
                or item.get("text")
                or item.get("message")
                or ""
            ).strip()

            if not content:
                continue

            lowered = content.lower()

            if not any(k.lower() in lowered for k in match_keywords):
                continue

            meme_id = str(
                item.get("id")
                or item.get("barrageId")
                or item.get("_id")
                or content
            )

            normalized[meme_id] = item

        final_list = list(normalized.values())

        if not final_list:
            logger.warning(
                f"sb6657 搜索到了 {len(candidates)} 条候选，"
                f"但过滤后没有匹配关键词的弹幕。当前搜索关键词={keywords}"
            )
            return None

        fresh = []
        sent_ids = self._get_sent_ids(target_key)

        for item in final_list:
            item_key = str(
                item.get("id")
                or item.get("barrageId")
                or item.get("_id")
                or item.get("barrage")
                or item.get("content")
                or item.get("text")
                or item.get("message")
            )

            if item_key not in sent_ids:
                fresh.append(item)

        if not fresh:
            fresh = final_list
            sent_ids.clear()

        chosen = random.choice(fresh)

        chosen_id = str(
            chosen.get("id")
            or chosen.get("barrageId")
            or chosen.get("_id")
            or chosen.get("barrage")
            or chosen.get("content")
            or chosen.get("text")
            or chosen.get("message")
        )

        sent_ids.add(chosen_id)

        if len(sent_ids) > self.no_repeat_cache_size:
            key = target_key or "__manual__"
            trimmed = list(sent_ids)[-self.no_repeat_cache_size:]
            self.sent_ids_by_target[key] = set(trimmed)

        return chosen

    async def _search_meme(self, keyword: str) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": f"{PLUGIN_NAME}/1.4.1",
            "Content-Type": "application/json",
            "Referer": "https://sb6657.cn/",
            "Origin": "https://sb6657.cn",
        }

        async with httpx.AsyncClient(timeout=20.0, headers=headers, verify=False) as client:
            try:
                url = f"{self.base_url.rstrip('/')}/machine/Query"
                payload = {
                    "D": "油猴",
                    "barrage": keyword,
                }

                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                logger.info(f"sb6657 Query keyword={keyword} response={str(data)[:500]}")

                items = self._extract_items(data)
                if items:
                    return items

            except Exception as e:
                logger.warning(f"Query 搜索失败 keyword={keyword}: {e}")

            try:
                url = f"{self.base_url.rstrip('/')}/machine/pageSearch"
                payload = {
                    "barrage": keyword,
                    "tags": "",
                    "sort": 0,
                    "pageSize": self.page_size,
                    "pageNum": 1,
                }

                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                logger.info(f"sb6657 pageSearch keyword={keyword} response={str(data)[:500]}")

                return self._extract_items(data)

            except Exception as e:
                logger.warning(f"pageSearch 搜索失败 keyword={keyword}: {e}")
                return []

    def _extract_items(self, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []

        if data.get("code") not in (None, 0, 200):
            logger.warning(f"sb6657 接口返回失败: {data}")
            return []

        node = data.get("data")
        if node is None:
            node = data.get("flatData")
        if node is None:
            node = data

        if isinstance(node, list):
            return [x for x in node if isinstance(x, dict)]

        if isinstance(node, dict):
            for key in ("list", "records", "rows", "data", "result"):
                val = node.get(key)

                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]

                if isinstance(val, dict):
                    nested = self._extract_items(val)
                    if nested:
                        return nested

        return []

    def _format_meme(self, meme: dict[str, Any]) -> str:
        content = str(
            meme.get("barrage")
            or meme.get("content")
            or meme.get("text")
            or meme.get("message")
            or ""
        ).strip()

        tags = str(meme.get("tags") or meme.get("tag") or "").strip()
        likes = meme.get("likes", meme.get("like"))
        copy_count = meme.get("cnt", meme.get("copyCount", meme.get("copy_count")))
        submit_time = str(
            meme.get("submitTime")
            or meme.get("createTime")
            or meme.get("createdAt")
            or ""
        ).strip()

        text = f"{self.push_prefix}\n{content}" if self.show_prefix else content

        if not self.include_meta:
            return text

        meta = []

        if tags:
            meta.append(f"标签：{tags}")
        if likes not in (None, ""):
            meta.append(f"点赞：{likes}")
        if copy_count not in (None, ""):
            meta.append(f"复制：{copy_count}")
        if submit_time:
            meta.append(f"投稿：{submit_time}")

        if meta:
            return f"{text}\n\n" + "｜".join(meta)

        return text

    def _split_keywords(self, raw: str) -> list[str]:
        if not raw:
            return []

        parts = re.split(r"[,，、;；|\n\r\t ]+", raw)
        return [x.strip() for x in parts if x.strip()]

    def _get_search_keywords(self) -> list[str]:
        keywords = [
            self.keyword,
            "niko",
            "NiKo",
            "NIKO",
        ]

        keywords.extend(self._split_keywords(self.extra_keywords_raw))

        result = []
        seen = set()

        for kw in keywords:
            if not kw:
                continue

            key = kw.lower()
            if key in seen:
                continue

            seen.add(key)
            result.append(kw)

        return result

    def _get_match_keywords(self) -> list[str]:
        keywords = self._get_search_keywords()
        keywords.extend(["尼扣", "虾"])

        result = []
        seen = set()

        for kw in keywords:
            if not kw:
                continue

            key = kw.lower()
            if key in seen:
                continue

            seen.add(key)
            result.append(kw)

        return result

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开", "开启")

        return bool(value)

    def _load_targets(self) -> set[str]:
        if not DATA_FILE.exists():
            return set()

        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return set(data.get("targets", []))
        except Exception as e:
            logger.warning(f"读取 {DATA_FILE} 失败: {e}")
            return set()

    def _save_targets(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(
            json.dumps({"targets": sorted(self.targets)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
