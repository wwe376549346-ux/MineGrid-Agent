"""Shared source text for the non-domain baselines."""
from collections import Counter, defaultdict
from math import log
import re

from laowadi.queries import READINGS, evidence
from laowadi.standards import load_corpus


def dump(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def locator(source):
    return f'{source["file"]} / {source["sheet"]}!{source["cell"]}'


def terms(text):
    text = text.lower()
    tokens = re.findall(r"[a-z]+|\d+|[\u3400-\u9fff]+", text)
    words = []
    for token in tokens:
        if "\u3400" <= token[0] <= "\u9fff" and len(token) > 1:
            words.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            words.append(token)
    return words


class TextCorpus:
    def __init__(self, archive):
        self.documents = []
        self.cells = {}
        self.overview = (
            "老洼地 S35 为地面 35 kV 站，2026-11-10 到 2026-11-16 每日报有 24 个整点。"
            "C1535 为井下1535中央变电所。两站截图归档为一次系统图，"
            "仅 S35 有逐小时日报。值班日志、铭牌、秒级同步遥测及实际开关 SOE 均未入本批归档。"
        )
        blocks = defaultdict(list)
        tables = defaultdict(dict)
        for row in archive.db.execute(READINGS + " ORDER BY day, cabinet, time, metric"):
            timestamp, cabinet, metric = row["time"], row["cabinet"], row["metric"]
            day, hour = row["day"], int(timestamp[11:13])
            cell = locator(evidence(row))
            self.cells[cell] = (row["value"], metric, cabinet, timestamp)
            # Fixed four-hour blocks of one cabinet, preserving every numeric cell and its locator.
            blocks[(day, cabinet, hour // 4)].append(f'{hour:02}时 {metric}={row["value"]} {cell}')
            tables[(day, cabinet)][(hour, metric)] = (row["value"], row["source_col"], row["source_row"])
        for (day, cab, _), values in blocks.items():
            label = archive.devices["S35", cab]["label"]
            self.documents.append(f"S35 地面35kV站 {day} {cab}柜 {label}\n" + "\n".join(values))
        derived = [self.overview]
        for row in archive.db.execute("SELECT * FROM line ORDER BY from_station, from_cabinet, to_station, to_cabinet"):
            derived.append(f'{row["from_station"]}.{row["from_cabinet"]} → {row["to_station"]}.{row["to_cabinet"]}'
                           f' {row["label"]}，一次系统图 35KV变电所.png 与 中央变电所.png')
        for clause in load_corpus():
            derived.append(" ".join(str(clause.get(k, "")) for k in ("id", "title", "text", "applicability")))
        self.documents.extend(derived)
        # Lossless per-day/per-cabinet table attachments. Selection is limited to
        # literal metadata from the user's question; no ranked retrieval.
        metric_order = ("Uab", "Ia", "P", "cos")
        self.table_help = ("原表附表。行按 00–23 时排序，各行带原表行号。"
                           "四列顺序 Uab,Ia,P,cos，附表头标出相应原表列字母。空串为缺测。"
                           "坐标格式示例 1113.xls / New Sheet1!C19。")
        self.tables = {}
        self.topology = derived[1:1 + archive.db.execute("SELECT count(*) FROM line").fetchone()[0]]
        filenames = {row[0]: row[1] for row in archive.db.execute("SELECT day, filename FROM report")}
        self.references = [text for text in derived if "GB/T " in text][:6]
        for (day, cab), table in tables.items():
            columns = ",".join(table[0, m][1] for m in metric_order)
            filename = filenames[day]
            summary = [f"{day} {filename} / New Sheet1 S35 {cab}柜 [{columns}]"]
            for hour in range(24):
                values = [table[hour, m] for m in metric_order]
                row_values = ",".join("" if v[0] is None else str(v[0]) for v in values)
                summary.append(f"{values[0][2]}:{row_values}")
            self.tables[day, cab] = "\n".join(summary)
        self.index = [Counter(terms(doc)) for doc in self.documents]
        self.inverted = defaultdict(list)
        lengths = [sum(doc.values()) for doc in self.index]
        avg = sum(lengths) / len(lengths)
        for idx, doc in enumerate(self.index):
            for word, count in doc.items():
                self.inverted[word].append((idx, count * 2.2 / (count + 1.2 * (0.25 + 0.75 * lengths[idx] / avg))))

    def vanilla_attachment(self, question):
        dates = set(re.findall(r"2026-11-\d{2}", question))
        if span := re.search(r"2026-11-(\d{2})至(\d{2})日", question):
            dates.update(f"2026-11-{day:02}" for day in range(int(span[1]), int(span[2]) + 1))
        cabinets = set(re.findall(r"(?<![0-9])(?:[0-9]{3,4})(?![0-9])", question))
        units = [text for (day, cab), text in self.tables.items() if day in dates and cab in cabinets]
        diagram = [line for line in self.topology if any(re.search(r"\." + cab + r"\b", line) for cab in cabinets)]
        references = self.references if re.search(r"N-1|规程|条款|电压判定", question) else []
        return "\n".join([self.overview, self.table_help, *units, *diagram, *references])

    def search(self, query, top_k=8):
        rank = Counter()
        count = len(self.documents)
        for word in sorted(set(terms(query))):
            hits = self.inverted.get(word, [])
            idf = log(1 + (count - len(hits) + 0.5) / (len(hits) + 0.5))
            for idx, freq in hits:
                rank[idx] += idf * freq
        picks = sorted(rank, key=lambda i: (-rank[i], i))[:max(1, min(int(top_k), 8))]
        return {"status": "ok", "chunks": [self.documents[i] for i in picks]}
