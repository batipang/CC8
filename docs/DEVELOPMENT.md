# 构建、签名与测试

## 环境

当前验证平台是 macOS / Apple Silicon。Android 部分使用 JDK 21、Gradle 8.11.1、Android Gradle Plugin 8.9.2、compileSdk 35、Build Tools 35.0.0；设备运行 SDK 27。两个示例 APK 都设置 minSdk 21、targetSdk 28，并在调用较新 API 时处理兼容性。安装助手特意限定 SDK 27 / AILABS_S6。

Python 安装工具和构建脚本使用 Python 3.10+；语音后台使用 Python **3.13**，其原生依赖和轮子校验针对 Apple Silicon / CPython 3.13。尚未验证 Windows、Linux 或 Intel Mac 的完整语音运行环境。

## 轻量测试与质量检查

开发测试与设备运行环境分开：Python 3.11+（本轮实测 3.13）、JDK 21+、OpenSSL CLI；不需要 Android SDK、API Key、Codex 登录、MLX、ASR 模型或私人录音。运行语音服务仍使用下文的 Apple Silicon / Python 3.13 专用环境，requirements-runtime.lock 不变。

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

# 如已有可用 JDK 可省略；按实际安装位置填写
export JAVA_HOME=/absolute/path/to/jdk21

# 一条命令：安装器、host、开发工具回归、diagnostics Java 音频检查
python tools/test_all.py
# 也可以 make test
```

JDK 按 CC8_TEST_JAVA_HOME → JAVA_HOME → 原有本机 paths.json → PATH 发现，不生成或要求 Android SDK 配置。显式配置错误或版本过低是失败，不能悄悄退回另一个 JDK；缺少核心 Python 依赖也非零退出，并提示安装 requirements-dev.txt。

默认不读取私人录音。现有回放需显式传 `--with-private-audio`，缺少对应文件/原生依赖时逐项报告 skip；不会录制新音频或下载模型。纯协议、状态机和伪造 provider 的用例不会因可选模型缺失整模块跳过。`--suite installer|host|tooling|java` 可只运行相关部分；任一部分失败/超时，统一入口返回非零，同时保留其他部分的结果。

### 格式和提交检查

```sh
# 首次显式下载已锁定 SHA-256 的 Java 格式工具；不下载模型或运行服务
python tools/quality.py setup
python tools/quality.py check
# 或 make check；只检查，不修改源码、不自动下载

python tools/quality.py format
# 或 make format；格式化与功能修改分开提交

python -m pre_commit install
python -m pre_commit run --all-files
```

Python 固定 Ruff 0.17.3、100 列、单引号、4 空格；检查 F/E4/E9 和多语句规则。Java 固定 google-java-format 1.35.0、JDK 21+，不自动重排/删除 import、不重写长字符串或 Javadoc。设置来源：[Ruff 配置](https://docs.astral.sh/ruff/configuration/)、[Java formatter](https://github.com/google/google-java-format/releases/tag/v1.35.0)、[本地 pre-commit hooks](https://pre-commit.com/#repository-local-hooks)。

Python 检查同时运行固定 mypy 2.4.1：`python -m mypy` 从 pyproject.toml 读取 strict=true，覆盖 protocol.py、transport.py、workspace.py 以及新增 config_schema.py / configuration.py。本地 quality、提交钩子和 CI 使用同一入口，不设全局忽略或 Any 兜底。协议递归 JSON、字节流 Protocol、传输事件/通道、任务/提醒 TypedDict 和配置快照明确输入输出；反序列化后的少量 cast 紧接运行时结构校验并写明理由。注解不能替代不可信 JSON、权限、代次和数值的运行时校验；bridge、DSP、平台集成等尚不在严格类型覆盖中。

非敏感配置、环境优先级、登录服务继承与两个 APK 的签名引用统一入口见 [CONFIGURATION.md](CONFIGURATION.md)。指定签名文件缺失时两个工程均失败，不生成替代密钥；此项不代替 F-14 的安装前证书比对。

Java JAR 留在忽略的 .toolchains/cc8/quality，下载大小和 SHA-256 锁在 tools/resources/google-java-format.json；缺失/损坏时 check 失败，只有显式 setup 才下载。1.35.0 对部分注释需要二次格式化，因此 format 有界执行两遍并最终只读检查，仍不稳定则失败。Python 检查/格式化排除工具链、产物、私有数据、构建目录与签名目录。钩子使用独立 Python 环境，仅检查，不调用模型、设备或安装 APK。

纯格式基线提交登记在 .git-blame-ignore-revs；可用 `git config --local blame.ignoreRevsFile .git-blame-ignore-revs` 在当前仓库启用，不修改全局配置。CI 复用上述检查命令，平台、工作流状态和安全扫描说明见 [CI_SECURITY.md](CI_SECURITY.md)；本地通过与真实 CI 结果分别记录。

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

两个 Android 构建默认同时运行 `assembleRelease` 和 `lintRelease`。输出在 `artifacts/`，版本来自各自 `app/build.gradle`；当前检测器 1.0.1，助手 0.8.6。构建脚本写 `local.properties`，所有生成内容均忽略。

首次 APK 构建用 JDK keytool 生成 `cc8-diagnostics/signing/cc8-diagnostics-debug.jks`；仅用于个人开发，别名 `cc8-diagnostics`、开发密码 `android`。语音助手也使用这份本地开发签名。**现有设备签名私钥没有收录；新生成的证书不具备覆盖旧签名安装包的能力。** 自行保存长期更新所需的证书，勿提交到 Git。

## 离线检查

```sh
# 安装器边界 / 传输 / 回执检查：不访问真实设备
python3 -m unittest discover -s device-access/tests -p 'test_*.py' -v

# 编译并运行实际 Java 音频数据处理 / WAV 编解码测试
python3 cc8-diagnostics/tools/test-audio.py

# 使用上文开发环境：不调用云端、不打开 CC8 麦克风 / 摄像头
python -m unittest discover \
  -s cc8-assistant/host -p 'test_*.py' -v
```

语音测试覆盖旧轮次取消、回声过滤、自然插话暂停 / 确认 / 恢复、播放时钟、字幕时间轴、MiniMax / Dot 适配、真实本地 TLS 通信、配对权限、USB 优先与 Wi-Fi 切换等。依赖私人实机录音的历史回放默认明确 skip；仅在显式 opt-in 且资料和可选原生依赖齐备时运行，仓库不包含这些录音。测试中的 API Key 与 IP 地址是虚构样例，不可用于服务认证。

本地 TLS 测试会通过系统 openssl 在临时目录生成测试证书、打开回环端口并在结束时删除临时文件。测试通过不能替代设备实测。当前验收边界见 [VALIDATION.md](VALIDATION.md)。

## 提交内容

只提交源码、合成测试参考音、头像资源、模型校验清单、版本锁文件、文档及 Gradle wrapper。原厂固件、厂商应用反编译代码、下载的第三方工具树、模型权重、用户音视频、设备原始报告和任何登录 / 签名 / 配对秘密均不提交。

更新已有设备时，可用环境变量 `CC8_SIGNING_KEYSTORE` 指向原项目的开发签名 JKS（保持原 alias / 密码设置）。设置了该变量但文件不存在时构建直接失败，不生成替代密钥；未设置则沿用默认本地开发签名。不要提交 JKS 或将签名文件复制到发布源码。
