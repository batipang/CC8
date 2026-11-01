# 统一命令与运行所有权

`./cc8` 是仓库内 Python stdlib 入口。Python 3.9+ 能显示帮助、做只读 doctor；setup 使用 Python 3.11+，默认寻找 3.13，可用 `--python /absolute/path/to/python3.13` 指定。完整语音运行环境仍限定 Apple Silicon macOS / CPython 3.13，不把 Linux 离线测试写成语音平台支持。

## 准备

| 命令 | 实际动作 | 边界 |
| --- | --- | --- |
| `./cc8 setup --profile dev` | 创建本仓库 `.toolchains/cc8/dev-venv`、安装固定 dev 依赖，下载校验固定 Java formatter | 不装语音模型、不开设备；JDK 21+ / OpenSSL 仍由 doctor 检查 |
| `./cc8 setup --profile android` | 复用已校验归档的 bootstrap 与 sdkmanager 入口 | SDK 许可交互式展示；不构建/安装 APK，不复制签名 |
| `./cc8 setup --profile voice` | 复用现有语音准备脚本，核对原生 AEC wheel、固定 runtime 清单和停止词模型 | 独立运行环境；不读取凭证内容、不连接设备、不启动助手 |
| `./cc8 setup --profile voice --sensevoice` | 额外准备固定版本和摘要的可选 SenseVoice 模型 | 不改变默认 MiniMax ASR 配置 |

setup 可加 `--dry-run` 输出 argv 计划，不创建环境、不下载、不改配置。拒绝位于本仓库之外或通过 symlink 共享的 venv；语音 setup 在 pip / 模型处理全程占有运行锁，拒绝与运行中的后台同时修改环境。直接运行 `scripts/setup-voice.py` 也遵守同一锁。现有外部 JDK / SDK / Gradle 仍可用 `scripts/configure-toolchain.py` 显式配置。

## 只读诊断

`./cc8 doctor --profile dev|android|voice|all [--json]`，默认 all。必要条件不足退出 2；可选状态未出现不阻止首次启动。每项失败提供修复入口，报告只含就绪位和固定提示，不输出配置值、进程命令、设备身份或密钥。dev 诊断不读语音配置或模型。

- dev：Python 版本与固定包版本、JDK 21+、OpenSSL、Java formatter 摘要。
- android：本机路径中的 JDK、Gradle / SDK 所需文件、配置和签名引用存在性；签名连续性仍须安装前 F-14 核验。未指定签名且尚未首次构建时作为 INFO；显式指定却缺失则必须修复。
- voice：CPython 3.13 / Apple Silicon、完整固定包元数据、停止词模型归档与解压文件摘要、所选 SenseVoice 摘要、配置、MiniMax 文件权限/格式、Codex 与所需 Node 的存在性、跨目录运行所有权。
- 已运行后台的 health / 已认证连接只是附加状态。doctor 不枚举 USB 序列号、不启动桥接、不开麦/摄像头、不下载、不启动 Codex app-server、不调用模型服务。

`network_request_tested=false` 明确表示本地就绪不能证明云端登录、额度或请求成功；Codex 只检查本机工具，不证明模型有权限。旧 Whisper 缓存尚无固定内容清单时，doctor 不把任意 snapshot 当作已验证模型，需先完善其资源验证。

## 启动与停止

`./cc8 run` 先检查 voice 必要条件，再复用当前目录的 macOS 登录服务。`./cc8 run --foreground` 在当前终端运行同一桥接；`./cc8 run --stop` 通过已有 service / launcher 入口明确停当前目录，禁止崩溃恢复重启。命令不自动打开 CC8 界面；设备前台、自动语音、权限和断线规则保持 [AUTO_VOICE.md](AUTO_VOICE.md) 的约定。已有服务不会因为再次 run 自动重载配置，配置更改需明确 stop / run。

桥接在模型加载前取得当前用户的 OS 排他锁，再取得本工作区锁，锁覆盖整个运行期，崩溃由 OS 释放；PID 文件不能授予所有权。不同目录的新版本只能有一份后台。为兼容尚未使用全局锁的旧版本，还只读核对本用户的 bridge / supervisor 进程及 enabled LaunchAgent；旧服务退避期也算占用。冲突只提示回原目录明确停止，不 kill / bootout / 读取另一目录的凭证。

旧版本不遵循新全局锁，因此不能保证一个旧版启动器在新版本检查之后突然启动时也被锁约束；日常切换必须先明确停旧目录。新版本之间的并发竞争由 OS 锁仲裁，不依赖这项进程快照检查。此规则不授权自动迁移当前运行实例、升级 MLX 或覆盖原签名。


## 分轮诊断采集

`./cc8 measure --seconds 60 --condition quiet --distance 1m --partition acceptance` 只读取已运行后台的 owner IPC，聚合最近窗口之外的已观察请求，不启动模型、设备界面或采集。先看到“诊断采集已就绪”再开始操作。现有服务在其他运行目录时，显式 `--socket /absolute/runtime/device-access/private/voice-runtime/announce.sock`，不会迁移或重启它。Python stdlib 即可运行，详见 [采集格式、私有输出与限制](MEASUREMENT_RUNS.md)。
