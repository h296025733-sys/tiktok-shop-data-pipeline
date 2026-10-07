# TikTok Shop 数据管线

这个项目负责把 TikTok Shop 的报表和分析数据变成后续工具能稳定使用的数据。采集、标准化、质量检查和输出各有明确职责，飞书机器人可以通过机器 JSON 合同读取结果。

我希望排查数据问题时能回答清楚：原始数据是什么时候拿到的、哪一页失败了、哪个字段转换过，以及本轮结果是否完整。

## 主要能力

- 接入手工导出报表，保留原件并产生标准化结果。
- 只读分析 endpoint 白名单、日期区间和分页处理。
- 为 raw JSON 保留 SHA-256，检查原始快照是否被改动。
- 输出重复、缺失、类型与采集状态等质量信息。
- 用稳定 JSON 接口给其他工具调用。

默认安全模式开启，店铺写操作和买家个人信息采集关闭。授权配置由每个使用者在本地填写。

## 快速开始

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m src.cli doctor
.\.venv\Scripts\python.exe -m src.cli --help
```

配置自己的应用授权后，可使用 `tiktok video-performance`、`tiktok analytics` 和 `tiktok raw-integrity` 等命令。真实 endpoint 的可用范围取决于应用权限、店铺授权和地区。

`src/` 保存管线与 CLI，`tests/` 保存隔离测试。仓库不含店铺报表、raw 快照、销售明细和正式授权；源码中的原企业标识已改为示例值。

## 本次公开整理的检查

见 [检查记录](docs/verification.md)，其中区分源码与语法检查、隔离测试和未执行的真实环境路径。
