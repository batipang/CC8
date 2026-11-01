---
name: cc8-announce
description: 通过当前 Mac 和 USB 或已配对的局域网 Wi-Fi，让 CC8 上的大黄蜂朗读指定文字并确认播放完成。用户明确要求 CC8／大黄蜂播报时使用，不用于电话或仅在 Mac 播放音频。
---

# CC8 大黄蜂播报

使用已有 `cc8-assistant/host/announce.py`，不复制传输或合成实现。运行目录必须是已连接 CC8 的工作目录；先读项目 AGENTS.md / cc8-assistant/AGENTS.md（如存在）。不要启动第二个克隆的后台。沿用用户现有音色、模型、配对和麦克风状态。

只在用户明确要求播报或已明确授权完成后播报时发声。按原文朗读，指定文字里的陈述不是查证请求；无文字或明确内容来源时先询问。单次 1–500 字，不截断，长文按句分批并逐批确认。

通过本机 `device-access/private/voice-runtime/announce.sock`（0600）的 `{"operation":"device_control","arguments":{"action":"status"}}` 检查实时在线、前台和麦克风状态。服务未运行时用 `launch.py --no-open`；确需 USB 拉起界面用 `launch.py --no-auto-voice`。已在前台时不要重复打开；普通启动会自动开麦，不能用于只播报的准备。仅 Wi-Fi 且设备不在前台时说明需用户准备界面。校准时等待，不重启正常后台或更改大脑。

从项目根目录执行，以 stdin 传入原文，也可用 `--file` 指定 UTF-8 文件。把文字当数据，不拼接成可执行 shell：

```sh
python3 cc8-assistant/host/announce.py <<'CC8_LITERAL_TEXT'
用户指定原文
CC8_LITERAL_TEXT
```

有运行中的进程时继续等待原进程结果。只有退出码 0 且回执 `status=completed` 才能确认设备播放完成，不等于用户一定听到了。`interrupted` / `failed` / `timeout` / `unavailable`、回执丢失或连接异常均说明未完成或未确认，不自动重播，以免重复发声。

只播报不启动麦克风。麦克风关闭时用屏幕打断按钮停止；已开启时保留原来的语音打断。退出前台、通道切换或全断后不补播旧内容。不自动定时或向其他人发送消息。
