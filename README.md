# Find_comm_papers

面向计算传播研究的中英文文献检索与 Zotero 入库 MCP。

Bilingual literature discovery, communication-prioritized ranking, verified PDF retrieval, and resumable Zotero ingestion for computational communication research.

## 功能

- **中英文联合检索**：中文由包内 CNKI 模块直接操作浏览器；英文通过 OpenAlex、Crossref、arXiv、Semantic Scholar、dblp 等入口。
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
- 中英文使用同一个 Python 环境；`pip install` 同时安装 Playwright 和 CloakBrowser Python 依赖。中文需要已有的兼容 Chromium/CloakBrowser 浏览器可执行文件，由 `COMM_CNKI_BROWSER_PATH` 指定。无需安装或配置另一个 CNKI MCP。浏览器二进制不随源码分发，也不会由检索操作自动下载。
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

自检应列出 78 个入口：15 个整合工具和上游 63 个兼容入口，其中 IEEE、ACM 的 6 个入口仍明确返回“上游未实现”。`requirements-local.txt` 记录已验证的 Windows 依赖版本，包含 Windows 专用包；它不是跨平台依赖清单。本项目未发布到 PyPI，直接安装同名或上游 PyPI 包不能获得此定制版本。

### 在 Codex 中注册

将以下内容加入 Codex 的 MCP 配置，并把示例路径替换为实际绝对路径：

```toml
[mcp_servers.Find_comm_papers]
command = 'D:\Research\Find_comm_papers\.venv\Scripts\python.exe'
args = ['-I', '-B', 'D:\Research\Find_comm_papers\launch.py']

[mcp_servers.Find_comm_papers.env]
COMM_MCP_DATA_DIR = 'D:\Research\FindPapersData'
COMM_CNKI_BROWSER_PATH = 'D:\Browsers\CloakBrowser\chrome.exe'
```

修改后重新加载 MCP 或重启客户端。服务注册名及内部服务名称均为 `Find_comm_papers`；为兼容现有调用，`comm_*` 工具名保留不变。

`COMM_MCP_DATA_DIR` 保存全文、下载记录、选文清单、浏览器诊断和加密授权，建议放在仓库外。通过 `launch.py` 启动且未指定时，默认使用用户目录下的 `Documents/FindPapersData`。

可选 API 参数使用 `.env.example` 中的变量名。复制为本机私有环境文件后，用 `PAPER_SEARCH_MCP_ENV_FILE` 指定绝对路径；不要提交填写后的文件。直接用模块启动时，也应显式设置数据目录。

### 合并后的结构

```text
Find_comm_papers（一个 MCP 服务、一个 Python 环境）
├── 英文检索与开放全文模块
├── 中文知网模块（内置原 CNKI 实现及既有修复）
│   └── 共享浏览器会话
└── 统一排序、选文、PDF 核验与 Zotero 入库
```

中文检索直接调用包内 Python 函数。不会读取 `[mcp_servers.cnki]`，不会启动第二个 MCP，也不依赖旧 CNKI 的安装路径或 Python 环境。浏览器/Playwright 驱动是普通运行时组件。

- `COMM_CNKI_BROWSER_PATH`：已有兼容浏览器可执行文件的绝对路径。启动前检查文件存在，防止隐式下载。
- `COMM_CNKI_COOKIE_FILE`：可选，仅用于首次读取已有登录 Cookie；也可以直接在打开的浏览器里登录。不会覆盖该输入文件。
- 当前服务保存自己的会话和 Cookie，均位于 `COMM_MCP_DATA_DIR/cnki`，不进入代码仓库。
- 登录与可操作的验证码仍由用户处理；空白验证页面报告为加载问题。

英文保留并注册原项目的检索、下载、阅读入口；中文直接使用合入的 CNKI 代码，两部分共用当前服务。

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

原上游工具 `search_papers`、`search_<来源>`、`download_<来源>`、`read_<来源>_paper` 及 DOI 查询入口也在同一服务中可用。来源、参数和未实现能力见 [英文来源说明](docs/ENGLISH-SOURCES.md)。`comm_search` 可选择全部 21 类已适配来源，默认仍调用四个主要来源，避免每次检索都遍历所有网站。

具体入库、重复检查、授权和恢复行为见 [WORKFLOW.md](docs/WORKFLOW.md)。

## 英文全文获取

```text
论文元数据与 OA 地址
  → 直接 PDF / 仓储与出版商链接
  → CloakBrowser 无头浏览器：渲染网页、提取链接、点击 PDF 按钮
  → 标题或 DOI 核验 → 保存 PDF → Zotero
```

浏览器用于直接获取失败后的回退，不能保证每篇开放论文都有可下载 PDF。验证页、登录页和访问限制会返回状态及诊断，供人工处理。中文继续使用可见的 CloakBrowser；英文使用独立的无头会话。

默认复用 `COMM_CNKI_BROWSER_PATH` 指定的浏览器，也可用 `COMM_BROWSER_PATH` 单独指定英文浏览器。`COMM_BROWSER_FALLBACK=0` 关闭英文浏览器回退。单篇共享下载流程保留时间预算，最多尝试两个浏览器地址；浏览器响应会检查声明长度和实际长度，但 Playwright 会先缓冲响应，这不是浏览器内存硬上限。

## 配置与边界

- 排序配置位于 `paper_search_mcp/comm_profile.json`；用 `COMM_MCP_PROFILE` 可指定外部配置。权重和期刊列表可编辑。
- 中文期刊检索保留原中文模块的传播学期刊白名单，最多读取五页；不是全库穷尽检索。学位论文走单独的数据库类型。
- 默认最多返回候选数量，数据源错误、限流、缺少摘要或 OA 链接都会影响覆盖率。
- 当前配置保留原定制版启用的 Sci-Hub 回退选项。仅使用开放获取和公开来源时，调用下载/入库时传 `use_scihub=false`，或把配置中的 `download.use_scihub` 改为 `false`。真实 Sci-Hub 全文下载未验证。
- CAJ 文件保留但不会作为已核验 PDF 导入；Blob 或桌面下载器流程不受支持。
- 浏览器资料、诊断文件、PDF、清单和测试中间产物默认保留，不自动清理。它们可能包含个人访问状态，不应公开。
- 新增文献后不会自动更新独立 Zotero MCP 的语义索引。

## 验证

回归覆盖来源适配、全部原生工具注册、文件保留、无头浏览器、PDF 身份核验、中文会话和 Zotero 恢复流程。最新测试结果见 [英文来源说明](docs/ENGLISH-SOURCES.md)。

真实 MCP 测试中，禁止导入外部 `cnki` 包，并将旧中文配置路径指向不存在的文件；同一服务成功返回中英文结果、下载中文 PDF、核验标题并完成 Zotero 文献及实际附件核验。同一文件的重复导入、重试和新选文清单检查通过。

已确认一个附件边界：知网不同时间下载的 PDF 可能字节不同而提取文本一致。当前按文件哈希检查附件，会保留这种不同字节的文件副本；测试样本因此在同一个文献条目下留下两份 PDF，并未新增文献条目。不会擅自删除或合并文件。先前英文样本验证过通过已知开放仓储 URL 获取新 PDF；这些单篇测试不代表任意文献均可下载或批量真实入库已全面验证。

运行重点回归测试（保留全部测试产物，不写入实际 Zotero 文库）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:COMM_BROWSER_FALLBACK = '0'
$env:COMM_TEST_ARTIFACTS = Join-Path $PWD 'test-artifacts'
.\.venv\Scripts\python.exe -B -m pytest -p no:tmpdir -p no:cacheprovider -p tests.retained_tmp tests/test_comm_ingestion.py tests/test_comm_workflow.py tests/test_stabilization_regressions.py tests/test_comm_bilingual.py tests/test_cnki_homepage.py tests/test_unified_cnki_runtime.py tests/test_comm_providers.py tests/test_comm_upstream.py tests/test_comm_browser.py tests/test_comm_browser_download.py -q
```

Windows 加密测试需要当前账户可访问原生 DPAPI。上游测试集还包含网络测试；不要将其结果与上述重点回归混为一谈。

## 许可

[MIT License](LICENSE)。保留上游 OPENAGS 版权和贡献来源。内置 CNKI 代码的 MIT 许可见 [CNKI-MCP-LICENSE](third_party/CNKI-MCP-LICENSE)。CloakBrowser、Zotero 及所访问内容分别适用各自的许可与访问条件。
