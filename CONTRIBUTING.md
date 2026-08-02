# Contributing

感谢你帮助改进 Codex Circuit Resumer。

## 提交 Issue 前

请先确认问题可以在最新版复现，并提供：

- 操作系统及版本（macOS 或 Windows）、CC Switch 和 Codex 版本；
- 期望行为与实际行为；
- 已脱敏的错误信息；
- 最小复现步骤。

不要提交 API Key、Token、Codex 对话内容、完整 `config.toml`、CC Switch 数据库或包含个人路径的日志。

## Pull Request

1. 从 `main` 创建功能分支；
2. 保持修改范围清晰，不夹带无关格式化；
3. 为行为变更补充测试；
4. 在本地运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/daemon.py tests/real_machine_drill.py
jq empty config.example.json
zsh -n scripts/control.sh scripts/build_app.sh
```

涉及 macOS 界面的修改还应运行 `./scripts/build_app.sh`，确认应用可以编译和启动。Windows 修改必须在 GitHub `windows-latest` CI 中通过 PSScriptAnalyzer、原生构建、安装生命周期烟测和 Defender 扫描。

## 设计原则

- 本地优先，不上传用户数据；
- 自动化动作必须可解释、可恢复；
- 不修改 CC Switch 原渠道顺序；
- 不把估算金额描述成真实账单；
- 不以重试掩盖登录、审批、权限或永久配置错误；
- 优先保持小体积和低常驻资源占用。
