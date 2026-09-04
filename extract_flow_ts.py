#!/usr/bin/env python3
"""extract_flow_ts.py — Tree-sitter 版 FunctionFlow (functionflow/v1) 抽取器。

与 ast 版 (extract_flow.py) 同产出、同 schema，但：
  - 解析器语言无关，靠 LANGUAGES 注册表按扩展名选 grammar；
  - 当前内置 python / javascript / typescript 适配器；
  - 加语言 = pip install tree-sitter-<lang> + 在 LANGUAGES 注册一个适配器，
    布局/导出逻辑完全复用（已验证：JS/TS 与 Python 产出形态一致）。

多语言就绪说明：
  - tree-sitter-languages 全集包在 cp313-win-amd64 上无可用 wheel，故走
    "核心库 + 单语言 grammar 包" 路线；每条语言一个轻量适配器，扩展成本极低。
  - 这正是相对 ast 的核心收益：ast 只能吃 Python，Tree-sitter 一张解析器吃多语言。

用法:
    python extract_flow_ts.py <目录或文件> [-o 输出.json] [--lang python]
    python extract_flow_ts.py src/aimh/hma_core.py --focus query_anchors --hops 1

    --focus <函数名>   以该函数为入口取子图（限定名精确匹配优先，裸名唯一匹配兜底）
    --hops <N>          沿调用边 BFS 的跳数（默认 1），配合 --focus 把审查拉回可读规模
    --dir down|up|both  子图扩展方向：下游被调 / 上游调用方 / 双向（默认 down）

忠实性边界（同 ast 版，不猜）:
  - 只抽"调用图"，不做跨过程数据流分析，边不带参数级 label
  - import 但未调用的函数不产生边（import ≠ 数据流）
  - 类构造（new Order()）/ 异常构造不是函数节点，不产生边
  - 类方法抽为限定名节点（Memory._conn），函数体内的嵌套函数定义属内部控制流，不展开、不收录
  - self/this 前缀与显式类名前缀（cls._parse_fm / EventPackage._parse_value）的方法调用沿继承链解析：
    先向上查基类最近定义，落空再向下查子类实现（基类 self.X 运行时落子类，多态分发）；同文件为限
  - 无编排代码时主链路不出边——as-is 现状；画布上人工补的数据流 = to-be 意图
"""
from __future__ import annotations

import sys
import json
import argparse
import datetime
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript

SKIP_DIRS = {"__pycache__", ".workbuddy", "node_modules", ".git"}


# ---------------------------------------------------------------------------
# 语言适配器注册表
#   每个适配器提供 4 个函数：
#     defs(root) -> 迭代 (func_node, 限定名) 二元组（类方法带 Class. 前缀，嵌套定义跳过）
#     class_bases(root) -> [(类限定名, [基类裸名])]，供 self/this 方法调用沿基类链解析
#     func_meta(func_node) -> (signature_text, docstring)
#     call_roots(func_node) -> 调用根名集合（obj.method -> obj；self/this.method -> "self.method" 待解析）
# ---------------------------------------------------------------------------

def _walk(root):
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        for c in n.children:
            stack.append(c)


def _qual(classes, name):
    return ".".join(classes + (name,)) if classes else name


# ---- Python ----
def _py_defs(node, classes=()):
    for c in node.children:
        t = c.type
        if t == "function_definition":
            nm = c.child_by_field_name("name")
            name = nm.text.decode() if nm else ""
            if name:
                yield (c, _qual(classes, name))
            # 不下钻函数体：嵌套 def 是内部控制流（黑盒不展开）
        elif t == "class_definition":
            nm = c.child_by_field_name("name")
            cname = nm.text.decode() if nm else ""
            body = c.child_by_field_name("body")
            if body is not None:
                yield from _py_defs(body, classes + (cname,))
        else:
            yield from _py_defs(c, classes)


def _py_class_bases(root):
    out = []

    def walk(node, classes=()):
        for c in node.children:
            t = c.type
            if t == "class_definition":
                nm = c.child_by_field_name("name")
                cname = nm.text.decode() if nm else ""
                qual = _qual(classes, cname)
                bases = []
                sup = c.child_by_field_name("superclasses")
                if sup is not None:
                    for x in _walk(sup):
                        if x.type == "identifier":
                            bases.append(x.text.decode())
                out.append((qual, bases))
                body = c.child_by_field_name("body")
                if body is not None:
                    walk(body, classes + (cname,))
            elif t == "function_definition":
                continue        # 函数体内的类是内部细节，不参与继承解析
            else:
                walk(c, classes)

    walk(root)
    return out


def _py_func_meta(node) -> tuple:
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    params_text = params.text.decode() if params else "()"
    ret_text = ret.text.decode() if ret else ""
    sig = params_text + (f" -> {ret_text}" if ret_text else "")
    doc = ""
    body = node.child_by_field_name("body")
    if body and body.children:
        first = body.children[0]
        if first.type == "expression_statement" and first.children:
            s = first.children[0]
            if s.type in ("string", "concatenated_string", "string_content"):
                doc = _strip_quotes(s.text.decode())
    return sig, doc


def _py_call_roots(node) -> set:
    roots = set()
    for n in _walk(node):
        if n.type == "call":
            f = n.child_by_field_name("function")
            if f is None:
                continue
            if f.type == "identifier":
                roots.add(f.text.decode())
            elif f.type == "attribute":
                obj = f.child_by_field_name("object")
                attr = f.child_by_field_name("attribute")
                if obj is not None and obj.type == "identifier" and attr is not None:
                    # 一律编码为 "接收者.方法"（含 self/cls），由边构建阶段判定：
                    # self/cls → 所属类继承链；本地类名 → 限定名；其余回退按根名
                    roots.add(obj.text.decode() + "." + attr.text.decode())
    return roots


# ---- JavaScript / TypeScript（TS 是 JS 的超集，节点类型基本一致，共用适配器）----
JS_FUNC_TYPES = ("function_declaration", "generator_function_declaration")
JS_ANON_FUNC_TYPES = ("arrow_function", "function_expression",
                      "generator_function", "function_declaration")
JS_CLASS_TYPES = ("class_declaration", "abstract_class_declaration")
JS_DECL_TYPES = ("lexical_declaration", "variable_declaration")


def _js_defs(node, classes=(), func_depth=0):
    """类方法 → Class.name；函数体内（func_depth>0）的声明与箭头一律视为嵌套，不收录。

    顶层对象字面量方法（无类上下文的 method_definition）保持裸名，与旧版行为一致。
    """
    for c in node.children:
        t = c.type
        if t in JS_FUNC_TYPES:
            nm = c.child_by_field_name("name")
            name = nm.text.decode() if nm else ""
            if name and func_depth == 0:
                yield (c, _qual(classes, name))
            # 不下钻函数体
        elif t == "method_definition":
            nm = c.child_by_field_name("name")
            name = nm.text.decode() if nm else ""
            if name:
                yield (c, _qual(classes, name))
            # 不下钻方法体
        elif t in JS_CLASS_TYPES:
            if func_depth > 0:
                continue        # 函数体内定义的类是内部细节
            nm = c.child_by_field_name("name")
            cname = nm.text.decode() if nm else ""
            body = c.child_by_field_name("body")
            if body is not None:
                yield from _js_defs(body, classes + (cname,), func_depth)
        elif t in JS_DECL_TYPES:
            if func_depth == 0:
                for d in c.children:
                    if d.type != "variable_declarator":
                        continue
                    val = d.child_by_field_name("value")
                    if val is not None and val.type in JS_ANON_FUNC_TYPES:
                        nm = d.child_by_field_name("name")
                        name = nm.text.decode() if nm else ""
                        if name:
                            yield (val, _qual(classes, name))
            # 不下钻（声明里的函数体不展开）
        elif t in JS_ANON_FUNC_TYPES:
            # 匿名/箭头函数（IIFE、回调等）不可具名收录，但其函数体内的具名声明同样视为嵌套
            yield from _js_defs(c, classes, func_depth + 1)
        else:
            yield from _js_defs(c, classes, func_depth)


def _js_class_bases(root):
    out = []

    def walk(node, classes=()):
        for c in node.children:
            t = c.type
            if t in JS_CLASS_TYPES:
                nm = c.child_by_field_name("name")
                cname = nm.text.decode() if nm else ""
                qual = _qual(classes, cname)
                bases = []
                for x in c.children:
                    if x.type == "class_heritage":
                        # 泛型基类 Base<T> 里的 T 也是 identifier，会一并混入（一期接受此噪声）
                        for y in _walk(x):
                            if y.type == "identifier":
                                bases.append(y.text.decode())
                out.append((qual, bases))
                body = c.child_by_field_name("body")
                if body is not None:
                    walk(body, classes + (cname,))
            elif t in JS_FUNC_TYPES or t in JS_ANON_FUNC_TYPES or t == "method_definition":
                continue
            else:
                walk(c, classes)

    walk(root)
    return out


def _js_func_meta(node) -> tuple:
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    params_text = params.text.decode() if params else "()"
    # TS 的 return_type 字段含前导 ":"（type_annotation 节点），JS 无 return_type；
    # 统一剥掉前导 ": " 再拼，避免双冒号。
    ret_text = ret.text.decode().lstrip(": ").strip() if ret else ""
    sig = params_text + (f": {ret_text}" if ret_text else "")
    return sig, ""


def _js_call_roots(node) -> set:
    roots = set()
    for n in _walk(node):
        if n.type == "call_expression":
            f = n.child_by_field_name("function")
            if f is None:
                continue
            if f.type == "identifier":
                roots.add(f.text.decode())
            elif f.type == "member_expression":
                # 沿成员链下钻到根：a.b.c() -> a；this.a.b() -> this
                cur = f
                while cur is not None and cur.type == "member_expression":
                    cur = cur.child_by_field_name("object")
                prop = f.child_by_field_name("property")
                if cur is not None and cur.type == "this":
                    if prop is not None:
                        roots.add("self." + prop.text.decode())
                elif cur is not None and cur.type == "identifier" and prop is not None:
                    # 编码为 "接收者.方法"，由边构建阶段判定（本地类名 → 限定名；其余回退按根名）
                    roots.add(cur.text.decode() + "." + prop.text.decode())
    return roots


def _strip_quotes(s: str) -> str:
    s = s.strip()
    for q in ('"""', "'''", '"', "'"):
        if len(s) >= 2 * len(q) and s.startswith(q) and s.endswith(q):
            return s[len(q):-len(q)].strip()
    return s


LANGUAGES = {
    "python": {
        "ext": {".py"},
        "lang": Language(tspython.language()),
        "defs": _py_defs,
        "class_bases": _py_class_bases,
        "func_meta": _py_func_meta,
        "call_roots": _py_call_roots,
    },
    "javascript": {
        "ext": {".js"},
        "lang": Language(tsjavascript.language()),
        "defs": _js_defs,
        "class_bases": _js_class_bases,
        "func_meta": _js_func_meta,
        "call_roots": _js_call_roots,
    },
    "typescript": {
        "ext": {".ts"},
        "lang": Language(tstypescript.language_typescript()),
        "defs": _js_defs,
        "class_bases": _js_class_bases,
        "func_meta": _js_func_meta,
        "call_roots": _js_call_roots,
    },
}


def collect(target: Path, lang: str) -> list:
    if target.is_file():
        return [target]
    exts = LANGUAGES[lang]["ext"]
    return sorted(
        p for p in target.rglob("*")
        if p.suffix in exts
        and not any(part in SKIP_DIRS or part.startswith(".") for part in p.parts)
    )


def main():
    ap = argparse.ArgumentParser(description="源码(Tree-sitter) → functionflow/v1 JSON")
    ap.add_argument("target", help="目录或单个源文件")
    ap.add_argument("-o", "--out", default=None, help="输出 JSON 路径（缺省打印到 stdout）")
    ap.add_argument("--lang", default=None, help="强制语言（缺省按扩展名推断）")
    ap.add_argument("--focus", default=None, help="以该函数为入口取子图（限定名精确匹配优先，裸名唯一匹配兜底）")
    ap.add_argument("--hops", type=int, default=1, help="子图 BFS 跳数（默认 1，配合 --focus）")
    ap.add_argument("--dir", choices=("down", "up", "both"), default="down",
                    help="子图扩展方向：down=下游被调 / up=上游调用方 / both（默认 down）")
    args = ap.parse_args()

    target = Path(args.target)
    if args.lang:
        lang = args.lang
        if lang not in LANGUAGES:
            sys.exit(f"unsupported --lang={lang}; available: {', '.join(LANGUAGES)}")
    else:
        ext = target.suffix if target.is_file() else ".py"
        lang = next((k for k, v in LANGUAGES.items() if ext in v["ext"]), None)
        if lang is None:
            sys.exit(f"cannot infer language from {ext}; pass --lang")

    adapter = LANGUAGES[lang]
    parser = Parser(adapter["lang"])
    files = collect(target, lang)
    if not files:
        sys.exit(f"no {lang} files found under {target}")

    funcs = []
    class_bases = {}   # 类限定名 -> [基类限定名]；跨文件基类一期不解析
    class_bare = {}    # 裸类名 -> 类限定名（跨文件先到先得），用于解析 Obj.method(...) 显式类名调用
    for f in files:
        tree = parser.parse(f.read_bytes())
        bare2qual = {}
        raw_classes = adapter["class_bases"](tree.root_node)
        for qual, _bases in raw_classes:
            bare2qual.setdefault(qual.rsplit(".", 1)[-1], qual)
        for qual, bases in raw_classes:
            class_bases[qual] = [bare2qual[b] for b in bases if b in bare2qual]
            class_bare.setdefault(qual.rsplit(".", 1)[-1], qual)
        for fn, qual in adapter["defs"](tree.root_node):
            sig, doc = adapter["func_meta"](fn)
            funcs.append({
                "file": f.as_posix(),
                "name": qual,
                "owner": qual.rsplit(".", 1)[0] if "." in qual else None,
                "node": fn,
                "sig": sig,
                "doc": doc,
                "start": fn.start_point[0] + 1,
                "end": fn.end_point[0] + 1,
            })
    funcs.sort(key=lambda d: (d["file"], d["start"]))

    name2id, nodes = {}, []
    for i, d in enumerate(funcs, 1):
        nid = f"n{i}"
        name2id.setdefault(d["name"], nid)
        nodes.append({
            "id": nid,
            "type": "function",
            "data": {
                "function": d["name"],
                "file": d["file"],
                "line": d["start"],
                "endLine": d["end"],
                "signature": d["sig"],
                "comment": d["doc"],
            },
        })

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
        for root_name in adapter["call_roots"](d["node"]):
            for tid in resolve_call(root_name, d["owner"]):
                if not tid or tid == caller_id:
                    continue
                key = (caller_id, tid)
                if key in seen:
                    continue
                seen.add(key)
                raw_edges.append(key)
    edges = [{"id": f"e{i+1}", "from": a, "to": b} for i, (a, b) in enumerate(raw_edges)]

    # ---- --focus/--hops 子图过滤（在全图拓扑上 BFS，子图重新布局；与 ast 版同语义） ----
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

    payload = {
        "schema": "functionflow/v1",
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "projectRoot": (args.target.replace("\\", "/").rstrip("/")) + "/",
        "language": lang,
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
