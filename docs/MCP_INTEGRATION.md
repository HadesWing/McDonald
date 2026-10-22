# 与麦当劳 MCP Server 的对接说明

本项目所有餐品、价格、套餐结构数据都来自麦当劳官方 MCP Server，**不使用爬虫，不内置任何写死的价格表**。

## 1. 连接方式

| 项 | 值 |
|---|---|
| 端点 | `https://mcp.mcd.cn` |
| 鉴权 | `Authorization: Bearer <MCP_TOKEN>` |
| 协议 | JSON-RPC 2.0 over HTTP POST（Streamable HTTP） |
| 协议版本 | `2025-06-18` |
| 响应格式 | `application/json` 或 `text/event-stream`（SSE），两种都要处理 |
| 会话 | 服务端在 `initialize` 响应头返回 `Mcp-Session-Id`，后续请求需带上 |

Token 申请：<https://open.mcd.cn/mcp> → 控制台 → 激活。

**Token 不进仓库。** 客户端只从环境变量 `MCD_MCP_TOKEN` 读取，仓库里只有 `.env.example` 占位符。

## 2. 客户端实现

`src/mcd_solver/mcp_client.py` 是一个**零第三方依赖**的 MCP 客户端，只用了 `urllib`：

- `initialize` 握手，抓取 `Mcp-Session-Id`；
- `tools/list` 列出全部 35 个工具及入参 schema；
- `tools/call` 调用工具。

之所以不直接 `pip install mcp`，是为了让评审把仓库 clone 下来、**不装任何依赖**就能复现全部结论。

三处容易踩的实现细节：

1. **SSE 响应要在 `\n\n` 处切分，只取完整的 `data:` 行**。服务端可能分块推送，半个 JSON 到达时直接 `json.loads` 会抛异常。
2. **`Mcp-Session-Id` 必须回传**。第一次握手成功后如果不带这个头，后续 `tools/call` 会返回会话无效。
3. **`Accept` 要同时声明两种类型**：`application/json, text/event-stream`。只写 json 时服务端可能仍返回 SSE。

## 3. 用到的 5 个工具

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `query-nearby-stores` | 定位门店，拿到 `storeCode` | `beType`、`searchType`、`city` + `keyword` |
| `query-meals` | 拉门店全量菜单（返回编码与标签） | `storeCode`、`orderType`、`beType` |
| `query-meal-detail` | 拿单个商品/套餐的详情与**套餐组成结构** | `storeCode`、`code` |
| `calculate-price` | 官方计价，返回实付金额 | `items[]`，可带 `roundList` 指定套餐内的换选 |
| `list-nutrition-foods` | 营养成分表 | 无 |

## 4. 完整调用链

`python mcd.py fetch --city 杭州 --keyword 西湖` 会依次执行：

```
query-nearby-stores          → storeCode
      ↓
query-meals                  → 全量餐品编码 + 分类标签
      ↓
query-meal-detail  × N       → 逐个补齐名称、价格、套餐 rounds（含可换选项与差价）
      ↓
calculate-price    × N       → 对套餐内「不可单点」的选项补价（关键一步）
      ↓
list-nutrition-foods         → 解析文本表格，按名字匹配回餐品
      ↓
data/store_hangzhou.json     → 落盘快照，后续所有分析都离线跑
```

### 为什么第 4 步很关键

`query-meal-detail` 返回的套餐选项带的是 `diff_cents`——**相对默认项的差价，不是绝对价格**。同时，套餐里的饮料往往是套餐专属编码（如「可乐中杯」），在门店菜单里查不到单点价。

如果直接跳过，所有套餐都会算成「不可比」，整个审计就废了。

所以 fetch 会收集所有缺失单点价的套餐选项，用 `calculate-price` 批量询价补齐。这样「套餐价 vs 单点合计」才两边都是真实价格。

### 用 `calculate-price` 复核而不是自算

审计里的两个数都是官方报价：

```python
# 套餐价
calculate-price(items=[{"productCode": combo_code, "quantity": 1}])

# 同样的东西单点一遍
calculate-price(items=[
    {"productCode": main_code,  "quantity": 1},
    {"productCode": fries_code, "quantity": 1},
    {"productCode": cola_code,  "quantity": 1},
])
```

相减就是差价。`mcd.py audit --verify N` 做的就是这件事——重新询价，把官方返回值和本地结论并排打印，一致性对不上会显示 `✗`。

## 5. 踩坑记录

这些都是实际调试时遇到的，写下来省得别人再撞一遍：

| 现象 | 原因 | 处理 |
|---|---|---|
| `query-meals` 返回一堆编码，没有名称和价格 | 该接口只给列表骨架 | 必须逐个 `query-meal-detail` 补全 |
| `calculate-price` 一次传太多商品会报错 | 单次请求有商品数上限 | 批量按 10 个一组切片 |
| 套餐选项询价返回 0 或报错 | 该编码是套餐专属，门店不单卖 | 标记「不可比」，不估算 |
| 套餐被识别成自己的子项 | 名字模糊匹配时把套餐本身也放进了候选池 | 匹配时排除全部套餐编码 |
| 「7 件任选」套餐件数对不上 | 接口只展开了 3 个 round | 用套餐名里的 `N 件` 校验，不符则排除 |
| `list-nutrition-foods` 返回的是文本表格 | 不是结构化 JSON | 按行解析，用名字归一化后匹配 |
| `query-nearby-stores` 只传 `city` 查不到 | 参数组合敏感 | `city` + `keyword` 一起传 |

## 6. 复现步骤

```bash
# 1. 离线跑（不需要 token）
python mcd.py slot
python mcd.py audit

# 2. 需要 token 的部分
export MCD_MCP_TOKEN=<你的 token>      # Windows: set MCD_MCP_TOKEN=...
python mcd.py tools                     # 列出 35 个工具
python mcd.py fetch --city 杭州 --keyword 西湖   # 重新抓快照
python mcd.py audit --verify 5          # 官方复核前 5 个结论
```

Python 3.10+，无第三方依赖。
