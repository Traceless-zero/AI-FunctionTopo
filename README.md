# AI-FunctionTopo

把代码函数块可视化成 workflow 节点，用于审查代码和规划重构，并把画布上重排好的数据流导出 JSON 交给 AI 照图修改源码——「人定方向、AI 干活」。

**AI-FunctionTopo: function-level code visualization & AI refactoring collaboration. Extract functions to a canvas, reroute data flows, export JSON for AI to rewrite code. Buildless, zero deps.**

## 快速开始（免构建，零 node_modules）

画布是单文件 `canvas.html` + `vendor/` 静态库（React/ReactFlow UMD，共 ~310KB），浏览器直接跑：

- **双击打开 `canvas.html`**：默认出导入遮罩——把 JSON 拖进窗口 / 点「选择文件」/ 载入 orders 示例 / ＋ 新增空画布（从零编排）；
- 或起静态服务后用 `?src=` 深链直载你的 JSON：
  `python -m http.server 8642` → <http://127.0.0.1:8642/canvas.html?src=json/my.json>（`src=` 后为相对 canvas.html 的 JSON 路径）
- 把 JSON 文件**拖进窗口** = 导入。

> 源码查看用 `fetch()` 读文件，`file://` 直接打开会被浏览器拦截。除源码栏外，其余功能不受影响。

## 从源码生成图

```bash
python extract_flow_ast.py <你的Python源码目录或文件>            # 主力：Python 专用（内置 py ast 模块解析，零依赖）
python extract_flow_ts.py <你的源码目录或文件> -o out.json      # 可选：Tree-sitter 版（py/js/ts 多语言，与 ast 版语义全等，仅布局常数不同）
python extract_flow_ts.py <你的源码目录> --focus <入口函数> --hops 1   # 子图：按入口取 ±N 跳
python extract_flow_ts.py <你的源码目录> --dir up --focus <函数>       # 改动影响面（上游调用方）
```

> ast 版**只能解析 Python**（解析器是 Python 内置的 `ast` 模块）；要提取 JavaScript / TypeScript 源码，用 Tree-sitter 版。

产出的 JSON 拖进画布即可开图。`--strip-position` 仍兼容保留，但画布导出已不携带坐标，一般无需使用。

Tree-sitter 版为可选扩展，如需提取 JS/TS：`pip install -r requirements.txt`

## 画布功能

- 函数节点黑盒：函数名 / 签名 / 注释(docstring) / 文件位置行号，左入右出端口
- 拖拽、缩放（5%–250%）、框选、小地图、动态数据流边；选中后 Backspace/Delete 删除
- **＋ 节点**：画布上直接新增 to-be 函数节点（编排/新函数）
- **◎ 聚焦**：只显示该函数 ±1 跳上下游（诱导子图）；聚焦态导出 = 只导聚焦子图 + intent
- **双击节点** = 编辑批注（comment）；**双击边** = 编辑标签（label）
- **右键节点菜单**：删除节点 / 复制节点（边一并复制，只选中新副本）/ 修改节点信息
- **右侧栏**：改动意图（intent 字段）+ 源码审查（`{ }` 按钮，行号区间高亮；to-be 节点提示「暂无源码」）
- **流入预警**（图论叫扇入）：≥5 琥珀、≥10 红色；人工补边后实时重算
- 隐藏孤岛节点（默认开，画布新增节点豁免）
- **多图层**（左侧图层面板）：一个载入文件 = 一个图层，拖 JSON 追加不覆盖；双击图层名重命名、✕ 关闭、点击切换；每个图层记住自己的缩放与平移
- **图层操作**：「清空画布」清空当前图层（停在空画布，不退遮罩）、「重置图层」还原导入时初始 JSON、「新建画布」新开空白图层
- **导出**：文件名自动取当前图层名；已剔除时间戳 / 重复类型 / 占位零值 / 坐标等噪音
- **大图性能**：连线超过 30 条自动转实线（选中边除外），拖拽节点时小地图视框逐帧跟随
- 帮助按钮 `!`：画布内使用说明

## AI 改码闭环

```
源码 --提取--> origin JSON --画布重排/聚焦/写 intent--> to-be JSON（无坐标）
     --交 AI--> AI 照图改源码（一期只限：新增编排函数串联既有函数）
     --再提取--> after JSON --机器 diff--> 差集 == 画布意图 即闭环成功
```

配套 skill：`skills/aift-refactor-diff`（JSON → 收敛 diff）+ `skills/aift-sandbox-refactor`（ff_workspace 沙箱闭环，副本编辑、原文件只读、回归需确认）。

## 目录结构

```
functionflow/
├── canvas.html              # 主画布（项目核心，单文件 React + React Flow）
├── extract_flow_ast.py      # Python 源码 → functionflow/v1 JSON
├── extract_flow_ts.py       # TS / JS 源码 → functionflow/v1 JSON
├── skills/                  # aift 双 skill 权威源
├── ff_workspace/            # 工作区：orders/ 为随库样例（scripts + json）
└── vendor/                  # canvas.html 的本地依赖（react / reactflow / htm）
```

## JSON 契约：functionflow/v1

**一份 JSON，两种用途**：能导回画布，也能直接喂给 AI 改码。

本仓库真实样例 `ff_workspace/orders/json/orders 示例.json` 的完整内容（与下方字段说明一一对应）：

```json
{
  "schema": "functionflow/v1",
  "projectRoot": "ff_workspace/orders/scripts/",
  "language": "python",
  "nodes": [
    {
      "id": "n1",
      "data": {
        "function": "send_confirmation",
        "file": "ff_workspace/orders/scripts/notify.py",
        "line": 15,
        "endLine": 29,
        "signature": "(order: Order) -> None",
        "comment": "异步发送确认邮件 + 短信，失败重试 3 次。"
      }
    },
    {
      "id": "n2",
      "data": {
        "function": "_render",
        "file": "ff_workspace/orders/scripts/notify.py",
        "line": 32,
        "endLine": 37,
        "signature": "(order: Order) -> Rendered",
        "comment": "渲染邮件正文与短信模板。"
      }
    },
    {
      "id": "n3",
      "data": {
        "function": "compute_total",
        "file": "ff_workspace/orders/scripts/pricing.py",
        "line": 8,
        "endLine": 25,
        "signature": "(cart: ValidatedCart, user: User) -> Money",
        "comment": "商品小计 + 税费 - 优惠；VIP 用户享额外折扣。"
      }
    },
    {
      "id": "n4",
      "data": {
        "function": "_coupon_discount",
        "file": "ff_workspace/orders/scripts/pricing.py",
        "line": 28,
        "endLine": 30,
        "signature": "(cart: ValidatedCart, subtotal: Decimal) -> Decimal",
        "comment": "优惠券折扣，占位实现。"
      }
    },
    {
      "id": "n5",
      "data": {
        "function": "load_user",
        "file": "ff_workspace/orders/scripts/service.py",
        "line": 12,
        "endLine": 17,
        "signature": "(user_id: int) -> User",
        "comment": "按 user_id 加载用户，不存在时抛 UserNotFound。"
      }
    },
    {
      "id": "n6",
      "data": {
        "function": "validate_cart",
        "file": "ff_workspace/orders/scripts/service.py",
        "line": 28,
        "endLine": 42,
        "signature": "(cart: Cart) -> ValidatedCart",
        "comment": "校验库存、价格与上下架状态，返回校验后快照。"
      }
    },
    {
      "id": "n7",
      "data": {
        "function": "_order_no",
        "file": "ff_workspace/orders/scripts/service.py",
        "line": 52,
        "endLine": 54,
        "signature": "() -> str",
        "comment": "生成全局唯一订单号。"
      }
    },
    {
      "id": "n8",
      "data": {
        "function": "create_order",
        "file": "ff_workspace/orders/scripts/service.py",
        "line": 60,
        "endLine": 88,
        "signature": "(user: User, cart: ValidatedCart, total: Money) -> Order",
        "comment": "落库创建订单、扣库存、生成支付单，事务内执行。"
      }
    }
  ],
  "edges": [
    {
      "id": "e1",
      "from": "n1",
      "to": "n2"
    },
    {
      "id": "e2",
      "from": "n3",
      "to": "n4"
    },
    {
      "id": "e3",
      "from": "n8",
      "to": "n1"
    },
    {
      "id": "e4",
      "from": "n8",
      "to": "n7"
    }
  ],
  "intent": "人工修改意图"
}
```

字段说明：

- `schema`：固定 `functionflow/v1`，导入时校验。
- `projectRoot`：源码相对根路径，用于定位文件。
- `language`：源语言（python / ts），提取器写入。
- `nodes[].id`：节点标识，边通过它引用。
- `nodes[].data.function`：函数名，即画布节点标题。
- `nodes[].data.file`：源文件路径（相对仓库根）。
- `nodes[].data.line` / `endLine`：函数体起止行，用于源码栏高亮；to-be 占位节点的零值在画布导出时自动省略。
- `nodes[].data.signature`：函数签名。
- `nodes[].data.comment`：节点批注。
- `edges[].from` / `to`：数据流向（调用 / 依赖）。
- `edges[].label`：边标签，标数据含义（如 `user`、`cart`），可空。
- `intent`：**改动意图**——人工写给 AI 的注脚（画布右侧栏编排后加入，机械提取不产），样例中的「人工修改意图」即一例。
- 坐标不进 JSON：布局由画布按调用图最长路径 + 真实节点高度实时计算；提取器产物自带的 `position` 在画布导出时省略，旧版带坐标文件仍可导入。
- 结构约定：类方法节点用限定名（如 `Memory._conn`）；函数体内嵌套 def 不收录（黑盒不展开）。

## License

MIT
