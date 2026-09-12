from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register


_ROOM_PATH_RE = re.compile(r"^/live/(\d+)/?$")
_INITIAL_STATE_MARKER_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
_POLL_INTERVAL_SECONDS = 60
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


@dataclass(frozen=True)
class MonitorConfig:
    room_url: str
    push_target: str


@dataclass(frozen=True)
class LiveSnapshot:
    is_live: bool
    live_id: str = ""
    streamer_name: str = ""
    title: str = ""
    cover_url: str = ""


def _normalize_room_url(value: str) -> str:
    url = value.strip()
    if "://" not in url:
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname != "live.acfun.cn":
        raise ValueError("room_url 必须是 live.acfun.cn/live/<主播ID> 形式的地址")

    match = _ROOM_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ValueError("room_url 必须包含数字主播 ID，例如 https://live.acfun.cn/live/52002191")
    return f"https://live.acfun.cn/live/{match.group(1)}"


def _read_config(raw: AstrBotConfig) -> MonitorConfig:
    room_url = _normalize_room_url(str(raw.get("room_url", "")))
    push_target = str(raw.get("push_target", "")).strip()
    if not push_target:
        raise ValueError("push_target 不能为空；请在目标会话发送 /sid 并复制 UMO")

    return MonitorConfig(room_url, push_target)


def _extract_snapshot(html: str) -> LiveSnapshot:
    match = _INITIAL_STATE_MARKER_RE.search(html)
    if match is None:
        raise ValueError("未找到 AcFun 页面状态数据，页面结构可能已经变化")

    try:
        state, _ = json.JSONDecoder().raw_decode(html[match.end() :])
    except json.JSONDecodeError as e:
        raise ValueError(f"无法解析 AcFun 页面状态数据：{e.msg}") from e

    info = state.get("liveInfo")
    if not isinstance(info, dict) or info.get("result") != 0:
        raise ValueError("AcFun 未返回有效的直播间数据")

    live_id = info.get("liveId")
    if not isinstance(live_id, str) or not live_id:
        return LiveSnapshot(is_live=False)

    user = info.get("user")
    streamer_name = user.get("name", "") if isinstance(user, dict) else ""
    title = info.get("title", "")
    cover_urls = info.get("coverUrls")
    cover_url = ""
    if isinstance(cover_urls, list):
        for value in cover_urls:
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                cover_url = value
                break
    return LiveSnapshot(
        is_live=True,
        live_id=live_id,
        streamer_name=str(streamer_name),
        title=str(title),
        cover_url=cover_url,
    )


@register(
    "astrbot_plugin_acfun_live_monitor",
    "bpking",
    "Minimal AcFun live room monitor",
    "0.2.0",
)
class AcFunLiveMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._initialized = False
        self._was_live = False
        self._last_live_id = ""
        self._room_url = ""
        self._last_config_error = ""

    async def initialize(self) -> None:
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession(headers=_HEADERS)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("AcFun live monitor started; configure it in the AstrBot WebUI")

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _poll_loop(self) -> None:
        while True:
            config = self._load_config()
            if config is None:
                await asyncio.sleep(30)
                continue

            if config.room_url != self._room_url:
                self._room_url = config.room_url
                self._initialized = False
                self._was_live = False
                self._last_live_id = ""

            try:
                snapshot = await self._fetch_snapshot(config.room_url)
                await self._handle_snapshot(config, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"AcFun live monitor check failed: {e}")

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    def _load_config(self) -> MonitorConfig | None:
        try:
            config = _read_config(self.config)
        except ValueError as e:
            message = str(e)
            if message != self._last_config_error:
                logger.warning(f"AcFun live monitor config error: {message}")
                self._last_config_error = message
            return None

        self._last_config_error = ""
        return config

    async def _fetch_snapshot(self, room_url: str) -> LiveSnapshot:
        if self._session is None:
            raise RuntimeError("HTTP session is not initialized")
        timeout = aiohttp.ClientTimeout(total=15)
        async with self._session.get(
            room_url, timeout=timeout, allow_redirects=True
        ) as response:
            response.raise_for_status()
            html = await response.text()
        return _extract_snapshot(html)

    async def _handle_snapshot(
        self, config: MonitorConfig, snapshot: LiveSnapshot
    ) -> None:
        should_notify = (
            self._initialized
            and snapshot.is_live
            and (not self._was_live or snapshot.live_id != self._last_live_id)
        )

        self._was_live = snapshot.is_live
        self._last_live_id = snapshot.live_id if snapshot.is_live else ""
        self._initialized = True

        if not should_notify:
            return

        name = snapshot.streamer_name or "AcFun 主播"
        title = snapshot.title or "未提供标题"
        text = f"🟢 {name} 开播了！\n{title}\n{config.room_url}"
        sent = await self._send_notification(config.push_target, text, snapshot.cover_url)
        if sent is False:
            logger.warning(f"AcFun live monitor could not send to {config.push_target}")
        else:
            logger.info(f"AcFun live notification sent: {name}")

    async def _send_notification(
        self, push_target: str, text: str, cover_url: str
    ) -> bool:
        if not cover_url:
            return await self.context.send_message(
                push_target, MessageChain(chain=[Comp.Plain(text)])
            )

        try:
            sent = await self.context.send_message(
                push_target,
                MessageChain(chain=[Comp.Plain(text), Comp.Image.fromURL(cover_url)]),
            )
            if sent is not False:
                return sent
        except Exception as e:
            logger.warning(f"AcFun cover send failed, retrying text-only: {e}")

        return await self.context.send_message(
            push_target, MessageChain(chain=[Comp.Plain(text)])
        )
