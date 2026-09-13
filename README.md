# AcFun / Tracker 直播监控

这是一个独立的 AstrBot 插件，监控一个 AcFun 直播间，并可额外监控 Tracker 共享放映室中的一条自建直播，在检测到新的开播和下播时分别推送一次通知。开播通知包含标题、直播间地址和直播封面；下播通知包含上一场直播的标题和直播间地址。

在 AstrBot WebUI 的「插件」页面打开本插件设置，先填写 AcFun 的两个配置项：

- `room_url`：完整直播间地址，例如 `https://live.acfun.cn/live/52002191`。
- `push_target`：目标会话的 UMO。在目标群聊或私聊发送 `/sid`，复制返回的 `UMO` 字段。

保存设置后重新加载插件。插件首次启动或更换直播间时只记录当前状态，不补发正在进行中的直播；之后检测到开播、新的直播场次或下播时才推送。

## Tracker 共享放映室通知

保留原来的 AcFun 配置和 `push_target`，再填写：

| 设置 | 本次配置 |
| --- | --- |
| `tracker_enabled` | 开启 |
| `tracker_api_url` | `http://v.738888.xyz` |
| `tracker_watch_url` | `http://v.738888.xyz/#synctv` |
| `tracker_media_name` | `木柱的直播` |
| `tracker_media_id` | 可留空；当前条目 ID 为 `med_3`，也可直接填入 |

两个地址都是 **Tracker** 地址，不是 SyncTV 服务地址。插件不需要 SyncTV 账号、密码或推流密钥。若 AstrBot 访问外网 API 被拒绝，可将 `tracker_api_url` 改成它能访问的 Tracker 内网地址；通知里的 `tracker_watch_url` 仍保留外网链接。

先更新并重启 Tracker，使 `GET /api/synctv/live-status?mediaId=med_3` 接口生效，再更新插件并重新加载。两个监控独立运行、共用通知会话。每分钟检查一次，识别真正的推流状态和开播时间；即使房间正在播放别的影片，也能检测“木柱的直播”开播。

默认通过根片单精确匹配名称；有重名、移到子片单或片单过大时，请直接配置媒体 ID。首次启动、重新加载或修改监控配置仅记录当前状态，不补发已有直播。之后开播/换场和下播各推送一次；查询失败不误报下播，发送失败在后续检查时重试。60 秒内完成的短暂开播或断流可能无法检测。

通知示例：

```text
🟢 木柱的直播 开播了！
http://v.738888.xyz/#synctv
```

观看链接只打开放映室，不会替所有人切换当前影片。

开发验证：`python3 -m unittest discover -s tests -v`（使用模拟 HTTP 和模拟会话，不发送真实通知）。
