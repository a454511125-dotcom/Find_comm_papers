# 来源与定制范围

本项目基于 [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)，起始提交为 `808e462a824ce6b26fdccbed352b4bf47d7b84cb`。上游代码适用 MIT 许可证，原始 OPENAGS 版权及完整许可保留于根目录 LICENSE。

Find_comm_papers 0.3.1 增加：

- 面向计算传播的候选排序、传播学期刊偏好及多查询检索。
- 中英文分路检索与结果合并，不跨语言去重。
- 兼容已安装 CNKI 后端的独立浏览器桥接、单关键词检索、会话连续性和验证页面诊断。
- PDF 身份核验、持久化选文清单和分阶段状态记录。
- Zotero 本机显式授权、Windows DPAPI 授权缓存、文献及附件重复检查、实际存储文件核验与重试。
- 对部分上游检索及下载连接器的错误报告、超时和兼容性修复。

Python 模块命名空间继续使用 `paper_search_mcp`，内部 `comm_*` 工具名保持兼容。公开源码分发名为 `find-comm-papers`，新增命令行入口 `find-comm-papers`；没有向 PyPI 发布。原上游的 PyPI 自动发布配置、品牌推广材料及旧锁文件没有纳入这个源码快照。

该仓库不分发 CNKI 后端、CloakBrowser、Python 运行时或第三方依赖源码；安装时分别获取相应依赖。中文桥接依赖兼容后端的内部接口，需要单独配置和验证。本仓库不包含个人浏览器会话、账号凭据、实际检索结果、文献 PDF、Zotero 文库快照或授权缓存。

测试范围和已知限制见根目录 README。公开验证记录仅保留功能结论，不包含真实个人文库条目 ID、选文清单 ID 或附件文件哈希。
