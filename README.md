# AcFun 直播监控

这是一个独立的极简 AstrBot 插件，只监控一个 AcFun 直播间，只在检测到新的开播时推送一次通知。通知包含标题、直播间地址和直播封面。

编辑同目录的 `config.json`：

```json
{
  "room_url": "https://live.acfun.cn/live/主播ID",
  "push_target": "平台名:消息类型:会话ID"
}
```

`room_url` 填完整直播间地址。`push_target` 填 AstrBot 的 UMO：在希望接收通知的群聊或私聊中发送 `/sid`，复制返回的 `UMO` 字段。AstrBot 将它作为主动推送的目标。

配置文件会在每次轮询前重新读取，保存后最多等待一分钟即可生效。插件首次启动或更换直播间时只记录当前状态，不补发正在进行中的直播；之后检测到开播或新的直播场次才推送。
