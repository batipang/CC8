# 贡献与验证约定

先读 [AGENTS.md](AGENTS.md)、[文档导航](docs/README.md) 和 [TODO](docs/TODO.md)。TODO 是唯一明细；提交说明关联 P/F 编号，记录问题、实际行为、验证和剩余缺口。本仓库未为完整项目选择新的开源许可，贡献前核对 [NOTICE](NOTICE.md)，不要随贡献擅自加入 LICENSE 或第三方角色授权声明。

## 开发与审查

1. 使用独立 dev venv，按 [DEVELOPMENT](docs/DEVELOPMENT.md) 安装固定开发依赖、JDK 21+、OpenSSL。不要升级正在运行助手的语音环境来跑工具测试。
2. 缺陷用能在基线失败的定向复现验证；新功能覆盖取消、旧 generation、断线、权限、失败清理。纯格式/职责重构与功能修复分开提交，不能靠删除断言或静默 skip 获得绿灯。
3. 按改动运行 `tools/test_all.py`、`tools/quality.py check`；Android 改动做相应 compile/lint。私人音频仅在明确授权且显式 `--with-private-audio` 时回放；缺少核心依赖必须失败。保留通过/跳过/失败数和原因。
4. 涉及服务/工具时验证最小 PATH 与真实登录条件，并区分工具可发现、本地初始化、远端请求和实机体验。不要把假 API、合成音、CI 或加速时钟写成真人/设备/长期验收。
5. 通过 PR 合入 main。Required checks 强制、分支须跟上 main；核对实际通过的 head SHA 后合入，不绕过失败门槛，不用无结果的“应当通过”代替 CI。

PR 描述围绕最终问题/行为、相关 P/F 编号、测试证据、部署范围和未完成项。不要提交 API key、Codex 登录、配对、私钥、私有配置、设备 ID、原始报告、用户录音/照片、下载模型、构建缓存或 APK。public fixture 使用通用合成数据；安全问题走 [私密报告](SECURITY.md)。

## 设备和授权边界

- 普通 Android、唯一 USB AILABS_S6 / SDK27 / shell UID2000；原签名集合、流/安装后哈希和实际 PackageInstaller 完成回执均须核对。不引入 Root、分区写入、BootROM、网络 ADB、停用原厂服务或静默卸载/清数据。
- 受保护 staging 是默认；显式 shared-staging 有其他应用替换临时助手的剩余风险，不能用哈希掩盖或自动降级。参见 [安装](docs/APK_INSTALL.md) 与 [签名](docs/SIGNING.md)。
- 无关 QA 不打开自动收音界面。维护用 --no-auto-voice / --no-open；Home、手动 Stop、两路全断后停止采集，重连不能重开。相机只接受已授权的显式前台单次请求。
- Dot 继承主人的模型/技能/工具与现有审批，不自动批准。记录真实执行结果；停止朗读不改写已完成工作。失败/断连后不自动重放写入、拍照、上传或其他未确认操作。

新机型反馈用 [兼容表单](https://github.com/batipang/CC8/issues/new?template=compatibility.yml) 的四字段摘要，不收集唯一设备标识或完整日志。真实设备/固件结论保留版本和来源等级；纯离线贡献不要求作者进行刷机或打开私人设备。
