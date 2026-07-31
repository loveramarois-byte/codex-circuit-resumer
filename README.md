# Codex Circuit Resumer / Codex 熔断续聊

[![CI](https://github.com/loveramarois-byte/codex-circuit-resumer/actions/workflows/ci.yml/badge.svg)](https://github.com/loveramarois-byte/codex-circuit-resumer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black?logo=apple)](#环境要求)
[![Release](https://img.shields.io/badge/release-2.5.5-blue)](CHANGELOG.md)

一个面向 macOS、CC Switch 与 Codex Desktop 的本地守望工具。它监控中转线路熔断、模型池满载和临时网关故障，在条件恢复后继续原 Codex 对话，并提供渠道健康、真实倍率、人民币费用及 Claude Desktop 渠道状态总览。

> 这是社区维护的非官方项目，与 OpenAI、Codex 或 CC Switch 官方无隶属关系。

## 为什么需要它

低倍率中转 API 常通过 CC Switch 组成故障转移队列，但长时间无人值守任务仍可能因为以下情况停止：

- CC Switch 渠道熔断后，Codex 对话不会自行继续；
- `Selected model is at capacity`、429、502、503、504 或连接超时导致任务中断；
- 某条线路余额不足、分组失效或网关返回 OpenResty HTML 400；
- Codex 被切回直连配置，绕开了 CC Switch 的候补队列；
- 运行一整夜时，用户无法及时点击“继续”。

Codex Circuit Resumer 将这些情况收敛成一个轻量的本地后台守望器和一个可点击的 SwiftUI 桌面界面。

## 核心能力

### 自动恢复原对话

- 监听 CC Switch 熔断器 `Open -> Closed`，线路稳定后调用 `codex exec resume`；
- 扫描 Codex rollout 中的模型满载、限流、503、超时和余额不足等可重试错误；
- 使用指数退避、随机错峰和上游 `Retry-After`，避免反复冲击低速渠道；
- 成功、人工继续或出现登录/审批等不可自动处理问题时及时收队；
- 保留原 thread，不新建重复对话。

### 模型满载自动降档与恢复

- 推理强度可按 `xhigh -> high -> medium -> low` 逐档降低；
- 默认最低停在 `low`，不会无限降低；
- 降档后每 15 分钟尝试回到原推理强度；
- 通过单次 CLI 配置覆盖实现，不修改用户的全局 Codex 设置；
- 已经运行中的单次请求不会被强制终止，升档在下一次安全续接时生效。

### CC Switch 渠道体检

- 分别监控 Codex 与 Claude Desktop，不监控 Claude Code；
- 严格保持 CC Switch 中的原始渠道排列顺序；
- 显示当前线路、候补线路、降级、熔断、余额不足、鉴权失败和分组失效原因；
- 同时展示 CC Switch 备注倍率与可验证的网站真实倍率；
- 支持 Sub2API billing 信息，并对可识别接口探测余额；
- API Key 仅在本机内存中用于查询，不写入界面或运行日志。

### 人民币费用对账

- 显示 CC Switch 当天标准成本、中转 API 折前价和已核实倍率部分的折后估算；
- 单独列出缺失倍率或已删除渠道导致的“暂时无法折算”金额；
- 美元金额统一换算成人民币，并标明汇率来源与更新时间；
- 金额每 30 秒从 CC Switch 本地日志重新汇总；
- 折后金额明确标记为估算，不冒充中转站账单。

### 本地可靠性与隐私

- 使用 macOS LaunchAgent 后台运行，关闭窗口不影响守望；
- 接电时通过 `caffeinate -s` 减少夜间系统睡眠造成的中断；
- 状态原子写入并保留安全备份，可接管异常退出后的遗留续接进程；
- 日志轮转并限制保留数量；
- 不上传 Codex 对话、rollout、CC Switch 数据库、API Key 或运行日志；
- 核心后台仅使用 Python 标准库，不引入常驻第三方依赖。

## 工作流程

```text
CC Switch / Codex rollout
          |
          v
  本地故障识别与去重
          |
          +--> 不可重试：标记“需人工处理”
          |
          +--> 可重试：指数退避 / 换线 / 推理降档
                              |
                              v
                    codex exec resume 原 thread
                              |
                     成功清理 / 失败继续守候
```

## 环境要求

- macOS 13 Ventura 或更高版本；
- 已安装并配置 CC Switch；
- 已安装 Codex Desktop，或系统中存在可用的 Codex CLI；
- Codex 已完成登录；
- 夜间无人值守建议 Mac 接电并保持开盖。

当前实现主要在 Apple Silicon Mac 上验证。Intel Mac 理论上可运行 Python 后台，但默认构建脚本生成 arm64 应用，需要自行调整 Swift 编译目标。

## 快速开始

```bash
git clone https://github.com/loveramarois-byte/codex-circuit-resumer.git
cd codex-circuit-resumer
./scripts/build_app.sh
open "dist/Codex熔断续聊.app"
```

首次打开后点击“启动守望”。应用会安装登录用户级 LaunchAgent，并把运行数据放在：

```text
~/Library/Application Support/CodexCircuitResumer/
```

也可以直接使用命令行：

```bash
./scripts/control.sh start
./scripts/control.sh status
./scripts/control.sh providers
./scripts/control.sh sleep-check
```

卸载后台守望器：

```bash
./scripts/control.sh uninstall
```

卸载不会删除 CC Switch 或 Codex 数据。

## 界面说明

- **总览**：后台健康、正在续接、待重试和需人工处理；
- **渠道**：Codex / Claude Desktop 渠道顺序、倍率、余额、费用、延迟和故障原因；
- **事件**：近期熔断、恢复、排队和续接结果；
- **设置**：重试间隔、持续守候、模型满载自动降档及自动升回；
- **系统**：运行自检、睡前检查、打开日志和卸载。

## 配置

默认配置见 [`config.example.json`](config.example.json)。启动脚本会将缺失字段合并到用户配置，并保留用户已经修改的值。

常用配置：

| 字段 | 默认值 | 说明 |
| --- | ---: | --- |
| `model_retry_until_success` | `true` | 持续重试，直到成功或需要人工处理 |
| `model_retry_base_seconds` | `60` | 首次重试等待 |
| `model_retry_max_seconds` | `900` | 指数退避上限 |
| `capacity_reasoning_fallback_enabled` | `true` | 模型满载时自动降低推理强度 |
| `capacity_reasoning_minimum` | `low` | 自动降档最低档位 |
| `capacity_reasoning_promote_enabled` | `true` | 定期尝试恢复原推理强度 |
| `capacity_reasoning_promote_after_seconds` | `900` | 恢复探测间隔 |
| `provider_snapshot_seconds` | `30` | 渠道与费用快照间隔 |

## 项目优点

- **真正续接原对话**：以 Codex thread ID 恢复，不复制上下文、不制造新任务；
- **面向低倍率中转场景**：熔断、余额不足、网关 400、503 和模型池满载统一处理；
- **小而美**：Python 标准库后台加原生 SwiftUI，不需要 Electron、Docker 或独立数据库；
- **默认安全**：不打印 Key，不上传对话，写配置前保留私密备份；
- **行为可解释**：界面显示为什么不可用、何时重试、使用哪个倍率和费用口径；
- **有回归验证**：包含单元测试和 50 次进程级实机操练脚本。

## 已知限制

- 目前仅提供 macOS 桌面端和 LaunchAgent 集成；
- 依赖 CC Switch SQLite 表结构、日志格式以及 Codex rollout/state DB，相关上游版本变化可能需要适配；
- 无法在一个已经运行的 Codex 请求中途热切换推理强度，只能在下一次续接时调整；
- 网站真实倍率和余额查询只覆盖已识别的接口，未识别站点会显示“无法验证”；
- 折后费用根据网站当前倍率估算，不等同于充值平台最终账单或历史结算单；
- 合盖、退出 macOS 登录会话或电池睡眠属于系统级暂停，LaunchAgent 无法绕过；
- 当前应用使用 ad-hoc 签名，尚未提供 Apple Developer ID 公证发行包；
- 该工具不能解决失效账号、人工审批、MCP 配置错误或永久性模型不兼容。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/daemon.py tests/real_machine_drill.py
jq empty config.example.json
zsh -n scripts/control.sh scripts/build_app.sh
./scripts/build_app.sh
python3 tests/real_machine_drill.py --app "dist/Codex熔断续聊.app"
```

实机操练覆盖故障续接、模型满载降档、OpenResty HTML 400 换线、真实渠道快照、状态恢复、睡前检查和应用启动。

## 安全

请勿在 Issue 中提交真实 API Key、`config.toml`、CC Switch 数据库、Codex rollout 或包含对话内容的日志。安全问题请参考 [`SECURITY.md`](SECURITY.md)。

## 贡献

欢迎提交可复现的 Issue 和范围清晰的 Pull Request。请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 社区致谢

Council Lab 认可并感谢 [LINUX DO](https://linux.do/) 社区及佬友们对开源交流、软件开发和项目成长提供的支持。

重试与守护设计参考了 [Tenacity](https://github.com/jd/tenacity)、[pybreaker](https://github.com/danielfm/pybreaker)、[Uptime Kuma](https://github.com/louislam/uptime-kuma)、[Healthchecks](https://github.com/healthchecks/healthchecks) 和 [Supervisor](https://github.com/Supervisor/supervisor) 的公开工程实践。

## 许可证

[MIT License](LICENSE)
