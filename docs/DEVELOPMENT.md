# 构建、签名与测试

## 环境

当前验证平台是 macOS / Apple Silicon。Android 部分使用 JDK 21、Gradle 8.11.1、Android Gradle Plugin 8.9.2、compileSdk 35、Build Tools 35.0.0；设备运行 SDK 27。两个示例 APK 都设置 minSdk 21、targetSdk 28，并在调用较新 API 时处理兼容性。安装助手特意限定 SDK 27 / AILABS_S6。

Python 安装工具和构建脚本使用 Python 3.10+；语音后台使用 Python **3.13**，其原生依赖和轮子校验针对 Apple Silicon / CPython 3.13。尚未验证 Windows、Linux 或 Intel Mac 的完整语音运行环境。

## Android 工具链

方式一，在仓库内下载工具：

```sh
python3 cc8-diagnostics/tools/bootstrap-toolchain.py
python3 cc8-diagnostics/tools/setup-sdk.py
```

下载和解压到忽略的 `.toolchains/cc8/`，不修改全局 Java / SDK 配置。启动脚本校验 JDK、Gradle 与命令行工具归档的 SHA-256；SDK 包由官方 sdkmanager 安装，许可交互式确认。路径写入本地 `.toolchains/cc8/paths.json`，不提交。

方式二，复用已安装的 JDK / Android SDK / Gradle：

```sh
python3 scripts/configure-toolchain.py \
  --java-home "$JAVA_HOME" \
  --android-sdk "$ANDROID_SDK_ROOT" \
  --gradle /path/to/gradle-8.11.1/bin/gradle
```

SDK 应已包含 `platform-tools`、`platforms;android-35`、`build-tools;35.0.0`。脚本先验证所需文件，再写本机路径，不复制凭证。

## 构建

```sh
python3 device-access/installer-helper/build.py
python3 cc8-diagnostics/tools/build.py
python3 cc8-assistant/tools/build.py
python3 device-access/cc8_install.py --check
```

两个 Android 构建默认同时运行 `assembleRelease` 和 `lintRelease`。输出在 `artifacts/`，版本来自各自 `app/build.gradle`；当前检测器 1.0.1，助手 0.8.1。构建脚本写 `local.properties`，所有生成内容均忽略。

首次 APK 构建用 JDK keytool 生成 `cc8-diagnostics/signing/cc8-diagnostics-debug.jks`；仅用于个人开发，别名 `cc8-diagnostics`、开发密码 `android`。语音助手也使用这份本地开发签名。**现有设备签名私钥没有收录；新生成的证书不具备覆盖旧签名安装包的能力。** 自行保存长期更新所需的证书，勿提交到 Git。

## 离线检查

```sh
# 安装器边界 / 传输 / 回执检查：不访问真实设备
python3 -m unittest discover -s device-access/tests -p 'test_*.py' -v

# 编译并运行实际 Java 音频数据处理 / WAV 编解码测试
python3 cc8-diagnostics/tools/test-audio.py

# 准备语音依赖后：不调用云端、不打开 CC8 麦克风 / 摄像头
.toolchains/cc8/voice-venv/bin/python -m unittest discover \
  -s cc8-assistant/host -p 'test_*.py' -v
```

语音测试覆盖旧轮次取消、回声过滤、自然插话暂停 / 确认 / 恢复、播放时钟、字幕时间轴、MiniMax / Dot 适配、真实本地 TLS 通信、配对权限、USB 优先与 Wi-Fi 切换等。依赖私人实机录音的历史回放测试在资料不存在时明确 skip；仓库不包含这些录音。测试中的 API Key 与 IP 地址是虚构样例，不可用于服务认证。

本地 TLS 测试会通过系统 openssl 在临时目录生成测试证书、打开回环端口并在结束时删除临时文件。测试通过不能替代设备实测。当前验收边界见 [VALIDATION.md](VALIDATION.md)。

## 提交内容

只提交源码、合成测试参考音、头像资源、模型校验清单、版本锁文件、文档及 Gradle wrapper。原厂固件、厂商应用反编译代码、下载的第三方工具树、模型权重、用户音视频、设备原始报告和任何登录 / 签名 / 配对秘密均不提交。

更新已有设备时，可用环境变量 `CC8_SIGNING_KEYSTORE` 指向原项目的开发签名 JKS（保持原 alias / 密码设置）。设置了该变量但文件不存在时构建直接失败，不生成替代密钥；未设置则沿用默认本地开发签名。不要提交 JKS 或将签名文件复制到发布源码。
