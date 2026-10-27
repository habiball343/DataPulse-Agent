"""Schema Linking 与动态 Few-Shot 检索。

真实业务库动辄几十上百张表，把全部表结构塞进提示词既贵又会干扰模型。
这里的做法：
  1. 把“表说明 / 字段说明 / 指标口径”分别向量化存入 Chroma；
  2. 用户提问时检索最相关的若干条，按表聚合打分，选出 Top-K 张表；
  3. 沿外键补齐连接所需的“桥接表”（例如问“客户 + 线路”时自动带上 waybills）；
  4. 对字段较多的表做列剪枝：只保留主键、外键和与问题相关的字段；
  5. 另建一个集合存“业务问题 - 标准 SQL”示例，按问题相似度取 Top-K 作为 Few-Shot。
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import chromadb
import yaml
from chromadb.config import Settings as ChromaSettings

from datapulse.db import ColumnInfo, Database
from datapulse.embeddings import Embedder

_GENERIC_TERMS = {"主键", "关联", "格式", "结果", "单位"}


def _is_time_column(name: str) -> bool:
    return any(k in name for k in ("date", "time", "month"))


def _metric_terms(metric: dict[str, Any]) -> list[str]:
    raw = [metric["name"], *re.split(r"[、，,]", str(metric.get("synonyms", "")))]
    terms = [re.sub(r"[（(].*$", "", t).strip() for t in raw]
    return [t for t in terms if len(t) >= 2]


def load_resource(name: str) -> Any:
    text = resources.files("datapulse.resources").joinpath(name).read_text(encoding="utf-8")
    return yaml.safe_load(text)


@dataclass
class TableSchema:
    name: str
    description: str
    columns: list[ColumnInfo]
    column_docs: dict[str, str]
    aliases: list[str] = field(default_factory=list)


@dataclass
class LinkedSchema:
    tables: list[str]
    schema_text: str
    metrics: list[dict[str, str]] = field(default_factory=list)
    examples: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"tables": self.tables, "schema": self.schema_text, "metrics": self.metrics, "examples": self.examples}


class SchemaIndex:
    SIM_FLOOR = 0.1  # 余弦相似度噪声阈值（针对默认的 HashEmbedding 标定）
    METRIC_FLOOR = 0.25
    TERM_HIT = 0.6  # 问题中出现字段业务词
    VALUE_HIT = 0.7  # 问题中出现字段真实取值
    ALIAS_HIT = 0.5  # 问题中出现表的业务别名

    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        catalog: dict[str, Any] | None = None,
        few_shots: list[dict[str, str]] | None = None,
        max_columns_per_table: int = 10,
    ):
        self.db = db
        self.embedder = embedder
        self.catalog = catalog if catalog is not None else load_resource("catalog.yaml")
        self.few_shots = few_shots if few_shots is not None else load_resource("few_shots.yaml")
        self.max_columns_per_table = max_columns_per_table

        self.tables: dict[str, TableSchema] = {}
        for name in db.table_names():
            meta = self.catalog.get("tables", {}).get(name, {})
            self.tables[name] = TableSchema(
                name=name,
                description=meta.get("description", ""),
                columns=db.columns(name, max_distinct=15),
                column_docs=meta.get("columns", {}) or {},
                aliases=[a.strip() for a in re.split(r"[、，,]", meta.get("aliases", "") or "") if a.strip()],
            )
        self.metrics: list[dict[str, Any]] = self.catalog.get("metrics", []) or []
        self._graph = self._build_fk_graph()
        self._lexicon = self._build_lexicon()

        client = chromadb.EphemeralClient(settings=ChromaSettings(anonymized_telemetry=False))
        suffix = uuid.uuid4().hex[:8]
        cfg = {"hnsw": {"space": "cosine"}}
        self._schema = client.create_collection(f"schema-{suffix}", embedding_function=None, configuration=cfg)
        self._examples = client.create_collection(f"examples-{suffix}", embedding_function=None, configuration=cfg)
        self._index()

    # ---------- 建索引 ----------

    def _build_fk_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = defaultdict(set)
        for t in self.tables.values():
            for c in t.columns:
                if c.references:
                    other = c.references.split(".")[0]
                    graph[t.name].add(other)
                    graph[other].add(t.name)
        return graph

    def _build_lexicon(self) -> list[tuple[str, str, set[str], set[str]]]:
        """为每个字段提取“业务词”（来自中文说明）和“枚举值”（来自真实数据），用于精确匹配。"""
        lexicon = []
        for t in self.tables.values():
            for c in t.columns:
                doc = t.column_docs.get(c.name, "")
                terms = {
                    w for w in re.split(r"[、，,（）()/：:；;=×\s]+", doc)
                    if 2 <= len(w) <= 8 and re.search(r"[\u4e00-\u9fff]", w) and w not in _GENERIC_TERMS
                }
                lexicon.append((t.name, c.name, terms, {v for v in c.sample_values if len(v) >= 2}))
        return lexicon

    def _index(self) -> None:
        ids, docs, metas = [], [], []
        for t in self.tables.values():
            ids.append(f"table:{t.name}")
            docs.append(f"表 {t.name}（{'、'.join(t.aliases)}）：{t.description}")
            metas.append({"kind": "table", "table": t.name})
            for c in t.columns:
                desc = t.column_docs.get(c.name, "")
                values = f" 取值：{'、'.join(c.sample_values)}" if c.sample_values else ""
                ids.append(f"column:{t.name}.{c.name}")
                docs.append(f"{t.description.split('，')[0]} 的字段 {c.name}：{desc}{values}")
                metas.append({"kind": "column", "table": t.name, "column": c.name})
        for i, m in enumerate(self.metrics):
            ids.append(f"metric:{i}")
            docs.append(f"指标 {m['name']}（{m.get('synonyms', '')}）：{m['definition']}")
            metas.append({"kind": "metric", "index": i, "tables": ",".join(m.get("tables", []))})
        self._schema.add(ids=ids, documents=docs, metadatas=metas, embeddings=self.embedder.embed(docs))

        if self.few_shots:
            questions = [ex["question"] for ex in self.few_shots]
            self._examples.add(
                ids=[f"ex:{i}" for i in range(len(questions))],
                documents=questions,
                metadatas=[{"index": i} for i in range(len(questions))],
                embeddings=self.embedder.embed(questions),
            )

    # ---------- 检索 ----------

    def link(self, question: str, top_k_tables: int = 4, top_k_examples: int = 3) -> LinkedSchema:
        q_vec = self.embedder.embed([question])[0]
        n = min(40, self._schema.count())
        res = self._schema.query(query_embeddings=[q_vec], n_results=n, include=["metadatas", "distances"])

        best: dict[str, float] = defaultdict(float)
        total: dict[str, float] = defaultdict(float)
        column_hits: dict[str, dict[str, float]] = defaultdict(dict)
        metric_hits: list[tuple[float, int]] = []

        def vote(table: str, sim: float) -> None:
            best[table] = max(best[table], sim)
            total[table] += sim

        for meta, dist in zip(res["metadatas"][0], res["distances"][0], strict=True):
            sim = 1.0 - float(dist)
            if sim < self.SIM_FLOOR:  # 低于噪声阈值的命中直接忽略
                continue
            kind = meta["kind"]
            if kind == "table":
                vote(meta["table"], sim)
            elif kind == "column":
                vote(meta["table"], sim)
                column_hits[meta["table"]][meta["column"]] = sim
            elif kind == "metric":
                metric_hits.append((sim, int(meta["index"])))

        # 精确匹配层：问题里出现了字段的业务词（如“车型”“运费收入”）或真实取值（如“外协车队”“广州”），
        # 直接视为强命中。这一层稳定、可解释，向量检索负责兜住同义改写等模糊情况。
        for t in self.tables.values():
            if any(a in question for a in t.aliases):
                vote(t.name, self.ALIAS_HIT)
        for table, column, terms, values in self._lexicon:
            if any(v in question for v in values):
                vote(table, self.VALUE_HIT)
                column_hits[table][column] = max(column_hits[table].get(column, 0.0), self.VALUE_HIT)
            elif any(term in question for term in terms):
                vote(table, self.TERM_HIT)
                column_hits[table][column] = max(column_hits[table].get(column, 0.0), self.TERM_HIT)

        # 表打分 = 最强命中 + 0.25 × 命中总和：既看“有没有强相关字段”，也看“相关字段多不多”
        score = {t: best[t] + 0.25 * total[t] for t in best}
        ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        top = ranked[0][1] if ranked else 0.0
        selected = [t for t, s in ranked if s >= top * 0.5][:top_k_tables]

        # 指标识别：先做关键词精确匹配（名称 / 同义词出现在问题里），再用向量相似度兜底
        metrics = [m for m in self.metrics if any(term in question for term in _metric_terms(m))]
        metric_hits.sort(reverse=True)
        for s, i in metric_hits[:2]:
            if s >= self.METRIC_FLOOR and self.metrics[i] not in metrics:
                metrics.append(self.metrics[i])
        metrics = metrics[:4]
        for m in metrics:
            for t in m.get("tables", []):
                if t not in self.tables:
                    continue
                if t not in selected:
                    selected.append(t)
                for c in self.tables[t].columns:
                    if re.search(rf"\b{re.escape(c.name)}\b", m["definition"]):
                        column_hits[t][c.name] = 1.0

        if not selected:  # 什么都没命中时退化为“全部表”
            selected = list(self.tables)
        selected = self._add_bridge_tables(selected)

        schema_text = "\n\n".join(self._render_table(t, column_hits.get(t, {})) for t in selected)
        examples = self.search_examples(question, top_k_examples)
        return LinkedSchema(
            tables=selected,
            schema_text=schema_text,
            metrics=[{"name": m["name"], "definition": m["definition"]} for m in metrics],
            examples=examples,
        )

    def search_examples(self, question: str, top_k: int = 3) -> list[dict[str, str]]:
        if not self.few_shots or top_k <= 0:
            return []
        q_vec = self.embedder.embed([question])[0]
        res = self._examples.query(
            query_embeddings=[q_vec], n_results=min(top_k, len(self.few_shots)), include=["metadatas"]
        )
        return [
            {"question": self.few_shots[m["index"]]["question"], "sql": self.few_shots[m["index"]]["sql"].strip()}
            for m in res["metadatas"][0]
        ]

    def _add_bridge_tables(self, selected: list[str]) -> list[str]:
        """选中的表之间不连通时，沿外键最短路径补上中间表。"""
        result = list(selected)
        anchor = selected[0]
        for target in selected[1:]:
            path = self._shortest_path(anchor, target)
            for t in path:
                if t not in result:
                    result.append(t)
        return result

    def _shortest_path(self, src: str, dst: str) -> list[str]:
        prev: dict[str, str | None] = {src: None}
        queue = deque([src])
        while queue:
            node = queue.popleft()
            if node == dst:
                break
            for nxt in self._graph.get(node, ()):
                if nxt not in prev:
                    prev[nxt] = node
                    queue.append(nxt)
        if dst not in prev:
            return []
        path, node = [], dst
        while node is not None:
            path.append(node)
            node = prev[node]
        return path[::-1]

    # ---------- 渲染 ----------

    def _render_table(self, name: str, hits: dict[str, float], prune: bool = True) -> str:
        t = self.tables[name]
        cols = t.columns
        if prune and len(cols) > self.max_columns_per_table:
            # 主键、外键、时间列、枚举列始终保留：统计几乎都要关联其他表、按时间或状态过滤
            keep = {
                c.name for c in cols
                if c.primary_key or c.references or c.sample_values or _is_time_column(c.name)
            }
            for col, _ in sorted(hits.items(), key=lambda kv: kv[1], reverse=True):
                if len(keep) >= self.max_columns_per_table:
                    break
                keep.add(col)
            cols = [c for c in cols if c.name in keep]
        lines = [f"表 {t.name} —— {t.description}"]
        for c in cols:
            tags = []
            if c.primary_key:
                tags.append("主键")
            if c.references:
                tags.append(f"关联 {c.references}")
            desc = t.column_docs.get(c.name, "")
            values = f"；可选值：{' / '.join(c.sample_values)}" if c.sample_values else ""
            tag_text = f"[{', '.join(tags)}] " if tags else ""
            lines.append(f"  - {c.name} {c.type} {tag_text}{desc}{values}")
        if len(cols) < len(t.columns):
            lines.append(f"  （已按相关性省略 {len(t.columns) - len(cols)} 个字段，需要时可调用 describe_table 查看）")
        return "\n".join(lines)

    def render_table(self, name: str) -> str:
        """完整渲染一张表（不剪枝），供 describe_table 工具使用。"""
        return self._render_table(name, {}, prune=False)
