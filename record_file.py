"""对局记录.csv 的读写。

用户会拿 Excel / WPS / 记事本等打开、改了再存，文件格式会变，这里都要认得：
  - 编码：UTF-8（带不带 BOM）、GBK/GB18030（Excel 默认存的「CSV(逗号分隔)」）、UTF-16（Excel「Unicode 文本」）
  - 分隔符：逗号、制表符（Unicode 文本）、分号（部分地区的 Excel）、竖线
  - 日期：2026-11-10、2026/11/10、2026/11/10 0:00、2026年11月10日、11/10/2026、10.11.2026、Excel 日期序号 46336
  - 时间：13:47、13:47:00、1:47 PM、下午 1:47、Excel 小数 0.5743
  - 数字：5671、5,671、5671.0、带空格
  - 表头：列被挪了位置、多了列、表头被删了、行被重新排过序、中间有空行
往后追加时沿用文件现在的编码、分隔符、列顺序和日期写法，免得一个文件里混着两种格式。
"""
import codecs
import csv
import datetime as _dt
import io
import os
import re

FIELDS = ("date", "time", "mode", "income", "tables", "fw")
HEAD = ("日期", "结束时间", "模式", "营业额", "送齐桌数", "满堂彩")
ALIASES = {  # 表头可能被改成的叫法（去空格、小写后比较）
    "date": ("日期", "date", "day"),
    "time": ("结束时间", "时间", "结束", "time"),
    "mode": ("模式", "mode", "经营模式"),
    "income": ("营业额", "收入", "营业", "income", "score"),
    "tables": ("送齐桌数", "送齐", "桌数", "送齐桌", "tables"),
    "fw": ("满堂彩", "fireworks", "fw"),
}


# ---------------- 编码 / 分隔符 ----------------
def _detect_encoding(data):
    """返回 (读用的编码, 追加用的编码)；追加时不再写 BOM"""
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16", "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", "utf-16-be"
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "utf-8"
    # 没 BOM 的 UTF-16（少见）：每隔一个字节是 0
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return "utf-16-le", "utf-16-le"
    try:
        data.decode("utf-8")
        return "utf-8", "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("gb18030")
        return "gb18030", "gb18030"
    except UnicodeDecodeError:
        return "latin-1", "latin-1"  # 实在认不出：能读出数字列就行


def _detect_delimiter(text):
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if not lines:
        return ","
    sample = "\n".join(lines)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        counts = {d: lines[0].count(d) for d in ",\t;|"}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


# ---------------- 单元格解析 ----------------
def _norm(s):
    return re.sub(r"\s+", "", str(s)).replace("﻿", "").lower()


def _field_of(cell):
    c = _norm(cell)
    if not c:
        return None
    for f, names in ALIASES.items():
        if c in (_norm(n) for n in names):
            return f
    for f, names in ALIASES.items():  # 宽松一点：包含关系（如「营业额(元)」）
        if any(_norm(n) in c for n in names if len(_norm(n)) >= 2):
            return f
    return None


def parse_date(s):
    """各种写法的日期 → datetime.date；认不出返回 None"""
    s = str(s).strip()
    if not s:
        return None
    try:
        m = re.match(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", s)
        if m:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"(\d{1,2})\s*([/.\-])\s*(\d{1,2})\s*[/.\-]\s*(\d{4})", s)
        if m:  # 年份在最后：斜杠按美式 月/日，点和横线按 日.月
            a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
            mon, day = (a, b) if m.group(2) == "/" else (b, a)
            if mon > 12:
                mon, day = day, mon
            return _dt.date(y, mon, day)
        m = re.match(r"(\d{1,2})\s*月\s*(\d{1,2})", s)
        if m:
            return _dt.date(_dt.date.today().year, int(m.group(1)), int(m.group(2)))
        v = float(s)
        if 30000 < v < 80000:  # Excel 日期序号
            return _dt.date(1899, 12, 30) + _dt.timedelta(days=int(v))
    except (ValueError, OverflowError):
        pass
    return None


def parse_time(s):
    """各种写法的时间 → (时, 分)；认不出返回 None"""
    s = str(s).strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        low = s.lower()
        if ("pm" in low or "下午" in s or "晚上" in s) and h < 12:
            h += 12
        elif ("am" in low or "上午" in s or "凌晨" in s) and h == 12:
            h = 0
        if h < 24 and mi < 60:
            return h, mi
        return None
    try:
        v = float(s)
        if 0 <= v < 1:  # Excel 把时间存成一天的几分之几
            mins = int(round(v * 24 * 60)) % (24 * 60)
            return mins // 60, mins % 60
    except ValueError:
        pass
    return None


def parse_int(s):
    """5671 / 5,671 / 5671.0 / ' 5671 ' → 5671；不是数字（如「中断」）返回 None"""
    t = re.sub(r"[\s,，]", "", str(s))
    try:
        return int(round(float(t)))
    except ValueError:
        return None


# ---------------- 读 ----------------
class RecordFile:
    def __init__(self, path):
        self.path = path
        self.exists = False
        self.read_enc, self.write_enc = "utf-8-sig", "utf-8"
        self.delim = ","
        self.columns = list(FIELDS)  # 每一列对应哪个字段（None = 不认识的列，追加时留空）
        self.has_header = False
        self.slash_date = False      # 文件里的日期是不是 2026/11/10 这种写法
        self.ends_newline = True
        self.rows = []               # dict 列表，按文件里的顺序

    def load(self):
        try:
            with open(self.path, "rb") as fh:
                data = fh.read()
        except OSError:
            return self
        if not data.strip(b"\x00\r\n\t ,;\xef\xbb\xbf\xff\xfe"):
            return self  # 空文件当没有
        self.exists = True
        self.read_enc, self.write_enc = _detect_encoding(data)
        text = data.decode(self.read_enc, errors="replace")
        self.ends_newline = text.endswith(("\n", "\r"))
        self.delim = _detect_delimiter(text)
        table = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=self.delim)
                 if any(c.strip() for c in r)]
        if not table:
            return self
        cols = [_field_of(c) for c in table[0]]
        if sum(c is not None for c in cols) >= 2:  # 第一行是表头
            self.has_header = True
            self.columns = cols
            table = table[1:]
        else:
            self.columns = list(FIELDS) + [None] * max(0, len(table[0]) - len(FIELDS))
        for r in table:
            rec = {}
            for i, f in enumerate(self.columns):
                if f and i < len(r) and f not in rec:
                    rec[f] = r[i].strip()
            if any(rec.values()):
                self.rows.append(rec)
        for rec in reversed(self.rows):
            if rec.get("date"):
                self.slash_date = bool(re.match(r"\s*\d{4}\s*/", rec["date"]))
                break
        return self

    def recent(self, n=200):
        """最近 n 局，按时间从旧到新（用户在 Excel 里重新排过序也没关系）；每局整理成界面要显示的样子"""
        out = []
        for i, rec in enumerate(self.rows):
            d, t = parse_date(rec.get("date", "")), parse_time(rec.get("time", ""))
            inc, tb = parse_int(rec.get("income", "")), parse_int(rec.get("tables", ""))
            out.append({
                "key": (d or _dt.date.min, t or (0, 0), i) if d else (_dt.date.min, (0, 0), i),
                "dated": d is not None,
                "date": d.strftime("%m-%d") if d else rec.get("date", ""),
                "time": f"{t[0]:02d}:{t[1]:02d}" if t else rec.get("time", ""),
                "mode": rec.get("mode", ""),
                "income": inc,
                "income_text": str(inc) if inc is not None else rec.get("income", ""),
                "tables": str(tb) if tb is not None else rec.get("tables", ""),
                "fw": rec.get("fw", ""),
            })
        if out and all(r["dated"] for r in out):
            out.sort(key=lambda r: r["key"])
        return out[-n:]

    # ---------------- 写 ----------------
    def append(self, date, time_, mode, income, tables, fw):
        """追加一局；文件被占用（Excel 开着）时抛 OSError，由调用方攒着下次再写"""
        vals = {"date": date, "time": time_, "mode": mode, "income": income, "tables": tables, "fw": fw}
        if not self.exists:
            line = _csv_line(HEAD, ",") + _csv_line([vals[f] for f in FIELDS], ",")
            with open(self.path, "ab") as fh:
                fh.write(codecs.BOM_UTF8 + line.encode("utf-8"))
            self.exists, self.read_enc, self.write_enc, self.delim = True, "utf-8-sig", "utf-8", ","
            self.columns, self.has_header, self.ends_newline = list(FIELDS), True, True
            return
        if self.slash_date and isinstance(date, _dt.date):
            vals["date"] = f"{date.year}/{date.month}/{date.day}"
        elif isinstance(date, _dt.date):
            vals["date"] = date.strftime("%Y-%m-%d")
        line = ("" if self.ends_newline else "\r\n") + _csv_line(
            [vals.get(f, "") if f else "" for f in self.columns], self.delim)
        with open(self.path, "ab") as fh:
            fh.write(line.encode(self.write_enc, errors="replace"))
        self.ends_newline = True


def _csv_line(values, delim):
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="\r\n").writerow(
        [v.strftime("%Y-%m-%d") if isinstance(v, _dt.date) else v for v in values])
    return buf.getvalue()
