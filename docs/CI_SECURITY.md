# CI 与依赖安全检查

工作流：[CI](https://github.com/batipang/CC8/actions/workflows/ci.yml)。触发 main push、所有 PR、手动运行及每周一巡检。离线测试不接入真实设备、不读取模型密钥或私人录音；安装和安全数据库查询需要联网。工作流不部署助手。

## 检查范围

| 检查 | 环境与入口 | 失败条件 |
| --- | --- | --- |
| 可移植回归 | Ubuntu 24.04、macOS 14；Python 3.13、JDK 21；`python tools/test_all.py` | 核心缺依赖、失败、超时均失败；4 项私人录音用例明确 opt-in skip |
| 格式 | Ubuntu；`python tools/quality.py check` | 与本地 Ruff / Java formatter 相同锁版、规则及范围，只读检查 |
| 工作流 | actionlint 1.7.12 | YAML、表达式、job/step 等检查失败；不声称包含 shellcheck（显式禁用可选 shellcheck 集成） |
| Android | Ubuntu、JDK 21、Gradle 8.11.1；两个工程 `assembleRelease` / `lintRelease` | 编译或 lint 失败；只生成一次性 CI 开发签名，不上传 APK、签名或本机配置 |
| 密钥 | gitleaks 8.30.1；完整 Git 历史 `--all` | 发现未处置条目或扫描错误；`--redact=100`，不输出原始秘密，不上传泄露报告 |
| Python 依赖 | pip-audit 2.10.1；开发、安全工具、运行锁分别扫描 | 已知漏洞、无法查询的包、缺失报告、请求错误均失败；不自动 `--fix` |
| 合入门槛 | `Required checks` 汇总所有 job 和 matrix 结果 | 任何失败、取消或跳过的整组均失败，不能以最后一个成功 job 掩盖前序失败 |

Actions 引用固定完整 commit SHA，默认 token 仅 contents:read，checkout 不保留 Git 凭证；不使用 pull_request_target 或仓库秘密。CI 不缓存 Gradle，避免 PR 写入共享构建缓存。报告仅保留 lint 和包版本/公开漏洞信息，7 天过期。

原工作流配置及实际运行记录见 [VALIDATION.md](VALIDATION.md)。macOS/Ubuntu 测试通过不代表完整 Linux 语音服务、CC8 物理切换、AEC 或长期运行已通过验收。

## 本地复现

离线检查沿用 [开发说明](DEVELOPMENT.md#轻量测试与质量检查)。安全工具使用另一个环境，避免加重离线测试或修改当前语音运行环境：

```sh
python3.13 -m venv .toolchains/cc8/security-venv
.toolchains/cc8/security-venv/bin/python -m pip install -r requirements-security.txt
.toolchains/cc8/security-venv/bin/python tools/ci_tools.py setup gitleaks
.toolchains/cc8/security-venv/bin/python tools/ci_tools.py setup actionlint
.toolchains/cc8/security-venv/bin/python tools/security.py secrets
.toolchains/cc8/security-venv/bin/python tools/security.py audit
"$(.toolchains/cc8/security-venv/bin/python tools/ci_tools.py path actionlint)" -shellcheck=
```

setup 才下载；工具官方 release 归档的 SHA-256 锁在 `tools/resources/ci-tools.json`。path 在每次使用前核对归档和解压后的可执行文件；不执行归档脚本，不 extractall，不接受链接条目。当前提供 Linux x86_64 和本机 macOS arm64 的 CLI 下载条目，其他平台明确失败。格式工具独立使用原有 SHA 清单。

secrets 扫描**已经提交的历史**，不等于扫描所有未提交文件；提交前仍需检查 staged diff，提交后的 PR CI 扫描该提交。唯一 `.gitleaksignore` 条目限定到原提交、文件、规则及行号：SenseVoice 的 `tokens.txt` 公共词表文件 SHA-256 被当成 API token。没有忽略整个文件、目录或默认规则。新增例外必须说明具体误报证据，不能用通用 baseline 隐藏未核查条目。

审计 JSON 在忽略的 `artifacts/security/`；开发、审计工具和运行环境分开查询，避免相同包不同版本发生冲突。`--no-deps --disable-pip` 只查询已声明的固定版本，不在 Ubuntu 安装 Apple Silicon 原生轮子。**覆盖的是三个锁定清单中的包，不证明所有运行时自动解析的间接依赖、Gradle 依赖、模型或二进制工具都无漏洞。** 增加/调整实际运行环境时仍需审核完整依赖变化，不用零告警代表无风险。

本轮把源代码中的 `mlx` / `mlx-metal` 从 0.29.3 更新到 0.29.4，修复上游明确列出的 NPY / GGUF 文件解析问题；未忽略这些告警。见 [NPY 公告](https://github.com/ml-explore/mlx/security/advisories/GHSA-w6vg-jg77-2qg6)、[GGUF 公告](https://github.com/ml-explore/mlx/security/advisories/GHSA-j842-xgm4-wf88)。当前运行助手的环境不会被 CI 或审计自动升级；模型回归和运行环境补丁部署仍需独立验证。

## 更新与下载来源

Dependabot 每周检查根目录 pip 清单、GitHub Actions 和两个 Gradle 工程，只创建 PR，不自动合并、发布或安装。`requirements-runtime.txt` 通过 `-r` 引用原 runtime lock，供 pip 更新发现，不复制第二份版本列表；不能把它装进轻量开发环境。跨清单更新、Ruff required-version、pre-commit 锁版和原生资源校验值应一起审查，不能为了通过 CI 取消版本约束。

| 来源 | 更新/校验方式 | 不包含的承诺 |
| --- | --- | --- |
| Python 开发、安全工具、运行依赖 | 三份固定版本清单、pip-audit；Dependabot pip PR | 未列出的间接依赖未被本轮全量补锁 |
| GitHub Actions / Gradle | SHA 或固定版本；Dependabot PR、Android 编译/lint | Gradle 漏洞专用扫描尚未接入 |
| Java formatter / actionlint / gitleaks | 官方 GitHub release、仓库内 SHA-256 清单 | 仅校验来源/内容固定性，不证明上游代码无恶意 |
| JDK / Gradle / SDK | 原 bootstrap 工具链哈希、官方 SDK manager；CI 用固定版本设置 | SDK 官方包与 runner 镜像不是仓库静态 vendoring |
| AEC wheel / 快速停止模型 / SenseVoice | 现有 `cc8-assistant/host/resources/*.json` 固定来源、版本/修订、SHA；setup 时校验 | 不纳入 pip-audit 的模型内容扫描，不自动下载模型 |

依据：[GitHub Actions 安全配置](https://docs.github.com/en/actions/reference/security/secure-use)、[Dependabot 配置](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)、[pip-audit 范围与限制](https://github.com/pypa/pip-audit)、[Gitleaks 使用说明](https://github.com/gitleaks/gitleaks)。
