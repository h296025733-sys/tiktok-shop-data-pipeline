# TikTok Shop 数据管线

**保留原始输入，统一经营数据口径，再输出其他工具可以直接读取的结果。**

手工报表和 API 返回值都有数据，但并不天然适合进入自动化：日期口径可能不同，分页可能没取完，同一视频可能重复，权限不足时接口也可能只返回一部分能力。下游如果只看一个 CSV，很难知道本轮到底取得了什么。

这个项目把文件导入、平台读取、原始快照、标准化和质量检查组织成独立管线。结果不仅包含数据，还保留状态、错误、来源和能力范围。[飞书经营数据助手](https://github.com/h296025733-sys/feishu-data-assistant) 通过机器 JSON 消费结果，不从终端提示文字猜测任务是否成功。

当前平台能力是只读数据接入与分析。官方授权、店铺报表和原始业务快照由使用者在本机准备，公开仓库提供管线实现、配置示例与隔离测试。

## 两种输入，统一处理方式

### 工作簿快照

从已有工作簿读取指定工作表和快照日期，保留输入来源，再做字段和数据检查。工作簿检查还关注结构、公式、断链与跨表依赖，避免只读取显示值后漏掉计算关系。

### 官方只读 API

根据应用能力、店铺授权、日期与分页参数请求数据，保存原始响应，再生成规范化结果。商品、视频表现、分析、订单归因和风险相关模块分别管理自己的口径与错误。

不是所有应用都能取得所有数据。能力状态需要结合当前权限与地区解释，接口失败也应进入对外合同。

```text
工作簿 / 官方只读请求
  → 原始输入与来源记录
  → 字段规范化、日期与分页处理
  → 重复、缺失、冲突和完整性检查
  → 规范化数据 + 状态与质量信息
  → 机器 JSON / 本地分析 / 飞书消费端
```

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `src/ingest.py` | 工作簿日期快照导入 |
| `src/audit_workbook.py` | 工作簿结构、公式与依赖检查 |
| `src/tiktok/client.py` | 官方请求、错误与响应处理 |
| `src/tiktok/signing.py` | 平台请求签名 |
| `src/tiktok/oauth.py` | 授权交换、刷新与本机凭证管理 |
| `src/tiktok/video_performance.py` | 视频表现分页与规范化合同 |
| `src/tiktok/analytics.py` | 只读分析数据 |
| `src/tiktok/order_attribution.py` | 订单归因相关数据处理 |
| `src/tiktok/raw_integrity.py` | 原始快照与哈希核对 |
| `src/capability_matrix.py` | 能力矩阵与可用状态 |
| `src/doctor.py` | 本地配置、开关、状态库和依赖检查 |

业务模块通过 CLI 对外，签名、授权与安全检查不会重复写进每一个消费端。

## 请求限制：精确到能力、方法与路径

`EndpointPolicy` 在发起请求前检查五项内容：

1. 请求声明的 capability 是否与白名单条目一致。
2. HTTP method 是否一致。
3. URL 是否使用 HTTPS。
4. host 是否为配置中的精确目标。
5. path 是否为精确路径，或匹配首尾锚定的完整表达式。

仅仅“属于某个平台域名”不够，同域名上不同路径仍可能具有不同业务效果。配置位于 `config/datasets/tiktok_read_endpoints.json`。

默认安全模式开启，买家个人信息采集关闭。当前版本没有实现 TikTok 店铺写操作：即使修改写入开关，声明为变更的请求仍会被拒绝。授权 Token 操作是接入管理，与店铺业务写操作分开理解。

## 原始快照：校验已有证据，不重写它来消除错误

原始响应与标准化数据分别保存，快照保留 SHA-256 侧车文件。`raw_integrity.py` 核对内容与已有哈希，已有侧车不因为发现不一致就被悄悄改成新的哈希。

否则一份被替换过的原件会在重新计算后再次显得“检查通过”，无法判断发生过什么。当前检查保留不一致结果，让操作者决定是否重采、核对来源或处理存储问题。

哈希说明文件字节是否与记录相符，不说明平台数据本身正确，也不代替可信签名。

## 结果合同：数据与状态一起交给下游

`--machine-json` 路径将标准输出保留为一份稳定 JSON；错误也使用结构化合同和退出状态，不要求下游解析中文终端文案。

消费端应同时读取：

| 信息 | 为什么需要 |
| --- | --- |
| 数据集与日期范围 | 知道这一轮在查询什么 |
| 行与行数 | 知道实际取得多少数据 |
| 能力 / scope 信息 | 区分没有数据与缺少授权 |
| 错误和完整性信息 | 保留分页、冲突与采集异常 |
| 原始来源与输出记录 | 便于追溯标准化结果 |

没有取得的数据不能自动补为零，分页中断不能自动当作完整快照。不同数据集的具体字段由其模块定义，不存在一份能替代所有业务口径的通用数值表。

## 日期、归因与分析口径

视频表现命令采用开始日期包含、结束日期不包含的范围，便于连续区间拼接。店铺时区、注册口径与业务时区会影响日期解释，应在配置和消费端保持一致。

播放、销售、广告和订单归因不是同一个指标来源。归因模块与风险分析分别处理自己的数据条件，不能根据某条视频有播放就推断有对应订单，也不能把缺失的广告数据当作零费用。

能力矩阵用于显式记录当前数据范围，平台接口版本与权限会变化。仓库保留的是实现时使用的接口配置，真实使用前应按自己的官方应用授权核对。

## 本地准备

要求 Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m src.cli doctor
.\.venv\Scripts\python.exe -m src.cli --help
```

`doctor` 用于检查当前本地状态，不等于真实 API 已经成功调用。`.env` 只填写自己的配置，不能提交正式授权。

### 先做工作簿检查

下面文件、工作表与日期都是示例，需换成自己的输入：

```powershell
.\.venv\Scripts\python.exe -m src.cli audit workbook --file "inputs/demo.xlsx"
.\.venv\Scripts\python.exe -m src.cli ingest workbook-snapshot --file "inputs/demo.xlsx" --sheet "视频表现" --snapshot-date "2026-10-01"
```

### 再配置官方授权

按 `.env.example` 和数据集配置准备应用信息与授权 endpoint。`auth exchange` 通过隐藏输入取得临时授权码，当前 Windows 路径使用系统凭证管理保存 Token：

```powershell
.\.venv\Scripts\python.exe -m src.cli auth status
.\.venv\Scripts\python.exe -m src.cli tiktok capabilities
```

应用是否有相应权限，需要在自己的环境确认；仓库没有代替使用者授权任何店铺。

### 视频表现的机器输出

配置就绪后，可以使用明确日期范围：

```powershell
.\.venv\Scripts\python.exe -m src.cli tiktok video-performance --start-date "2026-10-01" --end-date-exclusive "2026-10-08" --machine-json
.\.venv\Scripts\python.exe -m src.cli tiktok raw-integrity
```

第一条是实际请求入口示例，第二条核对本机保存的原始快照。是否能执行，取决于授权、endpoint、地区和当前已有文件。

## 可以从这些代码开始

| 想了解的问题 | 源码 |
| --- | --- |
| 请求怎样被限制 | [safety.py](src/tiktok/safety.py) |
| 签名与错误怎样处理 | [signing.py](src/tiktok/signing.py)、[client.py](src/tiktok/client.py) |
| 视频分页和结果怎样组织 | [video_performance.py](src/tiktok/video_performance.py) |
| 原始文件为何不能静默改哈希 | [raw_integrity.py](src/tiktok/raw_integrity.py) |
| 导入与工作簿检查 | [ingest.py](src/ingest.py)、[audit_workbook.py](src/audit_workbook.py) |
| 能力和命令入口 | [capability_matrix.py](src/capability_matrix.py)、[cli.py](src/cli.py) |

## 验证与边界

2026-10-07 执行了 48 个离线用例，使用替代 API、请求签名样例与本地测试数据。没有完成真实店铺授权、生产 API 分页和真实经营数据的端到端验收，见 [检查记录](docs/verification.md)。

公开版没有店铺报表、销售明细、raw 快照、正式应用授权和真实企业标识。下游接入时应保留错误与完整性信息，不能只取数值字段便宣称取得了完整经营数据。
