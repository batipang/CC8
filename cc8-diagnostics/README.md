# CC8 离线能力检测器

Android Java / 平台控件应用，包名 `com.happyhome.cc8diagnostics`，版本 1.0.3，与语音助手独立安装。不联网，不 Root，不读取设备序列号 / MAC / 账户标识，不自动录音。

可查看 Android / CPU / 内存 / 屏幕 / 音频能力，测试参考音、麦克风录音、10 秒同时录放，导出 JSON 报告与最近录音 WAV。录音仅在前台点击并授予麦克风权限后开始；离开页面会取消并释放音频资源。参考音 `app/src/main/assets/reference.wav` 为测试播放素材，不是用户录音。

“音频测试”页新增“双声道统计 6 秒”和“四声道统计 6 秒”。Android 7 以上还可选择 UNPROCESSED 原始输入源。多声道统计沿用所选输入源与采样率，使用原音频模式，输出各通道电平、通道间相关性、相同采样比例和完整帧数。此模式只在内存中累计数字，不生成 WAV，也不覆盖之前的单声道录音。离开前台或点击停止会释放收音资源。端口声明支持多声道仍需实测，完全重复、静音或相关通道不能直接证明具有独立麦克风或定位能力。

多声道检测可勾选“多声道统计时播放本机参考音”，在同一个有界窗口内播放固定参考素材并采集通道统计。报告记录参考采样率、写入帧数和实际播放头，便于比较各输入通道与本机输出的关系。停止或离开前台会同时释放播放与收音设备。

AEC 显示可用或创建成功不代表实际消回声有效；应分别录制安静时纯播放和有人讲话的双讲对照，确认残余回声与人声保留情况。

在仓库根目录构建、检查并安装：

```sh
python3 cc8-diagnostics/tools/build.py
python3 cc8-diagnostics/tools/test-audio.py
python3 device-access/cc8_install.py --install artifacts/cc8-diagnostics-1.0.3.apk --launch --shared-staging
```

`--shared-staging` 的共享存储替换风险及工具链、开发签名和设备工程通道见[开发说明](../docs/DEVELOPMENT.md)及[APK 安装步骤](../docs/APK_INSTALL.md)。报告和 WAV 保存在设备本地，可能包含个人内容；仓库不包含用户实机报告与录音。
