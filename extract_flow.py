#!/usr/bin/env python3
"""extract_flow.py — 从 Python 源码抽取 FunctionFlow (functionflow/v1) 数据流图。

用法:
    python extract_flow.py <目录或 .py 文件> [-o 输出.json]
    python extract_flow.py src/aimh/hma_core.py --focus query_anchors --hops 1 -o focus.json

    --focus <函数名>   以该函数为入口取子图（限定名精确匹配优先，裸名唯一匹配兜底）
    --hops <N>          沿调用边 BFS 的跳数（默认 1），配合 --focus 把审查拉回可读规模
    --dir down|up|both  子图扩展方向：下游被调 / 上游调用方 / 双向（默认 down）

产出:
    nodes: 全部函数（file / line-endLine / signature / docstring→comment）
    edges: 函数间调用边（解析 from-import 与属性调用根名，如 send_confirmation.delay → send_confirmation）
    布局: 按调用图拓扑深度分列（列距 360），列内 280 间距、中心对齐

忠实性边界（一期，不猜）:
    - 只抽"调用图"，不做跨过程数据流分析，边不带参数级 label
    - import 但未调用的函数不产生边（import ≠ 数据流）
    - 类构造（Order(...)）、异常构造（UserNotFound(...)）不是函数节点，不产生边
    - 类方法抽为限定名节点（Memory._conn），函数体内的嵌套 def 属内部控制流，黑盒不展开、不收录
    - self/cls 前缀与显式类名前缀（cls._parse_fm / EventPackage._parse_value）的方法调用沿继承链解析：
      先向上查基类最近定义，落空再向下查子类实现（基类 self.X 运行时落子类，多态分发）；同文件为限
    - 无编排代码时主链路不出边——这就是 as-is 现状；画布上人工补的数据流 = to-be 意图，二者之差即 diff 来源
"""
from __future__ import annotations

import ast
import json
import sys
import argparse
import datetime
from pathlib import Path

SKIP_DIRS = {"__pycache__", ".workbuddy", "node_modules", ".git"}


def collect(target: Path):
    if target.is_file():
        return [target]
    return sorted(
        p for p in target.rglob("*.py")
        if not any(part in SKIP_DIRS or part.startswith(".") for part in p.parts)
    )


def iter_defs(node, classes=()):
    """产出 (函数节点, 限定名)：顶层函数裸名，类方法带类前缀（Memory._conn）。

    函数体内的嵌套 def 是内部控制流，按黑盒铁律不展开、不收录；
    类体内的 if/try 等包裹语句照常下钻（条件定义的方法仍属类）。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child, (".".join(classes + (child.name,)) if classes else child.name)
        elif isinstance(child, ast.ClassDef):
            yield from iter_defs(child, classes + (child.name,))
        else:
            yield from iter_defs(child, classes)


def iter_classes(node, classes=()):
    """产出 (类限定名, 基类裸名列表)，供 self.X() 调用沿本地基类链解析。

    函数体内定义的类是内部细节，不参与继承解析。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qual = ".".join(classes + (child.name,))
            yield qual, [b.id for b in child.bases if isinstance(b, ast.Name)]
            yield from iter_classes(child, classes + (child.name,))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        else:
            yield from iter_classes(child, classes)


def import_map(tree) -> dict:
    """local_name -> qualified name，用于过滤外部调用。"""
    m = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                m[a.asname or a.name] = f"{n.module}.{a.name}"
        elif isinstance(n, ast.Import):
            for a in n.names:
                m[(a.asname or a.name).split(".")[0]] = a.name
    return m


def signature(fn) -> str:
    a = fn.args
    parts = []
    args = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(args) - len(a.defaults)) + list(a.defaults)
    for arg, default in zip(args, defaults):
        s = arg.arg
        if arg.annotation is not None:
            s += ": " + ast.unparse(arg.annotation)
        if default is not None:
            s += " = " + ast.unparse(default)
        parts.append(s)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for kw, d in zip(a.kwonlyargs, a.kw_defaults):
        s = kw.arg
        if kw.annotation is not None:
            s += ": " + ast.unparse(kw.annotation)
        if d is not None:
            s += " = " + ast.unparse(d)
        parts.append(s)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    sig = "(" + ", ".join(parts) + ")"
    if fn.returns is not None:
        sig += " -> " + ast.unparse(fn.returns)
    return sig


def callee_roots(fn) -> set:
    """调用根名。裸调用 → 名字本身；属性调用 obj.attr() → "obj.attr"（含 self/cls）。

    复合名交边构建阶段判定：self/cls 按所属类沿继承链解析；本地类名按限定名解析；
    普通变量接收者回退按根名（send_confirmation.delay() → send_confirmation）。
    """
    roots = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            roots.add(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            roots.add(f.value.id + "." + f.attr)
    return roots


def main():
    ap = argparse.ArgumentParser(description="Python 源码 → functionflow/v1 JSON")
    ap.add_argument("target", help="目录或单个 .py 文件")
    ap.add_argument("-o", "--out", default=None, help="输出 JSON 路径（缺省打印到 stdout）")
    ap.add_argument("--focus", default=None, help="以该函数为入口取子图（限定名精确匹配优先，裸名唯一匹配兜底）")
    ap.add_argument("--hops", type=int, default=1, help="子图 BFS 跳数（默认 1，配合 --focus）")
    ap.add_argument("--dir", choices=("down", "up", "both"), default="down",
                    help="子图扩展方向：down=下游被调 / up=上游调用方 / both（默认 down）")
    ap.add_argument("--strip-position", action="store_true",
                    help="AI 消费形态：position 置 null（画布布局对改码是噪声，剥离省 token）")
    args = ap.parse_args()

    target = Path(args.target)
    files = collect(target)
    if not files:
        sys.exit("no .py files found under " + str(target))

    # ---- 收集函数节点（类方法限定名）+ 类继承表（仅同文件解析） ----
    funcs = []
    class_bases = {}   # 类限定名 -> [基类限定名]；跨文件基类一期不解析
    class_bare = {}    # 裸类名 -> 类限定名（跨文件先到先得），用于解析 EventPackage._parse_value(...) 显式类名调用
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        raw_classes = list(iter_classes(tree))
        bare2qual = {}
        for qual, _bases in raw_classes:
            bare2qual.setdefault(qual.rsplit(".", 1)[-1], qual)
        for qual, bases in raw_classes:
            class_bases[qual] = [bare2qual[b] for b in bases if b in bare2qual]
            class_bare.setdefault(qual.rsplit(".", 1)[-1], qual)
        for n, qual in iter_defs(tree):
            funcs.append({
                "file": f.as_posix(),
                "name": qual,
                "owner": qual.rsplit(".", 1)[0] if "." in qual else None,
                "node": n,
                "sig": signature(n),
                "doc": ast.get_docstring(n) or "",
            })
    funcs.sort(key=lambda d: (d["file"], d["node"].lineno))

    name2id = {}
    nodes = []
    for i, d in enumerate(funcs, 1):
        nid = f"n{i}"
        name2id.setdefault(d["name"], nid)
        nodes.append({
            "id": nid,
            "type": "function",
            "position": None,  # 布局阶段回填
            "data": {
                "function": d["name"],
                "file": d["file"],
                "line": d["node"].lineno,
                "endLine": d["node"].end_lineno,
                "signature": d["sig"],
                "comment": d["doc"],
            },
        })

    # ---- 调用边（caller → callee，限本地函数；同名函数多处调用去重） ----
    subclasses = {}    # 类限定名 -> [直接子类]
    for qual, bases in class_bases.items():
        for b in bases:
            subclasses.setdefault(b, []).append(qual)

    def resolve_targets(cls_qual, method_name):
        """方法调用落点：先沿基类向上取最近定义；落空再沿子类向下——
        基类作用域里的 self.X() 运行时落到子类实现（多态分发），命中几个实现就连几条边。"""
        seen, stack = set(), [cls_qual]
        while stack:
            c = stack.pop(0)
            if c in seen:
                continue
            seen.add(c)
            tid = name2id.get(c + "." + method_name)
            if tid:
                return [tid]
            stack.extend(class_bases.get(c, ()))
        seen, stack, hits = set(), [cls_qual], []
        while stack:
            c = stack.pop(0)
            if c in seen:
                continue
            seen.add(c)
            for ch in subclasses.get(c, ()):
                tid = name2id.get(ch + "." + method_name)
                if tid:
                    hits.append(tid)
            stack.extend(subclasses.get(c, ()))
        return hits

    def resolve_call(root_name, owner):
        """单个调用根名 → 落点 id 列表。"""
        if "." not in root_name:
            tid = name2id.get(root_name)
            return [tid] if tid else []
        base, attr = root_name.split(".", 1)
        if base in ("self", "cls"):
            return resolve_targets(owner, attr) if owner else []
        if base in class_bare:
            return resolve_targets(class_bare[base], attr)
        tid = name2id.get(base)     # 普通变量接收者：回退按根名（obj.delay() → obj）
        return [tid] if tid else []

    raw_edges, seen = [], set()
    for d in funcs:
        caller_id = name2id[d["name"]]
        for root_name in callee_roots(d["node"]):
            for tid in resolve_call(root_name, d["owner"]):
                if not tid or tid == caller_id:
                    continue
                key = (caller_id, tid)
                if key in seen:
                    continue
                seen.add(key)
                raw_edges.append(key)

    edges = [{"id": f"e{i+1}", "from": a, "to": b} for i, (a, b) in enumerate(raw_edges)]

    # ---- --focus/--hops 子图过滤（在全图拓扑上 BFS，子图重新布局） ----
    if args.focus:
        adj_down, adj_up = {}, {}
        for e in edges:
            adj_down.setdefault(e["from"], []).append(e["to"])
            adj_up.setdefault(e["to"], []).append(e["from"])
        fid = None
        for n in nodes:
            if n["data"]["function"] == args.focus:
                fid = n["id"]
                break
        if fid is None:
            cands = [n for n in nodes if n["data"]["function"].rsplit(".", 1)[-1] == args.focus]
            if len(cands) > 1:
                sys.exit(f"--focus {args.focus} 有多个匹配: " +
                         ", ".join(c["data"]["function"] for c in cands))
            if cands:
                fid = cands[0]["id"]
        if fid is None:
            sys.exit(f"--focus {args.focus} 未找到")
        keep, frontier = {fid}, {fid}
        for _ in range(max(0, args.hops)):
            nxt = set()
            for u in frontier:
                if args.dir in ("down", "both"):
                    nxt.update(adj_down.get(u, ()))
                if args.dir in ("up", "both"):
                    nxt.update(adj_up.get(u, ()))
            nxt -= keep
            keep |= nxt
            frontier = nxt
            if not frontier:
                break
        nodes = [n for n in nodes if n["id"] in keep]
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]

    # ---- 布局：调用图最长路径分列，列内居中 ----
    level = {n["id"]: 0 for n in nodes}
    for _ in range(len(nodes) + 1):
        changed = False
        for e in edges:
            if level[e["to"]] < level[e["from"]] + 1:
                level[e["to"]] = level[e["from"]] + 1
                changed = True
        if not changed:
            break
    layers = {}
    for n in nodes:
        layers.setdefault(level[n["id"]], []).append(n)
    GAP_X, GAP_Y, BASE_Y = 360, 280, 250
    for lv in sorted(layers):
        group = sorted(layers[lv], key=lambda n: n["data"]["line"])
        for i, n in enumerate(group):
            n["position"] = {
                "x": 40 + lv * GAP_X,
                "y": int(BASE_Y + (i - (len(group) - 1) / 2) * GAP_Y),
            }

    if args.strip_position:
        for n in nodes:
            n["position"] = None
    payload = {
        "schema": "functionflow/v1",
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "projectRoot": (args.target.replace("\\", "/").rstrip("/")) + "/",
        "nodes": nodes,
        "edges": edges,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print("WROTE", out)
    else:
        sys.stdout.write(text)
    print(f"{len(nodes)} nodes, {len(edges)} edges", file=sys.stderr)


if __name__ == "__main__":
    main()
