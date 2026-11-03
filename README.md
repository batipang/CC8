# CC8：APK 安装与大黄蜂语音助手

[English](README.en.md) · [文档导航](docs/README.md) · [兼容性反馈](docs/COMPATIBILITY.md) · [贡献](CONTRIBUTING.md) · [安全报告](SECURITY.md)

[![CI](https://github.com/batipang/CC8/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/batipang/CC8/actions/workflows/ci.yml)

整理自一台天猫精灵 CC8 的实际开发记录，包含 **APK 安装工具、离线能力检测器，以及完整的大黄蜂（Bumblebee）语音助手代码**。

已验证设备：底部型号 **TG_S6B01**，Android 型号 **AILABS_S6**，Android SDK **27**，固件 **6.1.8-RS-20251125.1919**。其他硬件或固件的工程入口、ADB 限制和安装权限未验证。

本项目已经走通的安装路径是：正常开机 → 工程调试入口 → USB ADB 工程命令通道 → shell UID 2000 → 原生 `PackageInstaller` API。**不需要 Root、刷机或解锁 Bootloader。** 工具会检查设备型号、SDK、UID、传输哈希和安装回执。 工程模式角落文字本身不代表通道可用；`--probe` 未通过就停止。当前固件的受保护 staging 被拒绝，显式共享兼容路线仍有助手被替换的风险，详见 [适用范围/停止入口](docs/COMPATIBILITY.md) 和 [安装排障](docs/INSTALL_TROUBLESHOOTING.md)。

## 文档

[当前部署：后台0.14.8／保留原APK0.9.3，以及自动验收范围](docs/CURRENT_DEPLOYMENT.md)。[原P0软件交接](docs/P0_SOFTWARE_HANDOFF.md)保留历史快照。

- [从准备设备到安装 APK 的完整步骤](docs/APK_INSTALL.md)
- [构建、签名与测试](docs/DEVELOPMENT.md)
- [原签名备份、导入与安装前证书核对](docs/SIGNING.md)
- [轻量测试、格式规范与提交钩子](docs/DEVELOPMENT.md#轻量测试与质量检查)
- [CI、密钥扫描、依赖审计与更新边界](docs/CI_SECURITY.md)
- [配置来源、私有覆盖、迁移与恢复](docs/CONFIGURATION.md)
- [统一 setup / doctor / run / measure / score-asr / observe 与单实例规则](docs/COMMANDS.md)
- [本地双唤醒词](docs/LOCAL_WAKE.md)
- [分类错误与恢复](docs/FAILURE_FEEDBACK.md)
- [固定语料评分与 A/B](docs/ASR_EVALUATION.md)
- [只读资源曲线与长期观察边界](docs/RUNTIME_OBSERVATION.md)
- [分轮诊断采集与计时边界](docs/MEASUREMENT_RUNS.md)
- [独立版本、兼容性与构建回执](docs/RELEASES.md)
- [语音助手配置与使用](cc8-assistant/README.md)
- [方案一界面、任务与提醒](docs/WORKSPACE.md)
- [功能迭代列表](docs/ROADMAP.md)
- [统一 TODO：功能、工程质量与验收条件](docs/TODO.md)
- [ToFix 合并说明](docs/ToFix.md)
- [P0 第一批：阶段诊断、登录启动与后台恢复](docs/P0_FOUNDATION.md)
- [第二轮竞品复盘（2026-11-01）](docs/COMPETITOR_REVIEW_2026-11-01.md)
- [离线检测器](cc8-diagnostics/README.md)
- [打开助手自动开始语音](docs/AUTO_VOICE.md)
- [摄像头看图与 Dot 分析](docs/VISION.md)
- [从当前 Dot 聊天播报与拍照](docs/DOT_CONTROL.md)
- [返回桌面后一键打开助手](docs/DESKTOP_ENTRY.md)
- [系统架构与 USB / Wi-Fi 通信](docs/ARCHITECTURE.md)
- [已验证结果与尚未验证的边界](docs/VALIDATION.md)
- [全面代码检查与修复记录](docs/CODE_REVIEW.md)

## 快速开始

统一入口（Python 3.9+ 可显示帮助和自检；实际准备选 Python 3.13）：

```sh
./cc8 setup --profile dev       # 独立开发环境；仍需 JDK 21+ / OpenSSL / C++17 编译器
./cc8 doctor --profile dev

./cc8 setup --profile android   # 官方构建工具与交互式 SDK 许可
./cc8 doctor --profile android

# 配好原签名、MiniMax 私有凭证和 owner 的 Codex CLI 后
./cc8 setup --profile voice     # Apple Silicon macOS / Python 3.13
./cc8 doctor --profile voice
./cc8 run                       # 登录后台；不打开 CC8 界面或麦克风
./cc8 run --stop                # 只停当前目录的后台
```

已有其他目录运行的助手时，先在原目录明确停止再切换；新命令不会替你终止或升级它。doctor 为本地只读检查，未验证 API 额度、登录和真实音频效果；可加 `--json`。首次安装与设备端工程入口仍按下文执行，详情见 [命令说明](docs/COMMANDS.md)。

已验证开发环境为 Apple Silicon Mac，Python 3.13、JDK 21、Gradle 8.11.1、Android SDK 35 / Build Tools 35.0.0。所有命令在仓库根目录执行。

```sh
# 首次准备本仓库的本地构建工具；SDK 许可会交互式展示
python3 cc8-diagnostics/tools/bootstrap-toolchain.py
python3 cc8-diagnostics/tools/setup-sdk.py

# 构建临时安装助手和示例检测 APK
python3 device-access/installer-helper/build.py
python3 cc8-diagnostics/tools/build.py

# 本地自检，不接触设备
python3 device-access/cc8_install.py --check

# CC8 正常开机、进入工程模式、用 USB 数据线连接后
python3 device-access/cc8_install.py --probe
python3 device-access/cc8_install.py --install artifacts/cc8-diagnostics-1.0.1.apk --launch --shared-staging
```

这些命令不能替代设备端工程入口；若 `--probe` 未通过，不继续安装。示例中的 `--shared-staging` 是本固件已验证路线的显式兼容选项：临时助手位于共享存储，其他具备存储权限的应用可能替换它，仅适用于你信任设备上应用的环境。默认不加该选项时只使用受保护目录；本机固件会拒绝写入该目录，工具因此停止，不自动回退。详细的成功条件、失败处理与签名注意事项见安装文档。

## 代码目录

| 目录 | 内容 |
| --- | --- |
| `device-access/` | USB 设备识别、APK 流式安装、Java 安装助手与离线测试 |
| `cc8-diagnostics/` | Android 离线硬件 / 音频检测器、构建脚本、Java 测试 |
| `cc8-assistant/app/` | Android 语音终端、头像、字幕、录放、相机预览测试、双通道通信 |
| `cc8-assistant/host/` | Mac 后台、AEC/VAD、ASR、Dot、MiniMax、播报入口及回归测试 |
| `scripts/` | 现有工具链配置、语音运行环境及模型准备 |

语音链路：CC8 麦克风 → USB 优先 / 已配对 Wi-Fi 备用 → Mac 消回声与打断判断 → MiniMax ASR → Dot / Codex → MiniMax TTS → CC8 播放、字幕和表情。设备必须通过原装电源供电，首次局域网配对通过 USB 完成。

仓库不包含 API Key、Codex 登录信息、签名私钥、局域网配对密钥、原厂固件、分区备份、用户录音、设备序列号或原始检测日志。构建产物和模型权重在本机生成或单独下载；历史 BootROM / MTK 尝试不属于成功安装步骤，也未收录可执行工具。
