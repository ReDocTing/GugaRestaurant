"""咕嘎餐馆 V2.1 —— 缘缘庄「食有老饕」全自动经营（发布版界面）
核心逻辑在 laotao_bot.py（识别、走位、接待、上菜、做菜、满堂彩、结算），这里只是给使用者的界面。"""
import os
import csv
import datetime
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk

import laotao_bot as core
import record_file
from laotao_bot import (BASE_DIR, PROMPTS, Game, Navigator, RoundLost, Stopped, load_config,
                        pydirectinput, save_config)

VERSION = "V2.1"

# ---------- 配色 ----------
C_BG = "#13151a"
C_CARD = "#1c1f26"
C_CARD2 = "#232731"
C_LINE = "#2c313c"
C_TEXT = "#eceef2"
C_MUTED = "#8a90a0"
C_ACCENT = "#ff9f43"
C_ACCENT2 = "#ffb86b"
C_GREEN = "#36d399"
C_RED = "#ff6b6b"
C_BLUE = "#5aa9ff"
C_GOLD = "#f6c453"
FONT = "Microsoft YaHei UI"

TABLE_STYLE = {  # 桌态 → (显示文字, 底色, 字色)
    "other": ("空闲", "#262a33", C_MUTED),
    "laotao": ("老饕等接待", "#4a3216", C_ACCENT2),
    "order": ("待上菜", "#16304d", C_BLUE),
    "pay": ("结账中", "#163d30", C_GREEN),
}


def f(size, bold=False):
    return (FONT, size, "bold" if bold else "normal")


RECORD_PATH = os.path.join(BASE_DIR, "对局记录.csv")  # 只记界面上「最近对局」那几列，Excel 直接打开
RECORD_HEAD = ("日期", "结束时间", "模式", "营业额", "送齐桌数", "满堂彩")
HINT = "紧急停止：键盘 ~ 键（Tab 上面）"


def stop_reason(s):
    """停下来时给使用者看的一句话（不写任何日志文件）"""
    s = re.sub(r"（截图在[^）]*）", "", s.strip())
    if "客源不足" in s:
        return "客源不足，开不了正式经营（每局约 60 客源，恢复后再开，或改用试营业）"
    if s.startswith("出错"):
        return "出错停下了：" + s[3:].strip()[:40]
    if s.startswith("已停止"):
        return "已手动停止"
    return None


class Card(tk.Frame):
    """带标题的深色卡片"""

    def __init__(self, master, title=None, **kw):
        super().__init__(master, bg=C_CARD, highlightthickness=1, highlightbackground=C_LINE, **kw)
        if title:
            head = tk.Frame(self, bg=C_CARD)
            head.pack(fill="x", padx=14, pady=(10, 4))
            tk.Frame(head, bg=C_ACCENT, width=3, height=14).pack(side="left", padx=(0, 8))
            tk.Label(head, text=title, bg=C_CARD, fg=C_TEXT, font=f(11, True)).pack(side="left")
            self.head = head
        self.body = tk.Frame(self, bg=C_CARD)
        self.body.pack(fill="both", expand=True, padx=14, pady=(2, 12))


class FlatButton(tk.Label):
    """扁平按钮（鼠标悬停变色）"""

    def __init__(self, master, text, command, bg, fg="#ffffff", hover=None, size=12, bold=True, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, font=f(size, bold), cursor="hand2", **kw)
        self._bg, self._hover, self._cmd = bg, hover or bg, command
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<Button-1>", lambda e: self._cmd())

    def set_colors(self, bg, hover, fg=None):
        self._bg, self._hover = bg, hover
        self.configure(bg=bg, **({"fg": fg} if fg else {}))


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"咕嘎餐馆 {VERSION}")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)
        self._set_icon()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.cfg = load_config()
        self.results = []  # 本次运行每局：dict(time, income, tables, fw, lost)
        self.round_no = 0
        self.round_total = 0
        self._pending_rows = []  # 记录文件被 Excel 开着写不进去时先攒着，下局再写
        self._build()
        self._load_history()
        self.root.after(100, self.poll)
        self.root.after(800, self.watch_game)

    # ================= 界面 =================
    def _build(self):
        r = self.root
        # ---- 顶栏 ----
        top = tk.Frame(r, bg=C_BG)
        top.pack(fill="x", padx=20, pady=(16, 8))
        self.avatar = self._load_avatar(56)
        if self.avatar:
            tk.Label(top, image=self.avatar, bg=C_BG).pack(side="left", padx=(0, 12))
        tbox = tk.Frame(top, bg=C_BG)
        tbox.pack(side="left")
        line = tk.Frame(tbox, bg=C_BG)
        line.pack(anchor="w")
        tk.Label(line, text="咕嘎餐馆", bg=C_BG, fg=C_TEXT, font=f(22, True)).pack(side="left")
        tk.Label(line, text=f" {VERSION} ", bg=C_ACCENT, fg="#1a1205", font=f(10, True)).pack(side="left", padx=10, pady=(8, 0))
        tk.Label(tbox, text="缘缘庄 · 食有老饕 全自动经营  —  接待 / 上菜 / 做菜 / 满堂彩 / 结算 一条龙",
                 bg=C_BG, fg=C_MUTED, font=f(10)).pack(anchor="w")
        right = tk.Frame(top, bg=C_BG)
        right.pack(side="right")
        self.pill = tk.Canvas(right, width=220, height=34, bg=C_BG, highlightthickness=0)
        self.pill.pack(anchor="e")
        self.status_lbl = tk.Label(right, text=HINT, bg=C_BG, fg=C_MUTED, font=f(9))
        self.status_lbl.pack(anchor="e", pady=(4, 0))
        self._set_pill("待机", C_MUTED)

        body = tk.Frame(r, bg=C_BG)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 6))
        left = tk.Frame(body, bg=C_BG, width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        rightc = tk.Frame(body, bg=C_BG)
        rightc.pack(side="left", fill="both", expand=True, padx=(14, 0))

        # ---- 左：开始前 ----
        chk = Card(left, "开始前准备")
        chk.pack(fill="x")
        for i, t in enumerate(("游戏窗口化 1920×1080，不要被别的窗口挡住",
                               "人物站在吧台「开始经营」提示旁",
                               "视角转到面朝吧台，开始后别动鼠标"), 1):
            row = tk.Frame(chk.body, bg=C_CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=str(i), bg=C_CARD2, fg=C_ACCENT, font=f(9, True), width=2).pack(side="left")
            tk.Label(row, text=t, bg=C_CARD, fg=C_TEXT, font=f(9), anchor="w", wraplength=260, justify="left").pack(side="left", padx=8)
        det = tk.Frame(chk.body, bg=C_CARD2)
        det.pack(fill="x", pady=(8, 0))
        self.game_lbl = tk.Label(det, text="● 正在找游戏窗口…", bg=C_CARD2, fg=C_MUTED, font=f(9, True), anchor="w")
        self.game_lbl.pack(fill="x", padx=10, pady=(6, 0))
        self.prompt_lbl = tk.Label(det, text="当前提示：—", bg=C_CARD2, fg=C_MUTED, font=f(9), anchor="w")
        self.prompt_lbl.pack(fill="x", padx=10, pady=(0, 6))

        # ---- 左：设置 ----
        st = Card(left, "运行设置")
        st.pack(fill="x", pady=(12, 0))
        rr = tk.Frame(st.body, bg=C_CARD)
        rr.pack(fill="x", pady=(2, 6))
        tk.Label(rr, text="局数", bg=C_CARD, fg=C_TEXT, font=f(10)).pack(side="left")
        self.rounds = tk.IntVar(value=0)
        FlatButton(rr, "－", lambda: self._bump(-1), C_CARD2, C_TEXT, C_LINE, 11, width=3).pack(side="left", padx=(12, 0))
        self.rounds_lbl = tk.Label(rr, text="∞", bg=C_BG, fg=C_ACCENT, font=f(12, True), width=5)
        self.rounds_lbl.pack(side="left", padx=2, ipady=2)
        FlatButton(rr, "＋", lambda: self._bump(1), C_CARD2, C_TEXT, C_LINE, 11, width=3).pack(side="left")
        chips = tk.Frame(st.body, bg=C_CARD)
        chips.pack(fill="x", pady=(0, 8))
        for v, t in ((1, "1 局"), (3, "3 局"), (5, "5 局"), (10, "10 局"), (0, "一直跑")):
            FlatButton(chips, t, lambda v=v: self._set_rounds(v), C_CARD2, C_MUTED, C_LINE, 9, False,
                       padx=7, pady=3).pack(side="left", padx=(0, 5))
        mr = tk.Frame(st.body, bg=C_CARD)
        mr.pack(fill="x")
        tk.Label(mr, text="模式", bg=C_CARD, fg=C_TEXT, font=f(10)).pack(side="left")
        self.mode = tk.StringVar(value=self.cfg.get("business_mode", "试营业"))
        self.mode_btns = {}
        seg = tk.Frame(mr, bg=C_LINE)
        seg.pack(side="left", padx=12)
        for m in ("试营业", "正式经营"):
            b = FlatButton(seg, m, lambda m=m: self._set_mode(m), C_CARD2, C_MUTED, C_LINE, 10, True, padx=14, pady=4)
            b.pack(side="left", padx=1, pady=1)
            self.mode_btns[m] = b
        self._set_mode(self.mode.get())

        # ---- 左：小贴士 ----
        tip = Card(left, "小贴士")
        tip.pack(fill="x", pady=(12, 0))
        for t in ("开始后程序窗口会自动退到游戏后面", "每局结算后自动走回吧台开下一局"):
            tk.Label(tip.body, text="·  " + t, bg=C_CARD, fg=C_MUTED, font=f(9), anchor="w").pack(fill="x", pady=1)

        # ---- 左：按钮 ----
        self.stop_btn = FlatButton(left, "■   停止", self.stop, C_BG, C_RED, "#2a1717", 12, pady=8,
                                   highlightthickness=1, highlightbackground=C_RED)
        self.stop_btn.pack(side="bottom", fill="x")
        self.start_btn = FlatButton(left, "▶   开始挂机", self.start, C_ACCENT, "#1a1205", C_ACCENT2, 14, pady=12)
        self.start_btn.pack(side="bottom", fill="x", pady=(14, 8))

        # ---- 右：数据 ----
        tiles = tk.Frame(rightc, bg=C_BG)
        tiles.pack(fill="x")
        self.tile_vals = {}
        for i, (key, title, unit, color) in enumerate((("done", "已完成", "局", C_TEXT), ("avg", "平均营业额", "", C_ACCENT),
                                                      ("best", "最高营业额", "", C_GOLD), ("tables", "平均送齐", "桌", C_GREEN))):
            c = tk.Frame(tiles, bg=C_CARD, highlightthickness=1, highlightbackground=C_LINE)
            c.pack(side="left", fill="both", expand=True, padx=(0 if i == 0 else 10, 0))
            tk.Label(c, text=title, bg=C_CARD, fg=C_MUTED, font=f(9)).pack(anchor="w", padx=14, pady=(10, 0))
            vl = tk.Frame(c, bg=C_CARD)
            vl.pack(anchor="w", padx=14, pady=(0, 10))
            v = tk.Label(vl, text="—", bg=C_CARD, fg=color, font=f(20, True))
            v.pack(side="left")
            if unit:
                tk.Label(vl, text=unit, bg=C_CARD, fg=C_MUTED, font=f(9)).pack(side="left", padx=(4, 0), pady=(10, 0))
            self.tile_vals[key] = v

        # ---- 右：餐桌（按店里的摆法：两列桌子，中间是过道） ----
        tb = Card(rightc, "餐桌实时状态")
        tb.pack(fill="x", pady=(12, 0))
        legend = tk.Frame(tb.head, bg=C_CARD)
        legend.pack(side="right")
        for k in ("laotao", "order", "pay", "other"):
            text, bg_, fg_ = TABLE_STYLE[k]
            tk.Label(legend, text="■", bg=C_CARD, fg=fg_, font=f(9)).pack(side="left", padx=(10, 0))
            tk.Label(legend, text=text, bg=C_CARD, fg=C_MUTED, font=f(8)).pack(side="left", padx=(2, 0))
        bar = tk.Label(tb.body, text="吧  台   ·   饮品台在最左边", bg="#3a2b1c", fg=C_ACCENT2, font=f(8, True))
        bar.pack(fill="x", pady=(2, 6), ipady=3)
        grid = tk.Frame(tb.body, bg=C_CARD)
        grid.pack(fill="x")
        self.table_tiles = {}
        for row, (a, b) in enumerate(((1, 2), (3, 4), (5, 6))):
            for col, n in enumerate((a, b)):
                t = tk.Frame(grid, bg=TABLE_STYLE["other"][1], height=50)
                t.grid(row=row, column=col * 2, pady=4, sticky="nsew")
                t.pack_propagate(False)
                l1 = tk.Label(t, text=f"{n} 号桌", bg=t["bg"], fg=C_TEXT, font=f(10, True))
                l1.pack(side="left", padx=(14, 0))
                l2 = tk.Label(t, text="空闲", bg=t["bg"], fg=C_MUTED, font=f(10))
                l2.pack(side="right", padx=(0, 14))
                self.table_tiles[n] = (t, l1, l2)
        tk.Label(grid, text="过\n\n道", bg=C_CARD, fg="#4a505d", font=f(8)).grid(row=0, column=1, rowspan=3)
        grid.columnconfigure(0, weight=1, uniform="t")
        grid.columnconfigure(1, minsize=44)
        grid.columnconfigure(2, weight=1, uniform="t")

        # ---- 右：最近对局 ----
        hist = Card(rightc, "最近对局")
        hist.pack(fill="both", expand=True, pady=(12, 0))
        FlatButton(hist.head, "打开记录表", self.open_record, C_CARD2, C_MUTED, C_LINE, 9, False,
                   padx=10, pady=2).pack(side="right")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("G.Treeview", background=C_CARD, fieldbackground=C_CARD, foreground=C_TEXT,
                        rowheight=26, borderwidth=0, font=f(10))
        style.configure("G.Treeview.Heading", background=C_CARD2, foreground=C_MUTED, borderwidth=0, font=f(9, True))
        style.map("G.Treeview", background=[("selected", C_CARD2)])
        style.layout("G.Treeview", [("G.Treeview.treearea", {"sticky": "nswe"})])
        self.tree = ttk.Treeview(hist.body, columns=("d", "t", "m", "inc", "tb", "fw"), show="headings", style="G.Treeview", height=6)
        for col, txt, w in (("d", "日期", 80), ("t", "结束时间", 90), ("m", "模式", 90), ("inc", "营业额", 100),
                            ("tb", "送齐", 70), ("fw", "满堂彩", 90)):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor="center")
        self.tree.tag_configure("good", foreground=C_GOLD)
        self.tree.tag_configure("bad", foreground=C_RED)
        self.tree.pack(fill="both", expand=True)

        foot = tk.Frame(r, bg=C_BG)
        foot.pack(fill="x", padx=20, pady=(4, 10))
        tk.Label(foot, text=f"咕嘎餐馆 {VERSION}  ·  仅供学习交流，使用风险自负  ·  不含任何第三方付费/AGPL 组件",
                 bg=C_BG, fg="#555b68", font=f(8)).pack(side="left")
        tk.Label(foot, text="每局结果自动记进「对局记录.csv」", bg=C_BG, fg="#555b68", font=f(8)).pack(side="right")

    def _set_icon(self):
        """标题栏和任务栏图标换成咕嘎头像（默认是 Tk 的羽毛）"""
        try:
            ico = os.path.join(BASE_DIR, "picture_laotao", "guga.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(default=ico)
        except Exception:
            pass
        try:
            from PIL import Image, ImageTk
            im = Image.open(os.path.join(BASE_DIR, "picture_laotao", "app_avatar.png")).convert("RGBA")
            self._icons = [ImageTk.PhotoImage(im.resize((n, n), Image.LANCZOS)) for n in (256, 64, 32, 16)]
            self.root.iconphoto(True, *self._icons)
        except Exception:
            pass

    def _load_avatar(self, size):
        try:
            from PIL import Image, ImageDraw, ImageTk
            p = os.path.join(BASE_DIR, "picture_laotao", "app_avatar.png")
            im = Image.open(p).convert("RGBA").resize((size * 2, size * 2), Image.LANCZOS)
            mask = Image.new("L", im.size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, im.size[0] - 1, im.size[1] - 1), fill=255)
            out = Image.new("RGBA", im.size, (0, 0, 0, 0))
            out.paste(im, (0, 0), mask)
            ring = ImageDraw.Draw(out)
            ring.ellipse((2, 2, im.size[0] - 3, im.size[1] - 3), outline=(255, 159, 67, 255), width=5)
            return ImageTk.PhotoImage(out.resize((size, size), Image.LANCZOS))
        except Exception:
            return None

    def _set_pill(self, text, color):
        c = self.pill
        c.delete("all")
        w, h = 220, 34
        r = h // 2
        fill = "#1c1f26"
        c.create_oval(0, 0, h, h, fill=fill, outline=fill)
        c.create_oval(w - h, 0, w, h, fill=fill, outline=fill)
        c.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        c.create_oval(16, r - 5, 26, r + 5, fill=color, outline=color)
        c.create_text(36, r, text=text, fill=C_TEXT, anchor="w", font=f(10, True))

    def _bump(self, d):
        self._set_rounds(max(0, self.rounds.get() + d))

    def _set_rounds(self, v):
        self.rounds.set(v)
        self.rounds_lbl.configure(text="∞" if v == 0 else str(v))

    def _set_mode(self, m):
        self.mode.set(m)
        for k, b in self.mode_btns.items():
            if k == m:
                b.set_colors(C_ACCENT, C_ACCENT2, "#1a1205")
            else:
                b.set_colors(C_CARD2, C_LINE, C_MUTED)

    # ================= 运行 =================
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.cfg["business_mode"] = self.mode.get()
        save_config(self.cfg)
        rounds = self.rounds.get()
        self.stop_event.clear()
        self.round_no = 0
        self.round_total = rounds
        for n in self.table_tiles:
            self._set_table(n, "other")
        self.status_lbl.configure(text=HINT, fg=C_MUTED)
        self._why_shown = False
        self._refresh_stats()
        self.root.lower()  # 程序窗口别挡住游戏（识别是按屏幕画面来的）
        self.worker = threading.Thread(target=self.work, args=(rounds,), daemon=True)
        self.worker.start()
        self._set_pill("准备开局", C_GREEN)

    def stop(self):
        self.stop_event.set()
        core.release_inputs()  # 点停止就先放开按键和鼠标，不等这一步做完
        self._set_pill("已停止", C_RED)

    def work(self, rounds):
        g = None
        try:
            g = Game(self.cfg, self.log, self.stop_event)
            g.find_window()
            nav = Navigator(g, self.log, lambda *a: self.q.put(a))
            i = 0
            while rounds <= 0 or i < rounds:
                i += 1
                self.q.put(("round", i, rounds))
                self.log(f"===== 第 {i} 局开始 =====")
                if i > 1 and not nav.back_to_start():
                    nav.teleport_home()
                nav.reset_round()
                nav.start_business()
                try:
                    nav.auto(100000)
                except RoundLost:
                    nav.log("走丢了：结束这一局，开下一局")
                    nav.abort_round()
                nav.settle()
                self.log(f"===== 第 {i} 局完成 =====")
            self.q.put(("pill", "已完成", C_BLUE))
        except Stopped as e:
            self.log(f"已停止：{e}" if str(e) else "已停止")
            self.q.put(("pill", "已停止", C_RED))
        except Exception as e:
            self.log(f"出错：{e}")
            self.q.put(("pill", "出错停止", C_RED))
        finally:
            core.release_inputs()  # 键盘和鼠标键全放开（不只 WASD），用户接手时不会乱
            if g is not None and getattr(g, "slow", False):
                g.set_slow(False)

    def log(self, s):
        self.q.put(("log", s))

    # ================= 刷新 =================
    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._on_log(item[1])
                elif kind == "states":
                    self._on_states(item[1])
                elif kind == "round":
                    self.round_no = item[1]
                    total = "" if item[2] <= 0 else f" / {item[2]}"
                    if not self.stop_event.is_set():
                        self._set_pill(f"运行中 · 第 {item[1]}{total} 局", C_GREEN)
                elif kind == "probe":
                    if not (self.worker and self.worker.is_alive()):
                        self.game_lbl.configure(text="● 已连接游戏窗口" if item[1] else "● 没找到游戏窗口（Endfield）",
                                                fg=C_GREEN if item[1] else C_RED)
                        self.prompt_lbl.configure(text=item[2], fg=item[3])
                elif kind == "pill":
                    self._set_pill(item[1], item[2])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def _on_log(self, s):
        """核心的过程日志不写文件、不显示；只从里面取界面要用的：每局结果、停下的原因"""
        if "【本局总分】" in s:
            self._on_score(s)
            return
        why = stop_reason(s)
        if why and not (why == "已手动停止" and getattr(self, "_why_shown", False)):
            # 已经显示了具体原因（如客源不足）时，后面跟着的「已停止」不再盖掉它
            self.status_lbl.configure(text=why, fg=C_RED)
            self._why_shown = why != "已手动停止"

    def _on_score(self, s):
        inc = re.search(r"营业额 (\d+)", s)
        tb = re.search(r"送齐 (\d+) 桌", s)
        fw = re.search(r"开启时还剩 (\d+) 秒", s)
        lost = "走丢" in s
        rec = {"t": time.strftime("%H:%M"), "income": int(inc.group(1)) if inc else 0,
               "tables": int(tb.group(1)) if tb else 0, "fw": "已开启" if fw else "未开启", "lost": lost}
        self.results.append(rec)
        shown_inc = "中断" if lost and not inc else rec["income"]
        mode = self.cfg.get("business_mode", "试营业")
        self._add_row(time.strftime("%m-%d"), rec["t"], mode, shown_inc, rec["tables"], rec["fw"])
        self._refresh_stats()
        self._write_record((datetime.date.today(), rec["t"], mode, shown_inc, rec["tables"], rec["fw"]))

    def _add_row(self, date, t, mode, inc, tb, fw):
        """最近对局表最上面加一行"""
        v = record_file.parse_int(inc)
        tag = "bad" if v is None or v < 4500 else ("good" if v >= 5250 else "")
        self.tree.insert("", 0, values=(date, t, mode, inc, tb, fw), tags=(tag,) if tag else ())

    def _load_history(self, n=200):
        """打开程序时把记录表里最近的 n 局放进最近对局（最新的在最上面）。
        记录表被 Excel/WPS 等改过存过、格式变了也能读（见 record_file.py）"""
        try:
            rows = record_file.RecordFile(RECORD_PATH).load().recent(10 ** 9)
        except Exception:
            return
        for r in rows[-n:]:
            self._add_row(r["date"], r["time"], r["mode"], r["income_text"], r["tables"], r["fw"])
        # 上面四个统计数字也算上记录表里所有的局（之后每跑完一局接着往上加）
        for r in rows:
            self.results.append({"income": r["income"] or 0, "tables": record_file.parse_int(r["tables"]) or 0})
        self._refresh_stats()

    def _write_record(self, row):
        """追加一局到 对局记录.csv，沿用文件现在的编码、分隔符、列顺序；文件被 Excel 开着就先攒着下局再写"""
        self._pending_rows.append(row)
        try:
            rf = record_file.RecordFile(RECORD_PATH).load()
            while self._pending_rows:
                rf.append(*self._pending_rows[0])
                self._pending_rows.pop(0)
        except OSError:
            pass
        except Exception:
            self._pending_rows = []  # 别的意外：不让它每局都重试同一行

    def open_record(self):
        if os.path.exists(RECORD_PATH):
            try:
                os.startfile(RECORD_PATH)
            except OSError:
                pass
        else:
            self.status_lbl.configure(text="还没有记录：跑完一局后会自动生成「对局记录.csv」", fg=C_GOLD)

    def _refresh_stats(self):
        ok = [r for r in self.results if r["income"]]
        self.tile_vals["done"].configure(text=str(len(self.results)) if self.results else "0")
        if ok:
            self.tile_vals["avg"].configure(text=str(round(sum(r["income"] for r in ok) / len(ok))))
            self.tile_vals["best"].configure(text=str(max(r["income"] for r in ok)))
            self.tile_vals["tables"].configure(text=f"{sum(r['tables'] for r in ok) / len(ok):.1f}")
        else:
            for k in ("avg", "best", "tables"):
                self.tile_vals[k].configure(text="—")

    def _set_table(self, n, s, items=None):
        text, bg, fg = TABLE_STYLE.get(s, TABLE_STYLE["other"])
        if s == "order" and items:
            text = f"待上菜 · 缺 {len(items)} 样"
        t, l1, l2 = self.table_tiles[n]
        t.configure(bg=bg)
        l1.configure(bg=bg)
        l2.configure(bg=bg, fg=fg, text=text)

    def _on_states(self, st):
        for n, (s, items) in st.items():
            if n in self.table_tiles:
                self._set_table(n, s, items)

    def watch_game(self):
        """没在运行时，每 1.5 秒看一次游戏窗口和 F 提示"""
        if not (self.worker and self.worker.is_alive()):
            def job():
                g = getattr(self, "_wg", None)
                try:
                    if g is None:
                        g = self._wg = Game(self.cfg, lambda s: None, threading.Event())
                    g.find_window()
                    if g.find_ui("ui_prompt_open", 0.75):
                        txt, col = "✓ 已站在「开始经营」旁，可以开始", C_GREEN
                    else:
                        p = g.prompt()
                        txt, col = (f"当前提示：{PROMPTS[p]}", C_MUTED) if p in PROMPTS else ("还没看到「开始经营」提示（走到吧台旁）", C_GOLD)
                    self.q.put(("probe", True, txt, col))
                except Exception:
                    self._wg = None
                    self.q.put(("probe", False, "当前提示：—", C_MUTED))
                finally:
                    # 每次都是新线程：线程里建的截图对象要关掉，不然每次漏 GDI 句柄，挂久了程序会崩
                    try:
                        sct = getattr(g._local, "sct", None) if g is not None else None
                        if sct is not None:
                            sct.close()
                            del g._local.sct
                    except Exception:
                        pass
            if not getattr(self, "_probing", False):  # 上一次还没看完就不再开新线程
                self._probing = True

                def run():
                    try:
                        job()
                    finally:
                        self._probing = False
                threading.Thread(target=run, daemon=True).start()
        self.root.after(1500, self.watch_game)

    def mainloop(self):
        self.root.mainloop()


if __name__ == "__main__":
    import sys
    core.avoid_bad_cpus()
    core.full_speed()  # 不让 Windows 把程序放进效率模式（窗口在游戏后面时会被降速，识别慢好几倍）
    app = App()
    # 命令行：--pos X,Y 指定窗口位置；--autostart N 打开 4 秒后自动开始 N 局（录视频/计划任务用）
    if "--pos" in sys.argv:
        try:
            x, y = sys.argv[sys.argv.index("--pos") + 1].split(",")
            app.root.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass
    if "--autostart" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--autostart") + 1])
        except Exception:
            n = 1
        app._set_rounds(n)
        app.root.after(4000, app.start)
    app.mainloop()
