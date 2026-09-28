# 英文来源与验收

统一检索直接调用合入的原 `paper-search-mcp` 来源类。`comm_get_profile` 返回完整来源列表及限制；用 `comm_search.sources` 或联合检索的 `english_sources` 选择。默认使用 OpenAlex、Crossref、arXiv、Semantic Scholar，另外增加传播学期刊定向召回。

| 来源 | 当前接口与限制 |
|---|---|
| OpenAlex、Crossref、Semantic Scholar | 关键词和元数据检索；支持各自的年份参数，检索后再统一筛选发表年份 |
| arXiv、dblp | 计算机及预印本文献；保留原查询接口，网站错误不会被视为成功检索 |
| PubMed、PMC、Europe PMC | 生物医学及相关社会行为研究；可作为跨学科补充 |
| CORE、OpenAIRE、BASE、HAL、Zenodo、DOAJ | 开放仓储和期刊；凭据、接口和限流条件各异。BASE 的采集日期不当作发表日期 |
| Google Scholar、SSRN、CiteSeerX、IACR | 保留原网页/API 适配；网页结构、验证和限流可能影响返回结果 |
| bioRxiv | DOI、日期区间或 `category:<分类>`；分类模式沿用上游最近 30 天窗口 |
| medRxiv | 当前上游实现仅支持 `category:<分类>`，最近 30 天；不支持任意关键词或 DOI 查询 |
| Unpaywall | DOI 查找开放版本，需要 `UNPAYWALL_EMAIL`；不执行通用关键词检索 |
| IEEE、ACM | 上游类仍是占位代码。保留兼容入口，明确返回未实现，即使提供 key 也不宣称可用 |

共 21 类已有来源适配，加上 IEEE/ACM 两个占位类和 OpenAlex 传播学别名，注册表共有 24 个名称。来源数量不代表每个网站都已完成真实网络验收。

## 原上游工具

主服务注册原上游的 57 个默认工具及 IEEE/ACM 的 6 个兼容入口，输入名称和参数签名保持兼容。与 15 个 `comm_*` 工具一起，共 78 个入口；不启动原上游的第二个服务。

原生工具包括综合检索 `search_papers`、21 个单来源 `search_*`、DOI 元数据查询、16 对下载/阅读工具，以及 `download_scihub`、`download_with_fallback`。IEEE/ACM 各三个入口单独报告未实现。

原生下载和阅读沿用原实现。部分来源的原生下载方法本来就未实现，入口会返回相应说明；可将检索到的元数据、DOI、公开全文地址交给 `comm_download`。原生下载仅检查 PDF 可读性，**标题/DOI 身份核验及 Zotero 入库使用 `comm_*` 流程**。

原生下载为每次调用分配独立目录，排他写入文件；失败产物和核验不通过的文件均保留。`read_*` 优先使用对应目录中已有的 PDF，避免重复获取。

## 无头浏览器

`comm_download` 先尝试直接 PDF、网页链接和 OA 解析，再尝试独立英文 CloakBrowser 会话。浏览器可处理网页渲染和 PDF 按钮，但无法保证任意开放论文均有可下载 PDF。遇到登录、人机验证、访问限制时返回诊断；不会自动解决验证。

英文与中文共用已安装的浏览器程序、分别保存会话。浏览器取得的内容仍须通过统一的 PDF 身份核验，才能进入正常入库流程。英文浏览器不会继承文献 API 或 Zotero 密钥。

## 0.5.0 验收

2026-09-29，192 项重点回归测试通过：原有中文/英文/Zotero 流程 115 项，来源适配 39 项，原生工具与文件保留 17 项，无头浏览器 15 项，浏览器接入共享下载流程 6 项。

本轮真实网络测试：

- Crossref、PubMed、Europe PMC 对“算法推荐与政治极化”的英文查询各返回 3 条候选，经统一筛选排序返回 9 条。
- arXiv 返回 HTTP 406；dblp 返回无法解析的 XML 内容且回退没有结果。保留异常，不将其解释为该主题没有文献。
- CloakBrowser 从 JMLR 的《Latent Dirichlet Allocation》公开论文页提取链接并取得 PDF，首页标题核验通过。这是浏览器获取流程样本，不是上述研究问题的候选结果。
- 一个此前可下载的开放仓储地址本轮返回 Anubis 人机验证页面；已据真实标题补充识别并通过回归测试。开放地址仍可能临时要求验证。

以上测试未新增 Zotero 文献。此前中英文联合检索、中文 PDF、Zotero 实际附件读回及同一文件重试的验证记录继续适用；不同字节但相同正文的 PDF 仍可能保留为多个附件。
