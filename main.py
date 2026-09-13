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
_LIVE_COVER_URL_TEMPLATE = (
    "https://ali-live.static.yximgs.com/bs2/ztlc/cover_{live_id}_raw.jpg"
)
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



@dataclass(frozen=True)
class TrackerConfig:
    api_url: str
    watch_url: str
    media_name: str
    media_id: str
    push_target: str


def _http_url(value: str, field: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field} 必须是完整的 HTTP(S) 地址")
    return value.strip()


def _read_tracker_config(raw: AstrBotConfig) -> TrackerConfig | None:
    if not raw.get("tracker_enabled", False):
        return None
    api_url = _http_url(str(raw.get("tracker_api_url", "")), "tracker_api_url").rstrip("/")
    parsed = urlparse(api_url)
    if parsed.query or parsed.fragment:
        raise ValueError("tracker_api_url 填 Tracker 服务地址，不包含 #synctv 或查询参数")
    watch_url = _http_url(str(raw.get("tracker_watch_url", "")), "tracker_watch_url")
    name = str(raw.get("tracker_media_name", "木柱的直播")).strip()
    media_id = str(raw.get("tracker_media_id", "")).strip()
    if media_id and re.fullmatch(r"med_[A-Za-z0-9]{1,60}", media_id) is None:
        raise ValueError("tracker_media_id 必须是 med_ 开头的媒体 ID")
    if not name:
        raise ValueError("tracker_media_name 不能为空")
    target = str(raw.get("push_target", "")).strip()
    if not target:
        raise ValueError("push_target 不能为空")
    return TrackerConfig(api_url, watch_url, name, media_id, target)


def _tracker_snapshot(data: dict, config: TrackerConfig, media_id: str) -> LiveSnapshot:
    if data.get("mediaId") != media_id or not isinstance(data.get("active"), bool):
        raise ValueError("Tracker 返回了无效的直播状态")
    active = data["active"]
    started = str(data.get("startedAt", ""))
    if active and (not started.isdecimal() or int(started) <= 0):
        raise ValueError("Tracker 返回了无效的开播时间")
    return LiveSnapshot(active, f"{media_id}:{started}" if active else "", config.media_name, config.media_name)


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
    return LiveSnapshot(
        is_live=True,
        live_id=live_id,
        streamer_name=str(streamer_name),
        title=str(title),
        cover_url=_LIVE_COVER_URL_TEMPLATE.format(live_id=live_id),
    )


@register(
    "astrbot_plugin_acfun_live_monitor",
    "bpking",
    "AcFun and Tracker live room monitor",
    "0.4.0",
)
class AcFunLiveMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._tracker_task: asyncio.Task | None = None
        self._tracker_previous: LiveSnapshot | None = None
        self._tracker_identity: tuple | None = None
        self._tracker_config_error = ""
        self._initialized = False
        self._was_live = False
        self._last_live_id = ""
        self._last_live_snapshot: LiveSnapshot | None = None
        self._room_url = ""
        self._last_config_error = ""

    async def initialize(self) -> None:
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession(headers=_HEADERS)
        self._task = asyncio.create_task(self._poll_loop())
        self._tracker_task = asyncio.create_task(self._tracker_poll_loop())
        logger.info("AcFun / Tracker live monitor started; polling interval: 60 seconds")

    async def terminate(self) -> None:
        if self._tracker_task is not None:
            self._tracker_task.cancel()
            await asyncio.gather(self._tracker_task, return_exceptions=True)
            self._tracker_task = None
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
                self._last_live_snapshot = None

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
        should_notify_live = (
            self._initialized
            and snapshot.is_live
            and (not self._was_live or snapshot.live_id != self._last_live_id)
        )
        should_notify_offline = (
            self._initialized and self._was_live and not snapshot.is_live
        )
        last_live_snapshot = self._last_live_snapshot

        self._was_live = snapshot.is_live
        self._last_live_id = snapshot.live_id if snapshot.is_live else ""
        if snapshot.is_live:
            self._last_live_snapshot = snapshot
        self._initialized = True

        if should_notify_live:
            name = snapshot.streamer_name or "AcFun 主播"
            title = snapshot.title or "未提供标题"
            text = f"🟢 {name} 开播了！\n{title}\n{config.room_url}"
            sent = await self._send_notification(
                config.push_target, text, snapshot.cover_url
            )
            if sent is False:
                logger.warning(f"AcFun live monitor could not send to {config.push_target}")
            else:
                logger.info(f"AcFun live notification sent: {name}")
            return

        if not should_notify_offline:
            return

        name = (
            last_live_snapshot.streamer_name
            if last_live_snapshot is not None
            else "AcFun 主播"
        ) or "AcFun 主播"
        title = (
            last_live_snapshot.title
            if last_live_snapshot is not None
            else "未提供标题"
        ) or "未提供标题"
        text = f"🔴 {name} 下播了。\n{title}\n{config.room_url}"
        sent = await self._send_notification(config.push_target, text, "")
        if sent is False:
            logger.warning(f"AcFun live monitor could not send to {config.push_target}")
        else:
            logger.info(f"AcFun live end notification sent: {name}")

    async def _tracker_poll_loop(self) -> None:
        disabled_logged = False
        while True:
            try:
                config = _read_tracker_config(self.config)
                self._tracker_config_error = ""
            except ValueError as error:
                message = str(error)
                if message != self._tracker_config_error:
                    logger.warning(f"Tracker monitor config error: {message}")
                    self._tracker_config_error = message
                config = None
            if config is None:
                if not disabled_logged and not self._tracker_config_error:
                    logger.info("Tracker monitor 未启用：请在插件设置中开启 tracker_enabled")
                    disabled_logged = True
                self._tracker_identity = None
                self._tracker_previous = None
                await asyncio.sleep(30)
                continue
            disabled_logged = False
            identity = (config.api_url, config.media_id, config.media_name, config.push_target, config.watch_url)
            if identity != self._tracker_identity:
                self._tracker_identity = identity
                self._tracker_previous = None
                logger.info(f"Tracker monitor 已启用：{config.media_name}，媒体 ID：{config.media_id or '按名称查找'}，每次检查后等待 {_POLL_INTERVAL_SECONDS} 秒")
            try:
                snapshot = await self._fetch_tracker_snapshot(config)
                status = "正在直播" if snapshot.is_live else "未开播"
                previous = self._tracker_previous
                if previous is None:
                    action = "首次检测，仅记录状态，不补发通知"
                elif previous.is_live == snapshot.is_live and previous.live_id == snapshot.live_id:
                    action = "状态未变化，不重复通知"
                else:
                    action = "检测到状态或场次变化，准备通知"
                logger.info(f"Tracker monitor 检查成功：{config.media_name}，{status}；{action}")
                await self._handle_tracker_snapshot(config, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Network/permission/lookup failures must never become offline events.
                logger.warning(f"Tracker monitor check failed: {error}")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _tracker_get(self, config: TrackerConfig, path: str, params: dict) -> dict:
        if self._session is None:
            raise RuntimeError("HTTP session is not initialized")
        async with self._session.get(
            config.api_url + "/api/synctv/" + path, params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            data = await response.json()
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("Tracker 接口未返回有效数据")
        return data

    async def _fetch_tracker_snapshot(self, config: TrackerConfig) -> LiveSnapshot:
        media_id = config.media_id
        if not media_id:
            matches = {}
            for page in range(1, 101):
                data = await self._tracker_get(config, "playlist", {"page": page, "pageSize": 50})
                if not any(key in data for key in ("media", "playlists", "total", "fileCount", "playlistCount", "page")):
                    raise ValueError("Tracker 片单响应不完整")
                items = data.get("media", [])
                folders = data.get("playlists", [])
                if not isinstance(items, list) or not isinstance(folders, list):
                    raise ValueError("Tracker 片单响应格式错误")
                for item in items:
                    if item.get("name") == config.media_name:
                        if item.get("sourceProvider") not in (5, "5", "SOURCE_PROVIDER_RTMP"):
                            raise ValueError("指定条目不是 SyncTV 自建推流直播")
                        candidate = item.get("id")
                        if not isinstance(candidate, str) or not re.fullmatch(r"med_[A-Za-z0-9]{1,60}", candidate):
                            raise ValueError("Tracker 片单媒体 ID 无效")
                        matches[candidate] = item
                count = len(items) + len(folders)
                if count < 50:
                    break
            else:
                raise ValueError("片单过大，请直接配置 tracker_media_id")
            if len(matches) != 1:
                raise ValueError("根片单中没有唯一匹配的直播，请检查名称或配置 tracker_media_id")
            media_id = next(iter(matches))
        data = await self._tracker_get(config, "live-status", {"mediaId": media_id})
        return _tracker_snapshot(data, config, media_id)

    async def _handle_tracker_snapshot(self, config: TrackerConfig, snapshot: LiveSnapshot) -> None:
        previous = self._tracker_previous
        if previous is not None:
            opened = snapshot.is_live and (not previous.is_live or snapshot.live_id != previous.live_id)
            closed = previous.is_live and not snapshot.is_live
            if opened or closed:
                text = (f"🟢 {config.media_name} 开播了！" if opened else f"🔴 {config.media_name} 下播了。") + f"\n{config.watch_url}"
                # Commit only after delivery succeeds so transient send failures retry.
                sent = await self._send_notification(config.push_target, text, "")
                if sent is False:
                    raise RuntimeError("Tracker 直播通知发送失败，下次检查将重试")
                logger.info(f"Tracker live notification sent: {config.media_name}")
        self._tracker_previous = snapshot

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
