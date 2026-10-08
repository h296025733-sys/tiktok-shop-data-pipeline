# TikTok Shop 数据管线

**把报表和只读分析数据转成可追溯的标准化结果，供其他工具稳定调用。**

同一轮数据可能来自手工导出文件和不同 API 分页。这个项目保留原始输入、规范字段、检查重复与缺失，再输出机器 JSON；[飞书经营数据助手](https://github.com/h296025733-sys/feishu-data-assistant) 可以读取这份合同。

我希望一轮采集之后能说清楚原件来自哪里、哪些页失败、字段怎样转换，以及结果缺了什么。平台没有开放的能力和授权不足的情况也需要出现在结果里。

## 三层约束

| 层 | 实现 |
| --- | --- |
| 请求范围 | [safety.py](src/tiktok/safety.py) 按 capability、HTTPS host、method 与路径检查精确只读白名单；当前版本拒绝店铺写操作。 |
| 数据来源 | 原始快照保留 SHA-256；[raw_integrity.py](src/tiktok/raw_integrity.py) 检查哈希侧车文件，已有哈希不被静默改写。 |
| 对外结果 | 导入、分析与能力状态分别记录；CLI 输出机器 JSON，消费端读取字段与状态，而非从终端文案猜测成功。 |

[ingest.py](src/ingest.py) 负责文件导入，[client.py](src/tiktok/client.py) 与 [signing.py](src/tiktok/signing.py) 负责 API 调用和签名，[capability_matrix.py](src/capability_matrix.py) 管理能力状态。日期、分页、去重、订单归因与质量检查有各自模块。

默认安全模式开启，买家个人信息采集关闭。各使用者在本地准备自己的授权与店铺配置。

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
