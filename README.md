# AI-FunctionTopo

把代码函数块可视化成 workflow 节点，用于审查代码和规划重构，并把画布上重排好的数据流导出 JSON 交给 AI 照图修改源码——「人定方向、AI 干活」。

**AI-FunctionTopo: function-level code visualization & AI refactoring collaboration. Extract functions to a canvas, reroute data flows, export JSON for AI to rewrite code. Buildless, zero deps.**

## 快速开始（免构建，零 node_modules）

画布是单文件 `canvas.html` + `vendor/` 静态库（React/ReactFlow UMD，共 ~306KB），浏览器直接跑：

- **双击打开 `canvas.html`**：默认出导入遮罩——把 JSON 拖进窗口 / 点「选择文件」/ 载入 orders 示例；
- 或起静态服务用深链直载提取产物：
  `python -m http.server 8642` → <http://127.0.0.1:8642/canvas.html?src=ff_workspace/orders/json/orders%20示例.json>
- 把 JSON 文件**拖进窗口** = 导入。

> 源码查看用 `fetch()` 读文件，`file://` 直接打开会被浏览器拦截。除源码栏外，其余功能不受影响。

## 从源码生成图

```bash
python extract_flow.py ff_workspace/orders/scripts                # ast 版（纯标准库零依赖）
python extract_flow_ts.py ff_workspace/orders/scripts -o out.json # Tree-sitter 版（py/js/ts，与 ast 版产出全等）
python extract_flow_ts.py ff_workspace/samples/aimh --focus query_anchors --hops 1   # 子图：回 20-30 节点可读规模
python extract_flow_ts.py ff_workspace/samples/aimh --focus _conn --dir up --hops 2  # 改动影响面（上游调用方）
python extract_flow.py ff_workspace/orders/scripts --strip-position  # AI 消费形态（剥离画布坐标）
```

Tree-sitter 版依赖：`pip install tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript`

> 注：`ff_workspace/samples/aimh` 若不在本地（工作区不入 git），可对任意真实项目源码跑同样命令。

## 画布功能

- 函数节点黑盒：函数名 / 签名 / 注释(docstring) / 文件位置行号，左入右出端口
- 拖拽、缩放（5%–250%）、框选、小地图、动态数据流边；选中后 Backspace/Delete 删除
- **＋ 节点**：画布上直接新增 to-be 函数节点（编排/新函数）
- **◎ 聚焦**：只显示该函数 ±1 跳上下游（诱导子图）；聚焦态导出 = 只导聚焦子图 + intent
- **双击节点** = 编辑批注（comment）；**双击边** = 编辑标签（label）
- **右侧栏**：改动意图（intent 字段）+ 源码审查（`{ }` 按钮，行号区间高亮）
- **流入预警**（图论叫扇入）：≥5 琥珀、≥10 红色；人工补边后实时重算
- 隐藏孤岛节点（默认开，画布新增节点豁免）
- 多图层 / 右键菜单 / 抽屉侧栏 / 空态导入遮罩

## AI 改码闭环

```
源码 --提取--> origin JSON --画布重排/聚焦/写 intent--> to-be JSON
     --交 AI（读图忽略 position 即可）--> AI 照图改源码（一期只限：新增编排函数串联既有函数）
     --再提取--> after JSON --机器 diff--> 差集 == 画布意图 即闭环成功
```

配套 skill：`skills/aift-refactor-diff`（JSON → 收敛 diff）+ `skills/aift-sandbox-refactor`（ff_workspace 沙箱闭环，副本编辑、原文件只读、回归需确认）。

## 目录结构

```
functionflow/
├── canvas.html              # 主画布（项目核心，单文件 React + React Flow）
├── extract_flow.py          # Python 源码 → functionflow/v1 JSON
├── extract_flow_ts.py       # TS / JS 源码 → functionflow/v1 JSON
├── skills/                  # aift 双 skill 权威源
├── ff_workspace/            # 默认工作与存储区：<订单>/scripts + json（gitignore，不入库）
└── vendor/                  # canvas.html 的本地依赖（react / reactflow / htm）
```

## JSON 契约：functionflow/v1

**一份 JSON，两种用途**：能导回画布，也能直接喂给 AI 改码。

```json
{
  "schema": "functionflow/v1",
  "generatedAt": "2026-09-02T07:10:00.000Z",
  "projectRoot": "src/orders/",
  "language": "python",
  "intent": "人工意图层：为什么这么改（画布编排后加入，机械提取不产）",
  "nodes": [
    {
      "id": "n1",
      "type": "function",
      "position": { "x": 40, "y": 60 },
      "data": {
        "function": "Memory._conn",
        "file": "src/aimh/hma_core.py",
        "line": 1846,
        "endLine": 1850,
        "signature": "(self)",
        "comment": "说明文字"
      }
    }
  ],
  "edges": [
    { "id": "e1", "from": "n1", "to": "n2", "label": "user" }
  ]
}
```

类方法节点用限定名（`Memory._conn`）；函数体内嵌套 def 不收录（黑盒不展开）。
position 是画布布局，AI 消费时忽略即可；`--strip-position` 可剥离省 token。

## License

MIT
