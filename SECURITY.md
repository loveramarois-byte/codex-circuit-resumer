# Security Policy

## Supported version

安全修复优先应用于最新发布版本。

## Reporting a vulnerability

请优先通过 GitHub 的 **Private vulnerability reporting / Security advisory** 私下报告安全问题，不要先创建公开 Issue。

报告中可以包含：

- 受影响版本；
- 最小复现步骤；
- 可能影响；
- 建议修复方向。

请勿提交真实 API Key、Token、Codex 对话、CC Switch 数据库、浏览器凭证、SSH 私钥或完整个人配置。

## Data handling

Codex Circuit Resumer 在本机读取必要的 CC Switch 与 Codex 状态。渠道 Key 仅用于用户主动启用的余额或倍率查询，不写入界面、状态快照或运行日志。项目不提供遥测和云端数据收集功能。
