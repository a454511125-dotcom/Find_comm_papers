# 代码来源与项目组成

本项目基于 [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)，起始提交为 `808e462a824ce6b26fdccbed352b4bf47d7b84cb`。上游的通用来源适配器、论文数据结构及基础接口位于 `paper_search_mcp` 命名空间。原始 OPENAGS 版权及完整 MIT 许可保留于根目录 [LICENSE](../LICENSE)。

CNKI 模块包含 [wuruiqi/cnki-mcp](https://github.com/wuruiqi/cnki-mcp) 的检索、解析、过滤和下载按钮处理代码，来源提交为 `419aa08142259e7492f9925adf2e9543a07ecedc`。Copyright (c) 2026 wuruiqi，完整 MIT 许可保留于 [third_party/CNKI-MCP-LICENSE](../third_party/CNKI-MCP-LICENSE)。该模块由主服务直接调用，浏览器依赖安装在同一 Python 环境。

Find_comm_papers 的研究工作流由以下模块组成：

- 中英文分路检索、传播学候选排序、显式多查询与语言内去重。
- 北外 WebVPN 学校认证、CNKI 页面适配与共享浏览器工作线程。
- WoS Core Collection 页面检索、Full Record 导出与机构全文访问。
- PDF 身份核验、持久化选文清单、分阶段状态与失败重试。
- Zotero 本地显式授权、Windows DPAPI 缓存、书目与附件去重、实际存储文件核验。

源码分发名为 `find-comm-papers`，主命令为 `find-comm-papers`，Python 命名空间为 `paper_search_mcp`。通用来源兼容组件可单独启用。Playwright、CloakBrowser 及其他依赖由包管理器安装，浏览器二进制由使用者配置，各依赖的许可及使用条件仍适用。

公开仓库仅包含源码、配置模板、文档及测试，不分发学校账号、机构订阅凭据、浏览器会话、文献 PDF、Zotero 文库或代理订阅。
