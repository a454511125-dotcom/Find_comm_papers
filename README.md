# Find_comm_papers

面向计算传播研究的中英文文献检索与 Zotero 入库 MCP。

Bilingual literature discovery, communication-prioritized ranking, verified PDF retrieval, and resumable Zotero ingestion for computational communication research.

当前源码版本：**0.3.1**。基于 [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp) 定制，保留 MIT 许可证。上游来源和改动范围见 [UPSTREAM.md](docs/UPSTREAM.md)。本仓库提供源码，不包含 Python 运行时、浏览器、账号授权、文献全文或个人文库数据。

## 功能

- **中英文联合检索**：中文通过兼容的本地 CNKI 后端；英文通过 OpenAlex、Crossref、arXiv、Semantic Scholar、dblp 等入口。
- **传播学优先**：同时保留计算社会科学、计算机及 Nature、Science 等综合期刊中的相关研究。默认权重为主题相关性 65%、学科匹配 25%、时间 5%、引用量 5%。
- **知网单关键词检索**：每次只提交一个关键词或概念，从 `https://www.cnki.net/` 首页提交检索，处理需要再次点击搜索按钮的情形。
- **保留两种语言**：不跨语言去重；英文多源结果内部去重，Zotero 入库仍检查已有条目和附件。
- **全文与身份核验**：获取 PDF 后核对首页 DOI 或标题；中文检索与下载保留同一浏览器会话及来源页。
- **可恢复入库**：保存选文清单，分别记录文献和附件状态；重复调用跳过已完成条目，失败项目可单独重试。
- **Zotero 本机集成**：显式授权、Windows DPAPI 加密保存授权信息、入库后读取并核对实际附件。

排序是候选文献发现的启发式规则，不是研究质量评价。中英文分别排序后按语言内名次交替排列，跨语言分数不代表可直接比较的相关性概率。

## 环境要求

- 完整流程已在 **Windows 11、Python 3.12、Zotero Desktop 10.0.3** 上验证。
- Zotero 入库需要打开桌面客户端、启用本地 API，并完成本机写入授权。
- 中文功能额外依赖已经安装、与桥接接口兼容的 CNKI MCP Python 环境及 CloakBrowser 可执行文件。它们不随本仓库提供。仅使用英文功能无需配置 CNKI。
- 英文功能可独立运行；其他操作系统上的完整流程未验证，持久化本机 Zotero 授权目前依赖 Windows DPAPI。

## 安装源码

在 PowerShell 中运行：

```powershell
git clone https://github.com/a454511125-dotcom/Find_comm_papers.git
cd Find_comm_papers
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -I -B launch.py --self-check
```

自检应列出 15 个工具。`requirements-local.txt` 记录已验证的 Windows 依赖版本，包含 Windows 专用包；它不是跨平台依赖清单。本项目未发布到 PyPI，直接安装同名或上游 PyPI 包不能获得此定制版本。

### 在 Codex 中注册

将以下内容加入 Codex 的 MCP 配置，并把示例路径替换为实际绝对路径：

```toml
[mcp_servers.Find_comm_papers]
command = 'D:\Research\Find_comm_papers\.venv\Scripts\python.exe'
args = ['-I', '-B', 'D:\Research\Find_comm_papers\launch.py']

[mcp_servers.Find_comm_papers.env]
COMM_MCP_DATA_DIR = 'D:\Research\FindPapersData'
```

修改后重新加载 MCP 或重启客户端。对外注册名为 `Find_comm_papers`；为兼容现有调用，内部服务标识、授权应用名及 `comm_*` 工具名保留原名。

`COMM_MCP_DATA_DIR` 保存全文、下载记录、选文清单、浏览器诊断和加密授权，建议放在仓库外。通过 `launch.py` 启动且未指定时，默认使用用户目录下的 `Documents/FindPapersData`。

可选 API 参数使用 `.env.example` 中的变量名。复制为本机私有环境文件后，用 `PAPER_SEARCH_MCP_ENV_FILE` 指定绝对路径；不要提交填写后的文件。直接用模块启动时，也应显式设置数据目录。

### 中文后端

桥接器读取 `~/.codex/config.toml` 中已有的 `[mcp_servers.cnki]`。也可通过 `COMM_CNKI_MCP_CONFIG` 指向另一份配置。要求该条目：

- `command` 指向已安装 CNKI 后端的 Python；`args` 为 `["-m", "server"]`。
- 环境配置中的 `CLOAKBROWSER_BINARY_PATH` 指向已存在的浏览器文件。
- 后端提供兼容的 `cnki.browser`、`cnki.search`、`cnki.download` 接口和所需依赖。

本项目调用兼容后端的内部接口，不能保证任意 CNKI MCP 实现均可替换。保留原后端配置并先单独验证其可用性。桥接器不会自动安装或更新浏览器；缺少中文依赖时返回中文来源错误，英文功能仍可使用。知网全文访问使用用户已有权限；登录或可操作的验证码仍可能需要人工处理。

## 使用示例

向支持 MCP 的助手提出：

> 用 Find_comm_papers 检索 2020 年以来算法推荐与政治极化的中英文论文。知网先只检索“算法推荐”；传播学优先，保留 Nature、Science 和计算机领域的相关研究。先返回候选文献和排序理由，不直接入库。

对应联合检索参数示例：

```json
{
  "query": "2020年以来算法推荐与政治极化",
  "chinese_query": "算法推荐",
  "english_queries": [
    "algorithmic recommendation political polarization",
    "news feed affective polarization"
  ],
  "year_start": 2020,
  "max_results": 20
}
```

其他中文概念（如“政治极化”）应另发一次中文检索。英文检索词由调用助手明确给出并向用户说明，本工具不会调用隐藏的翻译服务。

推荐流程：**明确问题 → 检索与审阅 → 保存选文清单 → 获取并核验全文 → 经用户确认后导入 Zotero → 检查状态和必要重试**。全文阅读后再提取证据和写综述；检索排名、摘要及下载成功均不能代替原文核验。

## 工具列表

| 用途 | 工具 |
|---|---|
| 联合检索 | `comm_search_bilingual` |
| 英文检索 | `comm_search`、`comm_search_many` |
| 排序与配置 | `comm_rank`、`comm_get_profile` |
| 选文与状态 | `comm_save_selection`、`comm_status` |
| 全文获取与阅读 | `comm_download`、`comm_download_batch`、`comm_read_pdf` |
| 引文导出 | `comm_export_ris` |
| Zotero 连接与授权 | `comm_zotero_probe`、`comm_zotero_authorize` |
| 入库与重试 | `comm_import_to_zotero`、`comm_retry` |

具体入库、重复检查、授权和恢复行为见 [WORKFLOW.md](docs/WORKFLOW.md)。

## 配置与边界

- 排序配置位于 `paper_search_mcp/comm_profile.json`；用 `COMM_MCP_PROFILE` 可指定外部配置。权重和期刊列表可编辑。
- 中文期刊检索保留兼容后端的传播学期刊白名单，最多读取五页；不是全库穷尽检索。学位论文走单独的数据库类型。
- 默认最多返回候选数量，数据源错误、限流、缺少摘要或 OA 链接都会影响覆盖率。
- 当前配置保留原定制版启用的 Sci-Hub 回退选项。仅使用开放获取和公开来源时，调用下载/入库时传 `use_scihub=false`，或把配置中的 `download.use_scihub` 改为 `false`。真实 Sci-Hub 全文下载未验证。
- CAJ 文件保留但不会作为已核验 PDF 导入；Blob 或桌面下载器流程不受支持。
- 浏览器资料、诊断文件、PDF、清单和测试中间产物默认保留，不自动清理。它们可能包含个人访问状态，不应公开。
- 新增文献后不会自动更新独立 Zotero MCP 的语义索引。

## 验证

0.3.1 的 100 项重点回归测试已通过。真实流程各验证过一篇中文和英文文献：中文检索、PDF 身份核验、Zotero 新条目和附件核验通过；英文通过已知开放仓储 URL 获取新 PDF 并核验现有条目及附件。重复导入和重试未产生重复记录。这些结果不代表任意文献均可下载，也不代表批量真实入库已全面验证。

运行重点回归测试（保留全部测试产物，不写入实际 Zotero 文库）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:COMM_TEST_ARTIFACTS = Join-Path $PWD 'test-artifacts'
.\.venv\Scripts\python.exe -B -m pytest -p no:tmpdir -p no:cacheprovider -p tests.retained_tmp tests/test_comm_ingestion.py tests/test_comm_workflow.py tests/test_stabilization_regressions.py tests/test_comm_bilingual.py tests/test_cnki_homepage.py -q
```

Windows 加密测试需要当前账户可访问原生 DPAPI。上游测试集还包含网络测试；不要将其结果与上述重点回归混为一谈。

## 许可

[MIT License](LICENSE)。保留上游 OPENAGS 版权和贡献来源。CNKI 后端、CloakBrowser、Zotero 及所访问内容分别适用各自的许可与访问条件。
