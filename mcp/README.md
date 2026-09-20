# mcp/ —— 补充 MCP server（占位）

以后给如意工作台用的补充 MCP server 住在这里。**现在还没有任何代码。**

## 要放进来的东西得守什么

一个给模型用的工具，在如意里的接入面**只有 MCP 一种** —— 不要发明第三种接法。具体约定见
[`../docs/00-component-registry.md` §2.3](../docs/00-component-registry.md)，一句话版本：

- 写一个标准的 **stdio** MCP server（只支持 stdio）。
- 装完之后在 `~/.ruyi-toolbox/components/<id>.json` 放一份登记文件，`kind: "mcp"`，
  `run` 就是这个 server 的启动命令（绝对路径、不经 shell、`args` 是字符串数组）。
- 如意会把它作为一个外部 MCP 服务器接入（内部 id `toolbox-<id>`），生命周期、工具清单、
  权限分级全部走如意现有的 MCP 机制 —— 组件不需要也不应该知道如意内部怎么管 MCP。
- 自带 `register` / `unregister`；`register` 先自检再原子写；登记文件里不放任何密钥。

一个组件想同时提供「给模型用的工具」和「给如意自己用的能力端点」，就登记两份文件（两个 id）。
可以照抄 [`../asr-shim/ruyi_asr_shim/registry.py`](../asr-shim/ruyi_asr_shim/registry.py) 的写法
（它是 `kind: "service"`，但自检 + 原子写那套一样）。

主仓现有的 `mcp/ai-computer-control` **不搬过来**。
