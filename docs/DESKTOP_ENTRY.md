# 返回 CC8 桌面后打开大黄蜂

从 Android **0.5.2** 起，返回原厂桌面后，右侧保留一个带头像和“大黄蜂”文字的悬浮按钮。点击按钮即可在 CC8 本机打开助手，不依赖 Mac 发送启动命令或 USB 连接。0.5.3 起，主动打开后在已授权、连接就绪时自动开始语音，详见[自动开始说明](AUTO_VOICE.md)；实际语音对话继续依赖 Mac 后台和已配置服务。

按钮可沿右侧上下拖动，位置会保存。助手界面和摄像头测试页在前台时隐藏按钮；回到桌面或其他应用时重新显示。通知栏同时保留“大黄蜂 · 点此打开”的入口。

## 开关与权限

在助手顶部点连接状态，再点“桌面入口”，可以关闭 / 开启入口，或进入悬浮权限授权页。关闭入口后，若原厂桌面没有应用图标，需通过 USB / Mac 启动脚本重新打开助手再开启。

首次需要允许应用“显示在其他应用上层”。当前 TG_S6B01 已通过普通 USB shell 为本应用配置该权限，后续正常返回桌面无需重复操作。新安装可先使用入口设置中的授权按钮；若固件不提供授权页面，开发者可在仓库根目录、USB 连接时执行：

```sh
python3 - <<'PY'
import sys
sys.path.insert(0, 'device-access')
import cc8_install as access
target = access.target()
access.probe(target)
access.shell(target, 'appops set com.happyhome.cc8assistant SYSTEM_ALERT_WINDOW allow')
access.shell(target, 'am start -W -n com.happyhome.cc8assistant/.MainActivity')
PY
```

权限只授予大黄蜂应用，不修改原厂桌面、默认 HOME、固件或原厂语音服务。强行停止应用或重启设备后，目前仍需先打开一次助手，才能重新建立入口；没有加入开机自动启动或后台语音唤醒。

## 实现与隐私

`DesktopEntryService` 是独立的本机界面入口服务：使用静音、持续通知维持入口，通过 Android 悬浮窗口和显式 PendingIntent 打开 MainActivity。它不创建 AudioRecord、摄像头、音频播放器或网络连接，不直接开启麦克风；前台 MainActivity 会按用户明确选择的自动语音设置处理打开动作。

MainActivity 离开前台仍停止录放和通信。已有 Wi-Fi TLS 连接的关闭改到专用工作线程；先同步标记链路失效，再异步关闭 socket，避免 Conscrypt 发送 close_notify 时触发主线程网络异常。未放宽 StrictMode，也未为入口保留后台录音。

## 0.5.2 初始入口验证

- TG_S6B01 / SDK 27 实机安装 0.5.2，沿用原开发签名，安装后 APK 哈希一致；现有应用数据、配对和配置保留。
- 连续 3 次 Home → 点击本机入口 → 助手前台；按 Back 退出 Activity 后重新点击也能打开。
- 上述导航过程中进程保持不变，助手内入口隐藏、桌面入口出现，麦克风没有开启；此前退出时的 NetworkOnMainThreadException 未再复现。
- 拖动入口、重新打开 / 返回桌面后位置保留通过。
- 180 项后台回归无失败（4 项私人录音依赖跳过），Android 编译 / lint 通过；新增 Java 测试验证 TLS 关闭不占用调用线程、关闭异常不传播到界面线程。

截图及原始检测记录仅保存在忽略的私有目录，不上传到仓库。未为这项测试开启摄像头或麦克风，也未重启设备验证开机行为。
