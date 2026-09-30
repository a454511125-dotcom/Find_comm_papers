# Find_comm_papers

面向国际传播与计算社会科学研究的文献 MCP 服务。中文通过 **CNKI** 检索与下载，英文通过 **Web of Science Core Collection** 检索，再沿机构全文链接访问出版商或图书馆解析服务。候选筛选、PDF 核验、RIS 导出和 Zotero 导入在同一服务内完成。

校外认证适配北京外国语大学 WebVPN。用户在可见浏览器中完成学校登录，服务复用机构会话；学号、密码和验证码不作为 MCP 参数。

## 项目构成

```text
Find_comm_papers/
├── launch.py                       # 源码启动入口与工具自检
├── library_login.py                # CNKI / WoS 交互式学校认证
├── .env.example                    # 本地配置模板
├── paper_search_mcp/
│   ├── comm_server.py              # MCP 工具与工作流入口
│   ├── comm_bilingual.py           # 中英文分路检索和结果合并
│   ├── comm_cnki.py                # 串行浏览器工作线程与 CNKI 调用
│   ├── comm_cnki_host.py           # CNKI 页面操作与文件响应捕获
│   ├── cnki/                      # CNKI 解析、过滤和 WebVPN 适配
│   ├── comm_wos.py                 # WoS 多查询、排序和下载回执
│   ├── comm_wos_host.py            # WoS 页面、Full Record 与全文访问
│   ├── comm_ranking.py             # 可解释的候选排序
│   ├── comm_profile.json           # 排序权重、期刊偏好与词项配置
│   ├── comm_download.py            # PDF 身份核验与本地读取
│   ├── comm_manifest.py            # 选文清单与持久化状态
│   ├── comm_workflow.py            # 分批导入与失败重试
│   ├── comm_zotero.py              # Zotero 接口及附件核验
│   └── comm_auth.py                # 本地授权与 Windows DPAPI
├── tests/                          # 离线回归测试
├── docs/                           # 工作流、英文检索与来源说明
└── third_party/                    # 第三方许可证
```

## 检索与全文路径

| 环节 | 中文 | 英文 |
| --- | --- | --- |
| 校外入口 | 北外 WebVPN → 中国知网 | 北外 WebVPN → 图书馆资源 → Web of Science |
| 检索 | CNKI 单个中文关键词或概念 | WoS 主题表达式、年份与 English 语言限制 |
| 书目 | CNKI 结果和详情 | WoS Full Record：作者、DOI、摘要、期刊、年份、入藏号 |
| 全文 | CNKI 下载入口 | WoS 全文链接 → 出版商 / SFX → PDF |
| 文件处理 | PDF 身份核验、回执、RIS / Zotero | PDF 身份核验、回执、RIS / Zotero |

WoS 提供检索记录与全文链接，PDF 是否可得取决于开放获取状态、机构订阅及出版商页面。服务仅在 PDF 通过首页 DOI 或标题核验后报告下载成功；无订阅服务、登录验证和文件身份待核验分别返回状态。

中英文各自排序后交替合并，不跨语言去重。默认权重为相关性 65%、学科匹配 25%、时效 5%、引用 5%。排序用于筛选候选，不代表论文质量评价或系统综述的完整召回。

## 安装与配置

运行验证环境为 Windows 11、Python 3.12。包声明 Python ≥ 3.10；学校会话的持久化加密使用 Windows DPAPI。浏览器二进制需自行准备，服务不会自动下载或更新浏览器。

```powershell
git clone https://github.com/a454511125-dotcom/Find_comm_papers.git
cd Find_comm_papers
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

编辑 `.env`，将浏览器路径改为本机已有、与所用 CloakBrowser 兼容的 Chromium 可执行文件，将数据路径设在源码目录之外：

```dotenv
COMM_CNKI_ACCESS_MODE=bfsu_webvpn
COMM_CNKI_BROWSER_PATH='D:\Browsers\CloakBrowser\chrome.exe'
COMM_MCP_DATA_DIR='D:\Research\FindPapersData'
COMM_ENABLE_LEGACY_TOOLS=0
```

`launch.py` 和 `library_login.py` 自动读取根目录 `.env`。可用环境变量 `PAPER_SEARCH_MCP_ENV_FILE` 指定其他配置文件；已设置的环境变量优先。没有根目录 `.env` 或显式路径时，配置加载器读取 `~/.config/paper-search-mcp/.env`。

`COMM_CNKI_ACCESS_MODE=direct` 适用于能够直接访问 CNKI 的网络；WoS 学校入口仍使用北外 WebVPN。此适配针对北外门户，其他学校需要相应的入口和页面适配。

### MCP 客户端配置

以下路径为示例，替换为实际克隆位置：

```toml
[mcp_servers.Find_comm_papers]
command = 'D:\Research\Find_comm_papers\.venv\Scripts\python.exe'
args = ['-I', '-B', 'D:\Research\Find_comm_papers\launch.py']

[mcp_servers.Find_comm_papers.env]
PAPER_SEARCH_MCP_ENV_FILE = 'D:\Research\Find_comm_papers\.env'
```

先自检，再进行网页登录：

```powershell
.\.venv\Scripts\python.exe -I -B launch.py --self-check
.\.venv\Scripts\python.exe -I -B library_login.py cnki
.\.venv\Scripts\python.exe -I -B library_login.py wos
```

自检列出默认注册的 18 个工具，不验证网络或订阅权限。登录脚本打开专用浏览器并等待手动登录，确认机构身份后退出。MCP 内也可以直接调用认证工具；检索和下载包含认证前置检查。出现验证码或登录失效时，在服务打开的浏览器完成操作后重试。

### Clash Verge 网络规则

学校入口应按实际网络需求走直连。在 Clash Verge 的持久规则扩展中，将以下规则置于代理匹配规则之前；`DIRECT` 表示不经过代理，不是阻止访问：

```yaml
prepend:
  - DOMAIN-SUFFIX,bfsu.edu.cn,DIRECT
  - DOMAIN-SUFFIX,webofscience.com,DIRECT
  - DOMAIN-SUFFIX,webofknowledge.com,DIRECT
  - DOMAIN-SUFFIX,clarivate.com,DIRECT
  - DOMAIN-SUFFIX,clarivate.cn,DIRECT
```

项目不修改系统代理配置，也不携带代理订阅。经 WebVPN 改写的目标实际连接学校网关，需保留网关路径进入数据库。

## 默认 MCP 工具

| 功能 | 工具 |
| --- | --- |
| 配置与排序 | `comm_get_profile`、`comm_rank` |
| CNKI 认证与状态 | `comm_cnki_authenticate`、`comm_cnki_access_status` |
| WoS 认证与状态 | `comm_wos_authenticate`、`comm_wos_access_status` |
| 检索 | `comm_search_bilingual`、`comm_wos_search` |
| 选文与进度 | `comm_save_selection`、`comm_status` |
| PDF 与书目文件 | `comm_download`、`comm_download_batch`、`comm_read_pdf`、`comm_export_ris` |
| Zotero | `comm_zotero_probe`、`comm_zotero_authorize`、`comm_import_to_zotero`、`comm_retry` |

英文检索示例：

```json
{
  "queries": ["\"international communication\" AND \"social media\""],
  "ranking_query": "国际传播中的社交媒体研究",
  "year_start": 2020,
  "max_results": 20,
  "per_query": 30
}
```

传给 `comm_wos_search` 的表达式无需外包 `TS=`。支持 1–6 个显式表达式，单次每个表达式最多导出 100 条记录。

双语检索示例：

```json
{
  "query": "算法推荐与政治极化",
  "chinese_query": "算法推荐",
  "english_queries": ["\"algorithmic recommendation\" AND polarization"],
  "languages": ["zh", "en"],
  "year_start": 2020,
  "max_results": 20
}
```

中文每次提交一个关键词或概念，其他概念分次检索。只检索中文时传 `languages=["zh"]`。英文来源省略或设为 `["wos"]`。中文期刊检索应用传播学期刊筛选，并限制页面提取数量；学位论文不应用期刊名单。

下载时把选中的完整书目记录传给 `comm_download`。检查下载回执中的 `status`、`identity_check` 和 `pdf_path`；文件存在本身不代表核验成功。需要导入 Zotero 时先保存选文清单，再显式授权本地写入；书目成功与附件成功分开计数。完整流程见 [工作流](docs/WORKFLOW.md)。

## 数据与运行边界

- 数据目录保存会话、PDF、下载回执、选文清单和授权缓存。默认位于用户文档目录的 `FindPapersData`，不会自动清理。
- 北外网关 cookie 与 Zotero 本地授权缓存使用当前 Windows 用户的 DPAPI 加密。浏览器 profile、CNKI cookie 及全文文件仍属于私有运行数据，应保存在受控本地目录。
- 仓库不包含账号、密码、cookie、代理配置、个人文库或下载的论文；`.env` 和常见运行产物列入 `.gitignore`。
- 出版商登录、验证码、SFX 无可用服务或网页结构变化会影响全文下载。机构身份确认不等于每篇论文都具有全文权限。
- 上游通用来源适配器作为可选组件保留。`COMM_ENABLE_LEGACY_TOOLS=1` 可启用兼容工具；双语主流程仍使用 CNKI 与 WoS。`paper-search-mcp` 命令属于通用入口，本项目主入口为 `launch.py` 或 `find-comm-papers`。

## 测试

离线测试覆盖学校 URL 改写、认证状态、WoS Full Record、浏览器工作线程、文件响应捕获、PDF 身份核验和双语路由。下列命令保留测试产物，不自动清理临时目录：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:COMM_TEST_ARTIFACTS = 'D:\Research\FindPapersTestArtifacts'
.\.venv\Scripts\python.exe -m pytest -p no:tmpdir -p no:cacheprovider -p tests.retained_tmp tests/test_comm_wos.py tests/test_cnki_webvpn.py tests/test_cnki_homepage.py tests/test_unified_cnki_runtime.py tests/test_comm_bilingual.py tests/test_source_launchers.py -q
```

离线测试与工具注册检查不代替真实机构访问；真实全文下载需在用户自己的学校权限下验证。

## 文档与许可

- [检索、选文与 Zotero 工作流](docs/WORKFLOW.md)
- [Web of Science 检索与全文](docs/ENGLISH-SOURCES.md)
- [代码来源与第三方许可](docs/UPSTREAM.md)

本项目采用 MIT 许可证，基于 [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)，CNKI 模块包含 [wuruiqi/cnki-mcp](https://github.com/wuruiqi/cnki-mcp) 的代码。许可分别保留于 [LICENSE](LICENSE) 和 [third_party/CNKI-MCP-LICENSE](third_party/CNKI-MCP-LICENSE)。
