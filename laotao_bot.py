# -*- coding: utf-8 -*-
"""咕嘎餐馆 —— 缘缘庄「食有老饕」活动自动挂机

固定视角下：W=往里走  S=往吧台走  A=左  D=右（全程不动鼠标，避免转动视角）
定位方法：
  1. 按住 S 贴住吧台            -> 校正前后位置
  2. 按住 S+D 沿吧台往右滑，出现「饮品台」提示就停 -> 校正左右位置（锚点）
  3. 从锚点按 A 一段时间到走道，再按 W 一段时间到第几排桌子
走道：右走道 = 1/3/5 号桌左侧，左走道 = 2/4/6 号桌右侧
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

import cv2
import mss
import numpy as np
try:
    # v99：截屏不带 CAPTUREBLT（mss 文档里的已知问题：带着它截屏会让鼠标指针闪、画面发顿）。
    # 实测截出来的图一样、速度一样（约 17 毫秒一张）；只是不再截分层窗口（悬浮提示、叠加层），游戏画面不受影响
    import mss.windows.gdi as _mss_gdi
    _mss_gdi.CAPTUREBLT = 0
except Exception:
    pass
import pydirectinput

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

pydirectinput.PAUSE = 0.0
pydirectinput.FAILSAFE = False

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.IsIconic.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
PIC_DIR = os.path.join(BASE_DIR, "picture_laotao")
MENU_DIR = os.path.join(BASE_DIR, "menu")  # 老饕点单需求表（laotao_rules.json）

# 饮品台里卖的四种饮品（同香型里最贵的那种），按香型区分；物品栏图标模板 picture_laotao/hb_*.png
DRINK_TAGS = ("果香", "茶香", "酒香", "奶香")
DRINK_AVAIL = ("果香", "茶香")  # 饮品台现在实际有的香型（11-07 看面板：霞莓饮 果香、潮前龙井 茶香，没有酒香/奶香）
DRINK_NAMES = {"果香": "瓶装霞莓饮", "茶香": "潮前龙井", "酒香": "陈酿葡萄露", "奶香": "瓶装优质兽奶"}
HOTBAR_TPL = {"dish": "hb_dish", "果香": "hb_fruit", "茶香": "hb_tea", "酒香": "hb_wine", "奶香": "hb_milk"}


DISH_CATS = ("主食", "菜肴", "甜品", "汤羹")


def dish_cond_ok(cond, dish):
    """一道菜（menu/dishes.json 里的一项）满不满足一句点菜要求（如 "主食 非[辣]"、"<鳞肉>"）。
    空格分开的全部要满足；A/B 满足其一；前面带「非」取反"""
    import re

    def atom(tok):
        if tok in DISH_CATS:
            return dish["cat"] == tok
        m = re.fullmatch(r"\[(.+)\]", tok)
        if m:
            return m.group(1) in dish["tags"]
        m = re.fullmatch(r"<(\(优质\))?(优质)?(.+)>", tok)
        if m:
            name = m.group(3)
            if m.group(2):
                return ("优质" + name) in dish["ing"]
            return name in dish["ing"] or ("优质" + name) in dish["ing"]
        return False
    for tok in cond.split():
        neg = tok.startswith("非")
        hit = any(atom(a) for a in (tok[1:] if neg else tok).split("/"))
        if hit == neg:
            return False
    return True


_OUR_DISH = None


def soup_cond(cond):
    """这句点菜要求是不是点名要汤羹（「汤羹」或「汤羹/…」，不算「非汤羹」）"""
    return any("汤羹" in tok.split("/") for tok in cond.split() if not tok.startswith("非"))


def our_dish(name):
    """我们做的那道菜在 menu/dishes.json 里的数据（类别、标签、配料），找不到返回 None"""
    global _OUR_DISH
    if _OUR_DISH is None:
        try:
            with open(os.path.join(MENU_DIR, "dishes.json"), encoding="utf-8") as f:
                _OUR_DISH = {d["name"]: d for d in json.load(f)["dishes"]}
        except (OSError, ValueError, KeyError):
            _OUR_DISH = {}
    return _OUR_DISH.get(name)


def drink_cond_ok(cond, tag):
    """饮品要求（如 "[果香]"、"非[茶香]"、"[果香]/[奶香]"）对这种香型满不满足"""
    for tok in cond.split():
        neg = tok.startswith("非")
        alts = [a.strip("[]") for a in (tok[1:] if neg else tok).split("/")]
        if (tag in alts) == neg:
            return False
    return True


_OCR = None


def get_ocr():
    """文字识别（RapidOCR），第一次用时才加载，读谜语和看打烊共用一个"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


class RiddleReader:
    """读客人头顶气泡里的点单要求：OCR 认出文字，再对到需求表里的原句"""

    def __init__(self):
        import re
        self._re = re
        with open(os.path.join(MENU_DIR, "laotao_rules.json"), encoding="utf-8") as f:
            rules = json.load(f)["laotao"]
        self.sents = [(lt["no"], ln["text"], self._clean(ln["text"]), ln.get("kind", "dish"), ln["cond"])
                      for lt in rules for ln in lt["lines"]]
        # 每位老饕的饮品句（饮品那行字常常是淡的、OCR 读不到，就按老饕是谁来猜）
        self.drink_lines = {lt["no"]: [ln["cond"] for ln in lt["lines"] if ln.get("kind") == "drink"] for lt in rules}
        self.ocr = None

    def guess_drink(self, no):
        """只知道是几号老饕、没读到饮品句：选能满足他最多饮品句的香型，返回条件（如 "[茶香]"、"[果香]/[奶香]"）"""
        lines = self.drink_lines.get(no) or []
        if not lines:
            return None
        # v78：只在饮品台真有的香型里挑（11-09 用户实测：9 号老饕猜成 [酒香]，饮品台没有，2 号桌三分多钟一直没送饮品）
        cnt = {t: sum(drink_cond_ok(c, t) for c in lines) for t in DRINK_AVAIL}
        best = max(cnt.values())
        return "/".join(f"[{t}]" for t in DRINK_AVAIL if cnt[t] == best) if best else None

    def _clean(self, t):
        return self._re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]", "", t)

    def read(self, img):
        """img：整张游戏画面（1920x1080）。返回 {"dish": (分数, 老饕号, 原句, 条件), "drink": 同上或 None}"""
        from difflib import SequenceMatcher
        res, _ = get_ocr()(img[140:900])  # 气泡在画面中部；不看顶部桌位栏和底部物品栏
        res = res or []
        # 画面里常有好几个客人的气泡：把文字框按位置连成一个个气泡，只用离人物（画面中间偏上）最近的那个
        boxes = []
        for box, t, _ in res:
            xs = [q[0] for q in box]
            ys = [q[1] + 140 for q in box]
            boxes.append((min(xs), min(ys), max(xs), max(ys), t))
        groups = []
        for b in sorted(boxes, key=lambda b: b[1]):
            for gr in groups:
                g0 = gr[-1]
                if abs(b[0] - g0[0]) < 260 and 0 <= b[1] - g0[3] < 70:
                    gr.append(b)
                    break
            else:
                groups.append([b])
        def dist(gr):
            cx = (min(b[0] for b in gr) + max(b[2] for b in gr)) / 2
            cy = (min(b[1] for b in gr) + max(b[3] for b in gr)) / 2
            return (cx - 960) ** 2 + (cy - 420) ** 2
        groups.sort(key=dist)
        near = self._clean("".join(b[4] for b in groups[0])) if groups else ""
        text = self._clean("".join(r[1] for r in res))
        def rank(text):
            out = []
            for no, raw, c, kind, cond in self.sents:
                n = len(c)
                best = 0.0
                for i in range(0, max(1, len(text) - n + 1)):
                    best = max(best, SequenceMatcher(None, c, text[i:i + n]).ratio())
                out.append((round(best, 2), no, raw, cond, kind))
            return out
        ranked = rank(near)
        if max((r[0] for r in ranked), default=0) < 0.6:
            ranked = rank(text)  # 最近的那个气泡认不出来（被挡、太远），退回用整个画面
        ranked.sort(reverse=True)
        dish = next((r for r in ranked if r[4] == "dish"), None)
        drinks = [r for r in ranked if r[4] == "drink"]
        # 画面里可能同时有别桌的气泡：同一位老饕的饮品句分数够就优先用它
        same = [r for r in drinks if dish and r[1] == dish[1]]
        if dish and dish[0] >= 0.6:
            # 认出了是几号老饕：只认他自己的饮品句。画面里常有别桌（或弹出的大气泡）的饮品句，拿来用就会送错
            drink = same[0] if same and same[0][0] >= 0.6 else None
            if drink is None and not self.drink_lines.get(dish[1]) and drinks and drinks[0][0] >= 0.6:
                # 这位老饕在需求表里没有饮品句（1~4、13、14 号缺）：他说的饮品句会被记在别人名下，
                # 只认他自己的就永远读不到（实测 14 号说「饮料都挺不错，上果香奶香的吧」被丢掉）
                drink = drinks[0]
        else:
            drink = drinks[0] if drinks else None
        return {"dish": dish[:4] if dish else None, "drink": drink[:4] if drink else None, "text": text}

CONFIG_PATH = os.path.join(BASE_DIR, "laotao_config.json")

GAME_TITLE = "Endfield"

# 这台电脑的 CPU 有一个核心反复报硬件错误（系统日志 WHEA：APIC ID 40 = 第 10、11 号逻辑处理器，第 6 个性能核），
# 蓝屏重启都和它有关。不改 BIOS / 系统设置，只让游戏和本程序（及它启动的 MaaEnd 辅助程序，子进程会继承）不在这个核心上跑。
# 只对这几个进程生效，进程关掉就没了。配置 avoid_cpus 改成 [] 可以关掉
AVOID_CPUS_DEFAULT = []  # 发布版：不限制 CPU 核心


def avoid_bad_cpus(pid=None, cpus=None):
    """把进程（默认本进程）的 CPU 亲和性设成「除了坏核心以外的全部」。失败（没权限等）就算了，不影响运行"""
    try:
        if cpus is None:
            try:
                cpus = json.load(open(os.path.join(BASE_DIR, "laotao_config.json"), encoding="utf-8")).get("avoid_cpus", AVOID_CPUS_DEFAULT)
            except Exception:
                cpus = AVOID_CPUS_DEFAULT
        n = os.cpu_count() or 1
        mask = ((1 << n) - 1) & ~sum(1 << c for c in cpus if c < n)
        if not cpus or not mask:
            return False
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        if pid is None:
            return bool(k32.SetProcessAffinityMask(k32.GetCurrentProcess(), mask))
        h = k32.OpenProcess(0x0200 | 0x0400, False, pid)  # PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION
        if not h:
            return False
        ok = bool(k32.SetProcessAffinityMask(h, mask))
        pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
        k32.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        k32.GetProcessAffinityMask(ctypes.c_void_p(h), ctypes.byref(pm), ctypes.byref(sm))
        k32.CloseHandle(ctypes.c_void_p(h))
        try:  # 留个记录，方便确认生效了
            with open(os.path.join(BASE_DIR, "test_channel", "affinity.txt"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} 进程 {pid}：{'成功' if ok else '失败'}，现在的掩码 {pm.value:#x}\n")
        except Exception:
            pass
        return ok
    except Exception:
        return False
VK_STOP = 0xC0  # 紧急停止键：Tab 上面的 ~ 键（反引号键）

DEFAULT_CONFIG = {
    "facing_counter": True,       # 视角转 180° 面朝吧台（方向键自动对调）；False = 原来面朝餐厅的视角
    "pin_time": 1.5,              # 按住 S 贴吧台的时长
    "slide_timeout": 3.5,         # 沿吧台往右找饮品台的最长时间（太长会滑过头）
    "lane_center": 1.45,          # 从饮品台锚点按 S+A 到中间过道的时长（实测右走道 1.0、左走道约 2.0）
    "rows": [1.2, 1.9, 2.6],      # 从吧台按 W 到第 1/2/3 排的时长（实测）
    "below1": 0.75,               # 从饮品台锚点按 S+A 到 1 号桌正下方的时长
    "below2": 2.25,               # 从饮品台锚点按 S+A 到 2 号桌正下方的时长
    "below_max": 1.5,             # 从吧台往上走找 1/2 号桌提示的最长时间
    "sign_ref": {"6": [389, 268]},  # 各桌标准站位：到位时号码牌在 960x540 画面里的坐标（用「记录站位」录入）
    "servo_tol": 18,              # 站位允许偏差（像素）
    "servo_k": 0.0022,            # 每差 1 像素按多少秒方向键
    "slow_approach": False,       # 横着靠桌时也用慢走（关掉更快；慢走只用在桌边找提示）
    "use_slow": True,             # 靠桌、退回过道、原地找提示时用缓慢行走（Ctrl 切换），不容易走过头
    "slow_factor": 0.4,           # 缓慢行走的速度大约是正常的几倍（估计值，只影响时长上限）
    "side_min": 0.3,              # 横向靠桌时先固定走多久再开始看提示
    "side_max": 0.55,             # 在过道里横向靠近桌子的最长时间（出现提示就停；只到桌边，不能穿过桌子之间的空隙）
    "dish_timeout": 4.0,          # 从锚点 S+A 找取菜提示的最长时间
    "search_step": 0.12,          # 靠到桌边没提示时，前后微调每步时长
    "search_steps": 2,            # 前后各微调最多几步（范围要小，免得找到隔壁排的桌子）
    "top_view": False,            # 把视角压成俯视再按桌子位置对齐（餐厅里只能压到约45°，实测不可靠，默认关）
    "char_y": 260,                # 俯视画面（960x540）里角色所在的高度，桌子中线对到这里
    "align_tol": 35,              # 对齐允许的误差（像素）
    "align_k": 0.0025,            # 每差 1 像素按多少秒 W/S（实测约 350 像素/秒）
    "laotao_threshold": 0.58,     # 桌位栏“老饕”招手图标匹配下限（招手有动画，实测 0.63~0.96；其他状态不超过 0.49）
    "text_threshold": 0.75,       # 桌位格子状态文字（老饕/用餐中/点餐中/待结账）匹配下限（实测是 0.85~0.97，别的 ≤0.6）
    "icon_threshold": 0.6,        # 桌位栏点单图标（菜/饮品）匹配下限
    "busy_threshold": 0.45,       # 桌位栏像「用餐中」「点餐中」到这个分数，就不算等上菜
    "sign_threshold": 0.85,       # 桌号牌匹配分数下限
    "sign_margin": 0.05,          # 第一名要比第二名高出多少才算认出来
    "sign_min_scale": 0.75,       # 桌号牌最小尺寸（太小的是远处桌子的牌子）
    "verify_retry": 1,            # 桌号不对时重新走几次
    "near_scale": 0.95,           # 号码牌尺寸达到多少算“就在旁边”（到位时自己的牌子 ≥1.0，前方几排的牌子 ≤0.9）
    "dish_card": [1080, 372],     # 料理界面里找不到菜卡图片时，退而点这个位置
    "card_threshold": 0.85,       # 料理界面里菜卡图片的匹配下限（不是这道菜时最高 0.66）
    "sign_ref_fc": {"1": [395, 275], "2": [566, 272], "3": [400, 276], "4": [570, 278], "5": [400, 257], "6": [565, 258]},
                                  # 1、2 排站位往吧台靠一点（牌子 y 255→275）：实测站在 1~4 号按 F 常点到身后那排的桌（上了 0 样）
                                  # 面朝吧台时各桌的标准站位：桌号牌在 960x540 画面里的位置（用户 11-03 手动演示：站在桌子和长凳交界处）
    "servo_fc_tol": 25,           # 站位允许偏差（像素）
    "servo_fc_k": 0.0022,         # 每差 1 像素按多少秒方向键
    "counter_bias_deg": 10,       # 沿吧台横走时视角往吧台那边偏几度（往饮品台走往右偏、往外走往左偏），走完转回来
    "yaw_by_ref": True,           # 用参考图（test_channel/survey_wang）的画面平移量算视角歪了几度
    "ref_min_inliers": 60,        # 和参考图至少对上这么多点才信
    "first_dish_x_at_drink": 703, # 站在饮品台前时，第一格（凉糕）在画面里的 x（吧台采样实测）
    "wang_nav": True,             # 用新寻路（王字节点，用户 11-03 演示）；False = 旧走法
    "anchor_dish": True,          # 开局先做一份糯米凉糕摆在第一格当坐标原点，整局不拿（取菜认图标排除它）
    "anchor_dish_card": "card_liangao_top.png",
    "drink_card": [858, 335],     # 饮品台面板里固定饮品（左上角第一种）的位置
    "drink_cards": {"果香": [858, 335], "茶香": [1319, 335], "酒香": [858, 474], "奶香": [1319, 613]},
    "drink_by_request": True,     # 读客人气泡，按点的香型送饮品（False = 和以前一样只拿果香）
    "default_drinks": 0,          # 手上常备几瓶果香（0 = 不囤，按等着的桌点的香型去拿）
    "stock_drinks": ["茶香", "果香"],  # 手上常备的饮品（各一瓶），接待时当场就能送齐
    "ocr_min": 0.6,
    "fail_cooldown": 15,
    "repin_between_rows": False,
    "cook_fill_board": True,      # 有客人时按看板空位把三个厨房排满（不按估算的在途数量限制）
    "reserve_for_partial": False,
    "pin_sign_check": False,      # 贴吧台后还看得到桌号牌就挪开再贴（会来回挪，关掉） # 手上的东西给「送了一半」的桌预留（False = 谁要给谁，不锁在手里）   # 换排时先退回吧台贴住再走（多花一两秒，前后位置不会越积越偏）
    "lost_after": 2,              # 连续几次找不到饮品台就算走丢：提前结束这一局，结算后传送回餐馆          # 去某桌上菜送不出去后，多少秒内先不去这桌               # 气泡文字和需求表原句的相似度下限
    "cook_clicks": 3,             # 每个厨房点几次（队列最多 3 份）
    "cook_interval": 30,          # 每隔多少秒补一次菜（约一份菜的烹饪时间，保证厨房不空着）
    "dish_tries": 8,              # 取菜最多按几次 F
    "quit_cancel": [770, 696],    # 「提前结束经营」确认框里「取消」按钮的位置
    "trace": True,                # 行程记录：关键节点截图存到 test_channel/trace/
    "rare_gold": 250,             # 菜品图标外圈金色像素超过这个就是「珍馐」，优先拿
    "fireworks_score": 0.75,      # 「开启满堂彩」横幅匹配分数超过这个就按 R
    "business_mode": "试营业",    # 开局选「试营业」还是「正式经营」
    "start_timeout": 60,          # 开局流程最多用多少秒
    "start_settle": 1.5,          # 进入营业画面后等几秒再操作（开场动画期间按键无效）
    "yaw_fix": True,              # 离开吧台几步后，按画面横线角度校正左右朝向
    "yaw_walk": 0.6,              # 从过道口出发，先走多久再停下校正视角
    "yaw_walk_front": 0.5,        # 去 1、2 号桌时，先走多久再停下校正视角
    "yaw_tol": 2.0,               # 允许的倾斜角（度）
    "yaw_max": 12.0,
    "yaw_max_hall": 45.0,
    "yaw_after_f": False,         # 接待/上菜后放宽角度校正视角（实测读数会错、反而转歪，关掉）         # 在大厅里刚按过 F（接待/上菜）后，允许转回的最大角度              # 读数超过这个就当作读错，不转（实测漂移最多 6° 左右）
    "yaw_px_per_deg": 22.0,       # 吧台边每歪 1° 要转多少鼠标像素（实测往右转 80 → 歪 -3°，约 27，取小一点多次逼近）
    "yaw_sign": -1,               # 转的方向（实测：往右转吧台边角度变负，所以按角度同号转回）
    "recover_seq": [["w", 1.6], ["a", 2.3], ["w", 0.5], ["a", 1.3]],  # 从吧台右下角走回来的按键（照用户演示）
    "recover_side": 1.5,          # 面朝吧台时迷路：往过道方向挪多久再贴回吧台
    "leave_drink": 0.6,           # 离开饮品台时先往过道方向横挪多久（离开墙角）
    "boost_speed": 1.5,           # 满堂彩加速时的速度倍数（估计值，实测后调）
    "retrace_max": 20,            # 原路返回最多走多少秒
    "board_y": 1.0,               # 看板点：从吧台沿中间过道往里走多久（第一排桌子之间），在这里读厨房看板
    "slot_threshold": 0.70,       # 看板空位圆圈的匹配下限（参考截图里空位 0.79~1.0）
    "cook_ahead": 99,             # 在途的菜（正在做 + 吧台上）最多几份。原来 12：「做的−送的−手上的」记账会越记越多（凉糕、没下进去的单也算），
                                  # 实测「在途 13」时吧台一份菜都没有，厨房被卡着不做、整局缺菜 → 改成不设上限，每次都把厨房空位补满
    "debug_moves": True,          # 走路提前停下时记日志（查走不到位的原因）
    "slow_detect_ratio": 99,      # 滑回饮品台的实际用时超过按坐标估的这么多倍，算一次「偏慢」
    "log_combo": False,           # 每次送出东西、每 10 秒记一次满堂彩进度和连续上餐数（查什么会让连击清零）
    "orb_anchor": True,           # 回饮品台前先用大厅定位量横向距离，按距离滑（不再整段盲滑）
    "hall_lat_speed": 5.0,        # 沿吧台横移每秒走多少大厅坐标（估计）
    "read_boards": False,         # 经过看板点时读厨房看板并存图（做菜已改成在料理界面看空位，默认不读）
    "save_requests": False,       # 客人点单时存全分辨率截图（收集数据用，默认不存）
    "skip_laotao": [1, 3, 4],     # 这几号老饕点两样菜、界龙肉卷又满足不了这句时压着不送（1 号要咸/鲜/米饭/不花哨的主食、3 号两份甜品、4 号两份汤）
    "save_places": True,          # 位置确定时存画面（饮品台、到桌看到桌号牌），攒数据自建营业状态下的定位
    "blind_sweep": 2.4,           # 定位认不出、也不知道坐标时，沿吧台往饮品台最多滑几秒
    "face_check": True,           # 在大厅里出发前用营业样本检查镜头是不是转反了
    "face_min": 40,               # 和面朝吧台的干净样本对上的特征点少于这个就算转歪/转反（实测正常 51~189，转反 26 以下）
    "face_refs": 80,              # 朝向检查最多用几张样本（每张约 3 毫秒）
    "no_cook_left": 40,           # 营业倒计时少于这么多秒就不再做菜
    "max_held": 6,                # 满堂彩开启前，会打断连击的桌（3 号老饕）全部压着不送；开启后全部照常送
    "drink_yaw_check": True,      # 到饮品台时用营业样本检查镜头是不是被带歪了几十度，歪了就转正
    "drink_face_bad": 25,         # 饮品台前低于这个分数才算镜头真歪了、转一圈找（实测歪的 11~28，正对的 35~127）
    "drink_face_good": 45,        # 饮品台前和干净样本对上的点数达到这个就算正对（实测正对 51~127）
    "look_px_per_deg": 11.1,      # 镜头实际转 1° 的鼠标像素（实测 2000≈180°）；yaw_px_per_deg 是「横线倾斜 1°」对应的像素，不是一回事
    "pin_face_check": False,      # 每次退回吧台前查朝向（样本还不够，会误判乱转，先关掉；只在找不到饮品台时才查）
    "our_dish": "界龙肉卷",       # 我们做的那道菜（用来判断合不合客人的点菜要求）
    "rare_hb": 30,                # 物品栏格子左上角黄色角标像素数达到这个算珍馐（全分辨率，待实测校准）
    "log_rare": False,            # 送菜时记下各格的角标像素数（校准用）
    "rare_badge": 40,             # 盘子下方黄色角标像素数达到这个算珍馐
    "final_fill_window": 20,      # 停止做菜前这么多秒内做最后一次菜：把厨房空位全部填满
    "end_recv_guard": 25,         # 营业最后几秒：手上凑不齐一菜一饮就不再接新客人（接了送不上要扣分）
    "recv_cooldown": 8,
    "urgent_after": 60,
    "recv_urgent_after": 25,      # 客人等接待超过这么多秒，优先去接待           # 接待后超过这么多秒还没送齐的桌，优先去送           # 到桌边没看到接待提示：这桌几秒内先不去
    "calib_boost": False,         # 开局和满堂彩开启后各在饮品台量一次走路速度，算出加速倍数
    "cook_every": 65,            # 隔几秒开一次料理界面：一道菜 30~35 秒、每个厨房 3 个位置，65 秒后每个厨房约做完 2 道，开一次把空位全部补满
    "fw_min_left": 60,            # 满堂彩要在营业还剩这么多秒以上时开启（整整 1 分钟加速），做不到扣分
    "fw_miss_penalty": 300,       # 本局没开出 1 分钟以上满堂彩的扣分
    "dish_wait_max": 6,          # 手上没菜时回吧台最多等几秒出菜
    "use_coords": True,           # 面朝吧台时按虚拟坐标走（原点=饮品台）
    "coord_tol": 0.08,            # 坐标差小于这个就不再挪
    "table_side_x": 0.35,         # 靠桌后离中间过道大约多远（只用于估路程排顺序）
    "lateral_factor": 0.71,       # 面朝吧台沿吧台纯横移的时长 = 原来斜着滑的时长 × 这个系数
    "counter_sweep": 12.0,        # 面朝吧台时，贴着吧台往饮品台方向最多滑多久（够从吧台另一头滑到饮品台）
    "anchor_press_w": False,      # 找饮品台时边往右滑边往吧台压（防止视角歪了斜着滑出吧台）
    "counter_back": 1.5,          # 滑到头没找到饮品台时，往反方向退多久再找一次
    "drink_guard": True,          # 「饮品台」提示出现后，不再往饮品台那一侧走（不越过饮品台）
    "max_drinks": 5,              # 饮品最多同时拿几瓶（按等着的桌要几瓶拿几瓶，不多拿；受物品栏 5 格限制）
    "slide_more": 1.5,            # 第一次没滑到饮品台、但还看得到桌号牌时，再往右滑多久
    "keep_drinks": 2,             # 手上尽量保持几瓶饮品
    "keep_dishes": 3,             # 手上尽量保持几份菜
    "restock_below": 3,           # 手上少于几样（即 ≤2）就先回吧台补货，优先级最高
    "order_wait": 4.0,            # 接待后最多等几秒让客人点完单，再当场上菜
    "first_dishes": 0,            # 开局先等厨房出几道菜再出发（0 = 不等，下完单直接去接待）
    "first_wait_max": 60,         # 开局最多等多少秒
    "dish_retry": 8,              # 出餐台没菜时，隔多少秒再去看
    "dish_px_per_sec": 300,       # 贴着吧台横着走，菜品图标在画面上每秒移动多少像素（估计值，走完会再看图标位置微调）
    "dish_center_tol": 40,        # 菜品图标离画面正中多少像素以内就算对准了，可以按 F 取
    "turn_step": 300,             # 迷路找桌子时每次往右转多少（鼠标像素）
    "turn_steps": 12,             # 最多转几次
    "recover_walk": 1.5,          # 找到桌子后朝那边走多久
    "stuck_interval": 0.4,        # 每隔多久检查一次有没有卡住
    "stuck_diff": 2.5,            # 画面变化小于这个值视为卡住
    "jump_key": "space",          # 卡住时按的跳跃键
    "jump_back": 0.5,             # 卡住时往反方向退多久（拿着托盘时跳不起来，主要靠反方向退）
    "max_jumps": 2,               # 每次走路最多跳几次，还卡着就停下
    "row_gap": 0.7,               # 相邻两排之间按 W 的秒数（实测 1.2→1.9→2.6）
    "prompt_threshold": 0.8,
    "press_f": False,             # 到达后是否按 F（测试阶段默认不按）
}

# 桌号 -> (走道, 第几排)
TABLES = {1: ("right", 0), 3: ("right", 1), 5: ("right", 2),
          2: ("left", 0), 4: ("left", 1), 6: ("left", 2)}
PROMPTS = {"p_jiedai": "接待", "p_shangcai": "上菜", "p_dish": "取菜", "p_drink": "饮品台",
           "p_take": "取菜", "p_putback": "放回出餐台", "p_kitchen1": "厨房一"}  # p_take/p_putback：面朝吧台时出餐台的两种提示
TABLE_PROMPTS = ("p_jiedai", "p_shangcai")

def row_last(n):
    """n 号桌在最后一排（离镜头最近、离吧台最远的那排）"""
    return TABLES[n][1] == 2

OPPOSITE = {"w": "s", "s": "w", "a": "d", "d": "a"}
PROMPT_REGION = (1180, 620, 1720, 820)  # 游戏画面内 F 提示出现的区域 (x1, y1, x2, y2)


class Stopped(Exception):
    pass


_STOP_EVENT = None  # v99：当前的停止信号（Game 建好时记下），给不在 Game 里的阻塞调用（MaaEnd 子进程）查


def release_inputs():
    """v99：停下时把可能还按着的键和鼠标键全放开（原来只放 WASD：按着 Shift/Ctrl/鼠标键停下，用户接手时会乱）"""
    for k in ("w", "a", "s", "d", "shift", "ctrl", "alt", "space", "f", "e", "r", "q", "tab"):
        try:
            pydirectinput.keyUp(k)
        except Exception:
            pass
    for btn in ("left", "right", "middle"):
        try:
            pydirectinput.mouseUp(button=btn)
        except Exception:
            pass


class BusinessOver(Exception):
    """营业时间到，进入了结算画面"""


class RoundLost(Exception):
    """走丢了（连续找不到饮品台），放弃这一局"""


class FireworksReady(Exception):
    """「开启满堂彩」可以放了：放下当前任务，回饮品台再按 R"""


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def imread_gray(path):
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(path)
    return img


class Game:
    """游戏窗口：找窗口、截图、识别 F 提示、按键移动"""

    def __init__(self, cfg, log, stop_event):
        self.cfg = cfg
        self.log = log
        self.stop_event = stop_event
        global _STOP_EVENT
        _STOP_EVENT = stop_event
        self._local = threading.local()  # mss 对象不能跨线程用，每个线程各建一个
        self.ui_tpl = {}  # 开局流程用的界面按钮模板（用到时才读）
        self.passing = None  # 自动服务时：走路路过桌边出现「接待/上菜」提示就调用它
        self.abort_walk = False  # 顺手处理的正好是这趟要去的桌：停止后面的走路
        self.on_move = None  # 每次走完调用 on_move(实际按键, 走了几秒, 是否慢走)，用来推算坐标
        self.settle = float(cfg.get("settle", 0.10))  # 每次走完等角色停稳的秒数（到桌微调时缩短；v09 从 0.15 降到 0.10 提速）
        self.cur_mult = 1.0  # 这一段走路的速度倍数（满堂彩加速时大于 1）
        self.watch_over, self._over_t = False, 0.0  # 自动服务时随时检查营业是否结束
        self.watch_fw, self._fw_t = False, 0.0  # 自动服务走路时检查满堂彩是否可以放了
        self._boost, self._boost_t = False, 0.0
        self.pass_done = None
        self.last_moved = 0.0  # 上一次 hold 真正在走的秒数（不含路上办事、卡住的时间）
        self.slow = False  # 当前是不是缓慢行走（开局默认正常速度）
        self.tpl = {k: imread_gray(os.path.join(PIC_DIR, f"{k}.png")) for k in PROMPTS}
        # 顶部桌位栏：状态文字和点单图标模板（按 1920x1080 原尺寸裁的）
        self.st = {n: imread_gray(os.path.join(PIC_DIR, f"st_{n}.png")) for n in ("laotao", "diancan", "yongcan")}
        self.icons = {n: imread_gray(os.path.join(PIC_DIR, f"ic_{n}.png")) for n in ("dish", "drink")}
        # 桌位格子下面的状态文字：老饕 / 用餐中 / 点餐中 / 待结账（文字不动，比招手图标好认得多）
        self.st_text = {n: imread_gray(os.path.join(PIC_DIR, f"tx_{n}.png"))
                        for n in ("laotao", "yongcan", "diancan", "jiezhang")}
        self.ui_drink = imread_gray(os.path.join(PIC_DIR, "ui_drink.png"))  # 饮品台面板标题
        self.kitchen_tabs = {k: cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, f"kitchen{k}.png"), dtype=np.uint8),
                                             cv2.IMREAD_COLOR) for k in (1, 2, 3)}
        # 桌号牌模板（按 960x540 画面裁的），统一缩成 24x30 再做多尺度匹配。
        # 每个号可以有多张：sign2.png 是面朝餐厅时截的，sign2b.png 是面朝吧台时截的
        self.signs = {}
        for d in range(1, 7):
            self.signs[d] = [cv2.resize(imread_gray(os.path.join(PIC_DIR, f"sign{d}{x}.png")), (24, 30),
                                        interpolation=cv2.INTER_AREA)
                             for x in ("", "b") if os.path.exists(os.path.join(PIC_DIR, f"sign{d}{x}.png"))]
        self.hwnd = None

    # ---------- 窗口 ----------
    def find_window(self):
        self.hwnd = user32.FindWindowW(None, GAME_TITLE)
        if not self.hwnd:
            raise RuntimeError(f"没找到游戏窗口「{GAME_TITLE}」")
        if self.hwnd != getattr(self, "_avoided_hwnd", None):
            self._avoided_hwnd = self.hwnd
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
            avoid_bad_cpus(pid.value)
        return self.hwnd

    def client_origin(self):
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def focus(self):
        if not self.hwnd:
            self.find_window()
        if user32.GetForegroundWindow() == self.hwnd:
            return
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
        for _ in range(2):
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.4)
            if user32.GetForegroundWindow() == self.hwnd:
                return
        # 切不到前台（被别的窗口挡住、用户在用别的程序）：停下，不按键——不然按键会打进别的程序里，
        # 截图也截到别的窗口（实测 11-04 练习局被聊天窗口挡住，之后几趟全瞎了）
        raise Stopped("游戏窗口不在最前面（被别的窗口挡住或切走了），先停下，免得按键打到别的程序里")

    # ---------- 识别 ----------
    def prompt_scores(self):
        """各提示模板在提示区域里的匹配分数"""
        if not self.hwnd:
            self.find_window()
        ox, oy = self.client_origin()
        x1, y1, x2, y2 = PROMPT_REGION
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        img = np.array(self._local.sct.grab({"left": ox + x1, "top": oy + y1, "width": x2 - x1, "height": y2 - y1}))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        return {k: cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))[1] for k, t in self.tpl.items()}

    def can_take(self):
        """出餐台前：现在的提示是「取菜」（菜名）而不是「放回出餐台」"""
        sc = self.prompt_scores()
        return sc["p_take"] >= self.cfg["prompt_threshold"] and sc["p_take"] > sc["p_putback"]

    def has_prompt(self, key):
        """某个提示有没有出现（可能同时有好几行提示，比如拿着饮品时「饮品台」下面还有「放回饮品台」）"""
        return self.prompt_scores()[key] >= self.cfg["prompt_threshold"]

    def prompt(self):
        """返回当前 F 提示的模板名（p_jiedai / p_shangcai / p_dish / p_drink）或 None"""
        if not self.hwnd:
            self.find_window()
        ox, oy = self.client_origin()
        x1, y1, x2, y2 = PROMPT_REGION
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        img = np.array(self._local.sct.grab({"left": ox + x1, "top": oy + y1, "width": x2 - x1, "height": y2 - y1}))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        best, best_s = None, self.cfg["prompt_threshold"]
        for k, t in self.tpl.items():
            s = cv2.minMaxLoc(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED))[1]
            if s >= best_s:
                best, best_s = k, s
        return best

    # ---------- 输入 ----------
    def check(self):
        if self.watch_over and time.time() - self._over_t > 1.0:
            # 自动服务时每秒看一次是不是已经进入结算画面，不管正在做什么都立刻停
            self._over_t = time.time()
            if self.business_over():
                raise BusinessOver()
        if self.stop_event.is_set() or user32.GetAsyncKeyState(VK_STOP) & 0x8000:
            self.stop_event.set()
            raise Stopped()
        if time.time() - getattr(self, "_flag_t", 0) > 1.0:
            # 测试通道的急停：test_channel/stop.flag 出现就停下当前这一局（改完代码不用等这局跑完就能重新加载）
            self._flag_t = time.time()
            flag = os.path.join(BASE_DIR, "test_channel", "stop.flag")
            if os.path.exists(flag):
                try:
                    os.remove(flag)
                except OSError:
                    pass
                self.flag_stop = True  # 是通道的急停（不是用户按的停止）：停下这一局，但通道保持开着
                self.stop_event.set()
                raise Stopped()

    def thumb(self):
        """整个游戏画面的小缩略图，用来判断角色有没有在动（镜头跟着角色，走动时画面会变）"""
        if not self.hwnd:
            self.find_window()
        ox, oy = self.client_origin()
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        img = np.array(self._local.sct.grab({"left": ox, "top": oy, "width": 1920, "height": 1080}))
        return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY), (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)

    def frame960(self):
        """整个游戏画面缩到 960x540（BGR）"""
        if not self.hwnd:
            self.find_window()
        ox, oy = self.client_origin()
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        img = np.array(self._local.sct.grab({"left": ox, "top": oy, "width": 1920, "height": 1080}))
        return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR), (960, 540), interpolation=cv2.INTER_AREA)

    def _sign_best(self, gray, d):
        """d 号牌在画面里最像的位置：返回 (分数, 尺寸, 中心x, 中心y)"""
        w = gray.shape[1]
        best = (0.0, 0.0, 0, 0)
        for t in self.signs[d]:
            for s in self.sign_scales():
                tt = cv2.resize(t, (int(24 * s), int(30 * s)))
                r = cv2.matchTemplate(gray, tt, cv2.TM_CCOEFF_NORMED)
                # 屏蔽贴着画面左右边缘的位置：那里的牌子多半只露出半块，容易认错
                half = tt.shape[1] // 2
                r[:, :max(0, 70 - half)] = -1
                r[:, max(0, w - 70 - half):] = -1
                _, score, _, loc = cv2.minMaxLoc(r)
                if score > best[0]:
                    best = (float(score), float(s), loc[0] + half, loc[1] + tt.shape[0] // 2)
        return best

    def visible_signs(self):
        """画面里所有认得出的号码牌：{桌号: (尺寸, 分数)}"""
        gray = cv2.cvtColor(self.frame960(), cv2.COLOR_BGR2GRAY)
        out = {}
        for d in self.signs:
            score, scale, _, _ = self._sign_best(gray, d)
            if score >= self.cfg["sign_threshold"]:
                out[d] = (round(scale, 1), round(score, 2))
        return out

    def which_table(self):
        """识别离角色最近的桌号牌。返回 (桌号或 None, 分数)
        画面里常同时有好几块牌子，远处的分数也可能很高，所以在分数达标的牌子里取画面上最大（最近）的那块"""
        vis = self.visible_signs()
        if not vis:
            return None, 0.0
        d, (scale, score) = max(vis.items(), key=lambda kv: kv[1])
        return d, score

    def counter_angle(self, top=False):
        """测画面里"横线"的倾斜角（度）：桌子/长凳的前后边、后墙墙脚、吧台边，视角摆正时都是水平的。
        在餐厅里离开吧台几步后测最准（贴着吧台时镜头会被挤歪）。测不到返回 None"""
        img = self.frame960()
        edges = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 50, 140)
        # 屏蔽界面：顶部桌位栏、底部物品栏、左上经营信息面板（界面的边都是水平线，会把结果拉成 0°）
        edges[:95] = 0
        edges[95:280, :300] = 0
        if top:
            # 只看上方吧台带（后厨看板、窗口、吧台的横边）：在空旷的大厅里正对吧台时一定水平，桌子边不混进来
            edges[240:] = 0
            lines = cv2.HoughLinesP(edges, 1, np.pi / 360, 60, minLineLength=120, maxLineGap=10)
        else:
            edges[455:] = 0
            # 只要长线（吧台边、桌子长边），短的多半是提示框、气泡
            lines = cv2.HoughLinesP(edges, 1, np.pi / 360, 80, minLineLength=220, maxLineGap=12)
        if lines is None:
            return None
        angs, wts = [], []
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if a > 90:
                a -= 180
            elif a < -90:
                a += 180
            if abs(a) <= 40:  # 只要接近水平的线（视角歪得多时吧台边也会斜到 30° 以上）
                angs.append(a)
                wts.append(np.hypot(x2 - x1, y2 - y1))
        if not angs:
            return None
        order = np.argsort(angs)
        cum = np.cumsum(np.array(wts)[order])
        return round(float(np.array(angs)[order][np.searchsorted(cum, cum[-1] / 2)]), 1)  # 按长度加权的中位数

    # ---------- 物品栏 / 面板 / 鼠标 ----------
    HOTBAR_X = (767, 862, 957, 1052, 1147)

    def hotbar(self, img=None):
        """物品栏 5 格是否有东西（格子里纹理起伏大 = 有物品；实测有物品 ≥29，空格 ≤12）"""
        img = self.frame_full() if img is None else img
        g = cv2.cvtColor(img[935:995], cv2.COLOR_BGR2GRAY)
        return [float(g[:, x - 28:x + 28].std()) >= 20 for x in self.HOTBAR_X]

    def hotbar_count(self):
        return sum(self.hotbar())

    def find_card(self, fname):
        """料理界面里按模板（picture_laotao/fname，卡片上的菜图）找一张菜卡片，返回点击坐标或 None"""
        cache = self.__dict__.setdefault("_cards", {})
        if fname not in cache:
            cache[fname] = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, fname), dtype=np.uint8), cv2.IMREAD_COLOR)
        tpl = cache[fname]
        img = self.frame_full()
        x0, y0 = 700, 260
        region = img[y0:900, x0:1730]
        best = (0.0, None)
        for s in np.arange(0.9, 1.15, 0.05):
            t = cv2.resize(tpl, (int(tpl.shape[1] * s), int(tpl.shape[0] * s)))
            _, sc, _, loc = cv2.minMaxLoc(cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED))
            if sc > best[0]:
                best = (sc, (x0 + loc[0] + t.shape[1] // 2, y0 + loc[1] + t.shape[0] // 2))
        return best[1] if best[0] >= self.cfg["card_threshold"] else None

    def anchor_dish_x(self):
        """吧台上那份当坐标原点的糯米凉糕（白色方块，和界龙肉卷长得完全不同）在画面里的 x（960 宽），没看到返回 None。
        实测：凉糕（珍馐/普通）匹配 0.96~1.0，只有界龙肉卷的画面最高 0.59"""
        if not self.cfg.get("anchor_dish"):
            return None
        if not hasattr(self, "_lg_icon"):
            t = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, "icon_liangao.png"), dtype=np.uint8), cv2.IMREAD_COLOR)
            self._lg_icon = cv2.resize(t, (40, 40))
        band = self.frame960()[150:330]
        res = cv2.matchTemplate(band, self._lg_icon, cv2.TM_CCOEFF_NORMED)
        _, sc, _, loc = cv2.minMaxLoc(res)
        if sc < 0.8:
            return None
        # v37：满堂彩后会多做凉糕，吧台上可能有好几份：当原点的是最靠饮品台那份（画面最左边）
        xs = np.where(res.max(axis=0) >= 0.8)[0]
        return int(xs.min()) + 20 if len(xs) else loc[0] + 20

    def takeable_icons(self):
        """吧台上能拿的菜（dish_icons_rare 去掉当原点的凉糕）"""
        icons = self.dish_icons_rare()
        lg = self.anchor_dish_x() if icons else None
        if lg is not None:
            icons = [(x, r) for x, r in icons if abs(x - lg) > 35]
        return icons

    def find_dish_card(self):
        """料理界面里找固定菜的卡片（模板 picture_laotao/card_dish_top.png：卡片上的菜图），返回点击坐标或 None。
        换菜时只要换这张图"""
        if not hasattr(self, "_card"):
            self._card = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, "card_dish_top.png"), dtype=np.uint8),
                                      cv2.IMREAD_COLOR)
        img = self.frame_full()
        x0, y0 = 700, 260
        region = img[y0:900, x0:1730]
        best = (0.0, None)
        for s in np.arange(0.8, 1.2, 0.05):
            t = cv2.resize(self._card, (int(self._card.shape[1] * s), int(self._card.shape[0] * s)))
            _, sc, _, loc = cv2.minMaxLoc(cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED))
            if sc > best[0]:
                best = (sc, (x0 + loc[0] + t.shape[1] // 2, y0 + loc[1] + t.shape[0] // 2))
        return best[1] if best[0] >= self.cfg["card_threshold"] else None

    def board_free(self):
        """吧台后面三块厨房看板：每块一排 3 个队列格，空的是灰色空圆圈（模板 slot_empty.png）。
        只在固定的看板点（中间过道、第一排桌子之间，视角校正过）读：三块看板都完整地在画面上方，
        1/2/3 号厨房的格子横向大约在 570~710 / 920~1020 / 1220~1380。返回 [1号, 2号, 3号厨房空位数]"""
        if not hasattr(self, "_slot"):
            base = imread_gray(os.path.join(PIC_DIR, "slot_empty.png"))
            self._slot = [base]
            dim = os.path.join(PIC_DIR, "slot_empty_dim.png")
            if os.path.exists(dim):
                self._slot.append(cv2.resize(imread_gray(dim), base.shape[::-1], interpolation=cv2.INTER_AREA))
        x0, y0 = 480, 170
        g = cv2.cvtColor(self.frame_full()[y0:400, x0:1550], cv2.COLOR_BGR2GRAY)
        best = None
        for sc, tpl in [(sc, tpl) for sc in np.arange(0.85, 1.65, 0.1) for tpl in self._slot]:
            t = cv2.resize(tpl, None, fx=sc, fy=sc)
            r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
            # 换成以中心为坐标（四周补 -1，免得边缘出假峰）
            h2, w2 = t.shape[0] // 2, t.shape[1] // 2
            r = cv2.copyMakeBorder(r, h2, g.shape[0] - r.shape[0] - h2, w2, g.shape[1] - r.shape[1] - w2,
                                   cv2.BORDER_CONSTANT, value=-1)
            best = r if best is None else np.maximum(best, r)
        free = [0, 0, 0]
        for _ in range(9):
            _, mx, _, (x, y) = cv2.minMaxLoc(best)
            if mx < self.cfg["slot_threshold"]:
                break
            cv2.circle(best, (x, y), 18, -1, -1)
            ax = x + x0
            k = 0 if ax < 800 else (1 if ax < 1150 else 2)
            free[k] = min(3, free[k] + 1)
        return free

    def kitchen_free(self):
        """料理界面开着时，当前厨房左下角 3 个烹饪队列格子里有几个是空的
        （有菜的格子彩色像素 1200 以上，空格子是灰色圆圈 0）"""
        hsv = cv2.cvtColor(self.frame_full(), cv2.COLOR_BGR2HSV)
        free = 0
        for x in (366, 482, 603):
            s = hsv[785:835, x - 35:x + 35]
            # 有菜的格子是彩色的菜图（不管哪道菜），空格子是灰色圆圈
            if ((s[..., 1] >= 90) & (s[..., 2] >= 80)).sum() < 300:
                free += 1
        return free

    def hotbar_detail(self, img=None):
        """物品栏 5 格各放的是什么："dish" / 饮品香型（"果香" "茶香" "酒香" "奶香"）/ None（空格）。
        有东西的格子拿图标模板比（选中的格子底色变亮，分数会低一些，但仍是最高的那种）"""
        img = self.frame_full() if img is None else img
        if not hasattr(self, "_hb_tpl"):
            self._hb_tpl = {}
            for k, name in HOTBAR_TPL.items():
                path = os.path.join(PIC_DIR, f"{name}.png")
                if os.path.exists(path):
                    self._hb_tpl[k] = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if len(self._hb_tpl) < len(HOTBAR_TPL):
            # 缺模板时退回旧办法：粉色 = 饮品（当作果香）
            return [("果香" if x == "drink" else x) for x in self.hotbar_items(img, legacy=True)]
        gray = cv2.cvtColor(img[930:1002], cv2.COLOR_BGR2GRAY)
        out = []
        for x in self.HOTBAR_X:
            if float(gray[:, x - 28:x + 28].std()) < 20:
                out.append(None)
                continue
            slot = img[930:1002, x - 34:x + 34]
            sc = {k: cv2.minMaxLoc(cv2.matchTemplate(slot, t, cv2.TM_CCOEFF_NORMED))[1] for k, t in self._hb_tpl.items()}
            best = max(sc, key=sc.get)
            if sc[best] < float(self.cfg.get("hb_unknown", 0.6)):
                # v37：没有模板的东西（凉糕等）当菜；存一张图方便以后做模板
                if self.cfg.get("debug_files") and not getattr(self, "_hb_unknown_saved", False):
                    self._hb_unknown_saved = True
                    try:
                        cv2.imencode(".png", slot)[1].tofile(os.path.join(BASE_DIR, "test_channel", f"hb_unknown_{time.strftime('%H%M%S')}.png"))
                        print(f"物品栏有认不出的东西：{ {k: round(v, 2) for k, v in sc.items()} }，当成菜", flush=True)
                    except Exception:
                        pass
                best = "dish"
            out.append(best)
        return out

    def hotbar_rare(self, img=None):
        """物品栏每格左上角的黄色小角标有多少黄色像素（珍馐菜有这个角标，普通菜没有），按格返回"""
        img = self.frame_full() if img is None else img
        hsv = cv2.cvtColor(img[916:956], cv2.COLOR_BGR2HSV)
        out = []
        for x in self.HOTBAR_X:
            r = hsv[:, x - 46:x - 6]
            out.append(int(((r[..., 0] >= 20) & (r[..., 0] <= 36) & (r[..., 1] >= 150) & (r[..., 2] >= 130)).sum()))
        return out

    def hotbar_items(self, img=None, legacy=False):
        """物品栏 5 格各放的是什么："drink"（饮品，不分种类）、"dish" 或 None（空格）"""
        if not legacy:
            return [("drink" if x in DRINK_TAGS else x) for x in self.hotbar_detail(img)]
        img = self.frame_full() if img is None else img
        hsv = cv2.cvtColor(img[935:995], cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(img[935:995], cv2.COLOR_BGR2GRAY)
        out = []
        for x in self.HOTBAR_X:
            s = hsv[:, x - 28:x + 28]
            pink = ((s[..., 0] >= 145) & (s[..., 0] <= 175) & (s[..., 1] >= 60) & (s[..., 2] >= 120)).sum()
            if pink >= 200:
                out.append("drink")
            elif float(gray[:, x - 28:x + 28].std()) >= 20:
                out.append("dish")  # 不是粉色饮品、格子里又有东西，就是菜（换了菜也通用）
            else:
                out.append(None)
        return out

    def dish_icons(self):
        """面朝吧台时，出餐台上做好的菜会在吧台上方浮出白边圆形图标。返回这些图标的画面 x 坐标（960 宽）"""
        hsv = cv2.cvtColor(self.frame960(), cv2.COLOR_BGR2HSV)
        white = ((hsv[..., 1] < 40) & (hsv[..., 2] > 225)).astype(np.uint8) * 255
        white[:150] = 0
        white[330:] = 0  # 只看吧台上方那一条
        xs = []
        for c in cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            x, y, w, h = cv2.boundingRect(c)
            if 44 <= w <= 95 and 44 <= h <= 95 and 0.8 <= w / h <= 1.25:  # 站得离吧台远一点时圆环只有 49 像素
                inner = white[y + h // 4:y + 3 * h // 4, x + w // 4:x + 3 * w // 4].mean() / 255
                if inner < 0.5:  # 是圆环（中间是菜的图案，不是一整块白）
                    xs.append(x + w // 2)
        return sorted(xs)

    @staticmethod
    def rare_badges(hsv):
        """吧台菜品下方的珍馐角标：黄色小圆牌（约 17x15），中间有深色小图标。返回角标中心 x 列表。
        实测：角标色相约 29、亮度约 233、牌内深色像素 56~72；被选中那盘下面的光柱色相约 20、亮度约 200、没有深色；
        满堂彩倒计时圆钮偏橙红。只认角标这种样子"""
        band = hsv[225:305]
        m = ((band[..., 0] >= 18) & (band[..., 0] <= 38) & (band[..., 1] >= 120) & (band[..., 2] >= 170)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(m)
        out = []
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if not (12 <= w <= 30 and 10 <= h <= 28 and a >= 50):
                continue
            px = band[lab == i]
            if px[:, 0].mean() < 24 or px[:, 2].mean() < 220:
                continue
            if (band[y:y + h, x:x + w, 2] < 90).sum() < 25:
                continue
            out.append(int(x + w // 2))
        return out

    def selected_dish_x(self):
        """吧台上当前被选中（按 F 会拿走）的那盘：盘子上方的黄色小三角（约 21x10，y≈186）。返回它的 x，没有返回 None"""
        hsv = cv2.cvtColor(self.frame960()[170:200], cv2.COLOR_BGR2HSV)
        m = ((hsv[..., 0] >= 18) & (hsv[..., 0] <= 38) & (hsv[..., 1] >= 150) & (hsv[..., 2] >= 200)).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(m)
        best = None
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if 15 <= w <= 28 and 6 <= h <= 14 and 80 <= a <= 200 and (best is None or a > best[1]):
                best = (int(x + w // 2), int(a))
        return best[0] if best else None

    def dish_icons_rare(self):
        """同 dish_icons，但同时告诉是不是「珍馐」：珍馐的图标外圈有一圈金色光环
        （实测金色像素 290~550，普通菜 0~20，被选中的普通菜也只有 70~140）。返回 [(x, 是否珍馐)]"""
        img = self.frame960()
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = ((hsv[..., 1] < 40) & (hsv[..., 2] > 225)).astype(np.uint8) * 255
        white[:150] = 0
        white[330:] = 0
        # 有的光照下图标的白圈断成几截，认不成一个圈：先把小缺口连起来
        white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        out = []
        for c in cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            x, y, w, h = cv2.boundingRect(c)
            if not (50 <= w <= 95 and 50 <= h <= 95 and 0.8 <= w / h <= 1.25):
                continue
            if white[y + h // 4:y + 3 * h // 4, x + w // 4:x + 3 * w // 4].mean() / 255 >= 0.5:
                continue
            # 珍馐：盘子正下方有个黄色小角标（实测 94~279 个黄色像素，普通菜 0~7）。
            # 不能看外圈：外圈发黄只是「当前对准的那一盘」（普通菜对准时外圈也有 300~400），以前会把它当成珍馐
            out.append((x + w // 2, False))
        # 有的盘子被菜盖住了一圈白边，上面认不成一个圈：再用圆检测补找（只认圆周大半是白色的），
        # 补出来的圆心常偏下一点，珍馐角标用更严的门槛
        blur = cv2.GaussianBlur(white, (5, 5), 0)
        cs = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=60, param1=80, param2=22, minRadius=24, maxRadius=48)
        if cs is not None:
            ang = np.linspace(0, 2 * np.pi, 48, endpoint=False)
            for cx, cy, r in np.round(cs[0]).astype(int):
                if any(abs(cx - ox) < 40 for ox, _ in out):
                    continue
                px = (cx + (r - 2) * np.cos(ang)).astype(int)
                py = (cy + (r - 2) * np.sin(ang)).astype(int)
                ok = (px >= 0) & (px < img.shape[1]) & (py >= 0) & (py < img.shape[0])
                hh, ss, vv = hsv[py[ok], px[ok], 0], hsv[py[ok], px[ok], 1], hsv[py[ok], px[ok], 2]
                # 盘子边：白色，或者珍馐那一圈黄绿色的弧
                rim = ((ss < 60) & (vv > 200)) | ((hh >= 25) & (hh <= 45) & (ss >= 100) & (vv >= 150))
                if not ok.any() or float(rim.mean()) < 0.6:
                    continue
                out.append((int(cx), False))
        # 珍馐按角标认：角标正上方的盘子就是珍馐；珍馐盘外圈是黄绿色，上面按白圈可能没找到这盘，就按角标位置补上
        for bx in self.rare_badges(hsv):
            near = [k for k, (ox, _) in enumerate(out) if abs(ox - bx) <= 30]
            if near:
                k = min(near, key=lambda k: abs(out[k][0] - bx))
                out[k] = (out[k][0], True)
            else:
                out.append((bx, True))
        return sorted(out)

    def business_over(self):
        """营业时间到了会进入红色的结算画面：大半个画面都是红色"""
        hsv = cv2.cvtColor(self.frame960(), cv2.COLOR_BGR2HSV)
        red = (((hsv[..., 0] <= 10) | (hsv[..., 0] >= 170)) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 80)).mean()
        return red >= 0.35

    def find_ui(self, name, thr=0.8, img=None):
        """在 960x540 画面里找界面按钮/文字（模板 picture_laotao/ui_*.png），返回中心坐标（1920 原尺寸）或 None。
        img 传入同一张截图时，几个按钮共用一次截图"""
        if name not in self.ui_tpl:
            self.ui_tpl[name] = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, f"{name}.png"), dtype=np.uint8),
                                             cv2.IMREAD_COLOR)
        t = self.ui_tpl[name]
        img = self.frame960() if img is None else img
        _, s, _, loc = cv2.minMaxLoc(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED))
        if s < thr:
            return None
        return (loc[0] + t.shape[1] // 2) * 2, (loc[1] + t.shape[0] // 2) * 2

    def ocr_find(self, want, region=None, exclude=()):
        """在画面上用文字识别找一段文字，返回它中心的坐标（1920 原尺寸）或 None。
        region=(x0, y0, x1, y1) 只在这块里找；exclude 里的文字不算（比如找「传送」时排除「协议传送点」）"""
        img = self.frame_full()
        x0, y0, x1, y1 = region or (0, 0, 1920, 1080)
        try:
            res, _ = get_ocr()(img[y0:y1, x0:x1])
        except Exception:
            return None
        for box, text, _ in (res or []):
            t = text.replace(" ", "")
            if want in t and not any(e in t for e in exclude):
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                return int(x0 + sum(xs) / 4), int(y0 + sum(ys) / 4)
        return None

    def time_left(self):
        """营业倒计时还剩几秒（左上角「结束经营」下面那个 mm:ss）。读不到返回 None"""
        import re
        try:
            res, _ = get_ocr()(self.frame_full()[95:165, 20:320])
        except Exception:
            return None
        for _, t, _ in (res or []):
            m = re.search(r"(\d{1,2})[:：](\d{2})", t)
            if m:
                v = int(m.group(1)) * 60 + int(m.group(2))
                lp = getattr(self, "_left_pos", None)
                if v <= 1 and lp is not None and time.time() - lp[1] < 400:
                    # v35：倒计时走完后还有约 25 秒「已打烊」，读数一直是 0：按最后一次正读数往下算成负数
                    v = lp[0] - (time.time() - lp[1])
                elif v > 1:
                    self._left_pos = (v, time.time())
                self._left_at = (v, time.time())
                return v
        return None

    def close_secs(self):
        """v95：打烊后顶部横幅「尽快上齐菜品饮品，可获得剩余收益 ⏱ N秒」里的 N（离结算还剩几秒）。读不到返回 None"""
        import re
        try:
            res, _ = get_ocr()(self.frame_full()[130:200, 600:1480])
        except Exception:
            return None
        txt = "".join(t for _, t, _ in (res or [])).replace(" ", "")
        m = re.search(r"(\d{1,2})秒", txt)
        return int(m.group(1)) if m else None

    def left_est(self):
        """营业还剩几秒的估计：最近一次读到的倒计时减去过去的时间（20 秒内没读过就重新读一次）"""
        la = getattr(self, "_left_at", None)
        if la is None or time.time() - la[1] > 20:
            v = self.time_left()
            lp = getattr(self, "_left_pos", None)
            if v is None and lp is not None and time.time() - lp[1] < 120:
                return lp[0] - (time.time() - lp[1])  # 读不到就按最后一次正读数往下算
            return v
        return la[0] - (time.time() - la[1])

    def prompt_text(self):
        """F 提示框里的文字（OCR）；读不出返回空串"""
        x1, y1, x2, y2 = PROMPT_REGION
        try:
            res, _ = get_ocr()(self.frame_full()[y1:y2, x1:x2])
        except Exception:
            return ""
        return "".join(t for _, t, _ in (res or []))

    def drink_prompt_ok(self):
        """「饮品台」提示是真的：文字里有「饮」，或者是饮品台旁边的取菜提示（两条提示叠在一起时只读得到取菜那条）。
        模板会误认（满堂彩烟花里、吧台另一头都出过），没文字或读到厨房就不算"""
        txt = self.prompt_text()
        return "饮" in txt or ("界龙" in txt and "厨房" not in txt)

    def kitchen_no(self):
        """F 提示是「厨房几」：认出来返回 1/2/3，认不出返回 None"""
        x1, y1, x2, y2 = PROMPT_REGION
        try:
            res, _ = get_ocr()(self.frame_full()[y1:y2, x1:x2])
        except Exception:
            return None
        for _, t, _ in (res or []):
            if "厨房" in t:
                for i, c in enumerate("一二三"):
                    if c in t.split("厨房", 1)[1]:
                        return i + 1
        return None

    def combo(self):
        """右侧满堂彩进度（xx/100）和「成功连续上餐 N」。读不到的项返回 None"""
        import re
        try:
            res, _ = get_ocr()(self.frame_full()[600:700, 1620:1900])
        except Exception:
            return None, None
        txt = "".join(t for _, t, _ in (res or []))
        m = re.search(r"(\d{1,3})/100", txt)
        c = re.search(r"上餐\D*(\d{1,2})", txt)
        return (int(m.group(1)) if m else None), (int(c.group(1)) if c else None)

    def read_income(self):
        """结算画面左边「营业额」下面的数字（实测在 1920 画面约 (220~360, 580~620)）"""
        try:
            res, _ = get_ocr()(self.frame_full()[540:660, 180:760])
        except Exception:
            return None
        import re
        for _, t, _ in (res or []):
            m = re.fullmatch(r"\D*(\d{2,6})\D*", t.replace(",", "").replace(" ", ""))
            if m:
                return int(m.group(1))
        return None

    def map_anchor(self):
        """大地图（按 M）上找离人最近的协议传送点图标（地图以人为中心，餐馆下面那个就是最近的）。
        返回图标中心坐标（1920 原尺寸）或 None"""
        if not hasattr(self, "_anchor_tpl"):
            self._anchor_tpl = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, "map_anchor.png"), dtype=np.uint8),
                                            cv2.IMREAD_COLOR)
        img = self.frame960()
        r = cv2.matchTemplate(img, self._anchor_tpl, cv2.TM_CCOEFF_NORMED)
        th, tw = self._anchor_tpl.shape[:2]
        best = None
        for _ in range(8):
            _, mx, _, (x, y) = cv2.minMaxLoc(r)
            if mx < 0.85:
                break
            cx, cy = x + tw // 2, y + th // 2
            d = (cx - 480) ** 2 + (cy - 270) ** 2
            if best is None or d < best[0]:
                best = (d, cx, cy)
            cv2.rectangle(r, (x - tw // 2, y - th // 2), (x + tw // 2, y + th // 2), -1, -1)
        return (best[1] * 2, best[2] * 2) if best else None

    def shop_closed(self):
        """营业倒计时走完会显示「已打烊」（之后还有一小段时间能上菜）。读左上角那一块的文字：
        有「打烊」两个字、或者倒计时是 00:00 就算打烊。读不了（OCR 出错）当作没打烊"""
        try:
            res, _ = get_ocr()(self.frame_full()[0:170, 0:340])
        except Exception:
            return False
        text = "".join(r[1] for r in (res or []))
        return "打烊" in text or "00:00" in text.replace(" ", "")

    def find_btn(self, name, thr=0.75):
        """找结算画面的大按钮（模板是原尺寸截的，在 960x540 画面里按几种比例找），返回中心坐标（1920 原尺寸）或 None"""
        key = "btn_" + name
        if key not in self.ui_tpl:
            tl = []
            for fn in (f"{name}.png", f"{name}2.png"):  # 2 = 另一种样式（11-07：「继续」箭头没红圈的版本）
                fp = os.path.join(PIC_DIR, fn)
                if os.path.exists(fp):
                    tl.append(cv2.imdecode(np.fromfile(fp, dtype=np.uint8), cv2.IMREAD_COLOR))
            self.ui_tpl[key] = tl
        img = self.frame960()
        best = (0.0, None)
        for tpl, sc in ((tp, s_) for tp in self.ui_tpl[key] for s_ in (0.5, 0.45, 0.55, 0.4, 0.6)):
            t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED))
            if v > best[0]:
                best = (v, ((loc[0] + t.shape[1] // 2) * 2, (loc[1] + t.shape[0] // 2) * 2))
        return best[1] if best[0] >= thr else None

    def fireworks_ready(self):
        """右侧「成功连续上餐」进度圈满了，会出现红色图标和「开启满堂彩 ▶▶」横幅，这时按 R
        （认横幅文字：模板 ui_mantangcai.png，平时最高只有 0.43 分）"""
        if not hasattr(self, "_mtc"):
            self._mtc = cv2.imdecode(np.fromfile(os.path.join(PIC_DIR, "ui_mantangcai.png"), dtype=np.uint8),
                                     cv2.IMREAD_COLOR)
        region = self.frame_full()[400:800, 1450:1920]
        return cv2.minMaxLoc(cv2.matchTemplate(region, self._mtc, cv2.TM_CCOEFF_NORMED))[1] >= self.cfg["fireworks_score"]

    def trace(self, label):
        """行程记录：把当前画面存到 test_channel/trace/，文件名带时间和当时在做什么，方便事后查哪一步走错"""
        if not self.cfg["trace"]:
            return
        try:
            d = os.path.join(BASE_DIR, "test_channel", "trace")
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%H%M%S") + f"_{int(time.time() * 10) % 10}_{label}.jpg"
            cv2.imencode(".jpg", self.frame960(), [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tofile(os.path.join(d, name))
        except Exception:
            pass

    def close_quit_dialog(self):
        """如果弹出了「提前结束经营」确认框（画面中间一条白色横幅），点「取消」关掉。返回是否处理了"""
        img = self.frame960()
        band = cv2.cvtColor(img[200:340, 100:860], cv2.COLOR_BGR2GRAY)
        above = cv2.cvtColor(img[60:160, 100:860], cv2.COLOR_BGR2GRAY)
        below = cv2.cvtColor(img[380:500, 100:860], cv2.COLOR_BGR2GRAY)
        # 横幅上下都是压暗的模糊背景；料理/饮品台面板是一整块亮色，不会误判
        if band.mean() > 215 and above.mean() < 150 and below.mean() < 150:
            self.log("弹出了「提前结束经营」确认框，点取消")
            self.click(*self.cfg["quit_cancel"])
            time.sleep(0.8)
            return True
        return False

    def close_open_panel(self, keys=()):
        """走路时画面不动：饮品台面板或料理界面开着就按 Esc 关掉（按着的方向键先松开再按回），关了返回 True"""
        try:
            opened = self.drink_panel_open() or self.kitchen_open()
        except Exception:
            return False
        if not opened:
            return False
        for k in keys:
            pydirectinput.keyUp(k)
        pydirectinput.press("esc")
        time.sleep(0.4)
        try:
            self.log("走路时面板还开着，按 Esc 关掉")
        except Exception:
            pass
        for k in keys:
            pydirectinput.keyDown(k)
        return True

    def drink_panel_open(self):
        img = cv2.cvtColor(self.frame_full()[150:300, 200:460], cv2.COLOR_BGR2GRAY)
        return cv2.minMaxLoc(cv2.matchTemplate(img, self.ui_drink, cv2.TM_CCOEFF_NORMED))[1] >= 0.8

    def kitchen_open(self):
        """料理界面开着就返回当前是几号厨房（看哪个厨房页签是选中的黑底样式），没开返回 None"""
        img = self.frame_full()
        best, best_s = None, 0.0
        for k, t in self.kitchen_tabs.items():
            s = cv2.minMaxLoc(cv2.matchTemplate(img[150:400], t, cv2.TM_CCOEFF_NORMED))[1]
            if s > best_s:
                best, best_s = k, s
        return best if best_s >= 0.85 else None

    def click(self, x, y, right=False):
        """点击游戏画面里的 (x, y)（1920x1080 窗口坐标）。只在面板打开、鼠标可见时用"""
        self.check()
        self.focus()
        ox, oy = self.client_origin()
        pydirectinput.moveTo(ox + int(x), oy + int(y))
        time.sleep(0.05)
        pydirectinput.click(button="right" if right else "left")

    def sign_scales(self):
        """号码牌匹配用的尺寸：常见 0.75~1.7 倍细分，贴得很近时牌子会到 2.5 倍以上，粗一点覆盖到 2.8"""
        return list(np.arange(self.cfg["sign_min_scale"], 1.75, 0.1)) + list(np.arange(1.9, 2.85, 0.2))

    def frame_full(self):
        """整个游戏画面（1920x1080，BGR）"""
        if not self.hwnd:
            self.find_window()
        ox, oy = self.client_origin()
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        img = np.array(self._local.sct.grab({"left": ox, "top": oy, "width": 1920, "height": 1080}))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def table_states(self, img=None):
        """读顶部桌位栏，返回 {桌号: (状态, 物品)}。自动服务只关心两种状态：
          laotao 等接待（招手老饕图标，很好认）/ order 等上菜（两个圆形点单图标）
          其余（点餐中、用餐中、待结账、空位）都算 other，暂时不用管。
        物品：order 状态下两个图标各是 dish(菜) 还是 drink(饮品)"""
        img = self.frame_full() if img is None else img
        out = {}
        partial = set()
        for i in range(6):
            cx = 350 + 142 * i
            gray = cv2.cvtColor(img[18:142, cx - 62:cx + 62], cv2.COLOR_BGR2GRAY)
            sc = {n: cv2.minMaxLoc(cv2.matchTemplate(gray[14:96, 10:114], t, cv2.TM_CCOEFF_NORMED))[1]
                  for n, t in self.st.items()}
            # 先看格子下面的文字：「老饕」才是等接待；「用餐中」「点餐中」「待结账」都不用管。
            # （只看招手图标会把用餐中/点餐中/空位认成老饕，实测 0.58~0.64 混在一起，白跑过去）
            band = gray[80:124, 4:120]
            tx = {n: cv2.minMaxLoc(cv2.matchTemplate(band, t, cv2.TM_CCOEFF_NORMED))[1]
                  for n, t in self.st_text.items()}
            top = max(tx, key=tx.get)
            others = max(v for n, v in tx.items() if n != top)
            if tx[top] >= self.cfg["text_threshold"] and tx[top] >= others + 0.1:
                out[i + 1] = ("laotao" if top == "laotao" else "pay" if top == "jiezhang" else "other", [])  # 待结账也认出来（v13）
                continue
            busy = max(sc["diancan"], sc["yongcan"])
            # 待结账是大块红底，直接跳过（结账不用我们管）
            hsv_slot = cv2.cvtColor(img[18:142, cx - 62:cx + 62], cv2.COLOR_BGR2HSV)
            red = (((hsv_slot[..., 0] <= 8) | (hsv_slot[..., 0] >= 170)) & (hsv_slot[..., 1] >= 120)
                   & (hsv_slot[..., 2] >= 90)).mean()
            if red > 0.35:
                out[i + 1] = ("pay", [])  # 待结账（红底）：v13 起闲着时去按 F 结账，让桌子早点空出来接下一位
                continue
            items, served, best_icon = [], 0, 0.0
            hsv = cv2.cvtColor(img[18:142, cx - 62:cx + 62], cv2.COLOR_BGR2HSV)
            for x0, x1 in ((0, 64), (60, 124)):
                half = gray[36:114, x0:x1]
                sd = cv2.minMaxLoc(cv2.matchTemplate(half, self.icons["dish"], cv2.TM_CCOEFF_NORMED))[1]
                sk = cv2.minMaxLoc(cv2.matchTemplate(half, self.icons["drink"], cv2.TM_CCOEFF_NORMED))[1]
                best_icon = max(best_icon, sd, sk)
                if max(sd, sk) < self.cfg["icon_threshold"]:
                    continue
                # 已经送到的那一样，图标右下角会叠一个绿色笑脸或红色哭脸（实测约 300 个像素点）
                h = hsv[60:124, x0:x1]
                red = (((h[..., 0] <= 8) | (h[..., 0] >= 172)) & (h[..., 1] >= 120) & (h[..., 2] >= 120)).sum()
                green = ((h[..., 0] >= 35) & (h[..., 0] <= 85) & (h[..., 1] >= 90) & (h[..., 2] >= 100)).sum()
                if red + green >= 80:
                    served += 1
                    continue
                items.append("dish" if sd >= sk else "drink")
            # 用餐中（碗筷）、点餐中有时会被认成点单图标：这两种的分数比菜/饮品图标还高，就不算等上菜
            if busy >= self.cfg["busy_threshold"] and busy > best_icon:
                items = []
            # items 只剩还没送的；全送齐了就不算等上菜（送了一部分的记进 partial）
            out[i + 1] = ("order", items) if items else ("other", [])
            if items and served:
                partial.add(i + 1)
        self.partial_tables = partial  # 已经送了一部分、还没送齐的桌（优先补齐）
        h = self.__dict__.setdefault("_ts_hist", [])  # v71：每次读到的桌态都记下（结算时统计落下的客人用最近的一次）
        h.append((time.time(), out))
        del h[:-30]
        return out

    def waiting_tables(self):
        """看顶部桌位栏，返回正在等接待（显示「老饕」招手图标）的桌号列表。
        实测黄色占比：等接待≈0.8，点餐中 0.44~0.67，空位一般低于 0.4"""
        hsv = cv2.cvtColor(self.frame960(), cv2.COLOR_BGR2HSV)
        out = []
        for i, x in enumerate((175, 245, 316, 387, 458, 529)):
            roi = hsv[32:66, x - 28:x + 28]
            m = (roi[..., 0] >= 12) & (roi[..., 0] <= 35) & (roi[..., 1] >= 50) & (roi[..., 2] >= 150)
            if m.mean() >= 0.75:
                out.append(i + 1)
        return out

    def sign_pos(self, d):
        """在画面里找 d 号牌，返回 (中心x, 中心y, 分数)，坐标按 960x540 计；找不到返回 None"""
        gray = cv2.cvtColor(self.frame960(), cv2.COLOR_BGR2GRAY)
        score, _, x, y = self._sign_best(gray, d)
        if score >= self.cfg["sign_threshold"]:
            return int(x), int(y), round(score, 2)
        return None

    def tables(self):
        """俯视角下识别绿色桌面，返回 [(cx, cy, w, h)]，坐标按 960x540 缩小画面计"""
        img = self.frame960()
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (25, 45, 25), (50, 200, 115))  # 桌面的深绿色（实测 H≈33 S≈80 V≈63）
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        found = []
        for c in cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            x, y, w, h = cv2.boundingRect(c)
            # 去掉长条形的吧台和长凳坐垫
            if cv2.contourArea(c) > 4000 and w <= 300 and 0.7 <= w / h <= 2.4:
                found.append((x + w // 2, y + h // 2, w, h))
        return found

    def _k(self, key):
        """面朝吧台（视角转 180°）时方向键对调：程序里一律按“面朝餐厅”写，这里统一换算"""
        if self.cfg["facing_counter"]:
            return {"w": "s", "s": "w", "a": "d", "d": "a"}.get(key, key)
        return key

    def hold(self, keys, sec, stop_on=(), unstick=True, until_blocked=False):
        keys = [self._k(k) for k in keys]
        return self._hold(keys, sec, stop_on, unstick, until_blocked)

    def _hold(self, keys, sec, stop_on=(), unstick=True, until_blocked=False):
        if self.abort_walk:  # 这一趟已经在路上办完了，后面的走路都跳过
            self.last_moved = 0.0
            return None
        # v67（用户）：满堂彩结束时停 1~2 秒，防止速度切换时按错速度走、站错位置或走丢
        if self.cfg.get("boost_end_pause", 1.5) > 0 and getattr(self, "boost_until", None) is not None:
            left_b = self.boost_until - time.time()
            if 0 < left_b < 1.5:
                time.sleep(left_b + 0.05)  # 加速马上结束：等它结束再走，不让一次走路跨过速度切换
        active = self.boost_active()
        if getattr(self, "_was_boost", False) and not active and self.cfg.get("boost_end_pause", 1.5) > 0:
            self.log(f"满堂彩加速结束：原地停 {self.cfg.get('boost_end_pause', 1.5)} 秒再走")
            time.sleep(float(self.cfg.get("boost_end_pause", 1.5)))
        self._was_boost = active
        # 满堂彩期间移动速度变快：走同样的距离要按更短的时间，坐标也按加速后的距离算
        self.cur_mult = self.speed_mult()
        if until_blocked:
            return self._hold_inner(keys, sec, stop_on, unstick, until_blocked)
        return self._hold_inner(keys, sec / self.cur_mult, stop_on, unstick, until_blocked)

    def boost_active(self):
        """满堂彩生效中：以按下 R 的时间为准（之后 60 秒）。不能只看右边的圈变红：
        进度攒满、还没按 R 时那个图标也是红的，会被当成已经在加速，走路时长全减半，回饮品台的路上就走丢"""
        now = time.time()
        if getattr(self, "boost_until", None) is not None:
            if now < self.boost_until:
                return True
            if now < self.boost_until + float(self.cfg.get("boost_tail", 15)):
                # v65：按 R 满 60 秒后游戏里的加速可能还没完（几次站位失败都在加速刚结束 4~12 秒）：看右侧满堂彩圈还红不红
                if now - self._boost_t > 0.5:
                    hsv = cv2.cvtColor(self.frame_full()[560:660, 1800:1900], cv2.COLOR_BGR2HSV)
                    red = (((hsv[..., 0] <= 8) | (hsv[..., 0] >= 170)) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 110)).mean()
                    self._boost, self._boost_t = red >= 0.25, now
                return self._boost
            return False
        if now - self._boost_t > 0.5:  # 0.5 秒内复用上次结果，省得每走一步都截图
            hsv = cv2.cvtColor(self.frame_full()[560:660, 1800:1900], cv2.COLOR_BGR2HSV)
            red = (((hsv[..., 0] <= 8) | (hsv[..., 0] >= 170)) & (hsv[..., 1] >= 120) & (hsv[..., 2] >= 110)).mean()
            self._boost, self._boost_t = red >= 0.25, now
        return self._boost

    def speed_mult(self):
        return self.cfg["boost_speed"] if self.boost_active() else 1.0

    def _hold_inner(self, keys, sec, stop_on=(), unstick=True, until_blocked=False):
        """同时按住 keys，最多 sec 秒；出现 stop_on 里的提示就立刻松开。返回看到的提示
        走动中画面长时间不变视为卡住：第一次重按方向键（防止按键丢失），之后按跳跃"""
        self.check()
        self.focus()
        for k in keys:
            pydirectinput.keyDown(k)
        seen = None
        start = time.time()
        end = start + sec
        paused = 0.0  # 路上顺手接待/上菜、卡住后退花掉的时间，不算真正往前走的时间
        last_thumb, last_t, stalls = None, start, 0
        # 往饮品台那一侧走时，「饮品台」提示一出现就停：饮品台是左边的尽头，越过去就出了送餐范围
        guard = self.cfg["drink_guard"] and self._k("d") in keys and "p_drink" not in stop_on
        try:
            while time.time() < end:
                self.check()
                if self.watch_fw and time.time() - self._fw_t > 1.0:
                    # 只在走路时检查（不会打断开着的料理界面/饮品台面板）
                    self._fw_t = time.time()
                    if self.fireworks_ready():
                        raise FireworksReady()
                if stop_on or self.passing or guard:
                    sc = self.prompt_scores()
                    p = next((k for k in stop_on if sc[k] >= self.cfg["prompt_threshold"]), None)
                    if p:
                        seen = p
                        break
                    if guard and sc["p_drink"] >= self.cfg["prompt_threshold"]:
                        self.log("到饮品台了，不再往那边走")
                        break
                    if self.passing:
                        q = next((k for k in TABLE_PROMPTS if sc[k] >= self.cfg["prompt_threshold"]), None)
                        if q is None:
                            self.pass_done = None
                        elif q != self.pass_done:  # 同一个提示办过一次就不再办，等它消失
                            self.pass_done = q
                            # 路过的桌子有「接待」或「上菜」：停下顺手办了，再接着走（花掉的时间不算行走时间）
                            t0 = time.time()
                            for k in keys:
                                pydirectinput.keyUp(k)
                            cb, self.passing = self.passing, None  # 办事时不再触发，避免嵌套
                            try:
                                cb(q)
                            finally:
                                self.passing = cb
                            if self.abort_walk:
                                # 顺手办的正好是要去的那张桌：这一趟到此为止，不再接着走
                                paused += time.time() - t0
                                break
                            for k in keys:
                                pydirectinput.keyDown(k)
                            end += time.time() - t0
                            paused += time.time() - t0
                            last_thumb, last_t = None, time.time()
                now = time.time()
                if until_blocked and now - last_t >= 0.12 and now - start > 0.2:
                    # 贴吧台：画面一不动就说明已经顶住了，马上松手，不要一直撞
                    th = self.thumb()
                    if last_thumb is not None and float(np.abs(th - last_thumb).mean()) < self.cfg["stuck_diff"]:
                        break
                    last_thumb, last_t = th, now
                elif unstick and now - last_t >= self.cfg["stuck_interval"] and now - start > 0.25:
                    th = self.thumb()
                    if last_thumb is not None and float(np.abs(th - last_thumb).mean()) < self.cfg["stuck_diff"]:
                        stalls += 1
                        if stalls == 1 and self.close_quit_dialog():
                            pass  # 是确认框挡住了操作，关掉后接着走
                        elif stalls <= 2 and self.close_open_panel(keys):
                            pass  # v14b：饮品台面板/料理界面开着（实测拿完饮品面板没关，取菜卡了 50 秒）：关掉接着走
                        elif stalls == 1:
                            for k in keys:
                                pydirectinput.keyUp(k)
                            time.sleep(0.05)
                            for k in keys:
                                pydirectinput.keyDown(k)
                        elif stalls > 1 + self.cfg["max_jumps"]:
                            self.log("跳了几次还是卡住，先停下")
                            break
                        else:
                            # 往反方向跳一下脱困；顺着原方向跳容易直接跳出去
                            back = [OPPOSITE[k] for k in keys]
                            for k in keys:
                                pydirectinput.keyUp(k)
                            for k in back:
                                pydirectinput.keyDown(k)
                            pydirectinput.press(self.cfg["jump_key"])
                            time.sleep(self.cfg["jump_back"])
                            for k in back:
                                pydirectinput.keyUp(k)
                            for k in keys:
                                pydirectinput.keyDown(k)
                            self.log("好像卡住了，往反方向跳一下")
                        end += self.cfg["stuck_interval"]  # 卡住的时间不算行走时间
                        paused += self.cfg["stuck_interval"]
                    last_thumb, last_t = th, now
                time.sleep(0.03)
        finally:
            for k in keys:
                pydirectinput.keyUp(k)
            self.last_moved = max(0.0, time.time() - start - paused)
            if self.cfg.get("debug_moves") and self.last_moved < sec * 0.8 and sec >= 0.3:
                why = f"提示 {seen}" if seen else ("卡住" if stalls else "其他")
                self.log(f"  [走路] {''.join(keys)} 要走 {sec:.2f}s 只走了 {self.last_moved:.2f}s（{why}，倍数 {self.cur_mult}）")
            if self.on_move:
                self.on_move(keys, self.last_moved, self.slow)  # 通知坐标跟踪
        time.sleep(self.settle)  # 等角色停稳
        return seen

    def press(self, key):
        self.check()
        self.focus()
        pydirectinput.press(self._k(key))

    def set_slow(self, on):
        """缓慢行走：游戏里按一下 Ctrl 切换。程序记着当前是哪种，只在需要切换时按"""
        if not self.cfg["use_slow"] or on == self.slow:
            return
        self.focus()
        pydirectinput.press("ctrl")
        self.slow = on
        time.sleep(0.1)

    def look(self, dy=0, dx=0):
        """鼠标相对移动转视角。dy 正数往下看；dx 正数往右转（左右转会改变 W/A/S/D 的方向，用完要转回来）"""
        self.check()
        self.focus()
        n = max(1, max(abs(int(dx)), abs(int(dy))) // 40)  # 不足 40 也要走一步，小幅校正才有效
        for i in range(n):
            # 分小步移动，最后一步补齐余数，保证总量精确（转回去时才能对准）
            sx = int(dx * (i + 1) / n) - int(dx * i / n)
            sy = int(dy * (i + 1) / n) - int(dy * i / n)
            user32.mouse_event(0x0001, sx, sy, 0, 0)  # MOUSEEVENTF_MOVE，相对移动
            time.sleep(0.01)
        time.sleep(0.25)


class Navigator:
    def __init__(self, game, log, ui=lambda *a: None):
        self.g = game
        self.cfg = game.cfg
        self.log = log
        self.ui = ui
        self.where = "未知"
        self.side = None
        self.view_set = False
        self.last_sign = (None, 0.0)
        self.depth = 1.0  # 离吧台多远（按 W 的秒数）；开局一般就站在吧台前
        self.inv = []  # 物品栏里的东西（按格子顺序）："dish" / "drink"
        self.closed = False  # 这一局已经打烊（不再开厨房）
        self.anchor_fails = 0  # 连续找不到饮品台的次数（到 lost_after 就算走丢，放弃这一局）
        self.fail_until = {}  # 送不出去的桌：这个时间之前先不去（桌号 -> 时间）
        self.want = {}  # 各桌点的饮品要求（从气泡读出来的条件，如 "非[茶香]"；"" = 没点饮品或没读到）
        self.who = {}   # 各桌坐的是几号老饕（从点单气泡认出来的）
        self.held = set()  # 压着不送的桌
        self.soup_line = {}  # 各桌点菜句是不是要汤羹（要两份汤的不管几号老饕都压）
        self.dish_good = {}  # 这桌客人的点菜要求我们的菜能满足（饮品照样要对上：实测菜对了送错饮品也是哭脸）
        self.riddle = None  # 读谜语的 OCR，第一次用时才加载
        self.at_table = None  # 现在站在哪张桌边（确认过的），用来桌到桌直接走
        self.target, self.target_done = None, False  # 这一趟要去哪张桌；路上顺手办完了就不再找它
        self.last_dish_try = 0.0
        self.last_fireworks = 0.0
        self.at_head = False  # 是否站在吧台前的中间过道口（从桌子原路退回来的）
        self.at_drink = False  # 是否站在饮品台旁边（墙角，离开时要先横挪出来）
        self.last_cook = 0.0
        # 虚拟坐标（单位：正常速度走的秒数）。原点 = 饮品台；x 往过道方向增大，y 往大厅里增大（贴吧台 y=0）
        self.pos = None  # None = 不知道自己在哪，要先回饮品台定原点
        self.track = []  # 从原点开始走过的每一段：(逻辑方向键, 距离)
        self.board = (None, 0.0)  # 最近一次从吧台后面看板读到的空位 ([1,2,3号厨房], 时间)
        self.cooked = 0  # 本局一共下单做了几份菜
        self.served_dish = 0  # 本局一共送出去几份菜
        self.first_batch = 0  # 开局第一次补满做了几份（不计入多做的差值）
        game.on_move = self._on_move

    def _at(self, where):
        self.where = where
        self.ui("where", where)

    # ---------- 虚拟坐标 ----------
    # 只在面朝吧台的方案里用。程序里的方向一律是“面朝餐厅”的逻辑方向：
    #   w = 往大厅里走（y 变大）  s = 往吧台走（y 变小）
    #   a = 往过道方向（x 变大）  d = 往饮品台方向（x 变小）
    def use_coords(self):
        return self.cfg["facing_counter"] and self.cfg["use_coords"]

    def aisle_x(self):
        """中间过道（中间那排地砖）的 x：实测从饮品台斜着滑 lane_center 秒，换成纯横移的秒数"""
        return self.cfg["lane_center"] * self.cfg["lateral_factor"]

    def table_spot(self, n):
        """n 号桌站位的大概坐标（靠桌后站的地方），用来排顺序和估距离"""
        lane, row = TABLES[n]
        dx = self.cfg["table_side_x"]
        return (self.aisle_x() + (dx if lane == "left" else -dx), self.cfg["rows"][row])

    def _on_move(self, keys, moved, slow):
        """每走一段就按按了哪些键、走了多久推算新坐标"""
        if self.pos is None or moved <= 0:
            return
        logical = [self.g._k(k) for k in keys]  # 实际按键换回逻辑方向（面朝吧台时是对调的）
        f = moved * (self.cfg["slow_factor"] if slow else 1.0) * (0.71 if len(logical) > 1 else 1.0) * self.g.cur_mult
        # 轨迹：从原点（饮品台）开始的每一段移动，走丢时倒着走回去
        self.track.append((logical, f))
        dx = (("a" in logical) - ("d" in logical)) * f
        dy = (("w" in logical) - ("s" in logical)) * f
        self.pos = [max(0.0, self.pos[0] + dx), max(0.0, self.pos[1] + dy)]

    def _pos_str(self):
        return "未知" if self.pos is None else f"({self.pos[0]:.2f}, {self.pos[1]:.2f})"

    def _lateral_to(self, tx, stop_on=()):
        """横移到 x = tx"""
        dx = tx - self.pos[0]
        if abs(dx) < self.cfg["coord_tol"]:
            return None
        return self.g.hold(["a" if dx > 0 else "d"], abs(dx), stop_on=stop_on)

    def move_to_row(self, row_y):
        """按坐标走到某一排：先横移回中间过道，再沿过道前后走到那一排（中途校正视角）"""
        if self.pos is None:
            if not self.anchor():
                return False
        ax = self.aisle_x()
        if (self.cfg["repin_between_rows"] and self.pos[1] > 0.5
                and abs(row_y - self.pos[1]) > 0.3):
            # 换排：先退回吧台贴住（前后坐标归零），再沿过道走过去。直接在桌子之间斜着走，
            # 一被长凳/客人半挡住，按时间算的位置就越来越偏（实测会把第 1 排当成第 3 排）
            self.pin_counter()
        if self.pos[1] < 0.3 and self.at_drink:
            # 从饮品台（墙角）出发：先滑一小段离开墙角，校正视角
            self.at_drink = False
            self.g.hold(["a"], min(self.cfg["leave_drink"] * self.cfg["lateral_factor"], ax))
            self._fix_yaw_in_hall()
        self._lateral_to(ax)
        dy = row_y - self.pos[1]
        if self.cfg.get("debug_moves"):
            self.log(f"  [走路] 去 y={row_y:.2f} 那一排：现在坐标 {self._pos_str()}，过道 x={ax:.2f}")
        if abs(dy) >= self.cfg["coord_tol"]:
            key = "w" if dy > 0 else "s"
            by = self.cfg["board_y"]
            if key == "w" and self.pos[1] < by - 0.1 and row_y >= by:
                # 往里走会经过看板点：先走到看板点停下，校正视角，读一次厨房看板
                self.g.hold(["w"], by - self.pos[1])
                self._fix_yaw_in_hall()
                self.read_boards()
            else:
                first = min(abs(dy), self.cfg["yaw_walk"])
                self.g.hold([key], first)
                self._fix_yaw_in_hall()
            rest = row_y - self.pos[1]
            if abs(rest) >= self.cfg["coord_tol"]:
                self.g.hold(["w" if rest > 0 else "s"], abs(rest))
        if row_y <= self.cfg["board_y"] + 0.2:
            self._fix_yaw_in_hall()  # 第一排：横着靠桌之前再校正一次（深处测不准，不转，用看板点校正过的朝向）
        return True

    def pin_counter(self):
        """按住 S 退回吧台并顶住。走得越深退得越久（之前固定 1.5 秒，从第三排退不回来）"""
        cur = self.at_table
        aisle = f"aisle{TABLES[cur][1] + 1}" if cur in TABLES else None
        self.back_to_aisle()
        self.at_table = None
        if self.at_drink:
            # 站在饮品台旁边时别往前压：先从墙角滑出来
            self.leave_drink()
        depth = self.pos[1] if (self.use_coords() and self.pos is not None) else max(self.depth, 3.0)
        if self.cfg["facing_counter"] and depth > 0.5:
            # 刚在桌边按过 F，镜头常常被带歪二三十度、偶尔整个转反；直接往吧台走会斜着走到屏风那边、甚至走出门口。
            # 已经退回中间过道了：先确认面朝吧台（正常时只花 0.25 秒看一眼），再按横线校正，然后才往吧台走
            if self.cfg["pin_face_check"]:
                self.ensure_facing()
            self._fix_yaw_in_hall(place=aisle)  # 往回走之前也摆正（用户：最后走丢就是视角偏了）
        t = max(self.cfg["pin_time"], depth * 1.15 + 0.5)
        # 往吧台走，一顶住就停（画面不动了），然后校正视角
        self.g.hold(["s"], t, unstick=False, until_blocked=True)
        for side in (("a", "d", "d") if self.cfg["pin_sign_check"] else ()):
            if self.g.abort_walk:
                break  # 这一趟已经办完（后面的走路都会跳过），不做挡路检查
            d, sc = self.g.which_table()
            if not d:
                break
            # 还看得到桌号牌：是被桌子/长凳挡住了，不是贴到了吧台。往旁边挪一点再往吧台走
            self.log(f"往吧台走被 {d} 号桌挡住了，挪一下再走")
            self.g.hold([side], 0.6)
            self.g.hold(["s"], 3.0, unstick=False, until_blocked=True)
        self.depth = 0.0
        if self.pos is not None:
            self.pos[1] = 0.0  # 贴住吧台：前后坐标校零
        if self.cfg["facing_counter"] and self.cfg["yaw_fix"]:
            self.fix_yaw(confirm=False)

    def fix_yaw_after_f(self):
        """在桌边按过 F（接待/上菜）后：角色会转向客人、镜头跟着转。人在大厅里时上方吧台带读数可靠，放宽上限转回来"""
        if self.cfg.get("wang_nav"):
            return  # 新走法：按 F 后镜头实测不转；回过道时按消失点摆正（旧的横线读数会假报大角度、把镜头转歪）
        if self.cfg["yaw_after_f"] and self.cfg["facing_counter"] and self.cfg["yaw_fix"] and (self.pos is None or self.pos[1] > 0.5):
            self.fix_yaw(maxdeg=self.cfg["yaw_max_hall"])

    def _fix_yaw_in_hall(self, place=None):
        if self.cfg["yaw_fix"]:
            self.fix_yaw(place=place)

    def ref_yaw(self, place):
        """和 survey_wang 里的正对参考图比：返回 (歪了几度, 对上的点数, 参考图地点)，对不上返回 None。
        镜头往右转，画面往左平移：大厅里约 6.8 像素/度、饮品台前约 5.3 像素/度（960 宽，采样标定）"""
        if not hasattr(self, "_yaw_refs"):
            self._yaw_refs = []
            d = os.path.join(BASE_DIR, "test_channel", "survey_wang")
            try:
                self._yaw_orb = cv2.ORB_create(1000)
                self._yaw_bf = cv2.BFMatcher(cv2.NORM_HAMMING)
                try:
                    import hall_locator
                    self._yaw_mask = hall_locator.MASK.astype(np.uint8) * 255
                except ImportError:
                    self._yaw_mask = np.full((540, 960), 255, np.uint8)
                # 不随转镜头平移的界面会把平移量拉向 0（实测饮品台前转了 20° 也估成 0°）：挖掉 F 提示框和右边的进度条
                self._yaw_mask = self._yaw_mask.copy()
                self._yaw_mask[295:415, 585:865] = 0
                self._yaw_mask[265:385, 815:960] = 0
                for line in open(os.path.join(d, "index.jsonl"), encoding="utf-8"):
                    r = json.loads(line)
                    if r.get("yaw") != 0 or r["place"].startswith(("lost", "aisle1_raw", "aisle2_raw", "aisle3_raw")):
                        continue
                    g = cv2.cvtColor(cv2.imdecode(np.fromfile(os.path.join(d, r["file"]), dtype=np.uint8), cv2.IMREAD_COLOR),
                                     cv2.COLOR_BGR2GRAY)
                    k, des = self._yaw_orb.detectAndCompute(g, self._yaw_mask)
                    if des is not None and len(k) >= 50:
                        self._yaw_refs.append((r["place"], np.float32([q.pt for q in k]), des))
            except (OSError, ValueError, cv2.error) as e:
                self.log(f"参考图载入失败：{e}")
            self.log(f"摆正视角：载入 {len(self._yaw_refs)} 张参考图")
        if not self._yaw_refs:
            return None
        k, des = self._yaw_orb.detectAndCompute(cv2.cvtColor(self.g.frame960(), cv2.COLOR_BGR2GRAY), self._yaw_mask)
        if des is None or len(k) < 50:
            return None
        pts = np.float32([q.pt for q in k])
        best = None
        for pl, kp2, d2 in self._yaw_refs:
            if pl != place:
                continue
            try:
                ms = self._yaw_bf.knnMatch(des, d2, k=2)
            except cv2.error:
                continue
            good = [m for m, n2 in (x for x in ms if len(x) == 2) if m.distance < 0.75 * n2.distance]
            if len(good) < 20:
                continue
            a = pts[[m.queryIdx for m in good]]
            b = kp2[[m.trainIdx for m in good]]
            M, inl = cv2.estimateAffinePartial2D(b, a, method=cv2.RANSAC, ransacReprojThreshold=6)
            if M is None:
                continue
            inl = inl.ravel() > 0
            n_in = int(inl.sum())
            if best is None or n_in > best[1]:
                dx = float(np.median(a[inl, 0] - b[inl, 0]))  # 参考图 → 现在：画面往右平移了多少
                ppd = 5.3 if pl.startswith("drink") else 6.8
                best = (-dx / ppd, n_in, pl)
        return best

    def fix_yaw(self, maxdeg=None, top=False, confirm=True, place=None):
        """摆正视角。place 给了（站在采过参考图的同一个地方：饮品台前、退回过道时）就和那个地方的参考图比：
        准、不怕气泡和桌椅的线；转完再比一次，没变正就转回去。参考图对不上时横线只信 8° 以内的小读数
        （实测横线会在正对时假报 21°、38°，照着转一半就把镜头转歪了）。
        没给 place 照旧按横线（不同地方的参考图不能比：人站偏了画面也会平移，会被当成视角歪了）"""
        if not (place and self.cfg.get("yaw_by_ref")):
            return self._fix_yaw_lines(maxdeg, top, confirm)
        k = self.cfg["look_px_per_deg"]
        need = int(self.cfg["ref_min_inliers"])
        r = self.ref_yaw(place)
        if r and r[1] >= need:
            for _ in range(2):
                deg, n_in, _ = r
                if abs(deg) <= 1.5:
                    return
                step = max(-30.0, min(30.0, deg))
                self.g.look(dx=int(-step * k))
                time.sleep(0.15)
                r2 = self.ref_yaw(place)
                if not r2 or r2[1] < need or abs(r2[0]) > abs(deg) - 2:
                    # 转完没变正（参考图比错了）：转回去，不冒险
                    self.g.look(dx=int(step * k))
                    self.log(f"对照参考图（{place}）说歪了 {deg:+.1f}°，转完没变正（{r2[0] if r2 else None}），转回原样")
                    return
                self.log(f"对照参考图（{place}，{n_in} 点）：视角歪了 {deg:+.1f}°，已转正（剩 {r2[0]:+.1f}°）")
                r = r2
            return
        a = self.g.counter_angle(top)
        if a is None or abs(a) <= self.cfg["yaw_tol"]:
            return
        if abs(a) > 8:
            self.log(f"参考图对不上，横线读数 {a}° 偏大（常是假的），不转")
            return
        self.log(f"参考图对不上，按横线转回 {a}°")
        self.g.look(dx=int(-a * self.cfg["yaw_px_per_deg"] * self.cfg["yaw_sign"]))

    def _fix_yaw_lines(self, maxdeg=None, top=False, confirm=True):
        """校正左右朝向：画面上方和吧台平行的横线应该是水平的，歪了就按角度反向转回去。
        maxdeg：允许转回的最大读数（默认 yaw_max；在大厅里刚按过 F 时用 yaw_max_hall）"""
        tol, k = self.cfg["yaw_tol"], self.cfg["yaw_px_per_deg"] * self.cfg["yaw_sign"]
        limit = float(maxdeg or self.cfg["yaw_max"])
        cap = 15.0 if maxdeg else 8.0
        for _ in range(8):
            a = self.g.counter_angle(top)
            if a is None:
                self.log("没测到足够的横线，跳过朝向校正")
                return
            if abs(a) <= tol:
                return
            if abs(a) > limit:
                # 读数很大：可能是读错了（界面淡出、气泡、人物挡住），也可能是真的歪了（上菜时人转向客人，镜头跟着转了）。
                # 隔一会儿再读一次，两次一致就是真歪了，转回来；不一致才当成读错
                ok = False
                if not confirm:
                    # 贴着吧台时读数会被挤得很歪（实测 ±20° 而且两次一致），一律不转
                    self.log(f"视角读数 {a}° 太大（贴着吧台读不准），不转")
                    return
                if not maxdeg or self.g.boost_active():
                    # 满堂彩期间烟花会让读数稳定地错（实测 31.9° 照转连转 49° 走丢），但镜头也真会歪（实测 38.5° 不转也走丢）：同样只转一半。
                    # 平时（不是刚在桌边按过 F）读到大角度：有时是真歪了（沿吧台走到头撞歪，实测 31.5° 不转就走丢），
                    # 有时是附近桌椅的线把读数带偏（实测照着连转 43° 镜头朝店外）。折中：两次读数一致才转，
                    # 只转一半、最多 15°，10 秒内只转一次——真歪了几次就转回来，读错了也偏不远
                    if time.time() - getattr(self, "_big_yaw_t", 0) < 10:
                        self.log(f"视角读数 {a}° 太大，刚转过一次大角度，先不转")
                        return
                    time.sleep(0.3)
                    a2 = self.g.counter_angle(top)
                    if a2 is None or abs(a2 - a) > 3:
                        self.log(f"视角读数 {a}° 太大、两次不一致（{a2}°），不转")
                        return
                    self._big_yaw_t = time.time()
                    step = max(-15.0, min(15.0, a / 2))
                    self.log(f"视角读数 {a}° 很大（两次一致），先转回一半 {step:.1f}°")
                    self.g.look(dx=int(-step * k))
                    return
                if time.time() - getattr(self, "last_cook", 0) < 4.0:
                    # 刚关掉料理界面：读数也会稳定地错（实测两次都读成 23.5°/31.9°，转了 40 多度朝墙走丢）
                    self.log(f"视角读数 {a}° 太大，刚关料理界面读数不准，不转")
                    return
                # 贴着吧台时读数会被挤得很歪（实测 ±20° 而且两次一致），不能当真：confirm=False 时大读数一律不转
                if confirm and abs(a) <= float(self.cfg.get("yaw_max_confirmed", 35.0)):
                    time.sleep(0.4)
                    a2 = self.g.counter_angle(top)
                    ok = a2 is not None and abs(a2 - a) <= 3
                    if not ok and a2 is not None and limit < abs(a2) <= float(self.cfg.get("yaw_max_confirmed", 35.0)):
                        # 第一次读时镜头可能还在晃：再读第三次，后两次一致也算真歪了（实测歪了 30° 时不转就走丢）
                        time.sleep(0.3)
                        a3 = self.g.counter_angle(top)
                        ok = a3 is not None and abs(a3 - a2) <= 3
                        if ok:
                            a = a3
                if not ok:
                    self.log(f"视角读数 {a}° 太大，多半是读错了，不转")
                    return
                step = max(-12.0, min(12.0, a))
                self.log(f"视角歪了 {a}°（两次读数一致），转回 {step}°")
                self.g.look(dx=int(-step * k))
                continue
            if abs(a) > 8:
                # 偏得多时再测一次，两次对得上才转
                time.sleep(0.2)
                a2 = self.g.counter_angle(top)
                if a2 is None or abs(a2 - a) > 3:
                    self.log(f"视角读数不稳（{a}° / {a2}°），这次先不转")
                    return
            step = max(-cap, min(cap, a))  # 一次最多转 cap 度，分几次逐步转回来
            self.log(f"视角歪了 {a}°，转回 {step}°")
            self.g.look(dx=int(-step * k))

    def _slide_to_drink(self):
        """已经贴住吧台后，只按 D 往右滑到「饮品台」提示出现。
        不同时按 S：滑过吧台尽头时 S 会把角色带进吧台右下角卡住"""
        return self.g.hold(["d"], self.cfg["slide_timeout"], stop_on=("p_drink",))

    def go_head(self):
        """从桌边沿中间过道直接退回吧台前的过道口（不去饮品台），下一趟从这里出发"""
        if self.at_head:
            return
        if self.use_coords() and self.pos is not None:
            self.back_to_aisle()
            self.pin_counter()
            self._lateral_to(self.aisle_x())
            self.at_table = None
            self.at_head = True
            self._at("过道口")
            return
        if self.at_table is None:
            self.anchor()
            self.counter_slide("a", self.cfg["lane_center"])
        else:
            self.back_to_aisle()
            self._fix_yaw_in_hall()
            self.pin_counter()
        self.at_head = True
        self._at("过道口")

    def anchor(self):
        """回到饮品台锚点。回去的路上不认「目标桌」（见 _anchor），回到后恢复，接着去原来要去的桌"""
        target = self.target
        try:
            return self.wn_anchor() if self.cfg.get("wang_nav") else self._anchor()
        finally:
            self.target = target

    def _anchor(self):
        """回到饮品台锚点：贴吧台 -> 沿吧台往右滑到出现「饮品台」提示"""
        self.g.abort_walk = False  # 路上顺手办完了目标桌会置这个标记（跳过后面的走路），找饮品台不能被它跳过
        # 回饮品台的路上没有「目标桌」：不然路过刚办完的那桌又看到它的提示，会把退回吧台这一步也中止掉，
        # 人停在大厅中间，接着把长凳当吧台滑，直接走丢
        self.target = None
        self.at_head = False
        # 记下离饮品台还有多远（坐标 x 就是正常速度横走的秒数），滑到后和实际用时比，查是不是误开了缓慢行走
        self._slide_exp = self.pos[0] if self.pos is not None else None
        self.g.close_quit_dialog()
        self.set_view()
        if self.g.has_prompt("p_drink") and self.g.drink_prompt_ok():
            # 已经站在饮品台旁边：不要再往吧台压（饮品台在墙角，一压镜头就被挤歪、人会斜着走出去）
            self._at("饮品台")
            self.at_drink = True
            self.set_origin()
            return True
        if (self.pos is not None and self.pos[1] < 0.3 and self.at_table is None
                and self.where in ("出餐台", "出餐口等待")):
            # 已经贴着吧台（刚在出餐台取完菜）：直接沿吧台滑回饮品台，不退出去重新定位（省六七秒）
            seen = self.g.hold(["d"], self.pos[0] * 1.3 + 0.5, stop_on=("p_drink",), unstick=False)
            if (seen or self.g.has_prompt("p_drink")) and self.g.drink_prompt_ok():
                self._at("饮品台")
                self.at_drink = True
                self.set_origin()
                return True
        self.pin_counter()
        if self.g.has_prompt("p_drink"):
            self._at("饮品台")
            self.at_drink = True
            self.set_origin()
            return True
        if self.cfg["facing_counter"]:
            return self._anchor_along_counter()
        seen = self._slide_to_drink()
        if not seen and self.g.visible_signs():
            # 还能看到桌号牌 = 还在大厅里，只是离饮品台远（比如从 2 号桌那边回来），接着往右滑
            self.log("还没到饮品台，接着往右滑")
            # 只多滑一小段：滑过头就会钻进吧台右下角
            seen = self.g.hold(["d"], self.cfg["slide_more"], stop_on=("p_drink",))
        if not seen:
            # 被店员挡住，或者滑过了头卡进吧台右下角：先往上走出角落，再往左回到吧台前，重新贴台再滑
            self.log("没滑到饮品台，退出来再试")
            self.g.hold(["w"], 0.6)
            self.g.hold(["a"], 0.8)
            time.sleep(0.5)
            self.depth = 0.6
            self.pin_counter()
            seen = self._slide_to_drink()
        if not seen and self.recover():
            self.pin_counter()
            seen = self._slide_to_drink()
        if not seen:
            self.log("没找到「饮品台」提示，锚点校正失败")
            self.g.trace("饮品台失败")
            self._at("未知")
            return False
        self._at("饮品台")
        self.at_drink = True
        self.set_origin()
        self.log("已到饮品台（锚点）")
        self.g.trace("饮品台")
        return True

    def _biased(self, d, fn):
        """沿吧台横走前把视角往吧台那边偏 counter_bias_deg 度：往饮品台走（逻辑 d = 实际 A）往右偏，往外走往左偏，
        这样「横走」里带着一点往吧台的分量，一定贴着吧台走（用户演示的走法）。走完按同样的量转回来"""
        deg = float(self.cfg.get("counter_bias_deg", 0) or 0)
        if not self.cfg["facing_counter"] or deg <= 0:
            return fn()
        px = int(deg * self.cfg["look_px_per_deg"]) * (1 if d == "d" else -1)
        self.g.look(dx=px)
        try:
            return fn()
        finally:
            self.g.look(dx=-px)

    def counter_slide(self, d, t, stop_on=(), unstick=True):
        """沿吧台横移。面朝吧台时视角已校正、已贴住吧台，直接按左右（不再同时按着往吧台走，免得一直撞吧台）；
        时长是按“斜着贴吧台滑”测的，纯横移更快，乘 lateral_factor 换算"""
        if self.cfg["facing_counter"]:
            return self._biased(d, lambda: self.g.hold([d], t * self.cfg["lateral_factor"], stop_on=stop_on, unstick=unstick))
        return self.g.hold(["s", d], t, stop_on=stop_on, unstick=unstick)

    def _check_slow(self, used, what="滑回饮品台"):
        """沿吧台滑回饮品台用了 used 秒，按坐标本该用 _slide_exp 秒。缓慢行走（Ctrl 切换，MaaEnd 等别的操作可能留着没关）
        大约慢 1.7 倍：连续两次明显偏慢，就按一次 Ctrl 切回跑步"""
        exp = getattr(self, "_slide_exp", None)
        if exp is None or exp < 0.5 or self.g.cur_mult != 1.0:
            return
        if used > exp * self.cfg["slow_detect_ratio"] + 0.2:
            self.slow_hits = getattr(self, "slow_hits", 0) + 1
            self.log(f"{what}用了 {used:.2f}s，按坐标该用 {exp:.2f}s（偏慢第 {self.slow_hits} 次）")
            if self.slow_hits >= 2:
                self.g.focus()
                pydirectinput.press("ctrl")
                time.sleep(0.1)
                self.slow_hits = 0
                self.log("走得明显比平时慢：多半开着缓慢行走，按 Ctrl 切回跑步")
        else:
            self.slow_hits = 0
        self._slide_exp = None

    def _slide_drinkward(self, t):
        return self._biased("d", lambda: self._slide_drinkward_raw(t))

    def _slide_drinkward_raw(self, t):
        """沿吧台往饮品台滑最多 t 秒，「饮品台」或「厨房一」提示出来就停。
        「厨房二」「厨房三」的提示长得和「厨房一」几乎一样（模板会认错），所以停下后用文字识别确认是几号厨房，
        不是一号就接着滑（这一段先不看厨房提示，免得在同一个窗口前反复停）"""
        end = time.time() + t
        seen = None
        while time.time() < end:
            seen = self.g.hold(["d"], end - time.time(), stop_on=("p_drink", "p_kitchen1"), unstick=False)
            if seen != "p_kitchen1":
                break
            k = self.g.kitchen_no()
            if k in (None, 1):
                break
            self.log(f"看到的是「厨房{'一二三'[k - 1]}」，离饮品台还远，接着滑")
            seen = self.g.hold(["d"], min(0.5, max(0.0, end - time.time())), stop_on=("p_drink",), unstick=False)
            if seen:
                break
        return seen

    def _anchor_by_liangao(self):
        """贴着吧台、画面里看得到当原点的凉糕：它在第一格（饮品台旁边），站在饮品台前时在画面 x≈first_dish_x_at_drink。
        按偏差一小步一小步挪（每步后再看一次，满堂彩加速时走得快也不会冲过头），「饮品台」提示一出来就停。返回是否到了"""
        if not self.cfg.get("anchor_dish"):
            return False
        x0 = float(self.cfg["first_dish_x_at_drink"])
        fast = self.g.boost_active()
        for _ in range(10):
            if self.g.has_prompt("p_drink"):
                return True
            lgx = self.g.anchor_dish_x()
            if lgx is None:
                return False
            e = lgx - x0
            # 凉糕在画面左边（e<0）= 人在饮品台右边（出菜那头）：往饮品台走（逻辑 d = 实际 A）；e>0 = 走过了：往回（逻辑 a）
            key = "d" if e < 40 else "a"
            t = min(0.6, max(0.08, abs(e) / 370.0)) * (0.6 if fast else 1.0)
            if abs(e) <= 40:
                t = 0.1
            seen = self.g.hold([key], t, stop_on=("p_drink",), unstick=False)
            if seen:
                self.log(f"按凉糕位置挪到了饮品台（凉糕原来在 x={lgx}）")
                return True
        return self.g.has_prompt("p_drink")

    def _orb_lateral(self):
        """贴着吧台时用大厅定位量离饮品台的横向距离（大厅坐标 y − 饮品台 y，正数 = 在饮品台右边）。
        定位不可靠（特征点少、不在吧台前）返回 None"""
        if not self.cfg["orb_anchor"]:
            return None
        try:
            if not hasattr(self, "_orb"):
                cv2.setNumThreads(8)
                import hall_locator
                self._orb = hall_locator.OrbLocator(os.path.join(BASE_DIR, "test_channel", "survey"))
            r = self._orb.locate(self.g.frame960())
        except (cv2.error, ImportError, OSError, ValueError):
            return None
        if not r or r[2] < 40 or r[0] < 139.5:
            return None
        dy = r[1] - float(self.cfg.get("drink_y", 131.0))
        self.log(f"贴台定位：y={r[1]:.1f}，离饮品台 {dy:+.1f}（内点 {r[2]}）")
        return dy

    def _anchor_along_counter(self):
        """面朝吧台时找饮品台：已经贴住吧台了，只沿着吧台往饮品台那头（左）滑，提示一出现就停。
        吧台就是边界，贴着它走不会出界；滑到头还没有就往反方向退一段再找一次，不做别的横移"""
        # 饮品台在吧台尽头的墙角，往那头滑到底会被墙挡住、不会出界；反方向（吧台另一头）滑过头会出吧台进庭院，
        # 所以只往饮品台方向滑，时长够走完整条吧台；卡住了也不跳（顶着墙角正常）
        if self.cfg.get("anchor_press_w", True):
            # 分段滑，每段之间轻点一下 W 贴回吧台：视角歪了也不会斜着滑离吧台、滑进右边的休息区。
            # 不能一直按着 W：到了墙角会把镜头挤歪
            seen, left = None, self.cfg["counter_sweep"] * self.cfg["lateral_factor"]
            while left > 0 and not seen:
                t = min(1.0, left)
                seen = self.g.hold(["d"], t, stop_on=("p_drink",), unstick=False)
                left -= t
                if not seen and left > 0:
                    seen = self.g.hold(["w"], 0.12, stop_on=("p_drink",), unstick=False)
        else:
            # 先用大厅定位看一眼离饮品台多远（贴着吧台时特征点很多、横向也准）：坐标常常偏了，
            # 人其实已经在饮品台旁边甚至左边，照旧整段往左滑就会滑过饮品台进凹角
            seen = "p_drink" if self._anchor_by_liangao() else None
            pre = self.g.frame960()  # 滑之前的画面：滑到饮品台用了几秒，就知道这里在饮品台右边多远（存作位置样本）
            dy = None if seen else self._orb_lateral()
            lat = float(self.cfg["hall_lat_speed"])
            key = "d"
            if seen:
                pass
            elif dy is None and not self.g.dish_icons() and not self.g.has_prompt("p_kitchen1"):
                # 定位认不出、画面上方也没有吧台上的菜：多半是半路被桌子/长凳挡住，误以为贴到了吧台
                # （实测从 4 号桌回来走了 0.8 秒就停，接着把桌子当吧台滑，一路滑丢）。退开、挪一步，再往吧台走到底，重新定位
                self.log("贴台定位认不出、也没看到吧台上的菜：多半被桌子挡住了，挪开再往吧台走")
                self.g.hold(["w"], 0.4, unstick=False)
                self.g.hold(["d"], 0.5, unstick=False)
                self.g.hold(["s"], 3.0, unstick=False, until_blocked=True)
                pre = self.g.frame960()
                dy = self._orb_lateral()
            if seen:
                pass  # 已经按凉糕挪到饮品台
            elif dy is not None and dy < -0.8:
                # 已经在饮品台左边（凹角那头）：直接往右会被饮品台柜子侧面挡住。先退回大厅一步，往右走过去，再贴回吧台
                # 注意：吧台最右头的货架和凹角长得很像，定位会把那头也认成「饮品台左边」，所以这里不按定位的距离走，
                # 只走固定的一小段，再看提示决定往哪边挪（看到「厨房一」= 走过了，往左；什么都没有 = 还在左边，往右）
                self.g.hold(["w"], 0.6, unstick=False)
                seen = self.g.hold(["a"], 1.0, stop_on=("p_drink", "p_kitchen1"), unstick=False)
                if not seen:
                    seen = self.g.hold(["s"], 1.0, stop_on=("p_drink", "p_kitchen1"), unstick=False, until_blocked=True)
                key = "d" if seen == "p_kitchen1" or self.g.has_prompt("p_kitchen1") else "a"
            elif dy is not None:
                seen = self._slide_drinkward(min(self.cfg["counter_sweep"] * self.cfg["lateral_factor"], dy / lat + 0.2))
                if seen == "p_drink" and self.cfg["save_places"] and not self.g.boost_active():
                    self._save_counter(pre, self.g.last_moved)
                for _ in range(2):
                    # 滑完还没到：定位常把吧台远端认近了（实测读 +8.7 滑完还差一大截，接着小步找不到就走丢）。再定位，还远就接着滑
                    if seen:
                        break
                    dy2 = self._orb_lateral()
                    if dy2 is None or dy2 < 1.0:
                        break
                    self.log(f"按定位滑完还没到饮品台，离饮品台还有 {dy2:+.1f}，接着滑")
                    seen = self._slide_drinkward(min(self.cfg["counter_sweep"] * self.cfg["lateral_factor"], dy2 / lat + 0.2))
            else:
                # 定位认不出：知道坐标就按坐标估（x 就是正常速度横走的秒数），不知道就最多滑 blind_sweep 秒。
                # 以前一口气滑 3.55 秒，比从吧台最右头滑过来还长，一定冲过饮品台进凹角
                exp = getattr(self, "_slide_exp", None)
                t = min(self.cfg["blind_sweep"], exp * 1.2 + 0.6) if exp is not None else self.cfg["blind_sweep"]
                seen = self._slide_drinkward(t)
                dy = 0.0  # 滑完没看到提示也要小步来回找
            if seen == "p_kitchen1" or (not seen and dy is not None):
                # 「厨房一」窗口就在饮品台旁边：贴得太近时这里只出「厨房一」提示，一口气滑就会滑过饮品台进凹角。
                # 按定位滑完还没看到提示也一样：改成小步挪，「饮品台」提示一出来就停
                # 「厨房一」和饮品台挨着，最多挪一小段（太远会直接挪进凹角）；这边没有就往反方向挪回来找
                seen = None
                for k2, steps in ((key, 6), ("a" if key == "d" else "d", 10)):
                    for _ in range(steps):
                        if self.g.has_prompt("p_drink"):
                            seen = "p_drink"
                            break
                        seen = self.g.hold([k2], 0.15, stop_on=("p_drink",), unstick=False)
                        if seen:
                            break
                    if seen:
                        break
            if seen:
                self._check_slow(self.g.last_moved)
        if seen:
            txt = self.g.prompt_text()
            # 饮品台旁边常同时有取菜提示（「界龙肉卷」），文字只读到它也照样算到了；读到厨房或什么字都没有才算认错
            if "饮" not in txt and not ("界龙" in txt and "厨房" not in txt):
                # 「饮品台」模板在吧台另一头也会误认（实测离饮品台 -4.8 处认成到了，饮品台面板打不开）：文字对不上就不算
                self.log(f"「饮品台」提示认错了（实际是「{txt}」），不算到了")
                seen = None
        if seen:
            self._at("饮品台")
            self.at_drink = True
            self.set_origin()
            self.anchor_fails = 0
            self.log("已到饮品台（锚点）")
            self.g.trace("饮品台")
            return True
        self.log("沿吧台滑到头也没看到「饮品台」提示")
        # 多半是滑过了头（饮品台被店员挡住没出提示，或视角歪了斜着滑过去）：先原路往回滑一段找
        back = float(self.cfg.get("counter_back", 1.5))
        if back > 0 and self.g.hold(["a"], back, stop_on=("p_drink",), unstick=False):
            self._at("饮品台")
            self.at_drink = True
            self.set_origin()
            self.anchor_fails = 0
            self.log("往回滑找到了饮品台（锚点）")
            return True
        if not getattr(self, "_retracing", False):
            # 可能没贴到吧台（被桌子/客人挡住）：校正视角，按最远的距离往吧台走到顶住，再滑一次
            self._retracing = True
            try:
                self.ensure_facing()  # 已经出问题了才查朝向（镜头转反了的话先转回来）
                if self.orb_recover():
                    return True
                self._fix_yaw_in_hall()
                self.pos = None
                self.depth = 3.0
                self.pin_counter()
                ok = self._anchor_along_counter()
            finally:
                self._retracing = False
            if ok:
                return True
            # 重试也失败：算一次「找不到饮品台」，连续 lost_after 次就放弃这一局
            self.anchor_fails += 1
            if self.anchor_fails >= int(self.cfg["lost_after"]) and self.g.watch_over:
                self.maybe_lost()
            return False
        self.log("没找到「饮品台」提示，锚点校正失败")
        self.g.trace("饮品台失败")
        self._at("未知")
        if not getattr(self, "_retracing", False):
            self.anchor_fails += 1
            if self.anchor_fails >= int(self.cfg["lost_after"]) and self.g.watch_over:
                self.maybe_lost()
        return False

    def survey_biz(self, rounds=1):
        """营业时采一套干净的位置样本（test_channel/places_clean）：不接待、不按 F（镜头不会被带歪），
        每次从饮品台出发（先确认镜头正对），走到每张桌和那一排的过道各拍一张「正对」，再原地转 180° 拍一张「背对」当反例"""
        out = os.path.join(BASE_DIR, "test_channel", "places_clean")
        os.makedirs(out, exist_ok=True)
        flip = int(180 * self.cfg["look_px_per_deg"])

        def snap(label):
            time.sleep(0.4)
            name = f"{label}_{time.strftime('%m%d_%H%M%S')}_{int(time.time() * 10) % 10}.jpg"
            cv2.imencode(".jpg", self.g.frame960(), [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(os.path.join(out, name))

        def pair(label):
            snap(label)
            self.g.look(dx=flip)
            snap(label + "_away")
            self.g.look(dx=-flip)
            time.sleep(0.3)

        n_ok = 0
        for _ in range(rounds):
            for n in (1, 2, 3, 4, 5, 6):
                if not self.anchor():
                    self.log(f"采样：回不到饮品台，跳过 {n} 号桌")
                    continue
                if not self.drink_yaw_check():
                    self.log("采样：饮品台前镜头确认不了是正对的，这一趟不拍")
                    continue
                pair("drink")
                self.target = n
                if not self.go_table(n):
                    continue
                pair(f"table{n}")
                self.back_to_aisle()
                self._fix_yaw_in_hall()
                pair(f"aisle{TABLES[n][1] + 1}")
                n_ok += 1
        self.log(f"采样完成：{n_ok} 个桌位，存到 {out}")

    # ================= 新寻路：王字节点（用户 11-03 演示） =================
    WN_DEFAULTS = {"a0_gain": 0.0, "t65": 0.85, "DH": 1.15, "row1": 1.07, "row2": 1.1, "row3": 1.15,
                   "lat1": 0.45, "lat2": 0.45, "lat3": 0.45, "lat4": 0.45, "lat5": 0.45, "lat6": 0.45}

    def wn_p(self, key):
        """学到的各段时长（test_channel/wang_params.json），没有就用演示录像里量的"""
        if not hasattr(self, "_wn_params"):
            try:
                with open(os.path.join(BASE_DIR, "test_channel", "wang_params.json"), encoding="utf-8") as f:
                    self._wn_params = json.load(f)
            except (OSError, ValueError):
                self._wn_params = {}
        return float(self._wn_params.get(key, self.WN_DEFAULTS[key]))

    def wn_learn(self, key, val, lo, hi, a=0.3):
        """这一趟实际量到的值往学到的值里掺一点（每次只挪 30%，一次走偏不会带歪）"""
        old = self.wn_p(key)
        new = round(min(hi, max(lo, old + (val - old) * a)), 3)
        self._wn_params[key] = new
        try:
            with open(os.path.join(BASE_DIR, "test_channel", "wang_params.json"), "w", encoding="utf-8") as f:
                json.dump(self._wn_params, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        if abs(new - old) >= 0.01:
            self.log(f"  学习：{key} {old:.2f} → {new:.2f}（这趟 {val:.2f}）")

    def wn_refs(self):
        """各节点的参考图特征。遮罩：大厅定位的界面遮罩 + F 提示框 + 右边进度条（不随视角平移，会把估计拉向 0）"""
        if hasattr(self, "_wn_refs"):
            return self._wn_refs
        self._wn_refs = {}
        self._wn_orb = cv2.ORB_create(1000)
        self._wn_bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        try:
            import hall_locator
            m = hall_locator.MASK.astype(np.uint8) * 255
        except ImportError:
            m = np.full((540, 960), 255, np.uint8)
        m = m.copy()
        m[295:415, 585:865] = 0
        m[265:385, 815:960] = 0
        self._wn_mask = m
        d = os.path.join(BASE_DIR, "test_channel", "nodes")
        try:
            for line in open(os.path.join(d, "index.jsonl"), encoding="utf-8"):
                r = json.loads(line)
                g = cv2.cvtColor(cv2.imdecode(np.fromfile(os.path.join(d, r["file"]), dtype=np.uint8), cv2.IMREAD_COLOR),
                                 cv2.COLOR_BGR2GRAY)
                k, des = self._wn_orb.detectAndCompute(g, m)
                if des is not None and len(k) >= 50:
                    self._wn_refs.setdefault(r["node"], []).append((np.float32([q.pt for q in k]), des))
        except (OSError, ValueError, cv2.error) as e:
            self.log(f"节点参考图载入失败：{e}")
        self.log(f"新寻路：载入节点参考图 {sum(len(v) for v in self._wn_refs.values())} 张（{'、'.join(self._wn_refs)}）")
        return self._wn_refs

    def wn_match(self, node, img=None):
        """和 node 的参考图比：返回 (视角歪了几度, 对上的点数)，对不上返回 (None, 0)。
        用仿射变换算「画面正中」平移了多少（前后站得远近不同只会让画面放大缩小，不影响正中的平移）"""
        refs = self.wn_refs().get(node)
        if not refs:
            return None, 0
        img = self.g.frame960() if img is None else img
        k, des = self._wn_orb.detectAndCompute(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), self._wn_mask)
        if des is None or len(k) < 50:
            return None, 0
        pts = np.float32([q.pt for q in k])
        best = (None, 0)
        for kp2, d2 in refs:
            try:
                ms = self._wn_bf.knnMatch(d2, des, k=2)
            except cv2.error:
                continue
            good = [a for a, b in (x for x in ms if len(x) == 2) if a.distance < 0.75 * b.distance]
            if len(good) < 20:
                continue
            src = kp2[[a.queryIdx for a in good]]
            dst = pts[[a.trainIdx for a in good]]
            M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6)
            if M is None:
                continue
            n_in = int(inl.sum())
            if n_in > best[1]:
                xc = M[0, 0] * 480 + M[0, 1] * 270 + M[0, 2] - 480
                ppd = 5.3 if node in ("D", "H") else 6.8
                best = (round(-xc / ppd, 1), n_in)
        return best

    def wn_vp(self, img=None):
        """视角读数：地面上往远处收拢的斜线（拼缝、桌子长边、过道）交在消失点，转镜头它就左右移，人横着站偏了它不动。
        返回消失点的 x 或 None。按交点的 x 投票（长线权重大）取峰，只用地平线高度附近（y 150~265）的交点。
        实测（960 宽）：每转 1° 移 7.9 像素；61 张已知角度的照片读出 60 张，偏差超过 5° 的 4 张"""
        img = self.g.frame960() if img is None else img
        e = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 50, 140)
        e[:260] = 0
        e[470:] = 0
        e[295:415, 585:865] = 0
        e[265:385, 815:960] = 0
        L = cv2.HoughLinesP(e, 1, np.pi / 360, 50, minLineLength=40, maxLineGap=8)
        if L is None:
            return None
        H, W = [], []
        for x1, y1, x2, y2 in L.reshape(-1, 4):
            a = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) % 180
            if 15 <= min(a, 180 - a) <= 75:
                H.append(np.cross([x1, y1, 1.0], [x2, y2, 1.0]))
                W.append(float(np.hypot(x2 - x1, y2 - y1)))
        xs, ws = [], []
        for i in range(len(H)):
            for j in range(i + 1, len(H)):
                q = np.cross(H[i], H[j])
                if abs(q[2]) > 1e-6:
                    x, y = q[0] / q[2], q[1] / q[2]
                    if -200 < x < 1200 and 150 < y < 265:
                        xs.append(x)
                        ws.append(W[i] * W[j])
        if len(xs) < 3:
            return None
        xs, ws = np.array(xs), np.array(ws)
        hist, edges = np.histogram(xs, bins=np.arange(-200, 1220, 20), weights=ws)
        k = int(np.argmax(hist))
        c = (edges[k] + edges[k + 1]) / 2
        near = np.abs(xs - c) < 40
        return float(np.average(xs[near], weights=ws[near]))

    def wn_edge(self, img=None):
        """离吧台一小步（A0）时的视角读数：画面下半部吧台底边（长横线）的倾斜。镜头一转，地平线以下的横线就会倾斜，
        倾斜量和它离地平线的高度成正比：按 斜率 × 焦距 / (y − 地平线) 换成「度」（比例和正负号在 A0 第一趟实测标定）"""
        img = self.g.frame960() if img is None else img
        e = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 50, 140)
        e[:290] = 0
        e[455:] = 0
        e[265:385, 815:960] = 0
        L = cv2.HoughLinesP(e, 1, np.pi / 720, 80, minLineLength=220, maxLineGap=10)
        if L is None:
            return None
        vals, ws = [], []
        for x1, y1, x2, y2 in L.reshape(-1, 4):
            if x2 == x1:
                continue
            sl = (y2 - y1) / (x2 - x1)
            if abs(sl) > 0.25:
                continue
            ym = (y1 + y2) / 2 - 195.0
            if ym < 60:
                continue
            vals.append(np.degrees(np.arctan(sl * 390.0 / ym)))
            ws.append(float(np.hypot(x2 - x1, y2 - y1)))
        if not vals:
            return None
        o = np.argsort(vals)
        cw = np.cumsum(np.array(ws)[o])
        return float(np.array(vals)[o][np.searchsorted(cw, cw[-1] / 2)])  # 按长度加权的中位数

    def wn_yaw(self, node=None):
        """视角偏了几度（正 = 往右偏了）。连读两帧，两次差太多就当读不准，返回 None。
        正对时消失点的 x：大厅里 492、饮品台前 460（用户演示画面量的）；吧台中间没有往远处收的线，量不了"""
        if node == "H":
            return None
        if node == "A0":
            a = self.wn_edge()
            time.sleep(0.05)
            b = self.wn_edge()
            if a is None or b is None or abs(a - b) > 3:
                return None
            g = self.wn_p("a0_gain")
            z = float(self.cfg.get("a0_zero", 2.3))  # 正对时的读数（用户演示里贴着吧台的几张画面量的中位数）
            return round(g * ((a + b) / 2 - z), 1) if g else None
        c = float(self.cfg.get("vp_center_d", 460) if node == "D" else self.cfg.get("vp_center", 492))
        ppd = float(self.cfg.get("vp_px_per_deg", 7.9))
        a = self.wn_vp()
        time.sleep(0.05)
        b = self.wn_vp()
        if a is None or b is None or abs(a - b) > 25:
            return None
        return round((c - (a + b) / 2) / ppd, 1)

    def wn_straighten(self, node, rec=None):
        """在 node 摆正视角（看消失点）；转完再看一次，没变正就转回原样。
        同时和这个节点的参考图比一下，确认真的到了这里（对上的点数记进日志）。返回对上的点数"""
        k = self.cfg["look_px_per_deg"]
        n_in = self.wn_match(node)[1] if self.cfg.get("wn_check_refs") else None  # 和参考图比要 0.1~0.2 秒，只在查问题时开
        deg = self.wn_yaw(node)
        turned = 0.0
        for _ in range(2):
            if deg is None or abs(deg) <= 1.5:
                break
            step = max(-30.0, min(30.0, deg))
            self.g.look(dx=int(-step * k))
            if abs(step) <= 3.0:
                turned += step  # 小角度转完不再复查（v09 提速：消失点 1~2° 准，小转不会转反）
                deg = 0.0
                break
            time.sleep(0.12)
            d2 = self.wn_yaw(node)
            if d2 is None or abs(d2) >= abs(deg):
                self.g.look(dx=int(step * k))
                self.log(f"  [{node}] 消失点说偏了 {deg:+.1f}°，转完没变正（{d2}），转回原样")
                break
            turned += step
            deg = d2
        if rec is not None:
            rec.setdefault("nodes", []).append({"node": node, "inl": n_in, "deg": deg, "turned": round(turned, 1)})
        if turned:
            self.log(f"  [{node}] 摆正视角 {turned:+.1f}°（剩 {deg:+.1f}°）")
        return n_in

    def wn_slide(self, key, t, stop_on=()):
        """沿吧台横走（实际按键）：往饮品台（A）视角先往右偏、往外（D）往左偏，走完转回（用户演示：这样一定贴着吧台）"""
        deg = float(self.cfg.get("counter_bias_deg", 10) or 0)
        px = int(deg * self.cfg["look_px_per_deg"]) * (1 if key == "a" else -1)
        self.g.look(dx=px)
        try:
            return self.g._hold([key], t, stop_on=stop_on, unstick=False)
        finally:
            self.g.look(dx=-px)

    def wn_pin(self, t=4.0, expect=0.0):
        """按 W 往吧台走，顶住就停。expect：至少该走这么久才到吧台；走得明显比这短就是半路被桌子/长凳挡住了
        （实测 3 号桌没到位、退回时人其实不在过道里，往吧台走 1 秒就撞上第一排桌子，回不到饮品台算走丢）：
        退一步往右挪再走，还不行往左挪过去再走"""
        self.g._hold(["w"], t, unstick=False, until_blocked=True)
        if expect <= 0 and t >= 2 and self.cfg.get("wn_pin_repush", True):
            # v48：满堂彩时过道里进来的客人多，往吧台走常被客人挡住（08:00 从 5 号桌回来走 1 秒就停，之后沿「吧台」滑全落空）：
            # 还没看到吧台的提示就等一下再往前推，最多两次（真到了吧台再推也走不动，不费事）
            for _ in range(2):
                if self.g.last_moved >= 2.0 or any(self.g.has_prompt(k) for k in ("p_drink", "p_take")):
                    break
                time.sleep(0.6)
                self.g._hold(["w"], t, unstick=False, until_blocked=True)
                if self.g.last_moved < 0.25:
                    break  # 推不动：真的顶到了（吧台或桌椅）
        if expect <= 0 or self.g.last_moved >= expect * 0.6:
            return
        for side, dt in (("d", 0.35), ("a", 0.7)):
            self.log(f"往吧台走了 {self.g.last_moved:.1f} 秒就被挡住（该走约 {expect:.1f} 秒）：退一步往{'右' if side == 'd' else '左'}挪再走")
            self.g._hold(["s"], 0.3, unstick=False)
            self.g._hold([side], dt, unstick=False)
            self.g._hold(["w"], t, unstick=False, until_blocked=True)
            if self.g.last_moved >= max(0.3, expect * 0.6 - 0.3):
                return

    def wn_pin_to_D(self, rec=None):
        """往吧台走到底、贴着滑回饮品台；被挡住（过道里有客人等）就往后退一点再试一次"""
        if getattr(self, "_fresh_start", False) and time.time() - getattr(self, "_fresh_t", 0) < 40:
            # v47：开局后第一次往吧台走之前先查朝向（四次开局走丢都是第一次「往吧台走」走满 4 秒没被挡住：镜头没对着吧台）
            self._fresh_start = False
            if getattr(self, "_start_at_D", False):
                # v59：开局时人就在饮品台前、之后只开过料理界面没走动：做完菜后提示偶尔晚出来/认不出，
                # 以前当成「不在饮品台」往左滑，饮品台是吧台最左端，一滑就进了左边墙角（14:23 等多次开局走丢）
                self._start_at_D = False
                ok = self._wait(lambda: self.g.has_prompt("p_drink"), 2.5, step=0.1)
                self.log("开局后第一次回饮品台：开局时就在饮品台前，" + ("看到提示了" if ok else "没看到提示也不推不滑，当作在饮品台"))
                self.wn_at = "D"
                self.at_drink = True
                return True
            if not self.g.has_prompt("p_drink"):
                self.ensure_facing(force=True)
            # v49：开局时人本来就贴着吧台（正常局往吧台走 0.5 秒就顶住）。按住 W 4 秒时镜头稍微偏一点就会顺着吧台边往左滑，
            # 饮品台是最左端，一滑就进了左边走廊（五次开局走丢都是第一次往吧台走满 4 秒）：开局这次只推 0.8 秒
            self.wn_pin(t=0.8)
        else:
            self.wn_pin()
        if self.wn_to_D(rec):
            return True
        self.log("回饮品台没成功：退一步再试一次")
        self.wn_snap("D_fail1")  # 查走丢用：第一次没回到饮品台时面朝哪
        self.g._hold(["s"], 0.4, unstick=False)
        self.wn_pin()
        ok = self.wn_to_D(rec)
        if not ok:
            self.wn_snap("D_fail2")
            if self.cfg.get("wn_turn180_retry", True) and not self.g.boost_active():
                # v28：两次走丢截图都是背对吧台、面朝大厅后墙（视角转反了 180°，多在开局做完菜后）：转回来再试一次
                self.g.look(dx=int(180 * self.cfg["look_px_per_deg"]))
                time.sleep(0.4)
                self.wn_pin()
                ok = self.wn_to_D(rec)
                self.log("回饮品台：转 180° 后" + ("回到了饮品台" if ok else "还是没回去"))
                if not ok:
                    self.wn_snap("D_fail3")
        return ok

    def wn_to_D(self, rec=None):
        """贴着吧台回饮品台：看得到凉糕就按凉糕位置挪，否则视角右偏按 A 滑，「饮品台」提示出来就停"""
        t0 = time.time()
        # 刚关掉面板/刚停下时提示可能晚一点才出来：先等一下再看（凉糕在饮品台前的画面位置 655~818 不固定，不拿它定位：
        # 实测按凉糕「还远」往左挪，直接挪进了饮品台旁边的凹角）
        # v45：刚关料理界面（开局那次人本来就站在饮品台前）提示要晚一点才出来：等 1.2 秒再滑
        #（01:50、07:11 两次开局只等 0.2 秒就往左滑，滑进饮品台左边的墙角算走丢）
        wait = 1.2 if time.time() - self.last_cook < 4 else 0.2
        ok = self._wait(lambda: self.g.has_prompt("p_drink"), wait, step=0.1)
        if not ok and self.g.boost_active():
            # 满堂彩加速（1.7 倍速）时一口气滑容易错过提示冲进凹角：分小段滑，每段停下看一次
            for _ in range(10):
                if self.wn_slide("a", 0.3, stop_on=("p_drink",)) or self.g.has_prompt("p_drink"):
                    ok = True
                    break
        if not ok:
            ok = bool(self.wn_slide("a", 2.6, stop_on=("p_drink",))) or self.g.has_prompt("p_drink")
        if not ok:
            # 滑到头也没看到：可能滑过了（进了凹角）。往回走一点再小步找
            self.wn_slide("d", 0.6)
            ok = bool(self.wn_slide("a", 1.2, stop_on=("p_drink",))) or self.g.has_prompt("p_drink")
        if not ok and self.cfg.get("wn_d_sweep", True):
            # v46：往左滑都没找到，多半人已经在饮品台左边的凹角/走廊里（07:28、06:49 加速中从 3 号桌回来都停在左墙边）：往右扫一段
            ok = bool(self.wn_slide("d", 2.4, stop_on=("p_drink",))) or self.g.has_prompt("p_drink")
            if ok:
                self.log("往右扫回到了饮品台")
        self.wn_at = "D" if ok else None
        if rec is not None:
            rec["to_D"] = round(time.time() - t0, 1)
            rec["D_ok"] = ok
        if ok:
            self.at_drink = True
        return ok

    def wn_table_side(self, n):
        """面朝吧台时单号桌（1、3、5）在画面左边，双号在右边（演示录像）"""
        return "a" if n % 2 == 1 else "d"

    def wn_go_table(self, n, rec, from_h=False):
        """D → H → A1..A排 → 横走到 n 号桌标准站位（桌号牌在演示里的位置）。返回是否到了。
        from_h：已经贴着吧台站在 H（中间过道口）了，不用再从饮品台滑过去"""
        row = TABLES[n][1]
        if not from_h:
            self.wn_slide("d", self.wn_p("DH"))
        self.at_drink = False
        # 往外走之前先摆正（用户）：贴着吧台量不了（没有往远处收的线），先离开吧台一小步再看消失点
        self.g._hold(["s"], 0.35, unstick=False)
        if not self.wn_p("a0_gain") and not getattr(self, "_a0_cal_try", False):
            self._a0_cal_try = True
            k = self.cfg["look_px_per_deg"]
            r0 = self.wn_edge()
            self.g.look(dx=int(8 * k))
            time.sleep(0.15)
            r1 = self.wn_edge()
            self.g.look(dx=int(-8 * k))
            time.sleep(0.15)
            rec["a0_cal"] = [r0, r1]
            if r0 is not None and r1 is not None and abs(r1 - r0) > 1.0:
                gain = round(8.0 / (r1 - r0), 3)  # 往右转 8° 读数变了多少：读数 × gain = 往右偏的度数
                self._wn_params["a0_gain"] = gain
                self.wn_learn("a0_gain", gain, -20, 20, a=1.0)
                self.log(f"  [A0] 标定：吧台底边读数 {r0:.1f} → 右转 8° 后 {r1:.1f}，比例 {gain}")
            else:
                self.log(f"  [A0] 标定失败：读数 {r0} / {r1}")
        if self.cfg.get("debug_files") and not getattr(self, "_a0_saved", False):
            self._a0_saved = True
            try:
                d = os.path.join(BASE_DIR, "test_channel", "wang_fail")
                os.makedirs(d, exist_ok=True)
                cv2.imencode(".jpg", self.g.frame960())[1].tofile(os.path.join(d, f"A0_{time.strftime('%H%M%S')}.jpg"))
            except Exception:
                pass
        self.wn_straighten("A0", rec)
        a0 = rec["nodes"][-1] if rec.get("nodes") else {}
        need = a0.get("deg") is None  # A0 量不到视角时，第一排再摆正一次
        for r in range(row + 1):
            plan = self.wn_p(f"row{r + 1}") - (0.35 if r == 0 else 0.0)
            self.g._hold(["s"], plan, unstick=False)
            mult = self.g.cur_mult or 1.0
            moved = self.g.last_moved * mult  # last_moved 是真实按键秒数，换算成平时速度的秒数再和 plan 比
            for _ in range(2):
                # v54：沿过道往下走被挡住提前停了（满堂彩时过道里客人多）：等一下把剩下的走完。
                # 以前不查，停在上一排就横着找桌号牌，一直找到墙边（今天 5 次去 3 号桌没到位、走丢）
                if moved >= plan * 0.85 or self.g.abort_walk:
                    break
                self.log(f"  [A{r + 1}] 往下走了 {moved:.2f}/{plan:.2f} 秒就被挡住：等一下接着走")
                time.sleep(0.5)
                self.g._hold(["s"], plan - moved, unstick=False)
                moved += self.g.last_moved * (self.g.cur_mult or 1.0)
            # 提速：离吧台一小步（A0）已经摆正过，中间几排不停；要横走的那一排一定摆正（摆正了横走一定能到）
            if r == row or need:
                self.wn_straighten(f"A{r + 1}", rec)
                q = rec["nodes"][-1]
                need = abs(q["turned"]) > 2 or (q["deg"] is not None and abs(q["deg"]) > 2)
        return self.wn_from_aisle(n, rec)

    def wn_from_aisle(self, n, rec):
        """已经在 n 号桌那一排的过道、视角摆正了：横走到站位"""
        if n == 5 and self.cfg.get("wn_5_via_6", True):
            # 5 号桌从过道直接横过去，镜头会撞上身后的柱子、看不到牌子：先到最稳的 6 号桌站位，摆正，再往左平移（用户提议；
            # 演示里 6 号到 5 号站位横走约 0.85 秒）
            rec["via6"] = self.wn_to_stance(6, rec)
            self.wn_straighten("T6", rec)
            return self.wn_to_stance(5, rec, base_lat=self.wn_lat, base_dep=self.wn_dep, first_t=self.wn_p("t65"))
        return self.wn_to_stance(n, rec)

    def wn_to_stance(self, n, rec, base_lat=0.0, base_dep=0.0, first_t=None):
        """从本排过道横走到 n 号桌的标准站位：先走学到的时长，再按桌号牌位置一点点往桌子那边靠（左右、前后）。
        base_lat/base_dep：从别的桌站位接着走时（5 号经 6 号），离过道已经横走/前后走了多少；first_t：先走的时长"""
        side = self.wn_table_side(n)
        ref = self.cfg["sign_ref_fc"][str(n)]
        tol, kk = self.cfg["servo_fc_tol"], self.cfg["servo_fc_k"]
        lat, dep = base_lat, base_dep  # 离过道：实际 D 为正的横向秒数、实际 S 为正的前后秒数（退回过道时原样退回）
        t = first_t if first_t is not None else self.wn_p(f"lat{n}")
        self.g._hold([side], t, unstick=False)
        lat += t if side == "d" else -t
        ok, steps, trail, miss = False, 0, [], 0
        searched_back = False
        prev = None
        self.g.settle = 0.06  # 微调每步只挪一点，不用等太久（每步省 0.09 秒）
        sgn = 1 if side == "d" else -1  # 往桌子那边走时牌子在画面里往反方向移：右边的桌（D）牌子往左
        for steps in range(1, 15):
            if (self.cfg.get("servo_prompt_stop", True) and steps > 1 and base_lat == 0.0 and abs(lat) >= 0.9 * t
                    and any(self.g.has_prompt(k) for k in TABLE_PROMPTS)):
                # v75：横移已经接近学到的距离、桌边提示也出来了：就是到了，别再按牌子挪
                # （11-09 多次「没调到标准站位 14 步、提示 接待」，牌子认偏了一直挪，每次多花 3~5 秒）
                ok = True
                rec["by_prompt"] = True
                break
            pos = None
            for _ in range(3):  # 牌子偶尔有一帧认不出：先重读，别当成「还没看到」接着走（实测一走就冲过头）
                pos = self.g.sign_pos(n)
                if pos and not 190 <= pos[1] <= 330:
                    pos = None  # 高处的牌子是远处看到的，不算
                if pos:
                    break
                time.sleep(0.08)
            trail.append(pos[:2] if pos else None)
            if not pos:
                if any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
                    ok = True  # 牌子被客人/气泡挡住，但已经出了接待/上菜提示：就是到了
                    rec["by_prompt"] = True
                    break
                miss += 1
                if self.cfg.get("boost_sign_back", True) and miss >= 2 and not searched_back:
                    # v65：不只加速期——16:13 平时去 1 号桌也冲过了牌子、横移到上限，回不了饮品台
                    # v58：满堂彩加速时停步会多滑一段，常常已经冲过了桌号牌（牌子在身后），再往前找只会走到墙边
                    #（今天「没调到站位」13 次里多数在按 R 后 12~67 秒、3/5 号桌，之后回不到饮品台）：先往回小步找
                    searched_back = True
                    back = "a" if side == "d" else "d"
                    for _b in range(4):
                        self.g._hold([back], 0.1, unstick=False)
                        lat -= 0.1 * sgn
                        if self.g.sign_pos(n) or any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
                            break
                    if self.g.sign_pos(n) or any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
                        miss = 0
                        continue
                    break  # 往回也没有：放弃，按记的横移量退回过道，不再往墙那边走
                if miss > 5 or abs(lat - base_lat) > t + 0.4:
                    break  # 找不到牌子也别一直横着走（实测 6 号桌走了 1.28 秒钻进右边走廊，镜头被墙带转了 90°）
                self.g._hold([side], 0.1, unstick=False)
                lat += 0.1 * sgn
                continue
            ex, ey = pos[0] - ref[0], pos[1] - ref[1]
            if prev is not None and abs(pos[0] - prev[0]) < 5 and abs(ex) <= 2 * tol and abs(ey) <= 2 * tol:
                ok = True  # 往桌子那边推不动了（被桌椅/坐着的客人挡住，实测 4 号桌牌子卡在 x≈600 不动），只差一点就算到了
                break
            prev = pos
            left = ex * sgn  # 还差多少像素（>0 = 还没到）：往右走（D）牌子往左移，牌子在目标右边就是还没到；往左走反过来
            if left > tol:
                t = min(0.15, max(0.04, left * 0.0007))
                self.g._hold([side], t, unstick=False)
                lat += t * sgn
                continue
            if left < -2 * tol:
                # 冲过头了：往回退一小步
                back = "a" if side == "d" else "d"
                t = min(0.12, max(0.04, -left * 0.0007))
                self.g._hold([back], t, unstick=False)
                lat -= t * sgn
                continue
            if abs(ey) > tol and not (ey > 0 and dep >= 0.15) and abs(dep) < 0.3:
                key, t = ("s" if ey > 0 else "w"), min(0.15, max(0.04, abs(ey) * 0.0012))
                self.g._hold([key], t, unstick=False)
                dep += t if key == "s" else -t
                continue
            ok = True
            break
        if ok and not any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
            for _ in range(4):
                self.g._hold([side], 0.06, unstick=False)
                lat += 0.06 * sgn
                if any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
                    rec["nudge"] = True
                    break
        if (self.cfg.get("wn_nudge_dep", True) and not any(self.g.has_prompt(k) for k in TABLE_PROMPTS)
                and abs(lat - base_lat) > 0.1):
            # v14：横着挪了还没提示（到了桌边却「提示：无」，每次白跑一个来回 ~20 秒）：前后各试一小步
            for key, t_ in (("w", 0.08), ("s", 0.16), ("w", 0.08)):
                self.g._hold([key], t_, unstick=False)
                dep += t_ if key == "s" else -t_
                if any(self.g.has_prompt(k) for k in TABLE_PROMPTS):
                    rec["nudge_dep"] = key
                    break
        if (not ok and self.cfg.get("wn_row_fix", True) and not rec.get("row_fix") and base_lat == 0.0
                and not any(self.g.has_prompt(k) for k in TABLE_PROMPTS)):
            # v69：没找到这桌的牌子，却看到同一侧相邻排的牌子（11-09 11:14 加速刚开始去 6 号桌，停在 3/4 排、4 号牌就在旁边）：
            # 少走/多走了一排。退回过道、补走一排再找（以前往回小步找不到就回饮品台重来，白跑 17 秒）
            for m, key, why in ((n - 2, "s", "少走了一排"), (n + 2, "w", "多走了一排")):
                if m not in TABLES:
                    continue
                p2 = self.g.sign_pos(m)
                if not (p2 and 190 <= p2[1] <= 330):
                    continue
                rec["row_fix"] = why
                self.log(f"  [T{n}] 没看到 {n} 号牌、看到 {m} 号牌：{why}，退回过道补一排再找")
                back = "a" if side == "d" else "d"
                if abs(lat) > 0.02:
                    self.g._hold([back if lat * sgn > 0 else side], abs(lat), unstick=False)
                if abs(dep) > 0.02:
                    self.g._hold(["w" if dep > 0 else "s"], abs(dep), unstick=False)
                r = TABLES[n][1]
                gap = self.wn_p(f"row{r + 1}") if key == "s" else self.wn_p(f"row{r + 2}")
                self.g._hold([key], gap, unstick=False)
                self.g.settle = float(self.cfg.get("settle", 0.10))
                return self.wn_to_stance(n, rec)
        self.g.settle = float(self.cfg.get("settle", 0.10))
        self.wn_lat, self.wn_dep = lat, dep
        # v61：站位没对上、横移又异常大（镜头多半偏了）：记下来，往回走前先查朝向（14:45 去 1 号桌往右横移 1.11 秒，回程走丢）
        self._stance_odd = (not ok) and abs(lat - base_lat) > 0.95
        rec["trail"] = trail
        if not ok and self.cfg.get("debug_files"):
            try:
                d = os.path.join(BASE_DIR, "test_channel", "wang_fail")
                os.makedirs(d, exist_ok=True)
                cv2.imencode(".jpg", self.g.frame960())[1].tofile(os.path.join(d, f"T{n}_{time.strftime('%H%M%S')}.jpg"))
            except Exception:
                pass
        p = self.g.prompt()
        rec.update({"stance_ok": ok, "servo_steps": steps, "lat": round(lat, 2), "dep": round(dep, 2),
                    "prompt": p, "sign": self.g.sign_pos(n)})
        # 只从干净的一趟学（看到了牌子、3 步内调好）；范围收窄（实测靠提示/推不动算到位的那几趟把 6 号学到 0.78，接着就冲过头）
        clean = ok and not rec.get("by_prompt") and steps <= 3
        if clean and first_t is not None:
            self.wn_learn("t65", abs(lat - base_lat), 0.65, 1.0)
        elif clean:
            self.wn_learn(f"lat{n}", abs(lat), 0.15, 0.6)
            if abs(dep) >= 0.05:
                key = f"row{TABLES[n][1] + 1}"
                self.wn_learn(key, self.wn_p(key) + dep, 0.6, 1.8, a=0.2)
        self.wn_at = f"T{n}"
        self.at_table = n
        self.log(f"  [T{n}] {'到了标准站位' if ok else '没调到标准站位'}（横 {lat:+.2f}s 前后 {dep:+.2f}s，{steps} 步，提示 {PROMPTS.get(p, '无')}）")
        return ok

    def wn_back(self, n, rec):
        """从桌边原样退回本排过道、摆正，再按 W 走回吧台、回饮品台"""
        lat, dep = getattr(self, "wn_lat", 0.0), getattr(self, "wn_dep", 0.0)
        if abs(lat) > 0.02:
            self.g._hold(["a" if lat > 0 else "d"], abs(lat), unstick=False)
        if abs(dep) > 0.02:
            self.g._hold(["w" if dep > 0 else "s"], abs(dep), unstick=False)
        row = TABLES[n][1]
        if getattr(self, "_stance_odd", False) and not self.g.boost_active():
            self._stance_odd = False
            self.log("刚才站位异常（横移过大）：往回走前先查镜头朝向")
            self.ensure_facing(force=True)
        self.wn_straighten(f"A{row + 1}", rec)  # 往回走之前先摆正（用户：最后走丢就是视角偏了）
        total = sum(self.wn_p(f"row{r + 1}") for r in range(row + 1))
        self.wn_pin(total * 1.3 + 0.8, expect=total)
        self.at_table = None
        if self.wn_to_D(rec):
            return True
        self.log("回饮品台没成功：退一步再试一次")
        self.wn_snap("D_fail1")  # 查走丢用：第一次没回到饮品台时面朝哪
        self.g._hold(["s"], 0.4, unstick=False)
        self.wn_pin()
        ok = self.wn_to_D(rec)
        if not ok:
            self.wn_snap("D_fail2")
            if self.cfg.get("wn_turn180_retry", True) and not self.g.boost_active():
                # v28：两次走丢截图都是背对吧台、面朝大厅后墙（视角转反了 180°，多在开局做完菜后）：转回来再试一次
                self.g.look(dx=int(180 * self.cfg["look_px_per_deg"]))
                time.sleep(0.4)
                self.wn_pin()
                ok = self.wn_to_D(rec)
                self.log("回饮品台：转 180° 后" + ("回到了饮品台" if ok else "还是没回去"))
                if not ok:
                    self.wn_snap("D_fail3")
        return ok

    def wn_table_to_table(self, n, rec):
        """从当前桌站位去 n 号桌：原样退回本排过道、摆正，沿过道前后走到那一排、摆正，再横走到站位"""
        cur = self.at_table
        if getattr(self, "_stance_odd", False):
            # v61：上一桌站位异常：不桌到桌，先稳稳回饮品台（wn_back 里会先查朝向）
            if not self.wn_back(cur, rec):
                self.wn_lost()
            self.at_table = None
            return self.wn_go_table(n, rec)
        lat, dep = getattr(self, "wn_lat", 0.0), getattr(self, "wn_dep", 0.0)
        self.wn_snap(f"TT{cur}{n}_0")  # 查问题用：桌到桌出发前的画面（实测在桌边做过一次菜后，下一次桌到桌会走反）
        if abs(lat) > 0.02:
            self.g._hold(["a" if lat > 0 else "d"], abs(lat), unstick=False)
        if abs(dep) > 0.02:
            self.g._hold(["w" if dep > 0 else "s"], abs(dep), unstick=False)
        self.wn_lat = self.wn_dep = 0.0
        r0, r1 = TABLES[cur][1], TABLES[n][1]
        self.wn_straighten(f"A{r0 + 1}", rec)
        # 核对真的退回了过道：从过道中间看，刚才那桌的牌子在画面边上（单号桌在左边 x<330，双号桌在右边 x>630，演示画面量的）
        def side_ok(m):
            q = self.g.sign_pos(m)
            # 牌子在画面对应的那半边就算对（第三排看到的牌子离中间更近：实测 5 号牌在 x≈400、6 号在 x≈588，原来按 330/630 卡误判）
            return q, bool(q) and (q[0] < 470 if m % 2 == 1 else q[0] > 490)
        pos, good = side_ok(cur)
        if not good:
            # 自己那桌的牌子常被气泡/客人挡住（实测满堂彩时 1 号牌被气泡盖住误判）：对面那桌的牌子在另一边也算对
            pos2, good = side_ok(cur + 1 if cur % 2 == 1 else cur - 1)
            pos = pos or pos2
        if not good:
            self.wn_snap(f"TT{cur}{n}_1")
            rec["tt_check"] = pos
            self.log(f"桌到桌：退回过道后 {cur} 号牌位置不对（{pos}），先回饮品台再去 {n} 号桌")
            if not self.wn_pin_to_D(rec):
                self.wn_lost()
            self.at_table = None
            return self.wn_go_table(n, rec)
        if r1 != r0:
            lo, hi = min(r0, r1), max(r0, r1)
            t = sum(self.wn_p(f"row{r + 1}") for r in range(lo + 1, hi + 1))
            self.g._hold(["s" if r1 > r0 else "w"], t, unstick=False)
            self.wn_straighten(f"A{r1 + 1}", rec)
        rec["from"] = cur
        return self.wn_from_aisle(n, rec)

    def wn_go(self, n):
        """完整机器人去 n 号桌（wang_nav）：在桌边就沿过道换桌，否则先回饮品台再按王字走。到了返回 True。
        走的时候不被「满堂彩攒满」打断（打断会丢了横走记账、回不去）"""
        fw, self.g.watch_fw = self.g.watch_fw, False
        try:
            ok = self._wn_go(n)
            # v97：去过桌子就不再是「开局没走动」：之后回饮品台走正常流程（11:19:56 开局 31 秒内去过 4 号桌、
            # 沿吧台取过菜，还按开局捷径「不推不滑当作在饮品台」，从错的位置去 5 号桌走进花坛、走丢）
            self._fresh_start = False
            self._start_at_D = False
            return ok
        finally:
            self.g.watch_fw = fw

    def _trip_topup(self):
        """v53：去桌子的一趟路过饮品台时，手上不到两菜一饮就先补齐再出发（v52 放在主循环里，
        但一趟是「回饮品台→直接去桌」一口气走完的，不经过主循环：满堂彩后两次拿着菜没饮品去接客，各多等 20~40 秒）。
        补完回到饮品台站好；回不去返回 False"""
        if not self.cfg.get("topup_at_D", True) or time.time() - getattr(self, "_topup_t", 0) < 4:
            return True
        det = self.g.hotbar_detail()
        nd, nk = det.count("dish"), sum(1 for x in det if x in DRINK_TAGS)
        full = sum(1 for x in det if x) >= 5
        if time.time() < getattr(self, "_nodish_until", 0) and nd >= 1 and nk >= 1:
            return True  # v89：收尾刚判「来不及只拿饮品」：手上一菜一饮就直接出发，不在这里再取菜（06:42:41 多取 1 份菜，5 号没赶上）
        if full or (nd >= 2 and nk >= 1 and not (self.fired() and self.cfg.get("fired_topup_full", True))):
            return True  # v62：满堂彩后路过饮品台有空格就补满（收尾手上空了再专门跑回来补要 12 秒，顺路补只要 3~5 秒）
        self._topup_t = time.time()
        self.log(f"出发前在饮品台补货：手上 {nd} 菜 {nk} 饮")
        tgt = self.target
        self.restock()
        self.target = tgt
        if self.at_drink and self.g.has_prompt("p_drink"):
            return True
        if (self.pos is not None and self.pos[1] < 0.3 and self.where in ("出餐台", "出餐口等待")
                and 0.0 <= self.pos[0] <= float(self.cfg.get("counter_start_max", 1.8))):
            return "counter"  # v64：刚在出餐台取完菜：直接从这里滑到中间过道口出发，不再走回饮品台（v62 补完再回饮品台，一趟多 5~8 秒）
        return bool(self.wn_anchor())

    def _go_from_topup(self, n, rec, tp):
        if tp == "counter":
            dx = self.wn_p("DH") - self.pos[0]
            rec["from_counter"] = round(self.pos[0], 2)
            if abs(dx) > 0.05:
                self.wn_slide("d" if dx > 0 else "a", abs(dx))
            return self.wn_go_table(n, rec, from_h=True)
        return self.wn_go_table(n, rec)

    def _wn_go(self, n):
        self.target, self.target_done = n, False
        self.g.abort_walk = False
        rec = {"t": time.strftime("%H:%M:%S"), "n": n, "mode": "auto"}
        t0 = time.time()
        self.wn_close_panels()  # 做菜/饮品台面板没关上时按键会被吃掉，横走记账就乱了
        if self.at_table == n and getattr(self, "wn_at", None) == f"T{n}":
            return True
        if (self.at_table in TABLES and (getattr(self, "wn_at", None) or "").startswith("T")
                and (not self.g.boost_active()
                     or (self.cfg.get("boost_same_row_tt", True) and TABLES[self.at_table][1] == TABLES[n][1])
                     # v66：倍数改回 1.5 后加速期站位基本不失败了：相邻一排的桌也直接过去（满堂彩后每位客人约 17 秒，
                     # 先回饮品台再出发每趟多约 5 秒）；跨两排（1↔6 那种）仍先回饮品台
                     or (self.cfg.get("boost_adjacent_tt", True)
                         and abs(TABLES[self.at_table][1] - TABLES[n][1]) <= 1))):
            # v26：满堂彩加速时同一排对面那桌（1↔2、3↔4、5↔6）也直接过去：只横穿过道，不沿过道走
            ok = self.wn_table_to_table(n, rec)
        elif self.at_table in TABLES and (getattr(self, "wn_at", None) or "").startswith("T"):
            # 满堂彩加速时不直接桌到桌（实测加速中从 1 号桌直接去 6 号桌，走进了右边的花坛）：先回饮品台再去
            if not self.wn_anchor():
                return False
            tp = self._trip_topup()
            if not tp:
                return False
            ok = self._go_from_topup(n, rec, tp)
        elif (self.pos is not None and self.pos[1] < 0.3 and self.where in ("出餐台", "出餐口等待")
              and 0.0 <= self.pos[0] <= float(self.cfg.get("counter_start_max", 1.8))):
            # 刚在出餐台取完菜（贴着吧台、离饮品台 pos[0] 秒）：直接滑到中间过道口出发，不再绕回饮品台（每趟省 3~5 秒）
            dx = self.wn_p("DH") - self.pos[0]
            rec["from_counter"] = round(self.pos[0], 2)
            if abs(dx) > 0.05:
                self.wn_slide("d" if dx > 0 else "a", abs(dx))
            ok = self.wn_go_table(n, rec, from_h=True)
        else:
            if not (self.at_drink and self.g.has_prompt("p_drink")) and not self.wn_anchor():
                return False
            tp = self._trip_topup()
            if not tp:
                return False
            ok = self._go_from_topup(n, rec, tp)
        p = self.g.prompt()
        ok = ok or p in TABLE_PROMPTS
        rec["go_time"] = round(time.time() - t0, 1)
        self.wn_log(rec)
        self.at_drink = False
        self.pos = None  # 新走法不用旧的坐标推算
        if ok:
            self.at_table = n
            self._at(f"{n}号桌")
            self.log(f"到达 {n} 号桌（新走法），提示：{PROMPTS.get(p, '无')}，用时 {rec['go_time']} 秒")
        else:
            self.log(f"去 {n} 号桌没到位（新走法），回饮品台重来")
            if not self.wn_back(n, rec):
                self.wn_lost()
            self.at_table = None
        return ok

    def wn_anchor(self):
        """完整机器人回饮品台（wang_nav）：在桌边就退回过道、摆正、走回吧台；在吧台边就贴着滑。回不去算走丢"""
        fw, self.g.watch_fw = self.g.watch_fw, False
        try:
            return self._wn_anchor()
        finally:
            self.g.watch_fw = fw

    def _wn_anchor(self):
        self.g.abort_walk = False
        if self.at_drink and self.g.has_prompt("p_drink"):
            return True
        rec = {"t": time.strftime("%H:%M:%S"), "mode": "anchor"}
        self.wn_close_panels()
        if self.at_table in TABLES and (getattr(self, "wn_at", None) or "").startswith("T"):
            ok = self.wn_back(self.at_table, rec)
        else:
            ok = self.wn_pin_to_D(rec)
        if not ok:
            # 用户：走丢了就自己结束重开，别浪费时间（旧的兜底定位常常要一两分钟还找不回来）
            self.at_table, self.at_drink = None, False
            self.wn_lost()
            return False
        self.at_table = None
        if ok:
            self.wn_at = "D"
            self.at_drink = True
            self._at("饮品台")
            self.set_origin()
        return ok

    def wn_lost(self):
        """回不到饮品台：营业中算走丢（结束这一局重开）；已打烊/最后十几秒不算（打烊后饮品台提示会消失，
        实测打烊后找不到饮品台被判走丢，整局 3960 的营业额记 0 分）"""
        left = self.g.left_est()
        if self.closed or (left is not None and left < 15) or self.g.shop_closed():
            self.log("快收工/已打烊：回不到饮品台也不算走丢，原地等营业结束")
            return
        self.log("新走法回不到饮品台：算走丢，结束这一局重开")
        raise RoundLost()

    def wn_snap(self, name):
        """存一张画面到 test_channel/wang_fail（查问题用）"""
        if not self.cfg.get("debug_files"):
            return
        try:
            d = os.path.join(BASE_DIR, "test_channel", "wang_fail")
            os.makedirs(d, exist_ok=True)
            cv2.imencode(".jpg", self.g.frame960())[1].tofile(os.path.join(d, f"{name}_{time.strftime('%H%M%S')}.jpg"))
        except Exception:
            pass

    def wn_close_panels(self):
        """料理界面、饮品台面板、退出确认框开着就关掉（实测开局做完菜料理界面没关上，之后每一步都走不动）"""
        for _ in range(3):
            if not self.g.kitchen_open():
                break
            self.log("料理界面还开着，按 Esc 关掉")
            self.g.press("esc")
            self._wait(lambda: not self.g.kitchen_open(), 1.5)
        self.close_drink_panel()
        self.g.close_quit_dialog()

    def wn_trip(self, n):
        """一趟：饮品台 → n 号桌（有「接待」就按 F 接待，不送菜）→ 回饮品台。记日志"""
        rec = {"t": time.strftime("%H:%M:%S"), "n": n}
        t0 = time.time()
        self.wn_close_panels()
        if getattr(self, "wn_at", None) != "D" and not self.wn_to_D(rec):
            self.wn_pin()
            if not self.wn_to_D(rec):
                # 兜底：旧的整套回饮品台（退回过道、大厅定位、沿吧台找）
                self.log("新寻路回不到饮品台：用旧的定位兜底")
                self.at_table, self.at_drink = None, False
                if self.anchor():
                    self.wn_at = "D"
                    rec["fallback"] = True
                else:
                    rec["fail"] = "回不到饮品台"
                    self.wn_log(rec)
                    return False
        rec["start_prompt"] = self.g.prompt()
        ok = self.wn_go_table(n, rec)
        rec["go_time"] = round(time.time() - t0, 1)
        if self.g.has_prompt("p_jiedai"):
            self.g.press("f")
            time.sleep(0.6)
            rec["received"] = True
            self.log(f"  [T{n}] 已接待（不送菜）")
        back = self.wn_back(n, rec)
        rec["total"] = round(time.time() - t0, 1)
        rec["back_ok"] = back
        self.wn_log(rec)
        self.log(f"新寻路 {n} 号桌一趟：{'到位' if ok else '没到位'}、{'回到' if back else '没回到'}饮品台，用时 {rec['total']} 秒")
        return ok and back

    def wn_log(self, rec):
        if not self.cfg.get("debug_files"):
            return
        try:
            with open(os.path.join(BASE_DIR, "test_channel", "wang_log.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def wang_practice(self, sec=600, tables=None):
        """练习新寻路：只接待不送菜。优先去等接待的桌，没有就按 1~6 轮着走"""
        self.g.passing = None  # 路上不顺手办事
        self.wn_at = None
        self.wn_refs()
        if self.cooked == 0:
            try:
                self.wn_pin()
                self.wn_to_D()
                self.cook()  # 开局下凉糕（原点）
            except Exception as e:
                self.log(f"练习：开局做菜失败 {e}")
            self.wn_at = None
        order = list(tables or (1, 2, 3, 4, 5, 6))
        i, end, n_ok, n_all = 0, time.time() + sec, 0, 0
        while time.time() < end:
            st = self.g.table_states()
            wait = [m for m in order if st.get(m, ("", []))[0] == "laotao"]
            n = wait[0] if wait else order[i % len(order)]
            i += 1
            n_all += 1
            if self.wn_trip(n):
                n_ok += 1
            self.log(f"新寻路练习：{n_ok}/{n_all} 趟成功")

    def survey_wang(self, passes=2):
        """营业中采「王」字路线的参考图（test_channel/survey_wang + index.jsonl），给出发前摆正视角用：
        饮品台前、每张桌到达时、按 F 接待后带气泡时、退回过道时各拍一张；第一遍在饮品台/桌边/过道还按已知角度
        （±5°/±10°/±20°）转着各拍一张，用来标定「画面平移多少像素 = 歪了几度」。每张记下位置、提示、凉糕在画面里的 x、横线读数"""
        out = os.path.join(BASE_DIR, "test_channel", "survey_wang")
        os.makedirs(out, exist_ok=True)
        idx = open(os.path.join(out, "index.jsonl"), "a", encoding="utf-8")
        ppd = float(self.cfg["look_px_per_deg"])

        def snap(place, yaw=0, extra=None):
            time.sleep(0.35)
            img = self.g.frame960()
            name = f"{place}_{yaw:+d}_{time.strftime('%m%d_%H%M%S')}_{int(time.time() * 10) % 10}.jpg"
            cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tofile(os.path.join(out, name))
            try:
                ang = self.g.counter_angle()
            except Exception:
                ang = None
            rec = {"file": name, "place": place, "yaw": yaw, "prompt": self.g.prompt(), "lgx": self.g.anchor_dish_x(),
                   "angle": ang, "boost": self.g.boost_active(), "t": time.strftime("%H:%M:%S")}
            rec.update(extra or {})
            idx.write(json.dumps(rec, ensure_ascii=False) + "\n")
            idx.flush()

        def sweep(place, degs):
            snap(place, 0)
            for d in degs:
                self.g.look(dx=int(d * ppd))
                snap(place, d)
                self.g.look(dx=-int(d * ppd))
            time.sleep(0.2)

        # 开局先下凉糕（原点），才拍得到凉糕的位置
        if self.cooked == 0:
            try:
                self.anchor()
                self.cook()
            except Exception as e:
                self.log(f"采样：开局做菜失败 {e}")
        n_ok = 0
        for k in range(passes):
            degs = (-20, -10, -5, 5, 10, 20) if k == 0 else ()
            for n in (1, 2, 3, 4, 5, 6):
                if not self.anchor():
                    self.log(f"采样：回不到饮品台，跳过 {n} 号桌")
                    continue
                if not self.drink_yaw_check():
                    self.log("采样：饮品台前镜头确认不了是正对的，这一趟不拍")
                    continue
                sweep("drink", degs if n in (1, 4) else ())
                t0 = time.time()
                if not self.go_table(n):
                    snap(f"lost{n}", 0, {"pos": self.pos})
                    continue
                walk = round(time.time() - t0, 1)
                sweep(f"table{n}", (-10, 10) if degs else ())
                if self.g.has_prompt("p_jiedai"):
                    self.g.press("f")
                    time.sleep(0.6)
                    snap(f"table{n}_bubble", 0, {"walk": walk})
                    time.sleep(1.5)
                    snap(f"table{n}_bubble2", 0)
                else:
                    snap(f"table{n}_noprompt", 0, {"walk": walk})
                self.back_to_aisle()
                row = TABLES[n][1] + 1
                snap(f"aisle{row}_raw", 0)  # 刚退回过道、还没校正视角时的样子（按过 F 后镜头可能歪）
                self._fix_yaw_in_hall()
                sweep(f"aisle{row}", (-10, 10) if degs else ())
                n_ok += 1
                self.log(f"采样：{n} 号桌这一趟拍完（第 {k + 1} 遍，走了 {walk} 秒）")
        idx.close()
        self.log(f"王字路线采样完成：{n_ok} 趟，存到 {out}")

    def facing_score(self):
        """现在的画面和营业时采的「面朝吧台」样本（test_channel/places）能对上多少特征点。
        面朝吧台时一般 35 以上（多数 80~800），镜头转反了（对着门口、屏风）只有 10~26"""
        if not hasattr(self, "_face_refs"):
            import glob
            cv2.setNumThreads(8)
            self._face_orb = cv2.ORB_create(1000)
            self._face_bf = cv2.BFMatcher(cv2.NORM_HAMMING)
            try:
                import hall_locator
                self._face_mask = hall_locator.MASK.astype(np.uint8) * 255
            except ImportError:
                self._face_mask = None
            # 优先用专门采的干净样本（places_clean：不接待、不按 F、确认正对后拍的；*_away 是转身后的反例，不用）。
            # 跑动中顺手存的 places 里混有镜头已经转反时存的图，分不清正对和背对，只在没有干净样本时才用
            clean = [f for f in sorted(glob.glob(os.path.join(BASE_DIR, "test_channel", "places_clean", "*.jpg")))
                     if "_away" not in os.path.basename(f)]
            files = clean or [f for f in sorted(glob.glob(os.path.join(BASE_DIR, "test_channel", "places", "*.jpg")))
                              if "boost" not in os.path.basename(f)]
            files = files[::max(1, len(files) // int(self.cfg["face_refs"]))]
            def load(fs):
                out = []
                for f in fs:
                    g = cv2.cvtColor(cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
                    k, d = self._face_orb.detectAndCompute(g, self._face_mask)
                    if d is not None and len(k) >= 20:
                        out.append((np.float32([p.pt for p in k]), d))
                return out
            self._face_refs = load(files)
            # 反例：同一位置转身 180° 拍的（只在干净样本里有）
            self._away_refs = load(sorted(glob.glob(os.path.join(BASE_DIR, "test_channel", "places_clean", "*_away_*.jpg"))))
            self.log(f"朝向检查：载入 {len(self._face_refs)} 张正对样本、{len(self._away_refs)} 张背对样本")
        if not self._face_refs:
            return None
        return self._match_best(self._face_refs)

    def facing_pair(self):
        """(和正对样本最多对上几个点, 和背对样本最多对上几个点)。没有样本返回 None"""
        p = self.facing_score()
        if p is None:
            return None
        return p, (self._match_best(self._away_refs) if self._away_refs else 0)

    def _match_best(self, refs):
        """当前画面和一组样本最多能对上几个特征点（同一次检查里 0.3 秒内复用上一帧的特征，正反两组用同一帧比）"""
        now = time.time()
        if now - getattr(self, "_cur_kd_t", 0) > 0.3:
            self._cur_kd = self._face_orb.detectAndCompute(cv2.cvtColor(self.g.frame960(), cv2.COLOR_BGR2GRAY), self._face_mask)
        self._cur_kd_t = now
        k, d = self._cur_kd
        if d is None or len(k) < 20:
            return 0
        pts = np.float32([p.pt for p in k])
        best = 0
        for kp2, d2 in refs:
            try:
                ms = self._face_bf.knnMatch(d, d2, k=2)
            except cv2.error:
                continue
            good = [m for m, n in (x for x in ms if len(x) == 2) if m.distance < 0.75 * n.distance]
            if len(good) < 8:
                continue
            _, inl = cv2.findHomography(pts[[m.queryIdx for m in good]], kp2[[m.trainIdx for m in good]], cv2.RANSAC, 8.0)
            if inl is not None:
                best = max(best, int(inl.sum()))
        return best

    def ensure_facing(self, force=False):
        """在大厅里出发前确认镜头还面朝吧台：在桌边按 F 后镜头偶尔整个转反，这时「往吧台走」会走出门口、走到屏风那边去。
        对不上就依次转 180°、90°、180°（覆盖四个方向）找回面朝吧台的方向；满堂彩特效下样本对不上，不查"""
        if (not self.cfg["face_check"] and not force) or self.g.boost_active():
            return
        try:
            pr = self.facing_pair()
        except cv2.error:
            return
        thr = int(self.cfg["face_min"])
        if pr is None:
            return
        s0, n0 = pr
        if s0 >= thr or s0 > n0:
            return  # 比起背对的样本更像正对的：没转反（正对样本少的地方分数会偏低，不能只看分数）
        k = self.cfg["look_px_per_deg"] * self.cfg["yaw_sign"]  # 镜头真正转 1° 要移动的鼠标像素（实测 2000 像素≈180°）
        self.log(f"朝向检查：和面朝吧台的样本只对上 {s0} 个点，镜头多半转反了，转着找")
        self.g.trace("朝向不对")
        best, turned = (s0, 0), 0
        for t in (180, 90, 180):
            self.g.look(dx=int(t * k))
            turned += t
            time.sleep(0.35)
            s1, n1 = self.facing_pair() or (0, 0)
            if s1 > best[0]:
                best = (s1, turned % 360)
            if s1 >= thr and s1 > 1.5 * n1:  # 要明显更像正对、不像背对，才认这个方向
                self.log(f"朝向检查：转了 {turned % 360}° 对上 {s1} 个点，已转回面朝吧台")
                self.pos = None  # 方向错了一阵，坐标也不可信了
                self._fix_yaw_in_hall()
                return
        back = (0 - turned) % 360  # 四个方向都对不上：不是镜头转反的问题，转回原来的方向，不乱转
        if back:
            self.g.look(dx=int(back * k))
        self.log(f"朝向检查：四个方向都对不上（最好 {best[0]}），转回原来的方向")

    def measure_speed(self, label):
        """站在饮品台：往餐厅里直走 1 秒，用大厅定位量走了多远（x 变小多少），再原路退回贴台。
        开局量一次平时的速度，满堂彩开启后再量一次，两者之比就是加速倍数（boost_speed）。返回每秒走多少"""
        try:
            if not hasattr(self, "_orb"):
                cv2.setNumThreads(8)
                import hall_locator
                self._orb = hall_locator.OrbLocator(os.path.join(BASE_DIR, "test_channel", "survey"))
            time.sleep(0.3)
            r0 = self._orb.locate(self.g.frame960())
            self.g._hold_inner([self.g._k("w")], 1.0, unstick=False)
            time.sleep(0.3)
            r1 = self._orb.locate(self.g.frame960())
        except (cv2.error, ImportError, OSError, ValueError) as e:
            self.log(f"量速度出错：{e}")
            return None
        finally:
            self.g._hold_inner([self.g._k("s")], 1.2, unstick=False)  # 退回去贴着吧台
            self.pos = None  # 下一趟先在饮品台重新定位
        mi = int(self.cfg.get("orb_min_inliers", 20))
        if not r0 or not r1 or min(r0[2], r1[2]) < mi:
            self.log(f"量速度（{label}）：定位不够准，作废（内点 {r0[2] if r0 else 0}/{r1[2] if r1 else 0}）")
            return None
        v = r0[0] - r1[0]
        self.log(f"量速度（{label}）：1 秒走了 {v:.2f}（x {r0[0]:.1f}→{r1[0]:.1f}，内点 {r0[2]}/{r1[2]}）")
        return v if v > 0.5 else None

    def orb_recover(self):
        """找不到饮品台时用自建的大厅定位（hall_locator.OrbLocator：和不营业时采集的截图比特征点）看看自己在哪。
        大厅坐标：x 是前后（吧台前约 142，越往餐厅越小），y 是左右（画面右边 y 大；「开始经营」处 y≈138.6，饮品台在吧台左头 y≈131）。
        常见的走丢：往饮品台滑的时候没看到提示、滑过了头，进了饮品台左边挂画墙前的凹角（y≈126）。
        这时先往后退离墙面，再往画面右边走回饮品台附近，贴台后短距离往饮品台滑。
        注意：g.hold 会按「面朝餐厅」对调方向键，这里写的是实际按键，用 g._hold"""
        try:
            if not hasattr(self, "_orb"):
                cv2.setNumThreads(8)  # 只用一部分 CPU，留给用户
                import hall_locator
                self._orb = hall_locator.OrbLocator(os.path.join(BASE_DIR, "test_channel", "survey"))
            r = self._orb.locate(self.g.frame960())
            if (not r or r[2] < int(self.cfg.get("orb_face_inliers", 35))) and not self.g.boost_active():
                r = self.orb_face(r)  # 满堂彩期间转四个方向也认不出（加速特效），白花十秒，不转
        except (cv2.error, ImportError, OSError, ValueError) as e:
            self.log(f"大厅定位出错：{e}")
            return False
        if not r or r[2] < int(self.cfg.get("orb_min_inliers", 20)):
            # 认不出 = 不在大厅里：多半是往右滑过了饮品台、进了右边的休息区（沙发、茶具），往左原路退回去找
            self.log(f"大厅定位认不出这里（内点 {r[2] if r else 0}），多半滑过饮品台进了旁边的休息区，往回走")
            self.g.trace("大厅定位_认不出")
            # 满堂彩期间画面有加速特效，定位本来就认不出，不能当成「在休息区」往回走（会越走越远）
            if not self.g.boost_active() and self.g.hold(["a"], 2.5, stop_on=("p_drink",)):
                self._at("饮品台")
                self.at_drink = True
                self.set_origin()
                self.anchor_fails = 0
                self.log("往回走到了饮品台（锚点）")
                return True
            self._fix_yaw_in_hall()
            self.pos = None
            self.depth = 3.0
            self.pin_counter()
            seen = self.g.has_prompt("p_drink") or self.counter_slide("d", 1.5, stop_on=("p_drink",), unstick=False)
            if seen:
                self._at("饮品台")
                self.at_drink = True
                self.set_origin()
                self.anchor_fails = 0
                self.log("往回走后找到了饮品台（锚点）")
                return True
            return False
        x, y, n = r
        self.log(f"大厅定位：x={x:.1f} y={y:.1f}（内点 {n}）")
        spd = float(self.cfg.get("hall_speed", 4.0))  # 每秒走多少大厅坐标单位（粗估）
        drink_y = float(self.cfg.get("drink_y", 131.0))
        if y < drink_y - 2:
            self.g._hold(["s"], 0.8)  # 实际按 S：从墙前往后退一点
            self._fix_yaw_in_hall()
            self.g.trace("大厅定位_退离墙面")
            # 走完再看一眼：提示常常在刚停下时才出来；已经在饮品台就绝不能再往吧台上顶（会把镜头顶歪、滑进凹角）
            if (self.g._hold(["d"], max(0.4, min(2.5, (drink_y - y) / spd)), stop_on=("p_drink",))
                    or self._wait(lambda: self.g.has_prompt("p_drink"), 0.6)):
                self._at("饮品台")
                self.at_drink = True
                self.set_origin()
                self.anchor_fails = 0
                self.log("按大厅定位走回了饮品台（锚点）")
                return True
            self.g.trace("大厅定位_回到饮品台附近")
            self.pin_counter()
            seen = self.g.has_prompt("p_drink") or self.counter_slide("d", 1.5, stop_on=("p_drink",), unstick=False)
            if seen:
                self._at("饮品台")
                self.at_drink = True
                self.set_origin()
                self.anchor_fails = 0
                self.log("按大厅定位走回了饮品台（锚点）")
                return True
            return False
        self._fix_yaw_in_hall()
        self.pos = None
        self.depth = 3.0
        self.pin_counter()
        seen = self.g.has_prompt("p_drink") or self.counter_slide("d", self.cfg["counter_sweep"], stop_on=("p_drink",), unstick=False)
        if seen:
            self._at("饮品台")
            self.at_drink = True
            self.set_origin()
            self.anchor_fails = 0
            self.log("按大厅定位走回了饮品台（锚点）")
            return True
        return False

    def orb_face(self, r0):
        """视角整个转偏了（看到的是侧墙、门口，横线校正也看不出来）：每次转 90° 和采集的「面朝吧台」截图比一次，
        转回最能对上的方向（对得上的特征点最多），再用横线细调。返回那个方向上的定位结果"""
        k = self.cfg["look_px_per_deg"] * self.cfg["yaw_sign"]  # 镜头真正转 1° 要移动的鼠标像素（实测 2000 像素≈180°）
        res = [r0]
        for _ in range(3):
            self.g.look(dx=int(90 * k))
            time.sleep(0.3)
            res.append(self._orb.locate(self.g.frame960()))
        self.g.look(dx=int(90 * k))  # 转满一圈，回到原来的方向
        n = [r[2] if r else 0 for r in res]
        best = max(range(4), key=lambda i: n[i])
        if n[best] < int(self.cfg.get("orb_face_inliers", 35)):
            # 哪个方向都对不上（不在采集过的大厅范围里）：比出来的差别只是噪声，别乱转，保持原来的方向
            self.log(f"大厅定位：四个方向对上的特征点 {n}，都不够，不转")
            return r0
        self.log(f"大厅定位：四个方向对上的特征点 {n}，转到第 {best} 个方向")
        if best:
            self.g.look(dx=int(90 * best * k))
            time.sleep(0.3)
        self._fix_yaw_in_hall()
        return res[best]

    def set_origin(self):
        """到了饮品台：坐标归零（原点）"""
        if self.pos is None:
            self.log("到饮品台，建立坐标原点")
        self.pos = [0.0, 0.0]
        self.track = []
        if self.g.has_prompt("p_drink"):
            if self.drink_yaw_check():
                self.save_place("drink")  # 只存镜头正对的（歪着存进去会把样本库带坏）

    def drink_yaw_check(self):
        """站在饮品台：和营业时采的「正对吧台」样本比一下（约 0.25 秒）。镜头被带歪了几十度时
        （横线读数对这种情况没用：顺着吧台看也全是横线）分数会掉到 30 以下，这时往左右各试 45°、90°，
        转到分数最高的方向。返回现在是不是正对的"""
        if not self.cfg["drink_yaw_check"] or self.g.boost_active():
            return True
        try:
            s0 = self.facing_score()
        except cv2.error:
            return True
        if s0 is None:
            return True
        good = int(self.cfg["drink_face_good"])
        if s0 >= good:
            return True
        if s0 >= int(self.cfg["drink_face_bad"]) or getattr(self, "_firing", False):
            # 分数偏低但不算歪（饮品台的干净样本少，正对时也可能只有三四十分）；开满堂彩这一趟赶时间也不转
            return False
        best, cur = (s0, 0), 0
        for off in (-500, -1000, -1500, -2000, 500, 1000, 1500, 2000):
            self.g.look(dx=off - cur)
            cur = off
            time.sleep(0.3)
            s1 = self.facing_score() or 0
            if s1 > best[0]:
                best = (s1, off)
            if s1 >= good * 1.5:
                break  # 已经很明确是正对的了，不用再试
        # 最好的方向也达不到「正对」的分数：说明不是镜头的问题（被人挡住、站的位置不对），转回原来的方向不动
        target = best[1] if best[0] >= good else 0
        self.g.look(dx=target - cur)
        if target:
            self.log(f"饮品台前镜头歪了（正对样本只对上 {s0} 个点），转了 {target} 像素后对上 {best[0]} 个点")
            self.g.trace("饮品台转正")
        else:
            self.log(f"饮品台前和正对样本只对上 {s0} 个点，转一圈也没找到更像的方向（最好 {best[0]}），不转")
        return best[0] >= good

    def _save_counter(self, img, t):
        """贴着吧台、往饮品台滑 t 秒就到了：这张画面标成「饮品台右边 t 秒」"""
        try:
            d = os.path.join(BASE_DIR, "test_channel", "places")
            os.makedirs(d, exist_ok=True)
            name = f"counter_{t:.2f}s_{time.strftime('%m%d_%H%M%S')}.jpg"
            cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(os.path.join(d, name))
        except Exception:
            pass

    def save_place(self, label):
        """位置确定的时刻（看到「饮品台」提示、到桌看到自己的桌号牌）存一张画面，攒营业状态下的位置样本，
        以后用来自建定位（营业外采的参考图镜头不一样、满堂彩特效下也认不出）。同一类 8 秒内只存一张"""
        if not self.cfg["save_places"]:
            return
        if self.g.boost_active():
            label += "_boost"
        last = self.__dict__.setdefault("_place_t", {})
        if time.time() - last.get(label, 0) < 8:
            return
        last[label] = time.time()
        try:
            d = os.path.join(BASE_DIR, "test_channel", "places")
            os.makedirs(d, exist_ok=True)
            name = f"{label}_{time.strftime('%m%d_%H%M%S')}.jpg"
            cv2.imencode(".jpg", self.g.frame960(), [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(os.path.join(d, name))
        except Exception:
            pass

    def leave_drink(self):
        """离开饮品台：先往过道方向横挪离开墙角，再校正视角，再贴回吧台"""
        if not self.at_drink:
            return 0.0
        self.at_drink = False
        # 和去过道口同一个方向（贴着吧台往过道滑），只滑一小段，出了墙角就停下校正视角
        t = self.cfg["leave_drink"]
        self.counter_slide("a", t, unstick=False)
        self._fix_yaw_in_hall()
        self.depth = 0.0
        return t

    def go_drink(self):
        ok = self.anchor()
        self._arrive(ok)
        return ok

    def go_dish(self):
        if not self.anchor():
            return False
        seen = self.g.hold(["s", "a"], self.cfg["dish_timeout"], stop_on=("p_dish",))
        if seen:
            self._at("出餐台")
            self.log("已到出餐台（看到取菜提示）")
        else:
            self._at("未知")
            self.log("沿吧台往左没找到取菜提示（出餐台上可能没有菜）")
        self._arrive(bool(seen))
        return bool(seen)

    def go_table(self, n):
        """走到 n 号桌，并用桌号牌确认真的到了；不对就重走，还不对就放弃（不按 F，免得送错桌）。
        所有桌子都从侧面靠过去：现在站在某张桌边就沿中间过道直接走过去；在过道口就直接出发；
        都不是（或者第一次没走对）才回饮品台锚点重新定位"""
        if self.cfg.get("wang_nav"):
            return self.wn_go(n)
        self.target, self.target_done = n, False
        self.g.abort_walk = False
        for attempt in range(1 + int(self.cfg["verify_retry"])):
            if self.use_coords():
                if attempt > 0:
                    self.pos = None  # 第一次没对上：坐标可能累积偏了，回饮品台重新定原点
                p = self._go_table_coords(n)
            elif attempt == 0 and self.at_table:
                p = self._go_aisle_from_table(n)
            elif attempt == 0 and self.at_head:
                p = self._go_aisle_table(n)
            else:
                self.back_to_aisle()  # 重走前先回到过道中间，别从桌边直接斜着走
                self.at_table = None
                if not self.anchor():
                    continue
                p = self._go_aisle_table(n)
            if self.target_done:
                # 路上经过这张桌时已经顺手办完了：就停在这里，不再找位置、不再重复送
                self.g.abort_walk = False
                self.at_table = n
                self._at(f"{n}号桌")
                self.log(f"{n} 号桌在路上已经顺手办完")
                return True
            ok, d = self.locate(n, (self.g.prompt() or p) in TABLE_PROMPTS)
            if not ok and d and d != n and TABLES[d][0] == TABLES[n][0]:
                # 同一侧停错了排：按差几排就地往前/往后补走，再靠一次桌
                p = self._fix_row(n, d) or p
                ok, d = self.locate(n, (self.g.prompt() or p) in TABLE_PROMPTS)
            self.last_sign = (d, 0.0)
            if ok:
                how = "看到桌号牌" if d == n else "按周围的桌号牌推断"
                if (d == n or self.cfg["facing_counter"]) and self.servo(n):  # 按标准站位微调（面朝吧台时看不到牌子也先找）
                    p = self.g.prompt() or p
                if self.pos is not None:
                    self.pos[1] = self.cfg["rows"][TABLES[n][1]]  # 确认到了这一排：前后坐标对齐
                self.log(f"到达 {n} 号桌（{how}），提示：{PROMPTS.get(p, '无')}，坐标 {self._pos_str()}")
                self.g.trace(f"到{n}号桌")
                if d == n:
                    self.save_place(f"table{n}")
                self._arrive(p in TABLE_PROMPTS)
                self.at_table = n
                return True
            self.at_table = None
            seen = f"{d} 号桌附近" if d else "没有提示也认不出位置"
            self.log(f"想去 {n} 号桌，但判断在{seen}，重新走")
            self.g.trace(f"去{n}号桌没对上")
        self._at("未知")
        self.log(f"去 {n} 号桌失败，放弃这次")
        return False

    def _go_table_coords(self, n):
        """按坐标去 n 号桌：回过道 → 沿过道到那一排 → 往桌子那边横移，出现提示就停"""
        lane, row = TABLES[n]
        if not self.move_to_row(self.cfg["rows"][row]):
            return None
        self.at_head = False
        self._at(f"{n}号桌")
        p = self._approach("d" if lane == "right" else "a")
        return p or self._search()

    def _go_aisle_table(self, n):
        """从过道口沿中间那排地砖直走到那一排，再横向靠近桌子，出现提示就停。返回看到的提示
        （不在过道口时，先从饮品台锚点沿吧台滑到过道口）"""
        lane, row = TABLES[n]
        if not self.at_head:
            if self.at_drink:
                self.fix_yaw(place="drink")  # 出发前在饮品台按参考图摆正
            done = self.leave_drink()  # 从饮品台出发：先滑一小段离开墙角并校正视角
            self.counter_slide("a", self.cfg["lane_center"] - done)  # 再贴着吧台滑到中间过道入口
        self.at_head = False
        # 先走几步离开吧台（贴着吧台时镜头会被挤歪），停下校正视角，再走完剩下的
        first = min(self.cfg["yaw_walk"], self.cfg["rows"][row])
        self.g.hold(["w"], first)
        self._fix_yaw_in_hall()
        self.g.hold(["w"], self.cfg["rows"][row] - first)
        self.depth = self.cfg["rows"][row] + 0.6  # 加上前后微调可能多走的
        self._fix_yaw_in_hall()  # 横着靠桌之前再校正一次，视角歪了横移会走斜
        self._at(f"{n}号桌")
        if self.cfg["top_view"]:
            self.align(lane)
        side = "d" if lane == "right" else "a"
        p = self._approach(side)
        if not p:
            p = self._search()
        return p

    def _go_aisle_from_table(self, n):
        """从一张桌边直接去另一张桌：退回中间过道 -> 校正视角 -> 前后走到那一排 -> 横向靠桌"""
        cur = self.at_table
        self.back_to_aisle()
        self._fix_yaw_in_hall(place=f"aisle{TABLES[cur][1] + 1}" if cur in TABLES else None)
        dt = self.cfg["rows"][TABLES[n][1]] - self.cfg["rows"][TABLES[cur][1]]
        if abs(dt) > 0.05:
            self.g.hold(["w" if dt > 0 else "s"], abs(dt))
            self.depth += dt
        self._at(f"{n}号桌")
        side = "d" if TABLES[n][0] == "right" else "a"
        p = self._approach(side)
        return p or self._search()

    def servo(self, n):
        """按记录的标准站位微调：让 n 号牌落在画面里记录的坐标上（镜头跟着角色，牌子位置就代表站位）"""
        if self.cfg["facing_counter"]:
            return self.servo_fc(n)
        ref = self.cfg["sign_ref"].get(str(n))
        if not ref:
            return None
        tol, k = self.cfg["servo_tol"], self.cfg["servo_k"]
        for _ in range(6):
            pos = self.g.sign_pos(n)
            if not pos or abs(pos[0] - ref[0]) > 300 or abs(pos[1] - ref[1]) > 200:
                self.log(f"对齐时找不到合理位置的 {n} 号牌")
                return False
            ex, ey = pos[0] - ref[0], pos[1] - ref[1]
            if abs(ex) <= tol and abs(ey) <= tol:
                self.log(f"已对齐 {n} 号桌标准站位（偏差 {ex},{ey}）")
                return True
            # 牌子偏右 -> 角色往右(D)；牌子偏下 -> 角色往后(S)。镜头跟着角色走，所以是同方向
            if abs(ex) > tol:
                key, t = ("d" if ex > 0 else "a"), min(0.5, max(0.05, abs(ex) * k))
                self.g.hold([key], t, unstick=False)
                self._track_side(key, t)
            if abs(ey) > tol:
                key, t = ("s" if ey > 0 else "w"), min(0.5, max(0.05, abs(ey) * k))
                self.g.hold([key], t, unstick=False)
                self.depth += t if key == "w" else -t
        self.log(f"{n} 号桌对齐 6 次还差一点")
        return True

    def servo_fc(self, n):
        """面朝吧台时按演示的标准站位微调：n 号牌落到 sign_ref_fc 记的位置（桌子和长凳交界处，看得全气泡和牌子）。
        用实际按键：牌子偏右 → 人往右（D）；牌子偏下（人离吧台还近）→ 往外走（S）。
        最后一排（5、6 号）停得近时牌子在画面最下沿看不到：先往外走一小步再找"""
        ref = self.cfg["sign_ref_fc"].get(str(n))
        if not ref:
            return None
        tol, k = self.cfg["servo_fc_tol"], self.cfg["servo_fc_k"]
        miss = 0
        for _ in range(7):
            pos = self.g.sign_pos(n)
            if not pos or abs(pos[0] - ref[0]) > 380:
                if TABLES[n][1] == 2 and miss < 2:
                    miss += 1
                    self.g._hold(["s"], 0.2, unstick=False)
                    self.depth += 0.2
                    continue
                self.log(f"站位微调：看不到 {n} 号牌，不调")
                return False
            ex, ey = pos[0] - ref[0], pos[1] - ref[1]
            if abs(ex) <= tol and abs(ey) <= tol:
                self.log(f"已到 {n} 号桌标准站位（牌子偏差 {ex},{ey}）")
                return True
            if abs(ex) > tol:
                key, t = ("d" if ex > 0 else "a"), min(0.4, max(0.05, abs(ex) * k))
                self.g._hold([key], t, unstick=False)
                self._track_side(OPPOSITE[key], t)  # 面朝吧台时实际 D = 逻辑 a
            if abs(ey) > tol:
                key, t = ("s" if ey > 0 else "w"), min(0.4, max(0.05, abs(ey) * k))
                self.g._hold([key], t, unstick=False)
                self.depth += t if key == "s" else -t
        self.log(f"{n} 号桌站位微调 7 次还差一点")
        return True

    def _track_side(self, key, t):
        """记下横向净移动，回吧台前退回过道用"""
        if not self.side:
            return
        side, st, slow = (self.side + (False,))[:3]
        if slow:
            t /= self.cfg["slow_factor"]  # 这里是正常速度走的，换算成慢走的时长再记
        self.side = (side, st + t, slow) if key == side else (side, max(0.0, st - t), slow)

    def record_ref(self, n):
        """把当前画面里 n 号牌的位置记为 n 号桌的标准站位"""
        pos = self.g.sign_pos(n)
        if not pos:
            self.log(f"画面里没找到 {n} 号牌，记录失败")
            return False
        self.cfg["sign_ref"][str(n)] = [pos[0], pos[1]]
        save_config(self.cfg)
        self.log(f"已记录 {n} 号桌标准站位：牌子在 ({pos[0]}, {pos[1]})")
        return True

    def _approach(self, side):
        """横向靠桌：先固定走一小段（站在过道中间时对面桌子的提示也会出现，不能马上停），再走到出现提示为止。
        用缓慢行走，不容易冲过头穿过桌子之间的空隙；时长按慢走速度换算，走的距离和正常速度时一样"""
        f = self._slow_on() if self.cfg["slow_approach"] else 1.0
        self.g.hold([side], self.cfg["side_min"] / f)
        moved = self.g.last_moved
        p = self.g.hold([side], max(0.0, self.cfg["side_max"] - self.cfg["side_min"]) / f, stop_on=TABLE_PROMPTS)
        # 记下真正横着走了多久（按慢走计），回去时也用慢走原路退回过道
        self.side = (side, moved + self.g.last_moved, self.g.slow)
        self.g.set_slow(False)
        return p

    def _slow_on(self):
        """切到缓慢行走，返回速度倍数（正常速度时为 1）"""
        self.g.set_slow(True)
        return self.cfg["slow_factor"] if self.g.slow else 1.0

    def locate(self, n, has_prompt):
        """判断现在是不是站在 n 号桌边。号码牌常被顾客、菜、气泡挡住，所以结合看到的其他牌子判断：
        镜头朝前，站在某一排时只能看到同排对面和前方几排的牌子；看到身后排的牌子说明走过头了。
        返回 (是否到了, 推测停在哪桌或 None)"""
        vis = self.g.visible_signs()
        row = TABLES[n][1]
        # 面朝吧台时看到的是更靠吧台的几排；把排号反过来算，下面的规则就不用改
        rowof = (lambda d: -TABLES[d][1]) if self.cfg["facing_counter"] else (lambda d: TABLES[d][1])
        row = rowof(n)
        if vis:
            near, (near_scale, near_score) = max(vis.items(), key=lambda kv: kv[1][0])
            if near == n:
                return True, n
            # 牌子越大越可信；1.0 倍左右的要分数够高（避免把气泡、杂物误认成牌子）
            if near_scale >= self.cfg["near_scale"] and (near_score >= 0.88 or near_scale >= 1.3):
                return False, near  # 贴着别的桌子
        if n in (1, 2):
            other = 3 - n  # 1、2 号桌在桌子正下方看，自己的牌子约 0.9 倍；看到另一张前排桌的牌子就是停错了
            if other in vis and vis[other][0] >= 0.85 and vis[other][1] >= 0.88:
                return False, other
        if n in vis:
            # 3-6 号桌到位时自己的牌子至少 1.0 倍；只有 0.8~0.9 倍说明那张桌子还在前面
            if n in (1, 2) or vis[n][0] >= 0.95:
                return True, n
            return False, None
        behind = [d for d in vis if rowof(d) < row and vis[d][1] >= 0.88]
        if behind:
            return False, min(behind, key=lambda d: -vis[d][0])
        return has_prompt, None

    def _fix_row(self, n, seen):
        """停在了同一侧别的排（比如想去 5 却在 3 旁边）：先退回过道，按排差前后走，再横向靠桌"""
        diff = TABLES[n][1] - TABLES[seen][1]
        self.log(f"停在了 {seen} 号桌，差 {diff} 排，就地补走")
        self.back_to_aisle()
        t = abs(diff) * self.cfg["row_gap"]
        self.g.hold(["w" if diff > 0 else "s"], t)
        self.depth += t if diff > 0 else -t
        side = "d" if TABLES[n][0] == "right" else "a"
        p = self._approach(side)
        return p or self._search()

    def _go_front_table(self, n):
        """1、2 号桌离吧台最近：沿吧台滑到桌子正下方，直接往上走到出现提示。返回看到的提示"""
        self.g.hold(["s", "a"], self.cfg[f"below{n}"])
        self._at(f"{n}号桌")
        first = self.cfg["yaw_walk_front"]
        p = self.g.hold(["w"], first, stop_on=TABLE_PROMPTS)
        if not p:
            self._fix_yaw_in_hall()
            p = self.g.hold(["w"], self.cfg["below_max"] - first, stop_on=TABLE_PROMPTS)
        self.depth = self.cfg["below_max"]
        self.side = None  # 回吧台直接按 S 就行，不用退回过道
        return p

    def retrace(self):
        """按轨迹记录原路返回：把从原点开始走过的每一段，倒着顺序、反方向再走一遍，回到饮品台附近"""
        if not self.track:
            return False
        total = sum(d for _, d in self.track)
        self.log(f"按轨迹原路返回（{len(self.track)} 段，共 {total:.1f} 秒）")
        moves, self.track = list(reversed(self.track)), []
        self.pos = None  # 返回途中不再记录
        spent = 0.0
        for keys, d in moves:
            back = [OPPOSITE[k] for k in keys]
            t = d / (0.71 if len(back) > 1 else 1.0)
            self.g.hold(back, t, unstick=True)
            spent += t
            if spent > self.cfg["retrace_max"] or self.g.has_prompt("p_drink"):
                break
        self.g.trace("原路返回后")
        return True

    def recover(self):
        """迷路（走出了送餐范围）时回到吧台前"""
        self.g.trace("迷路")
        self.pos = None
        if self.cfg["facing_counter"]:
            # 面朝吧台时吧台就在正前方：先离开墙角一点，往过道方向（远离饮品台那头）挪，再往吧台走贴住，校正视角
            # 走出去多半是越过了饮品台（左边）：往过道方向（右边）横着走回来，走不通会自动往反方向退一下再试
            self.log("好像走出了送餐范围，往过道方向走回来再贴回吧台")
            self.at_drink = False
            self._fix_yaw_in_hall()
            self.depth = 3.0
            self.pin_counter()
            self.g.trace("迷路后贴回吧台")
            return True
        self.log("好像走出了送餐范围，按演示的路线走回去")
        # 用户演示：从吧台右下角先往前走出角落，再往左回到大厅、回到吧台前（镜头被墙挤歪，到空地会自己转回来）
        for key, sec in self.cfg["recover_seq"]:
            self.g.hold([key], sec)
        time.sleep(0.8)  # 等镜头转回来
        self.depth = 3.0
        self._fix_yaw_in_hall()
        if self.g.visible_signs():
            return True
        step, steps = int(self.cfg["turn_step"]), int(self.cfg["turn_steps"])
        turned, found = 0, False
        self.log("还是没看到桌子，转视角找")
        for _ in range(steps):
            d, _ = self.g.which_table()
            if d:  # 只认桌号牌，绿色识别在斜视角下会把别的东西当成桌子
                found = True
                break
            self.g.look(dx=step)
            turned += step
        if found:
            self.log(f"找到桌子了（转了 {turned}），往那边走")
            self.g.hold(["w"], self.cfg["recover_walk"])
        else:
            self.log("转了一圈也没找到桌子")
        if turned:
            self.g.look(dx=-turned)  # 转回原来的朝向，W/A/S/D 方向才对
        self.depth = 3.0  # 不知道离吧台多远，按最远退
        return found

    # ---------- 服务动作 ----------
    def _wait(self, cond, timeout, step=0.2):
        end = time.time() + timeout
        while time.time() < end:
            self.g.check()
            if cond():
                return True
            time.sleep(step)
        return False

    def fill_hands(self):
        """满堂彩开过后、还没打烊：每次回来把物品栏拿满。
        倒计时走完后还有十几秒「已打烊」：不再进新客人、只送已经在等的，这时要多少拿多少，省时间"""
        if not self.fired():
            return False
        left = self.g.left_est()
        if left is not None and left > 8:
            return True  # 倒计时还早，肯定没打烊（不用每次都 OCR）
        if time.time() - getattr(self, "_closed_chk_t", 0) > 1.5:
            self._closed_chk_t = time.time()
            self._closed_now = self.g.shop_closed()
        return not self._closed_now

    def dishes_ready(self):
        """吧台上现在摆着几份做好的菜（看菜品图标；认不出就按 0）"""
        try:
            return len(self.g.takeable_icons())  # 当原点的凉糕不算
        except Exception:
            return 0

    def surplus(self):
        """在途的菜 = 正在做的 + 吧台上做好的 = 一共做的 - 已经送出的 - 手上拿着的（手上的算已送出）"""
        return self.cooked - self.served_dish - self.sync_inv().count("dish")

    def cook_enough(self, left):
        """v30：在途的菜够这一局剩下的时间送了就不开料理界面（每开一次 6 秒左右）。
        剩下要送的菜 ≈ 剩余秒数/60 × cook_need_rate + cook_need_pad；最多隔 cook_max_gap 秒还是要开一次（防记账偏多）"""
        if not self.cfg.get("cook_cap", True) or left is None:
            return False
        if time.time() - self.last_cook > float(self.cfg.get("cook_max_gap", 100)):
            return False
        need = left / 60.0 * float(self.cfg.get("cook_need_rate", 3.0)) + float(self.cfg.get("cook_need_pad", 4))
        sur = self.surplus()
        if sur >= need:
            self.log(f"在途 {sur} 份够剩下 {left:.0f} 秒送（估计要 {need:.0f} 份）：这次不开料理界面")
            return True
        return False

    def cook(self):
        if self.closed or (self.cooked > 0 and self.g.shop_closed()):
            if not self.closed:
                self.log("已打烊：不再开厨房做菜，只把手上和吧台上的送完")
            self.closed = True
            return False
        if self.cooked > 0 and self.fired() and self.cfg.get("fast_end_cook", True):
            # v37：满堂彩开过后客人什么都收：只做 15 秒出锅的凉糕（界龙肉卷约 55 秒，收尾那 100 秒总断菜）。
            # 只在饮品台前开（关界面后这里马上能重新定位），隔 fast_cook_every 秒补一次空位
            left = self.g.left_est()
            if ((self.at_drink or getattr(self, "wn_at", None) == "D") and (left is None or left > -float(self.cfg.get("fast_cook_until", 10)))
                    and time.time() - self.last_cook > float(self.cfg.get("fast_cook_every", 20))
                    and not self.g.shop_closed()):
                return self._cook(read_panel=True, fill=True, card=self.cfg["anchor_dish_card"])
            return False
        if self.cooked > 0 and self.g.boost_active():
            # 满堂彩期间不开料理界面：实测加速中开过料理界面后几次都走丢（关界面后镜头/位置对不上）。
            # 按 R 之前已经在饮品台把厨房补满了
            return False
        if self.cooked > 0:
            # 厨房不能闲着：隔 cook_every 秒、在途的菜不到 cook_ahead 份就开一次料理界面，
            # 在界面里直接看每个厨房的空位补满（不再依赖看板点的读数，那个常常读不到导致厨房空转）
            every = self.cfg["cook_every"] / (2 if self.g.boost_active() else 1)  # 满堂彩期间做菜也变快
            left = self.g.left_est()
            # 倒计时不到 no_cook_left 秒：现在下单的菜做出来也来不及送，不再开料理界面（省下的时间用来送菜）
            if left is not None and left < self.cfg["no_cook_left"]:
                self.log(f"营业只剩 {left:.0f} 秒，不再做菜，只把吧台上和手上的送完")
                self.closed = True
                return False
            if (self.fired() and not getattr(self, "_post_fill", False) and left is not None
                    and self.cfg.get("post_boost_fill", True)):
                # v35：满堂彩一结束就再把厨房补满（加速期间不开料理界面，按 R 前那次的菜到收尾前就送光了，
                # 实测最后 40 秒在饮品台干等菜；倒计时到 0 后还有约 25 秒打烊时间可以送）
                self._post_fill = True
                self.log(f"满堂彩结束（倒计时 {left:.0f} 秒）：再把厨房补满一次")
                return self._cook(read_panel=True, fill=True)
            if (left is not None and left < self.cfg["no_cook_left"] + self.cfg["final_fill_window"]
                    and not getattr(self, "_final_filled", False)):
                if self.cook_enough(left):
                    self._final_filled = True
                    return False
                # 最后一次做菜：不看间隔和在途份数，把三个厨房的空位全部填满（之后就不再做了）
                self._final_filled = True
                self.log(f"营业还剩 {left:.0f} 秒：最后一次做菜，把厨房填满")
                return self._cook(read_panel=True, fill=True)
            if time.time() - self.last_cook < every:
                return False
            if self.surplus() >= self.cfg["cook_ahead"]:
                return False
            if time.time() - getattr(self, "last_cook_skip", 0) < 15:
                return False  # 刚判断过在途够：15 秒内不再判断（免得每圈都刷日志）
            if self.cook_enough(left):
                self.last_cook_skip = time.time()
                return False
        if (self.cooked > 0 and self.cfg.get("cook_defer", True)
                and time.time() - self.last_cook < self.cfg["cook_every"] + 30):
            # v44：手上有菜、桌上有人等着：先把这趟送完再开料理界面（吧台上的菜常常是够的，开一次要 6 秒）
            hand = self.sync_inv()
            if "dish" in hand and any(s_ in ("laotao", "order") for s_, _ in self.g.table_states().values()):
                return False
        return self._cook(read_panel=self.cooked > 0)

    def _cook(self, frees_known=(3, 3, 3), fill=False, read_panel=False, card=None):
        """打开料理界面，看三个厨房各有几个空位，按“轮流每个厨房一道”分配要做的份数，再关掉。
        开局第一次把空位全部补满；之后在途（正在做+吧台上）的菜不超过 cook_ahead 份"""
        sur = self.surplus()  # 开界面前算：料理界面会挡住物品栏，开着时读手上的菜会读成 0
        # 按 Tab 开料理界面；刚开局或镜头还在动时游戏可能没接到这一下，没打开就再按（最多 3 次）
        for _ in range(3):
            self.g.press("tab")
            if self._wait(lambda: self.g.kitchen_open(), 2.5):
                break
            self.g.close_quit_dialog()
        else:
            self.log("料理界面没打开")
            return False
        for _ in range(2):  # 先切到 1 号厨房
            self.g.press("q")
            time.sleep(0.35)
        first = self.cooked == 0
        lg = 0
        if first and self.cfg.get("anchor_dish"):
            # 坐标原点：先下一份糯米凉糕（15 秒最快出锅，摆在离饮品台最近的第一格），整局不拿
            pos = self.g.find_card(self.cfg["anchor_dish_card"])
            if pos:
                self.g.click(*pos)
                time.sleep(0.25)
                lg = 1
                self.log("下了一份糯米凉糕当坐标原点")
            else:
                self.log("料理界面没找到糯米凉糕的卡片，这局不放原点")
        # 空位：开局第一次一定是 [3,3,3]；read_panel 时到每个厨房当场看左下角的队列格
        frees = [3, 3, 3] if first or read_panel else list(frees_known)
        frees[0] -= lg
        budget = sum(frees) if (first or fill) else max(0, min(sum(frees), self.cfg["cook_ahead"] - sur))
        # 轮流分：每个厨房先做一道，还有剩余再每个厨房再加一道
        plan = [0, 0, 0]
        while budget > 0 and any(plan[i] < frees[i] for i in range(3)):
            for i in range(3):
                if budget > 0 and plan[i] < frees[i]:
                    plan[i] += 1
                    budget -= 1
        # 现在停在 1 号厨房，只去要下单的厨房（1 → 2 → 3），后面没有要做的就不再往后翻
        if read_panel:
            # 到每个厨房当场看空位，按顺序填，份数用完就不再往后翻（没翻到的厨房空位记成 None）
            left = sum(plan)
            plan, frees = [0, 0, 0], [None, None, None]
            for k in (1, 2, 3):
                time.sleep(0.3)
                frees[k - 1] = self.g.kitchen_free()
                plan[k - 1] = min(frees[k - 1], left)
                left -= plan[k - 1]
                if plan[k - 1] and not self._click_cards(k, plan[k - 1], card):
                    plan[k - 1] = 0
                if k < 3 and left > 0:
                    self.g.press("e")
                    time.sleep(0.3)
                else:
                    break
        for k in (() if read_panel else (1, 2, 3)):
            n = plan[k - 1]
            if n:
                time.sleep(0.3)
                pos = self.g.find_dish_card()
                if not pos:
                    self.log(f"{k} 号厨房没找到这道菜的卡片，跳过")
                    plan[k - 1] = 0
                else:
                    for _ in range(n):
                        self.g.click(*pos)
                        time.sleep(0.3)
                    self.cooked += n
            if k < 3 and any(plan[k:]):
                self.g.press("e")
                time.sleep(0.4)
        # 只有确认料理界面还开着才按 Esc：界面已经关了再按 Esc 会弹出「提前结束经营」。
        # 按一次后等它淡出（淡出动画期间还会被认成开着），等满 3 秒还开着才再按
        for _ in range(2):
            if not self.g.kitchen_open():
                break
            self.g.press("esc")
            if self._wait(lambda: not self.g.kitchen_open(), 3.0):
                break
        time.sleep(0.4)  # 等界面淡出（刚关界面后 4 秒内的大视角读数本来就不转，不用等满 0.8 秒）
        self.g.close_quit_dialog()
        self.last_cook = time.time()
        self.log(f"厨房空位 {frees}，这次做 {plan}{'（凉糕）' if card else ''}（共做 {self.cooked}，已送 {self.served_dish}，在途 {self.surplus()}）")
        return True

    def _click_cards(self, k, n, card=None):
        """料理界面停在 k 号厨房：点 n 次这道菜的卡片下单（card：别的菜的卡片模板，如凉糕）"""
        pos = self.g.find_card(card) if card else self.g.find_dish_card()
        if not pos:
            self.log(f"{k} 号厨房没找到这道菜的卡片，跳过")
            return False
        for _ in range(n):
            self.g.click(*pos)
            time.sleep(0.18)  # 连点下单（0.3 秒一下太慢，一局开五六次料理界面）
        self.cooked += n
        return True

    def get_drinks(self, tags):
        """到饮品台按香型拿饮品（tags 如 ["茶香", "果香"]，不超过上限）"""
        inv = self.sync_inv()
        tags = list(tags)[:max(0, min(int(self.cfg["max_drinks"]) - inv.count("drink"), 5 - len(inv)))]
        if not tags or not self.go_drink():
            return 0
        self._drink_try = time.time()  # 刚拿过：接着取菜时不再重复开饮品台
        before = self.g.hotbar_count()
        self.g.press("f")
        if not self._wait(self.g.drink_panel_open, 3):
            # v85：营业中（倒计时 > 3 秒）面板没开就关掉别的面板、重新对准饮品台再按一次
            #（04:17:52 满堂彩刚开、饮品台面板没打开，只拿了 4 份菜就去接 3、6 号，两桌都要回来补饮品，最后 4 号没接上）
            lf_ = self.g.left_est()
            p_ = self.g.prompt()
            if self.cfg.get("drink_retry", True) and (lf_ is None or lf_ > 3):
                self.log(f"饮品台面板没打开（提示 {p_}）：关面板、重新对准再按一次")
                self.wn_close_panels()
                if self.go_drink():
                    self.g.press("f")
                    if self._wait(self.g.drink_panel_open, 3):
                        p_ = "ok"
            if p_ != "ok":
                self.log("饮品台面板没打开")
                return 0
        cards = self.cfg["drink_cards"]
        for t in tags:
            x, y = cards.get(t, self.cfg["drink_card"])
            self.g.click(x, y)
            time.sleep(0.3)
        self.log("拿饮品：" + "、".join(DRINK_NAMES.get(t, t) for t in tags))
        # 右键关闭面板（和手动操作一样）；关不掉就再右键，不用 Esc（面板已关时 Esc 会弹出「提前结束经营」）
        for _ in range(3):
            self.g.click(x, y, right=True)
            if self._wait(lambda: not self.g.drink_panel_open(), 1.5):
                break
        self.close_drink_panel()
        got = self.g.hotbar_count() - before
        self.sync_inv()
        self.log(f"拿到饮品 {got} 瓶")
        return got

    def close_drink_panel(self):
        """饮品台面板还开着就关掉（实测右键没关上，开着面板取了 27 秒菜一份没拿到）。面板开着时 Esc 只关面板"""
        for _ in range(3):
            if not self.g.drink_panel_open():
                return
            self.log("饮品台面板还开着，按 Esc 关掉")
            self.g.press("esc")
            self._wait(lambda: not self.g.drink_panel_open(), 1.5)
        self.g.close_quit_dialog()

    def get_dishes(self, d):
        """到出餐台取 d 份菜：沿吧台往左找取菜提示，按一次 F 拿一份；这一盘拿完就继续往左找下一盘"""
        if d <= 0:
            return 0
        if self.cfg["facing_counter"]:
            return self._get_dishes_seen(d)
        if not self.anchor():
            return 0
        got = 0
        for _ in range(int(self.cfg["dish_tries"])):
            if got >= d or self.g.hotbar_count() >= 5:
                break
            if not self.g.has_prompt("p_dish"):
                if not self.g.hold(["s", "a"], self.cfg["dish_timeout"], stop_on=("p_dish",)):
                    break
            before = self.g.hotbar_count()
            self.g.press("f")
            time.sleep(0.5)
            if self.g.hotbar_count() > before:
                got += 1
                self.sync_inv()
            else:
                self.g.hold(["a"], 0.3)  # 这一盘没了，往左挪一点找下一盘
        self._at("出餐台")
        self.log(f"取到菜 {got} 份")
        self.g.trace(f"取菜{got}")
        return got

    def rare_elsewhere(self):
        """面前这盘不是珍馐、但吧台别处有珍馐：先去拿珍馐"""
        icons = self.g.takeable_icons()
        here = [r for x, r in icons if abs(x - 480) < 60]
        return any(r for _, r in icons) and not any(here)

    def _bar_hold(self, keys, t, **kw):
        """v72：取菜时沿吧台横走（v97：挪过就取消开局「不推不滑当作在饮品台」的捷径）。饮品台是吧台最左端、菜都摆在它右边：已经在饮品台前或凉糕原点在人右边时不再往左
        （11-09 12:43 对珍馐时往左冲过饮品台、滑进左边走廊，镜头被墙带歪，回不到饮品台算走丢）；往左一次最多 1.5 秒"""
        if keys == ["a"] and self.cfg.get("bar_left_guard", True):
            lgx = self.g.anchor_dish_x()
            if lgx is not None and lgx >= 450:
                self.log(f"取菜：已到吧台最左（凉糕在 x={lgx}），不再往左")
                return False
            # 站在饮品台前：最近那盘菜在画面里可能略偏左（13:21 两次全拦，一份没取到），只许挪一小步
            t = min(t, 0.3 if self.g.has_prompt("p_drink") else 1.5)
        self._start_at_D = False
        return self.g._hold(keys, t, **kw)

    def _get_dishes_seen(self, d):
        """面朝吧台：贴着吧台看上方的菜品图标，朝最近的那个横着走过去，出现取菜提示就按 F。
        菜从饮品台那头开始摆，所以不在吧台前时先回饮品台再取"""
        if self.at_table is not None or self.pos is None or self.pos[1] > 0.3:
            self.anchor()
        self.close_drink_panel()
        if self.at_drink and time.time() - getattr(self, "_drink_try", 0) > 5:
            # 人在饮品台：先把饮品拿了再往过道方向取菜，一趟办完（不然取完菜又滑回饮品台再对一次）
            tags = self.drink_plan()
            if tags:
                self._drink_try = time.time()
                self.get_drinks(tags)
        got, probed, miss_take = 0, False, 0
        rare_tried = False
        t_dish0 = time.time()  # v98：记下这次取菜用了几秒
        if d > 0 and self.g.dish_icons_rare():
            self._start_at_D = False  # v97：要沿吧台挪去取菜了：之后不能再当「开局没走动」
        for _ in range(int(self.cfg["dish_tries"])):
            if got >= d or self.g.hotbar_count() >= 5:
                break
            # 取菜提示上写的是菜名（换菜后旧的提示模板认不出），所以靠菜品图标对准角色来定位：
            # 朝目标图标横着走，图标走到画面正中（角色正上方）就按 F
            target, tgt_rare = None, False
            aligned = False
            t_align = time.time()
            lgx = None  # 凉糕原点在画面里的 x
            for _ in range(8):
                icons = self.g.dish_icons_rare()
                if not icons:
                    break
                lgx = self.g.anchor_dish_x()
                if lgx is not None:
                    icons = [(x, r) for x, r in icons if abs(x - lgx) > 35]
                    if not icons:
                        break  # 吧台上只剩凉糕原点：不拿
                # 「珍馐」有额外加成，先拿；没有珍馐就拿离角色最近的（角色在画面正中）
                # 满堂彩加速期间速度优先：走得快、对珍馐常要来回好几次，反而耽误送餐，这时就拿最近的
                # 对珍馐最多花 2.5 秒：对不上就拿最近的（实测有时来回挪 9 秒，比珍馐的加成亏得多）
                lf_r = self.g.left_est()
                rare = [] if (self.g.boost_active() or (self.fired() and rare_tried)
                              or (self.cfg.get("close_no_rare", True) and lf_r is not None and lf_r < 5)  # v92：收尾不对珍馐，拿最近的
                              or time.time() - t_align > (1.0 if self.fired() else 2.5)) else [x for x, r in icons if r]
                target = min(rare or [x for x, _ in icons], key=lambda v: abs(v - 480))
                if rare and not tgt_rare:
                    self.g.trace("珍馐目标")  # 留图核对吧台上的珍馐识别
                tgt_rare = bool(rare)
                if self.pos is not None:
                    self.at_drink = False  # 离开饮品台往过道方向取菜
                # 目标是珍馐时不能见到取菜提示就停：路过的普通菜也会出提示，一停就拿成普通菜了，要走到珍馐正下方
                if tgt_rare:
                    # 按 F 拿的是头上有黄色小三角的那盘：小三角在珍馐头上才算对准（实测只看图标居中，常拿成旁边的普通菜）
                    sel = self.g.selected_dish_x()
                    if sel is not None and abs(sel - target) <= 30:
                        aligned = True
                        break
                    if sel is not None:
                        key = "d" if target > sel else "a"
                        self.at_head = False
                        self._bar_hold([key], max(0.15, min(0.6, abs(target - sel) / self.cfg["dish_px_per_sec"])))
                        time.sleep(0.15)  # 等选中小三角跟上再看
                        continue
                if abs(target - 480) <= self.cfg["dish_center_tol"] or (not tgt_rare and self.g.can_take()):
                    break
                # 画面右边就按实际的 D 键（这里直接用实际按键，不经过方向对调）
                key = "d" if target > 480 else "a"
                t = min(3.0, abs(target - 480) / self.cfg["dish_px_per_sec"])
                self.at_head = False
                if self._bar_hold([key], max(0.08, t), stop_on=() if tgt_rare else ("p_take",)) and not tgt_rare:
                    break  # 走着走着取菜提示出来了，就地停下（珍馐不停，走完再看一次图标对准没有）
            if target is None:
                if got == 0 and not probed and self.surplus() > 0 and not self.cfg.get("anchor_dish"):
                    # 图标没认出来（也可能真没菜）：菜从饮品台那头往右摆，贴着吧台往右走一小段，取菜提示出来就拿
                    probed = True
                    if self.g._hold(["d"], 1.5, stop_on=("p_take",)) and self.g.can_take():
                        before = self.g.hotbar_count()
                        self.g.press("f")
                        time.sleep(0.5)
                        if self.g.hotbar_count() > before:
                            got += 1
                            self.sync_inv()
                            self.log("没认出菜品图标，往右摸到了菜")
                            continue
                break  # 出餐台上没有做好的菜
            # 只有出现「界龙肉卷」这条取菜提示才按 F：没对准时提示是「放回出餐台」，按了会把手上的菜放回去
            if not self.g.can_take():
                key = "d" if target > 480 else "a"
                self._bar_hold([key], 0.6, stop_on=("p_take",))
            if not self.g.can_take():
                self.log("没对准菜（提示是放回出餐台），不按 F")
                miss_take += 1
                if miss_take >= 2:
                    break  # 连着两次对不准：多半只剩凉糕/菜在饮品台左边够不着（实测开局空转 16 秒），先去办别的
                continue
            if self.cfg.get("anchor_dish"):
                # 按 F 前再确认：按 F 会拿走的（头上亮黄色小三角的）不是凉糕。小三角常认不出：
                # 人在画面正中（x≈480），凉糕离画面中间不到 70 像素就是离人最近的那盘，按 F 拿的就是它
                sel = self.g.selected_dish_x()
                k = self.g.anchor_dish_x()
                if k is not None and ((sel is not None and abs(sel - k) <= 30) or abs(k - 480) <= 70):
                    self.log("选中的是当原点的凉糕，不拿，往右挪一点拿别的")
                    self.g._hold(["d"], 0.3)  # 菜都摆在凉糕右边（画面往右 = 真实 D）
                    continue
            before = self.g.hotbar_count()
            # 按 F 前看选中小三角：在珍馐头上就是拿珍馐（物品栏的珍馐角标要 1 秒左右才显示，不等它）
            sel = self.g.selected_dish_x() if tgt_rare else None
            on_rare = tgt_rare and sel is not None and abs(sel - target) <= 30
            if tgt_rare:
                rare_tried = True  # v52（用户）：满堂彩开过后珍馐只试一次（这次按 F 之后不再去够），没拿到就拿最近的
            self.g.press("f")
            if self._took_dish(before):
                got += 1
                self.sync_inv()
                if on_rare:
                    self.log("拿到一份珍馐")
                elif tgt_rare:
                    self.log("想拿珍馐，没对准，拿到的是旁边那盘")
                    self.g.trace("珍馐没对准")
                # v98：面前这盘还能拿（提示还是「取菜」、选中的不是当原点的凉糕）：原地再按 F，不重新找图标、不挪位置
                while (self.cfg.get("dish_fast_repeat", True) and not tgt_rare and got < d
                       and self.g.hotbar_count() < 5 and self.g.can_take()):
                    if self.cfg.get("anchor_dish"):
                        sel = self.g.selected_dish_x()
                        k = self.g.anchor_dish_x()
                        if k is not None and ((sel is not None and abs(sel - k) <= 30) or abs(k - 480) <= 70):
                            break
                    b2 = self.g.hotbar_count()
                    self.g.press("f")
                    if not self._took_dish(b2):
                        break
                    got += 1
                    self.sync_inv()
        self._at("出餐台")
        self.log(f"取到菜 {got} 份（{time.time() - t_dish0:.1f} 秒）")
        self.g.trace(f"取菜{got}")
        return got

    def _took_dish(self, before, timeout=0.8):
        """v98：按 F 后每 0.1 秒看一次物品栏，多了一格就算拿到（原来固定等 0.5 秒）"""
        end = time.time() + timeout
        time.sleep(0.15)
        while True:
            if self.g.hotbar_count() > before:
                return True
            if time.time() >= end:
                return False
            time.sleep(0.1)

    def sync_inv(self):
        """按物品栏画面更新手上有什么（不靠自己记账，免得和实际对不上）"""
        self.inv = [x for x in self.g.hotbar_items() if x]
        return self.inv

    def serve_here(self, need, n=None, _again=0):
        """站在桌边上菜：只送这桌顶部还缺的那几样（need，例如 ["dish", "drink"]），
        在物品栏里找同种的格子（饮品还要合 n 号桌点的香型），按数字选中再按 F；
        送出去物品栏会少一格。返回送出去的东西列表"""
        need = [] if self.skip_table(n) else list(need)
        served = []
        tried = set()
        refreshed = 0  # v68：送不进去后按顶部重新读的次数（最多 2 次）
        while need:
            if not self.g.has_prompt("p_shangcai"):
                # v91：送出一样后「上菜」提示会消失一下（07:01:06 2 号桌送完菜就退出，饮品没送，2 秒后结算）：等它回来
                if not (served and self.cfg.get("serve_wait_prompt", True)
                        and self._wait(lambda: self.g.has_prompt("p_shangcai"), 0.8, step=0.15)):
                    break
            frame = self.g.frame_full()
            detail = self.g.hotbar_detail(frame)
            items = [("drink" if x in DRINK_TAGS else x) for x in detail]
            keep = self.reserved_slots(n, detail)  # 留给送了一半的别的桌的，不能给这桌
            # 珍馐有额外的满堂彩加成：手上有珍馐就先送珍馐
            rare = self.g.hotbar_rare(frame)
            want_n = self.want.get(n) if n else None
            # 珍馐排最前；饮品里合这桌要求的排在不合的前面（v80：满堂彩后不合的也能送，但有对的先送对的）
            order = sorted(range(len(detail)), key=lambda i: (-(detail[i] == "dish" and rare[i] >= self.cfg["rare_hb"]),
                                                              -(detail[i] in DRINK_TAGS and bool(want_n)
                                                                and drink_cond_ok(want_n, detail[i]))))
            slot = next((i for i in order
                         if items[i] in need and items[i] not in tried and i not in keep
                         and (detail[i] not in DRINK_TAGS or self.drink_ok(n, detail[i]))), None)
            if slot is not None and detail[slot] == "dish" and self.cfg.get("log_rare"):
                self.log(f"送菜用第 {slot + 1} 格（各格珍馐角标 {rare}，{'珍馐' if rare[slot] >= self.cfg['rare_hb'] else '普通'}）")
                try:
                    d = os.path.join(BASE_DIR, "test_channel", "hb")
                    os.makedirs(d, exist_ok=True)
                    name = time.strftime("%H%M%S") + "_" + "-".join(map(str, rare)) + ".png"
                    cv2.imencode(".png", frame[900:1010, 700:1220])[1].tofile(os.path.join(d, name))
                except Exception:
                    pass
            if slot is None:
                break  # 手上没有这桌还缺的东西（或饮品香型不对、或者要留给送了一半的桌）
            kind = items[slot]
            if kind == "drink":
                self.log(f"送{DRINK_NAMES.get(detail[slot], '饮品')}（{detail[slot]}）")
            before = sum(1 for x in items if x)
            self.g.press(str(slot + 1))
            time.sleep(0.25)
            self.g.press("f")
            if self.cfg.get("serve_poll", True):
                # v99：按 F 后每 0.1 秒看一次物品栏，少了一格就接着送下一样（原来固定等 0.7 秒，没少再等 0.4 秒）；
                # 最多还是等 1.1 秒，判「不收」的时间和原来一样
                t_end = time.time() + 1.1
                time.sleep(0.2)
                while True:
                    now_n = sum(1 for x in self.g.hotbar_items() if x)
                    if now_n < before or time.time() >= t_end:
                        break
                    time.sleep(0.1)
            else:
                time.sleep(0.7)
                now_n = sum(1 for x in self.g.hotbar_items() if x)
                if now_n >= before and self.cfg.get("serve_recheck", True):
                    # v76：物品栏有时晚一点才刷新（14:32 4 号桌菜其实收了、桌位栏已是笑脸，却判成「不收」）：再看一次
                    time.sleep(0.4)
                    now_n = sum(1 for x in self.g.hotbar_items() if x)
            if now_n < before:
                served.append(kind)
                need.remove(kind)
                if kind == "dish":
                    self.served_dish += 1
                self.ep_served(n, kind, remaining=len(need))
            else:
                tried.add(kind)  # 这种客人不收（谜语要的不是这个），换别的种类试
                if kind == "dish" and self.cfg.get("trace_reject", True):
                    # v73：菜被拒（11-09 三次：点两份菜/一菜一饮的客人不收界龙肉卷，结算落下）：存全分辨率截图，看他要什么
                    try:
                        if not self.who.get(n):
                            self._read_request(n)  # 满堂彩后没读过点单：补读一次，日志里留下他点菜的那句话
                        d_ = os.path.join(BASE_DIR, "test_channel", "rejects")
                        os.makedirs(d_, exist_ok=True)
                        cv2.imencode(".png", self.g.frame_full())[1].tofile(
                            os.path.join(d_, f"{time.strftime('%m%d_%H%M%S')}_{n}号桌_{self.who.get(n, '?')}号老饕.png"))
                        self.log(f"{n} 号桌不收界龙肉卷（{self.who.get(n, '?')} 号老饕）：截图存在 test_channel/rejects")
                    except Exception:
                        pass
                if n and self.cfg.get("refresh_need", True):
                    # v68：送不进去时重新看顶部这桌还缺什么（11-09 第三局：刚接待完顶部还是上一位客人的「一菜一饮」，
                    # 先送茶香被拒，新客人其实要两份菜，手上 3 份菜一份没上就走了，结算落下这桌）
                    st_n, now_need = self.g.table_states().get(n, ("", []))
                    if st_n == "order" and now_need and sorted(now_need) != sorted(need) and refreshed < 2:
                        refreshed += 1
                        self.log(f"{n} 号桌实际还缺 {now_need}（刚才读的是 {need}）：按新的送")
                        need = list(now_need)
                        tried = set()
        self.sync_inv()
        if (served and n and _again < 1 and self.cfg.get("refresh_need", True)
                and not self.skip_table(n) and self.g.has_prompt("p_shangcai")):
            # v68：读的需求送完了但提示还在：看顶部这桌是不是还缺（读到的是上一位客人的旧图标时会少送）
            st_n, now_need = self.g.table_states().get(n, ("", []))
            if st_n == "order" and now_need:
                self.log(f"{n} 号桌还缺 {now_need}：接着送")
                served += self.serve_here(now_need, n, _again + 1)
        return served

    def receive(self, n):
        """去 n 号桌接待"""
        if not self.go_table(n):
            return False
        if self.target_done:
            return True
        if not self.g.has_prompt("p_jiedai"):
            self._search()  # 刚停下时提示可能还没出来，前后小步找一下
        if self.g.has_prompt("p_jiedai"):
            self.g.press("f")
            time.sleep(0.6)
            if self.cfg.get("recv_refire", True) and self.g.has_prompt("p_jiedai"):
                # v77：按了 F 接待提示还在（15:32 1 号桌按 F 没接上，等 5 秒没点单就走了，结算落下）：再按一次
                self.g.press("f")
                time.sleep(0.6)
                self.log(f"{n} 号桌：第一下 F 没接上，又按了一次")
            if self.cfg.get("recv_verify", True) and self.cfg.get("wang_nav"):
                # v79：按完 F 看顶部：这桌还是「老饕」（等接待）就是没接上（11-09 17:35 用户实测 6 号桌站浅了，
                # 记了「已接待」其实没接上，等 4 秒走了，绕回来又花 19 秒）：往桌子那边靠一点，有提示再按
                if self._wait(lambda: self.g.table_states().get(n, ("", []))[0] != "laotao", 1.2, step=0.2) is False                         and self.g.table_states().get(n, ("", []))[0] == "laotao":
                    side = self.wn_table_side(n)
                    for _ in range(4):
                        self.g._hold([side], 0.12, unstick=False)
                        self.wn_lat = getattr(self, "wn_lat", 0.0) + (0.12 if side == "d" else -0.12)
                        if self.g.has_prompt("p_jiedai"):
                            self.g.press("f")
                            time.sleep(0.6)
                            break
                    ok_now = self.g.table_states().get(n, ("", []))[0] != "laotao"
                    self.log(f"{n} 号桌：按 F 后还在等接待，往桌边靠了靠{'，接上了' if ok_now else '，还是没接上'}")
            self.log(f"{n} 号桌：已接待")
            self.ep_receive(n)
            self.serve_after_receive(n)
            self.fix_yaw_after_f()
            return True
        if self.cfg.get("wang_nav") and self.g.table_states().get(n, ("", []))[0] != "laotao":
            # 这桌已经不在等接待了（刚接待/送齐过，顶部栏刚才还没刷新）：眼前的「上菜」提示多半是身后那排的桌，别乱送
            # （实测接待送齐 3 号后又来一次 receive(3)，对着 5 号的提示「上了 1 样」「上了 0 样」，白耗时间）
            return True
        if self.g.has_prompt("p_shangcai"):
            # 这桌已经点完单在等菜了：手上有什么就上什么（菜填菜位、饮品填饮品位，满了提示会消失）
            served = self.serve_here(["dish", "dish", "drink", "drink"], n)
            self.log(f"{n} 号桌：提示已经是上菜，直接上了 {len(served)} 样")
            if not served:
                # 多半站在了别的桌旁边（这桌其实还在等接待）：先办别的，坐标作废
                self.fail_until[n] = time.time() + self.cfg["recv_cooldown"]
                self.pos = None
            return True
        if self.cfg.get("wang_nav") and self.cfg.get("recv_wait_seat", 3.0) > 0:
            # v56：客人还在走向座位（顶部是「老饕」、桌边没提示）：原地等一会儿再走（12:31 5 号桌走开去补货，回来时已结算）
            wait_s = float(self.cfg.get("recv_wait_seat", 3.0))
            others = [m for m, (s_, _) in self.g.table_states().items() if m != n and s_ in ("laotao", "order")]
            if not others and self.cfg.get("recv_wait_idle", 10.0) > wait_s:
                # v71：别的桌都没事（没人等接待、没人等菜）：走开也只是回吧台干等，不如在这桌多等客人坐下
                # （11-09 12:04 1 号桌结算前 12 秒才来，等 3 秒没坐下就走了，3 秒后结算，落下这桌）
                wait_s = float(self.cfg.get("recv_wait_idle", 10.0))
            if self._wait(lambda: self.g.has_prompt("p_jiedai"), wait_s, step=0.2):
                self.g.press("f")
                time.sleep(0.6)
                self.log(f"{n} 号桌：等客人坐下后接待")
                self.ep_receive(n)
                self.serve_after_receive(n)
                self.fix_yaw_after_f()
                return True
        self.log(f"{n} 号桌：没看到接待提示（客人多半还没坐下），{self.cfg['recv_cooldown']} 秒内先办别的")
        self.fail_until[n] = time.time() + self.cfg["recv_cooldown"]
        return False

    def read_boards(self):
        """在看板点（中间过道、第一排桌子之间，刚校正完视角）读一次三块厨房看板的空位（记下时间）。
        做菜已经改成在料理界面里直接看空位，默认不再读（省掉每趟的停顿和存图）"""
        if not self.cfg["read_boards"]:
            return
        try:
            time.sleep(0.15)  # 等画面停稳
            self.board = (self.g.board_free(), time.time())
            self.log(f"看板空位 {self.board[0]}")
            self.save_board_shot()
        except Exception:
            pass

    def save_board_shot(self):
        """站在中间过道、离吧台不远时，存一张全分辨率截图：吧台后面的厨房看板（队列空位）看得清，
        攒够样本后用来做“不开厨房也知道有几个空位”"""
        if time.time() - getattr(self, "_board_t", 0) < 20:
            return
        self._board_t = time.time()
        try:
            d = os.path.join(BASE_DIR, "test_channel", "boards")
            os.makedirs(d, exist_ok=True)
            cv2.imencode(".png", self.g.frame_full())[1].tofile(os.path.join(d, time.strftime("%m%d_%H%M%S") + ".png"))
        except Exception:
            pass

    def save_request(self, n):
        """客人点完单时存一张全分辨率截图（气泡里的需求文字看得清），以后用来做读谜语（默认关：存图要半秒）"""
        if not self.cfg["save_requests"]:
            return
        try:
            d = os.path.join(BASE_DIR, "test_channel", "requests")
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%m%d_%H%M%S") + f"_{n}号桌.png"
            cv2.imencode(".png", self.g.frame_full())[1].tofile(os.path.join(d, name))
        except Exception:
            pass

    def read_request(self, n):
        if self.fired() and not self.cfg.get("read_after_fire", True):
            return  # 满堂彩开过后不用对谜语，省掉读气泡的时间
        # v80（用户）：满堂彩后也读饮品要求——手上刚好有对的就送对的（多拿赏钱），没有就随便送，不特意回去拿
        self._read_request(n)

    def _read_request(self, n):
        """客人刚点完单：读气泡，记下这桌饮品要什么香型"""
        if not self.cfg["drink_by_request"]:
            return
        try:
            if self.riddle is None:
                self.riddle = RiddleReader()
            r = self.riddle.read(self.g.frame_full())
        except Exception as e:
            self.log(f"读点单失败：{e}")
            return
        dish, drink = r["dish"], r["drink"]
        if dish and dish[0] >= self.cfg["ocr_min"]:
            self.log(f"{n} 号桌点单：「{dish[2]}」（{dish[1]} 号老饕）")
            self.who[n] = dish[1]
            soup = soup_cond(dish[3])
            self.soup_line[n] = soup
            if dish[1] not in self.cfg["skip_laotao"] and not soup and n in self.held:
                self.held.discard(n)  # 之前压它是读到了别桌的气泡：这桌其实不是要两份甜品的老饕，放出来照常送
                self.log(f"{n} 号桌其实是 {dish[1]} 号老饕，不压了")
            d = our_dish(self.cfg["our_dish"])
            self.dish_good[n] = bool(d) and dish_cond_ok(dish[3], d)
            if self.dish_good[n]:
                self.log(f"{n} 号桌：{self.cfg['our_dish']}正合他的点菜要求")
            # 只有点两份甜品（顶部是两个菜位、没有饮品位）的才压着：点了饮品的，饮品送对就算成功，照常送
            two_dish = self.g.table_states()[n][1].count("dish") >= 2
            # 界龙肉卷正合他这句的（如 1 号「管饱的碳水，但别辣」）照常送，满足不了的才压
            # 要两份汤的（不管几号老饕）也压：界龙肉卷不是汤，送了断连击（用户定）
            if (dish[1] in self.cfg["skip_laotao"] or soup) and not self.fired() and two_dish and not self.dish_good.get(n):
                # 压着不送会一直占着这张桌，新客人坐不进来：同时最多压 max_held 张，再来的照常送（成功率也有六成）
                if len(self.held) < int(self.cfg["max_held"]):
                    self.held.add(n)
                    self.log(f"{n} 号桌是 {dish[1]} 号老饕：我们的菜不合他口味，送了会打断连续上餐，这桌先不送")
                else:
                    self.log(f"{n} 号桌是 {dish[1]} 号老饕，但已经压着 {len(self.held)} 张桌了，这桌照常送（腾座位）")
        if self.want.get(n) and not getattr(self, "_want_guess", {}).get(n):
            return  # 这位客人的饮品要求已经读到过了：后面的读数可能是别桌的气泡，不覆盖（换新客人时会清空）
        if drink and drink[0] >= self.cfg["ocr_min"]:
            self.want[n] = drink[3]
            self.__dict__.setdefault("_want_guess", {})[n] = False
            ok = [t for t in DRINK_TAGS if drink_cond_ok(drink[3], t)]
            self.log(f"{n} 号桌饮品：「{drink[2]}」→ {drink[3]}（可以送：{'、'.join(ok) or '无'}）")
        elif dish and dish[0] >= self.cfg["ocr_min"] and self.riddle.guess_drink(dish[1]):
            # 饮品那行字是淡的、没读出来：按老饕是谁猜（他的两句饮品要求都满足的香型优先）
            self.want[n] = self.riddle.guess_drink(dish[1])
            self.__dict__.setdefault("_want_guess", {})[n] = True  # 猜的：之后读到真的要求可以覆盖
            self.log(f"{n} 号桌饮品：没读到，按 {dish[1]} 号老饕的习惯猜 {self.want[n]}")
        else:
            self.log(f"{n} 号桌饮品：没读到，先不送饮品，下次到桌边再读")

    def read_turned(self, n):
        """饮品那行没读到：转过去再读（用户：3、5 号桌往左转 90°，4、6 号往右转 90°；气泡常在画面外或被挡），读完按原样转回来。
        转过去可能看到别桌的气泡：只认这桌记下的那位老饕的饮品句"""
        deg = {3: -90, 5: -90, 4: 90, 6: 90}.get(n)
        who = self.who.get(n)
        if not deg or not who or self.fired():
            return
        px = int(deg * self.cfg["look_px_per_deg"])
        self.g.look(dx=px)
        try:
            time.sleep(0.35)
            try:
                if self.riddle is None:
                    self.riddle = RiddleReader()
                r = self.riddle.read(self.g.frame_full())
            except Exception as e:
                self.log(f"读点单失败：{e}")
                return
        finally:
            self.g.look(dx=-px)
            time.sleep(0.2)
        dish, drink = r["dish"], r["drink"]
        if not (dish and dish[1] == who and drink and drink[0] >= 0.6):
            self.log(f"{n} 号桌转头后没读到这桌老饕的饮品句（认出 {dish[1] if dish else None} 号，这桌是 {who} 号）")
            return
        self.want[n] = drink[3]
        self.__dict__.setdefault("_want_guess", {}).pop(n, None)
        self.log(f"{n} 号桌转头后读到饮品：「{drink[2]}」→ {drink[3]}")

    def drink_fallback(self, n):
        """点了饮品、读了两次也没读到（也猜不出）：当场定成果香。
        不能等下次到桌边再读——手上没有合适的饮品就不会再来这桌，客人等到走掉，连续上餐清零"""
        if not self.fired():
            return  # 用户定：满堂彩前饮品必须对上要求，读不到就先不送、下次到桌边再读（不默认果香）
        if "drink" in self.g.table_states()[n][1] and not self.drink_known(n):
            self.want[n] = "[果香]"
            self.__dict__.setdefault("_want_guess", {})[n] = True
            self.log(f"{n} 号桌饮品没读到：先按果香送")

    def fired(self):
        """这局的满堂彩已经开过了：剩下的时间攒不满第二次，连击不要紧，饮品和菜都不用对谜语，随便送"""
        ep = getattr(self, "ep", None) or {}
        return ep.get("fw_left") is not None

    def drink_known(self, n):
        return bool(n) and (self.fired() or bool(self.want.get(n)))

    def drink_ok(self, n, tag):
        """这种香型的饮品能不能送给 n 号桌：不知道这桌要什么就不送（送错比不送更糟）。
        满堂彩开过后、或者我们的菜正合这桌的点菜要求（菜对了就算成功），什么饮品都送"""
        c = self.want.get(n) if n else None
        if n and c and tag in DRINK_AVAIL and not any(drink_cond_ok(c, t) for t in DRINK_AVAIL):
            return True  # v78：要的香型饮品台根本没有（多半是猜错/读错）：有什么送什么，别让这桌一直送不齐
        if n and self.fired():
            # v80（用户）：满堂彩后什么饮品都能送；手上有对的先送对的（serve_here 里排在前面），没有就随便送
            if self.cfg.get("fired_any_drink", True):
                return True
            # v38：满堂彩后知道要求的照要求送（送错连击清零），不知道的随便送
            return (not c) or drink_cond_ok(c, tag)
        return bool(c) and drink_cond_ok(c, tag)

    def ensure_drink_known(self, n, need):
        """站在 n 号桌边、这桌还缺饮品但不知道要什么：再读一次气泡（客人等餐时气泡一直挂着）"""
        if "drink" in need and not self.drink_known(n):
            self.read_request(n)
            if not self.drink_known(n):
                # 还是读不到：送一瓶果香也比一直不送强（不送这桌永远送不齐，连续上餐断掉，满堂彩攒不起来）
                self.want[n] = "[果香]"
                self.log(f"{n} 号桌饮品两次都没读到：先按果香送")

    def skip_table(self, n):
        """这桌坐的老饕在 skip_laotao 里（3 号老饕只要甜品，送界龙肉卷常常判失败、连续上餐清零）：压着不送"""
        ep = getattr(self, "ep", None) or {}
        if ep.get("fw_left") is not None:
            return False  # 这局满堂彩已经开过了，剩下的时间攒不满第二次，连击不要紧了：照常送，把座位腾出来
        if (n and n not in self.held and (self.who.get(n) in self.cfg["skip_laotao"] or self.soup_line.get(n))
                and len(self.held) < int(self.cfg["max_held"])):
            # 刚接待完时顶部栏还是「点餐中」，看不出点了两样菜：等顶部栏出了点单再补判一次（实测漏压后送了，连击断）
            if time.time() - getattr(self, "_held_chk_t", 0) > 1.0:
                self._held_chk_t = time.time()
                self._held_chk = self.g.table_states()
            s, it = self._held_chk.get(n, ("other", []))
            if s == "order" and it.count("dish") >= 2 and not self.dish_good.get(n, False):
                self.held.add(n)
                what = "两份汤" if self.soup_line.get(n) else "两样菜"
                self.log(f"{n} 号桌是 {self.who[n]} 号老饕、点了{what}：先不送，免得打断连续上餐")
        return bool(n) and n in self.held

    def can_serve(self, n, need, detail=None):
        if self.skip_table(n):
            return False
        """手上有没有能送给 n 号桌的东西（need 是顶部显示这桌还缺的，如 ["dish", "drink"]）"""
        detail = self.g.hotbar_detail() if detail is None else detail
        keep = self.reserved_slots(n, detail) if getattr(self.g, "partial_tables", None) else set()
        free = [x for i, x in enumerate(detail) if i not in keep]  # 留给送了一半的别的桌的不算
        if "dish" in need and "dish" in free:
            return True
        return "drink" in need and any(t in DRINK_TAGS and self.drink_ok(n, t) for t in free)

    def reserved_slots(self, n, detail):
        if not self.cfg["reserve_for_partial"]:
            return set()
        """手上哪几格要留给「送了一半」的别的桌（n 号桌自己不算）：
        每张这样的桌，按它还缺的东西留一格（饮品要合它点的香型）。返回格子下标集合"""
        st = self.g.table_states()
        partial = set(getattr(self.g, "partial_tables", set())) - {n}
        keep = set()
        for m in self.route([m for m in partial if st[m][0] == "order"]):
            for kind in st[m][1]:
                i = next((i for i, x in enumerate(detail) if i not in keep and (
                    (kind == "dish" and x == "dish") or
                    (kind == "drink" and x in DRINK_TAGS and self.drink_ok(m, x)))), None)
                if i is not None:
                    keep.add(i)
        return keep

    @staticmethod
    def pick_tag(cond):
        """按要求选一种饮品：只在饮品台现在有的香型里挑（DRINK_AVAIL，先果香再茶香）。
        v17：饮品台已经没有酒香/奶香（那两个位置现在是梨花香茶、玉米汁），点过去拿回来的东西被当成菜，
        一局里拿了 9 次把物品栏塞满、菜拿不了（05:55 那局只有 1086）。都满足不了返回 None：不拿"""
        return next((t for t in DRINK_AVAIL if not cond or drink_cond_ok(cond, t)), None)

    def drink_plan(self):
        """这次到饮品台该拿哪几瓶。v53（用户定）：手上饮品 = 还没送饮品的客人数 + 常备 2 瓶（茶香、果香）；
        有空位就拿够（1 位没送拿到 3 瓶、2 位没送拿到 4 瓶）。没人点两份饮品，所以常备只要 2 瓶。
        种类：先配已知要求还配不上的，再补手上缺的常备香型，其余茶香/果香轮流"""
        det = self.g.hotbar_detail()
        held = [t for t in det if t in DRINK_TAGS]
        slots = 5 - sum(1 for x in det if x)
        st = self.g.table_states()
        need = [n for n, (s_, it) in st.items() if s_ == "order" and "drink" in it and not self.skip_table(n)]
        stock = list(self.cfg.get("stock_drinks", ["茶香", "果香"]))
        lf_ = self.g.left_est()
        if (self.cfg.get("close_stock_less", True) and self.fired() and lf_ is not None
                and lf_ < float(self.cfg.get("stock_less_left", 999))):
            # v90：满堂彩后全程都这样（06:54:12 倒计时 29 秒拿了 3 饮 2 菜，1 号老饕要两份菜，菜不够）
            # v87：满堂彩后收尾（什么饮品都能送）：常备饮品只按等接待的人数（至少 1 瓶），空出的格子拿菜
            #（05:47 手上 3 饮 2 菜，5 号点两份菜用光，6 号没菜可送）
            n_wait = sum(1 for s_, _ in st.values() if s_ == "laotao")
            held_set = [t for t in stock if t in held]
            stock = (held_set + [t for t in stock if t not in held_set])[:max(1, min(len(stock), n_wait))]
            # v93：手上饮品总数 = max(2, 待送饮品桌数 + 有人等接待再加 1)（手上已有的算在里面）：
            # v90 空手补货只拿 1 饮 3 菜、空一格（07:31:53）；05:47 那次手上 1 瓶又拿 2 瓶占了菜的格子
            total = max(2, len(need) + min(1, n_wait))
        else:
            total = len(need) + len(stock)
        n_fetch = max(0, min(slots, total - len(held), int(self.cfg.get("max_drinks", 5)) - len(held)))
        if n_fetch <= 0:
            return []
        pool, fetch = list(held), []
        for n in need:
            c = self.want.get(n, "") if self.drink_known(n) else ""
            if not c:
                continue
            have = next((t for t in pool if drink_cond_ok(c, t)), None)
            if have:
                pool.remove(have)
            else:
                tg = self.pick_tag(c)
                if tg:
                    fetch.append(tg)
                elif n not in getattr(self, "_no_drink_logged", set()):
                    self.__dict__.setdefault("_no_drink_logged", set()).add(n)
                    self.log(f"{n} 号桌要的饮品（{c}）饮品台没有：不拿")
        for t in stock:
            if t in pool:
                pool.remove(t)
            else:
                fetch.append(t)
        i = 0
        while len(fetch) < n_fetch:
            fetch.append(("茶香", "果香")[i % 2])
            i += 1
        return fetch[:n_fetch]

    def serve_after_receive(self, n):
        """接待完别急着走：等客人点完单（顶部变成要什么），手上有就当场送上"""
        if not self.sync_inv():
            if self._wait(lambda: self.g.table_states()[n][0] == "order", self.cfg["order_wait"]):
                self.save_request(n)
                self.read_request(n)
                for _ in range(1):
                    if "drink" not in self.g.table_states()[n][1] or self.drink_known(n):
                        break
                    time.sleep(0.8)
                    self.read_request(n)
                if "drink" in self.g.table_states()[n][1] and not self.drink_known(n):
                    self.read_turned(n)
                self.drink_fallback(n)
            return
        need = []
        if self._wait(lambda: self.g.table_states()[n][0] == "order", self.cfg["order_wait"]):
            need = self.g.table_states()[n][1]
            # v83：满堂彩后、估计离结算不到 late_skip_read_left 秒：不读要求（什么饮品都能送，读一次 1~2 秒），直接上
            #（00:15 6 号桌：接待→读要求→上菜 3 秒，菜刚上完就结算，饮品没送上）
            lf_ = self.g.left_est() if self.fired() else None
            rl_ = self.real_left() if lf_ is not None else None
            if (rl_ is not None and rl_ < float(self.cfg.get("late_skip_read_left", 10))
                    and self.cfg.get("late_skip_read", True)):
                if "drink" not in need:
                    time.sleep(0.25)
                    need = self.g.table_states()[n][1] or need
                self.log(f"{n} 号桌：快结算了（倒计时 {lf_:.0f} 秒），不读要求直接上 {need}")
                if need and self.can_serve(n, need):
                    # v94：刚接待完「上菜」提示可能还没出来：原地等 1 秒，不去前后小步找（找会挪位置，更慢）
                    self._wait(lambda: self.g.has_prompt("p_shangcai"), 1.0, step=0.1) or self._search()
                    served = self.serve_here(need, n)
                    if served:
                        self.log(f"{n} 号桌：接待后当场上了 {len(served)} 样")
                return
            self.save_request(n)
            self.read_request(n)
            for _ in range(1):  # 点了饮品却没读到：气泡可能还没完全出来，等一下再读
                if "drink" not in need or self.drink_known(n):
                    break
                time.sleep(0.8)
                self.read_request(n)
            if "drink" in need and not self.drink_known(n):
                self.read_turned(n)
            self.drink_fallback(n)
        if need and self.can_serve(n, need):
            self.g.has_prompt("p_shangcai") or self._search()
            served = self.serve_here(need, n)
            if served:
                self.log(f"{n} 号桌：接待后当场上了 {len(served)} 样")

    def checkout(self, n):
        """去 n 号桌结账：走到桌边，提示是「结账」就按 F。没看到就这桌冷却一会儿"""
        if not self.go_table(n):
            self.fail_until[n] = time.time() + 15
            return False
        txt = self.g.prompt_text() or ""
        if "结账" in txt:
            self.g.press("f")
            time.sleep(0.6)
            self.log(f"{n} 号桌：结账")
            self.ep.setdefault("checkout", 0) if isinstance(getattr(self, "ep", None), dict) else None
            return True
        self.log(f"{n} 号桌：没看到结账提示（{txt[:10]}），先不管")
        self.fail_until[n] = time.time() + 15
        return False

    def deliver(self, n):
        """去 n 号桌上菜。送不出去（这桌要的不是手上这种）就记下来，手上东西变了之前不再来"""
        if not self.go_table(n):
            return 0
        if self.target_done:
            return 0
        if not self.g.has_prompt("p_shangcai") and self.g.has_prompt("p_jiedai"):
            self._on_pass("p_jiedai")  # 旁边有客人等接待：先接待，提示才会变回「上菜」
        if not self.g.has_prompt("p_shangcai"):
            self._search()
        need = self.g.table_states()[n][1]  # 顶部这桌还缺的（送到的会带笑脸/哭脸，不算）
        # v78：饮品是按老饕习惯猜的：到桌边再读一次气泡（客人等餐时气泡一直挂着），读到真的就按真的送
        if "drink" in need and (not self.drink_known(n) or getattr(self, "_want_guess", {}).get(n)):
            self.read_request(n)
            if not self.drink_known(n):
                self.read_turned(n)
        self.ensure_drink_known(n, need)
        served = self.serve_here(need, n)
        if not served and self.cfg.get("wang_nav") and self.cfg.get("deliver_fix_neighbor", True):
            # v76：眼前的提示是同侧相邻排那张桌的（11-09 14:35 站在 1 号桌位、提示其实是 3 号桌的，顺手接待了 3 号，
            # 给 1 号的菜递到 3 号「客人还需要饮品，无法提交菜品」）：往自己这排挪一小步再送一次
            d_, _sc = self.g.which_table()
            if d_ and d_ != n and d_ in TABLES and TABLES[d_][0] == TABLES[n][0] and TABLES[d_][1] != TABLES[n][1]:
                key = "w" if TABLES[d_][1] > TABLES[n][1] else "s"
                self.log(f"{n} 号桌：眼前是 {d_} 号桌的提示，往{'吧台' if key == 'w' else '外'}挪一小步再送")
                self.g._hold([key], 0.25, unstick=False)
                self.wn_dep = getattr(self, "wn_dep", 0.0) + (0.25 if key == "s" else -0.25)
                time.sleep(0.15)
                need = self.g.table_states()[n][1]
                served = self.serve_here(need, n)
        self.log(f"{n} 号桌：上了 {len(served)} 样")
        self.fix_yaw_after_f()
        if not served:
            # 手上明明有这桌要的东西却送不进去：多半站在了别的桌旁边（位置是推断的）。
            # 这桌冷却一会儿，坐标作废，下一趟先回饮品台重新定位
            cd = float(self.cfg["fail_cooldown"])
            self.fail_until[n] = time.time() + cd
            if self.pos is not None:
                self.depth = self.pos[1] + 0.6
            self.pos = None
            self.log(f"{n} 号桌没送出去，可能站错了桌：{cd:.0f} 秒内先不来，回饮品台重新定位")
            self.g.trace(f"{n}号桌没送出去")
        return len(served)

    def _on_pass(self, q):
        """路过桌边看到提示：「接待」直接按 F；「上菜」就试着把手上的东西送出去"""
        if q == "p_jiedai":
            if getattr(self, "_firing", False):
                return  # 正赶回饮品台开满堂彩：路上不接（接一桌要 5~8 秒，满堂彩就开晚了）
            if not self.late_recv_ok():
                return  # 快收工了，接了也送不齐：路过的新客人不接
            before = self.g.table_states()
            self.g.press("f")
            time.sleep(0.5)
            # 是哪张桌：按 F 前还在等接待、按完后不再等接待的那张；认不出再看号码牌
            changed = [n for n, (s, _) in self.g.table_states().items()
                       if before[n][0] == "laotao" and s != "laotao"]
            d = changed[0] if len(changed) == 1 else self.g.which_table()[0]
            self.log(f"路过顺手接待了{f' {d} 号桌' if d else '一桌'}")
            self.ep_receive(d)
            if d:
                self.serve_after_receive(d)  # 手上有就等他点完单当场送上
                self._passed(d)
            self.fix_yaw_after_f()
        elif q == "p_shangcai":
            # 先认出是哪张桌，看顶部它还缺什么，手上有才送
            d, _ = self.g.which_table()
            if not d:
                return
            st, need = self.g.table_states()[d]
            if st != "order":
                return
            self.ensure_drink_known(d, need)
            if not self.can_serve(d, need):
                return
            served = self.serve_here(need, d)
            if served:
                self.log(f"路过 {d} 号桌，顺手上了 {len(served)} 样")
                self._passed(d)
                self.fix_yaw_after_f()

    def late_recv_ok(self):
        """营业最后 end_recv_guard 秒：只有手上能当场送齐（至少一份菜，再加一瓶饮品或满堂彩开过后随便送）才接新客人"""
        left = self.g.left_est()
        if left is None or left >= self.cfg["end_recv_guard"]:
            return True
        if left < -float(self.cfg.get("closed_recv_cut", 10)):
            return False  # v35：打烊后十几秒、离结算不到半分钟：走过去接了也来不及送
        hand = self.sync_inv()
        if self.fired() and (not self.cfg.get("late_strict", True) or left >= 0):
            # v40：倒计时走完前还有 25 秒以上（倒计时 0 后还有约 25 秒打烊时间），手上有东西就接，缺的回去拿还来得及。
            # v34 起从倒计时 35 秒就要求一菜一饮，满堂彩后送齐的桌数从昨晚的 6~8 掉到 1~5
            if left < float(self.cfg.get("late_need_drink", 15)):
                # v41：倒计时 0 一到饮品台就关了（实测 03:51:53「饮品台面板没打开」），离 0 不到 15 秒手上没饮品就别接
                return "dish" in hand and any(x and x != "dish" for x in hand)
            return bool(hand)
        # v34：结算时不能留没送完的客人——收尾只接手上能当场送齐（一菜一饮）的
        return "dish" in hand and any(x and x != "dish" for x in hand)

    def _passed(self, d):
        """顺手办的是这趟要去的那张桌：标记办完，让后面的走路停下（不再去找它）"""
        if d == self.target:
            self.target_done = True
            self.g.abort_walk = True

    def restock(self, no_dish=False, no_drink=False):
        """按等着的桌补一趟：饮品按各桌要的香型、菜按缺的份数；菜要等厨房做好，取不到就过一会儿再试。
        菜是从饮品台那头开始往过道方向摆的：先在饮品台拿饮品，再从饮品台旁边往过道方向取菜，顺路出门"""
        self.sync_inv()
        got = 0
        # 先算好缺几份菜再去饮品台：饮品台面板刚关上时顶部桌位栏还没出来，那时候读会以为没人点菜
        want_dish = sum(it.count("dish") for m, (st_, it) in self.g.table_states().items()
                        if st_ == "order" and not self.skip_table(m))
        tags = [] if no_drink else self.drink_plan()
        if tags:
            got += self.get_drinks(tags) or 0
        self.sync_inv()
        need = want_dish - self.inv.count("dish")
        if self.cfg.get("dish_topup", True):
            need = max(need, int(self.cfg["keep_dishes"]) - self.inv.count("dish"))  # v22：补到 keep_dishes 份
        d = min(need, 5 - len(self.inv))
        if self.fill_hands():
            d = 5 - len(self.inv)  # 满堂彩后：剩下的格子全拿菜（吧台上有多少拿多少）
        lf_ = self.g.left_est()
        if self.cfg.get("close_cap_dish", False) and lf_ is not None and lf_ < 5:
            # v83：快结算（倒计时 5 秒内/打烊后）最多再来 1~2 位客人：菜补到 2 份就够（00:21 取 3 份多花 1~2 秒）
            d = min(d, 2 - self.inv.count("dish"))
        if no_dish:
            d = 0  # v70：快结算了、手上已经有菜：只拿饮品就走，省下取菜的 3~4 秒
        if d > 0 and time.time() - self.last_dish_try > self.cfg["dish_retry"]:
            n = self.get_dishes(d)
            got += n or 0
            if not n:
                self.last_dish_try = time.time()  # 出餐台上没菜，过一会儿再来
        return got

    def auto(self, seconds):
        """自动服务：手上备好 2 饮品 3 菜；上菜优先，其次接待；路上经过的桌子顺手接待/上菜；定时排菜"""
        end = time.time() + seconds
        self.score = []  # 每 10 秒记一次「没接待 + 没送完」的桌数
        self.ep_reset()
        self.sync_inv()
        self.cook()
        if self.cfg["calib_boost"] and not getattr(self, "v_normal", None) and self.anchor():
            self.v_normal = self.measure_speed("平时")
        self.wait_first_dishes(end)
        self.g.passing = None if self.cfg.get("wang_nav") else self._on_pass  # 新走法：路上不顺手办事（会打乱横走记账）
        self.g.watch_over = True
        try:
            while True:
                try:
                    self.g.watch_fw = True
                    self._auto_loop(end)
                    break
                except FireworksReady:
                    self.fire()
        except BusinessOver:
            self.log("营业结束（进入结算画面），停止服务")
            self.log_left_customers()
            self.g.trace("营业结束")
        except RoundLost:
            self.g.watch_over = False
            self.ep_zero("走出送餐范围（走丢）")
            self.abort_round()
        finally:
            sc = getattr(self, "score", [])
            if sc:
                over = sum(1 for x in sc if x > 4)
                self.log(f"【本局评分】等待的桌数（没接待+没送完）：平均 {sum(sc) / len(sc):.1f}，最多 {max(sc)}，"
                         f"超过 4 桌的时间占 {over * 100 // len(sc)}%（{len(sc)} 次采样）")
            self.g.passing = None
            self.g.watch_over = False
            self.g.watch_fw = False
        self.log("自动服务时间到")

    def settle(self):
        """营业结束后：等「继续」出现点掉，再等「完成」点掉，回到餐厅"""
        for name, desc in (("ui_btn_continue", "继续"), ("ui_btn_done", "完成")):
            self.log(f"等待「{desc}」…")
            if not self._wait(lambda: self.g.find_btn(name) is not None, 90, step=0.4):
                self.g.trace(f"结算_没找到{desc}")
                raise RuntimeError(f"结算时 90 秒内没找到「{desc}」按钮")
            time.sleep(0.8)  # 等按钮出场动画结束
            if name == "ui_btn_continue" and getattr(self, "ep", None):
                self.ep["income"] = self.g.read_income()
            if name == "ui_btn_continue":
                self.g.trace("结算_明细")  # 看收入里基础/赏钱各多少
            for i in range(5):
                pos = self.g.find_btn(name)
                if not pos:
                    break
                self.g.click(*pos)
                self.log(f"点击「{desc}」" + (f"（第 {i + 1} 次）" if i else ""))
                time.sleep(1.2)
        time.sleep(3.0)  # 等回到餐厅画面
        self.g.trace("结算完成")
        self.ep_finish()

    def back_to_start(self):
        """上一局结束后人还停在最后的位置（不营业时没有「饮品台」提示，没法靠它定位）：
        已经看得到「开始经营」提示就直接开；否则传送到餐馆门口的传送点，再按固定路线走回吧台"""
        self.at_table = None
        self.pos = None
        if self.g.find_btn("ui_btn_continue") or self.g.find_btn("ui_btn_done"):
            self.log("还停在上一局的结算画面，先点完结算")
            self.settle()
        if self.g.find_ui("ui_prompt_open", 0.8):
            return True
        if self.g.find_ui("ui_in_business", 0.8):
            self.log("已经在营业中：不用传送，直接接着服务")
            return True
        self.log("走回吧台准备开新的一局：传送到餐馆门口再走进去")
        return self.teleport_home()

    def maybe_lost(self):
        """连续找不到饮品台：营业还剩很多时间才提前结束这一局（最后 90 秒结束也拿不回什么，接着试）"""
        left = self.g.time_left() if hasattr(self.g, "time_left") else None
        if left is None or left < 90:
            # 读不到倒计时多半是快结束了（「即将打烊」「营业结束」盖住了），也不提前结束
            self.log(f"找不到饮品台，但只剩 {left if left is not None else '不到几'} 秒了，不提前结束，接着试")
            self.anchor_fails = 0
            return
        raise RoundLost()

    def abort_round(self):
        """走丢了：按 Esc 弹出「提前结束经营」，点确认，让这一局直接进结算"""
        self.log("走丢了，提前结束这一局")
        self.g.trace("走丢_提前结束")
        for _ in range(3):
            self.g.press("esc")
            pos = None
            end = time.time() + 3.0
            while time.time() < end and not pos:
                time.sleep(0.4)
                # 按钮在画面中下方那一行（实测「取消」(880,696)、「确认」(1130,696)）；问题句也有「确认」，不在这一行
                pos = self.g.ocr_find("确认", region=(960, 640, 1400, 760))
            if pos:
                self.g.click(*pos)
                time.sleep(2.0)
                # 点了没反应（确认框还在）：按固定位置再点一次（实测「确认」在 (1130,696)）
                for _ in range(3):
                    if not self.g.ocr_find("确认", region=(960, 640, 1400, 760)):
                        break
                    self.g.click(1130, 696)
                    time.sleep(1.5)
                self.log("已提前结束这一局")
                return True
        self.log("没找到「提前结束经营」的确认按钮")
        return False

    def teleport_home(self):
        """回吧台：优先用 MaaEnd（小地图定位 + 寻路，见 maaend_bridge.py）；它不在或失败再用下面的老办法"""
        try:
            for attempt in range(3):  # MaaEnd 偶尔回到吧台但朝向不对、看不到「开始经营」：再来一次常常就好（实测第二次成功）
                if self._teleport_home_maa():
                    return True
        except _NotInHall:
            # 用自己的地图传送到门口、走进大厅后，再让 MaaEnd 精确走到吧台
            if self._teleport_home_old():
                return True
            for attempt in range(2):
                try:
                    if self._teleport_home_maa():
                        return True
                except _NotInHall:
                    break
            return False
        return self._teleport_home_old()

    def still_in_business(self):
        """v60：是不是还停在营业里（中途重载/停下时）。料理界面、饮品台面板、「提前结束经营」确认框都会挡住左上角的
        「结束经营」字样（14:30 重载时停在料理界面，之后按 M/Esc 弹出确认框，一直没认出来，卡了 9 分钟）：
        先关掉面板；确认框本身就说明在营业"""
        try:
            for _ in range(2):
                if self.g.kitchen_open():
                    self.g.press("esc")
                    time.sleep(1.0)
            self.close_drink_panel()
        except Exception:
            pass
        if self.g.ocr_find("提前结束经营", region=(300, 520, 1700, 760)):
            return "dialog"
        if self.g.find_ui("ui_in_business", 0.8) or self.g.ocr_find("结束经营", region=(0, 0, 420, 140)):
            return "business"
        return None

    def end_stale_round(self):
        """v60：结束停在半路的那一局并点完结算。确认框已经弹出来时直接点「确认」（再按 Esc 会把它关掉）"""
        st = self.still_in_business()
        if not st:
            return False
        self.log("还停在上一局的营业里：先提前结束这一局，重新开")
        if st == "dialog":
            pos = self.g.ocr_find("确认", region=(960, 640, 1400, 760)) or (1130, 696)
            self.g.click(*pos)
            time.sleep(2.0)
            self.log("已提前结束这一局（点了确认框）")
            self.settle()
            return True
        if self.abort_round():
            self.settle()
        return True

    def dismiss_daily_popup(self):
        """每天 05:00 跨天会弹「点击确认今日月卡奖励」整屏画面（11-08 05:00 卡了 10 分钟）：看到就点一下关掉"""
        for _ in range(3):
            if self.g.ocr_find("月卡", region=(300, 520, 1700, 1040)):
                self.log("跨天弹出了「今日月卡奖励」：点一下关掉")
                self.g.click(960, 800)
                time.sleep(2.5)
            else:
                return

    def _teleport_home_maa(self):
        self.dismiss_daily_popup()
        if self.end_stale_round():
            return False  # v60：原来还在营业里：已结束那一局，让上层重新找「开始经营」
        if self.cfg.get("use_maaend", True) and os.path.exists(os.path.join(BASE_DIR, "maaend_bridge.py")):
            self.log("用 MaaEnd 回吧台")
            r = maaend_run(["home"], timeout=300)
            self.log(f"MaaEnd：{r}")
            try:
                self._last_maa_loc = json.loads(r).get("detail") or {}
            except Exception:
                self._last_maa_loc = {}
            if '"not_in_hall": true' in r:
                self.log("不在餐馆大厅：不用 MaaEnd，改用地图传送")
                raise _NotInHall()
            try:
                cam = json.loads(r).get("detail", {}).get("camRot")
            except Exception:
                cam = None
            if cam is not None:
                diff = (88.0 - float(cam) + 180) % 360 - 180  # 把镜头转到正对吧台（MaaEnd 镜头朝向约 88°）
                self.g.look(dx=int(diff * 1000 / 90))
                time.sleep(0.3)
            self.g.hold(["s"], 1.5, unstick=False)  # 往前贴住吧台
            self._fix_yaw_in_hall()
            for d, t in (("a", 2.0), ("d", 4.0)):
                for _ in range(int(t / 0.3)):
                    if self.g.find_ui("ui_prompt_open", 0.8):
                        self.log("回到吧台「开始经营」处")
                        return True
                    self.g.hold([d], 0.3)
            self.log("MaaEnd 回吧台后没看到「开始经营」")
        return False

    def _teleport_home_old(self):
        """按 M 打开地图 → 点餐馆门口的协议传送点 →「协议传送点」→「传送」，
        落地后按固定路线走进正门、对着吧台走过去，再沿吧台找「开始经营」提示"""
        for attempt in range(2):
            self.g.press("m")
            time.sleep(2.0)
            pos = self.g.map_anchor()
            if not pos:
                self.log("地图上没找到传送点")
                self.g.press("esc")
                time.sleep(1.0)
                continue
            self.g.click(*pos)
            time.sleep(1.2)
            item = self.g.ocr_find("协议传送点")
            if item:
                self.g.click(*item)
                time.sleep(1.2)
            btn = self.g.ocr_find("传送", region=(1500, 900, 1920, 1080), exclude=("协议",))
            if not btn:
                self.log("没找到「传送」按钮")
                self.g.press("esc")
                time.sleep(1.0)
                continue
            self.g.click(*btn)
            self.log("传送到餐馆门口")
            time.sleep(9.0)  # 读图
            break
        else:
            loc = getattr(self, "_last_maa_loc", None) or {}
            if not (loc.get("mapName") == "Wuling_Base" and abs(float(loc.get("x", 0)) - 2202.5) < 2.5
                    and abs(float(loc.get("y", 0)) - 506.9) < 2.5 and abs(float(loc.get("camRot", 0)) - 350.3) < 8):
                return False
            # 地图上找不到传送点图标，是因为人就站在门口传送点上（图标被人的箭头盖住）：朝向也是落地的朝向，直接走进去
            self.log("人就在门口传送点（落地的位置和朝向）：不传送，直接按路线走进去")
        # 落地后的固定路线（实测）：往前 3 秒到荷花池边 → 右转 → 跳上台阶走进正门 → 右转对着吧台走过去
        # 注意：hold 用的是「面朝吧台」的逻辑方向，s = 实际往前走
        g = self.g
        g.hold(["s"], 3.0)
        g.look(0, 800)
        time.sleep(0.3)
        g.press("space")
        g.hold(["s"], 1.0)
        g.press("space")
        g.hold(["s"], 2.5)
        g.look(0, 450)
        time.sleep(0.3)
        self.fix_yaw(maxdeg=30, top=True)  # 刚进门、大厅空旷：用上方吧台带读数（实测这里准）
        g.hold(["d"], 0.6)  # 往左挪一点，避开 6 号桌的长凳（实测直着走会被它挡住）
        g.hold(["s"], 7.0)  # 一直往前走到贴住吧台（顶住了也没关系）
        for d, t in (("a", 2.5), ("d", 5.0)):  # 沿吧台小步滑找「开始经营」：先右后左，看到就停
            for _ in range(int(t / 0.3)):
                if g.find_ui("ui_prompt_open", 0.8):
                    self.log("回到吧台「开始经营」处")
                    return True
                g.hold([d], 0.3)
        self.log("传送后没走到「开始经营」处")
        g.trace("传送回吧台失败")
        return False

    # ===== 评分（奖励函数）=====
    # 每次送货：送对一份菜 +5；送对一瓶饮品 +5；一桌送齐再 +10，按从接待到送齐的用时每秒 -0.1（最少 0）
    # 等待惩罚：每 10 秒采样一次「没接待 + 没送完」的桌数，超过 4 桌每多一桌 -10
    # 营业额：结算画面的营业额每 10 金币 +1
    # 直接 0 分：走出送餐范围（判定走丢）、或者某次采样 6 桌全都在等
    def ep_reset(self):
        self.ep = {"recv": {}, "deliv": 0.0, "pen": 0.0, "zero": None, "served": 0, "done": 0,
                   "samples": [], "income": None, "t0": time.time()}

    def ep_receive(self, n):
        if n:
            self.want.pop(n, None)  # 新客人：上一位在这桌点的饮品作废
            self.__dict__.setdefault("_want_guess", {}).pop(n, None)
            self.who.pop(n, None)
            self.held.discard(n)
            self.dish_good.pop(n, None)
        if getattr(self, "ep", None) and n:
            self.ep["recv"].setdefault(n, time.time())

    def ep_served(self, n, kind, remaining=None):
        ep = getattr(self, "ep", None)
        if not ep:
            return
        ep["deliv"] += 5
        ep["served"] += 1
        msg = f"[送货评分] {n} 号桌 {'菜' if kind == 'dish' else '饮品'} +5"
        if remaining is not None:
            # v100：v99 送完马上接着送，顶部桌位栏还在刷新，读到的不是「点单」就被当成送齐（一局记成送齐 30 桌）：
            # 按这桌还剩几样没送来判断
            done = remaining == 0
        else:
            try:
                st, need = self.g.table_states()[n] if n else (None, None)
            except Exception:
                st = None
            done = st != "order"
        if self.cfg.get("log_combo"):
            time.sleep(0.6)
            msg += "｜满堂彩 {}/100 连续 {}".format(*self.g.combo())
        if n and done:
            used = time.time() - ep["recv"].pop(n, time.time())
            bonus = max(0.0, 10 - 0.1 * used)
            ep["deliv"] += bonus
            ep["done"] += 1
            msg += f"，这桌送齐 +{bonus:.1f}（从接待到送齐 {used:.0f} 秒）"
        self.log(msg)

    def ep_sample(self, st):
        ep = getattr(self, "ep", None)
        if not ep:
            return
        n = sum(1 for s_, _ in st.values() if s_ in ("laotao", "order"))
        ep["samples"].append(n)
        if n > 4:
            ep["pen"] += 10 * (n - 4)
        if n >= 6 and not ep["zero"]:
            ep["zero"] = "6 桌都在等"
            self.log("【评分】6 桌都在等接待/上菜：本局记 0 分")

    def ep_zero(self, why):
        if getattr(self, "ep", None) and not self.ep["zero"]:
            self.ep["zero"] = why
            self.log(f"【评分】{why}：本局记 0 分")

    def ep_finish(self):
        ep = getattr(self, "ep", None)
        if not ep:
            return None
        income = ep["income"] or 0
        # 硬要求：每局都要开出 1 分钟以上的满堂彩（开启时营业还剩 ≥ fw_min_left 秒），做不到扣分
        fw = ep.get("fw_left")
        fw_ok = fw is not None and fw >= self.cfg["fw_min_left"]
        fw_pen = 0 if fw_ok else self.cfg["fw_miss_penalty"]
        raw = income / 10 + ep["deliv"] - ep["pen"] - fw_pen
        score = 0.0 if ep["zero"] else round(raw, 1)
        sm = ep["samples"]
        rec = {"time": time.strftime("%m-%d %H:%M"), "score": score, "zero": ep["zero"], "income": ep["income"],
               "deliver_pts": round(ep["deliv"], 1), "wait_penalty": ep["pen"], "served": ep["served"],
               "tables_done": ep["done"], "wait_avg": round(sum(sm) / len(sm), 2) if sm else None,
               "wait_max": max(sm) if sm else None, "fw_left": fw, "fw_penalty": fw_pen,
               "left_recv": ep.get("left_recv"), "left_order": ep.get("left_order"),
               "cfg": {k: self.cfg.get(k) for k in ("order_wait", "cook_every", "cook_ahead", "max_drinks",
                                                     "first_dishes", "dish_wait_max")}}
        self.log(f"【本局总分】{score}" + (f"（0 分原因：{ep['zero']}）" if ep["zero"] else "")
                 + f"｜营业额 {ep['income']}，满堂彩 " + (f"开启时还剩 {fw} 秒" if fw is not None else "没开")
                 + ("" if fw_ok else f"（不到 1 分钟，扣 {fw_pen}）")
                 + f"，送货 {ep['deliv']:.1f}（{ep['served']} 样、送齐 {ep['done']} 桌），"
                 f"等待扣分 {ep['pen']}，等待桌数平均 {rec['wait_avg']} 最多 {rec['wait_max']}")
        try:
            if self.cfg.get("debug_files"):
                with open(os.path.join(BASE_DIR, "test_channel", "scores.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self.ep = None
        return rec

    def real_left(self):
        """v95：离结算还剩几秒。打烊后读一次顶部横幅的打烊倒计时校准（实测倒计时 0 后 19~24 秒不等，
        以前一律按 close_grace=24 估，差的那几秒常让最后一桌送不上）；读不到按倒计时 + close_grace"""
        ce = getattr(self, "_close_end", None)
        if ce is not None:
            return ce - time.time()
        lf = self.g.left_est()
        if lf is None:
            return None
        # v96：横幅实测倒计时 0 后 17~20 秒结算（5 次读数），不是 24：读到横幅前按 close_grace_est 估
        grace = float(self.cfg.get("close_grace_est", 20))
        if (lf < 1 and self.cfg.get("read_close_timer", True) and getattr(self, "_close_tries", 0) < 4
                and time.time() - getattr(self, "_close_try", 0) > 2):
            self._close_try = time.time()
            self._close_tries = getattr(self, "_close_tries", 0) + 1
            v = self.g.close_secs()
            if v is not None and 0 < v <= 40:
                self._close_end = time.time() + v
                self.log(f"打烊倒计时：还剩 {v} 秒（原来按倒计时 0 后 {grace:.0f} 秒估计是 {lf + grace:.0f} 秒）")
                return float(v)
        return lf + grace

    def reset_round(self):
        """每局开始前清掉上一局的计数"""
        self._close_end = None
        self._close_tries = 0
        self.g.boost_until = 0.0  # 新的一局：还没开满堂彩
        self.want = {}
        self.who = {}
        self.held = set()
        self.soup_line = {}
        self.dish_good = {}
        self.closed = False
        self._final_filled = False
        self._post_fill = False
        self.fail_until = {}
        self.anchor_fails = 0
        self.cooked = self.served_dish = self.first_batch = 0
        self.last_cook = self.last_fireworks = self.last_dish_try = 0.0
        self.board = (None, 0.0)
        self.target, self.target_done = None, False

    def _click_ui(self, name, desc, timeout=10):
        pos = None
        if self._wait(lambda: self.g.find_ui(name) is not None, timeout):
            pos = self.g.find_ui(name)
        if not pos:
            raise RuntimeError(f"开局时没找到「{desc}」")
        time.sleep(0.4)  # 等按钮出场动画
        self.g.click(*pos)
        self.log(f"点击「{desc}」")
        time.sleep(1.0)

    def align_camera_maaend(self):
        """开店前（还有小地图）用 MaaEnd 测镜头朝向，转到正对吧台（88°）。营业时镜头沿用这个朝向，
        开局歪了十几度时画面横线读数会被当成读错、不纠正，沿吧台就会滑错方向"""
        for _ in range(3):
            r = maaend_run(["locate"], timeout=60)
            try:
                cam = json.loads(r).get("detail", {}).get("camRot")
            except Exception:
                cam = None
            if cam is None:
                self.log("开店前没测出镜头朝向")
                return
            diff = (88.0 - float(cam) + 180) % 360 - 180
            if abs(diff) < 1.5:
                self.log(f"开店前镜头已对正（{cam:.1f}°）")
                return
            self.g.look(dx=int(diff * 1000 / 90))
            time.sleep(0.3)

    def _find_open_prompt_nearby(self):
        """开店前没看到「开始经营」：原地多看 1.5 秒，再贴着吧台左右各小滑一点（不转镜头），看到就停"""
        t = time.time() + 1.5
        while time.time() < t:
            if self.g.find_ui("ui_prompt_open", 0.75):
                return True
            time.sleep(0.15)
        for d, n in (("a", 3), ("d", 6), ("a", 3)):
            for _ in range(n):
                self.g.hold([d], 0.25, unstick=False)
                time.sleep(0.1)
                if self.g.find_ui("ui_prompt_open", 0.75):
                    self.log(f"「开始经营」提示在旁边一点（往 {d} 挪到了）")
                    return True
        return False

    def start_business(self):
        """开局：吧台前「开始经营」按 F → 选「食有老饕」→ 试营业/正式经营 → 设定菜单 → 开始经营。
        每 0.25 秒截一张图，看现在在哪个画面就做哪一步；同一个按钮连续两次都在同一位置（出场动画结束了）就马上点，
        点了没反应会再点，从中间某一步开始也能接着走"""
        mode = self.cfg["business_mode"]
        if self.cfg.get("use_maaend", True) and self.g.find_ui("ui_prompt_open", 0.8):
            self.align_camera_maaend()
        btn_mode = "ui_btn_formal" if mode == "正式经营" else "ui_btn_try"
        end = time.time() + self.cfg["start_timeout"]
        searched = False
        last = None      # 上一步做了什么
        seen = None      # 上一张截图看到的 (按钮, 位置)
        theme_clicked = False
        while time.time() < end:
            self.g.check()
            img = self.g.frame960()
            if self.g.find_ui("ui_in_business", 0.8, img):
                time.sleep(self.cfg["start_settle"])  # 等开场动画结束，游戏才接按键
                self.at_head = False
                self.at_table = None
                self.depth = 1.0
                self.pos = None
                self.g._left_pos = self.g._left_at = None  # v35：上一局的倒计时读数作废
                self._fresh_start, self._fresh_t = True, time.time()  # v46：开局后第一次回饮品台不往左滑
                # v59：开局时有没有站在饮品台前（开局截图里有「饮品台」提示）：站在前面的话，第一次回饮品台不推不滑
                self._start_at_D = bool(self.g.has_prompt("p_drink"))
                if self._start_at_D:
                    self.wn_at, self.at_drink = "D", True  # 直接去桌子的那条路（wn_trip）也不用先回饮品台
                self.log(f"开局位置：{'就在饮品台前' if self._start_at_D else '不在饮品台前（在右边）'}")
                self.log("已开始营业")
                self.g.trace("开局完成")
                try:
                    # 开店前镜头刚对正过：在「开始经营」这头存一张干净的正对样本（朝向检查用，这头以前没有样本）
                    if not self.cfg.get("debug_files"):
                        raise StopIteration  # 发布版不存
                    d = os.path.join(BASE_DIR, "test_channel", "places_clean")
                    os.makedirs(d, exist_ok=True)
                    cv2.imencode(".jpg", self.g.frame960(), [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(
                        os.path.join(d, f"start_{time.strftime('%m%d_%H%M%S')}.jpg"))
                except Exception:
                    pass
                return True
            step = None
            for name, desc, thr in (("ui_btn_open", "开始经营", 0.7), ("ui_btn_menu", "设定菜单", 0.75),
                                    (btn_mode, mode, 0.75)):
                pos = self.g.find_ui(name, thr, img)
                if pos:
                    step = (name, desc, pos)
                    break
            if step and step[0] == btn_mode and not theme_clicked:
                # 主题页：先点「食有老饕」
                pos = self.g.find_ui("ui_theme_laotao", 0.8, img)
                if pos:
                    self.g.click(*pos)
                    self.log("点击「食有老饕」")
                    theme_clicked = True
                    last = "theme"
                    time.sleep(0.4)
                    continue
            if step:
                name, desc, pos = step
                stable = seen and seen[0] == name and abs(seen[1][0] - pos[0]) <= 6 and abs(seen[1][1] - pos[1]) <= 6
                seen = (name, pos)
                if stable:
                    self.g.click(*pos)
                    self.log(f"点击「{desc}」")
                    self.g.trace(f"开局_{desc}")
                    last = name
                    seen = None
                    time.sleep(0.5)
                else:
                    time.sleep(0.25)
                continue
            seen = None
            if self.g.find_ui("ui_prompt_open", 0.8, img):
                self.g.press("f")
                self.log("按 F：开始经营")
                last = "f"
                theme_clicked = False
                time.sleep(0.8)
                continue
            if not searched and last is None and self.cfg.get("start_research_maa", True):
                # v29：一个开局画面都没看到。以前在这里 pin_counter（横线读数会假报 20~30°、把镜头转歪）再沿吧台滑，
                # 开局后第一趟回饮品台必丢（11-07 三次）。改成：先原地多看 1.5 秒（提示偶尔闪一下没认到），
                # 再左右小滑（不转镜头），还没有就重新让 MaaEnd 回吧台、对正镜头
                searched = True
                if self._find_open_prompt_nearby():
                    continue
                if self.end_stale_round():
                    # v37/v60：还在上一局营业里（中途重载等）：营业中按 M/Esc 只会弹确认框，回不了吧台——先结束这一局
                    searched = False
                    continue
                self.log("没看到「开始经营」提示：重新用 MaaEnd 回吧台")
                if self.teleport_home() and self.cfg.get("use_maaend", True):
                    self.align_camera_maaend()
                end = max(end, time.time() + 30)
                continue
            if not searched and last is None:
                # 一个开局画面都没看到：可能没站在「开始经营」提示处，贴着吧台左右找一找
                searched = True
                from_drink = self.at_drink
                self.pin_counter()
                self.log("没看到「开始经营」提示，沿吧台找一找")
                for d, t in ((("a", 4.0),) if from_drink else (("d", 4.0), ("a", 2.5))):
                    found = False
                    for _ in range(int(t / 0.3)):
                        if self.g.find_ui("ui_prompt_open", 0.8):
                            found = True
                            break
                        self.g.hold([d], 0.3)
                    if found:
                        break
                continue
            time.sleep(0.25)
        self.g.trace("开局失败")
        raise RuntimeError("开局超时：没能进入营业画面（截图在 test_channel/trace 里）")

    def wait_first_dishes(self, end):
        """开局先备货：拿好饮品，回到吧台前等厨房出够 first_dishes 道菜，取了菜再出去接待和上菜
        （空着手去接待，客人点完单还得回来等菜，很亏）"""
        if not self.cfg["facing_counter"] or self.cfg["first_dishes"] <= 0:
            return
        tags = self.drink_plan()
        if tags:
            self.get_drinks(tags)
        else:
            self.go_drink()  # 不拿饮品也先走到饮品台：菜从这头开始摆，站在开店的位置看不到
        # 就在饮品台旁边等：菜是从饮品台这头开始往过道方向摆的
        self.log(f"等厨房出 {self.cfg['first_dishes']} 道菜再出发")
        deadline = min(end, time.time() + self.cfg["first_wait_max"])
        while time.time() < deadline:
            self.g.check()
            if len(self.g.takeable_icons()) >= self.cfg["first_dishes"]:
                break
            time.sleep(1.0)
        self.last_dish_try = 0.0
        self.get_dishes(int(self.cfg["keep_dishes"]))

    def fire(self):
        """满堂彩攒满：先回饮品台站稳再按 R。在路上按的话会突然加速，按时间算的坐标一下子就偏了；
        在饮品台按，坐标原点是准的，加速后的每一段都从这里算起"""
        self.g.watch_fw = False  # 回去的路上不再被打断
        self.target_done = False
        self.g.abort_walk = False
        self.log("满堂彩可以放了：先回饮品台")
        self._firing = True  # 这一趟不做饮品台镜头检查（转一圈要好几秒）
        if self.cfg.get("wang_nav"):
            # 新走法：从桌边要先原样退回过道、摆正再回吧台（实测直接往吧台走撞上长凳，回不到饮品台算走丢）
            if not self.g.has_prompt("p_drink"):
                self.anchor()
        else:
            self.at_table = None
            self.back_to_aisle()
            if not self.g.has_prompt("p_drink"):
                self.anchor()
        left0 = self.g.time_left()
        if (self.g.fireworks_ready() and not self.closed and time.time() - self.last_cook > 10
                and (left0 is None or left0 >= self.cfg["fw_min_left"] + 12 or self.cfg.get("fast_end_cook", True))):
                # v32：按 R 前这次补满不再按在途判断跳过（v31 跳过后加速期间取菜 0~1 份，只送了 11 桌）
            # 时间够（按 R 时还能剩 1 分钟以上）才先补厨房；不够就直接按 R。
            # 加速期间不再开料理界面（开过就容易走丢），所以这一次把三个厨房的空位全部填满
            self._cook(read_panel=True, fill=True,
                       card=self.cfg["anchor_dish_card"] if self.cfg.get("fast_end_cook", True) else None)
            self._final_filled = True
            self.g.close_quit_dialog()
        if self.g.fireworks_ready():
            left = self.g.time_left()
            self.g.press("r")
            time.sleep(0.4)
            self.g.boost_until = time.time() + 60  # 加速 60 秒，以这个为准
            self.last_cook = time.time()  # 刚补过厨房：加速期间先不开料理界面
            self.log(f"在饮品台按 R：开启满堂彩（营业还剩 {left} 秒）")
            self.g.trace("满堂彩_开启")
            ep = getattr(self, "ep", None)
            if ep is not None and ep.get("fw_left") is None:
                # 倒计时读不到就按开局时间估（一局营业约 320 秒）
                ep["fw_left"] = left if left is not None else int(320 - (time.time() - ep["t0"]))
            if self.g.has_prompt("p_drink"):
                self.set_origin()  # 就站在饮品台：坐标原点不变，加速后从这里算
            else:
                self.pos = None
        self._firing = False
        self.last_fireworks = time.time()

    def log_left_customers(self):
        """结算前最后一次读到的桌态里还有几位客人没送完（没接待 laotao + 没送齐 order）：用户要求一位都不能落下"""
        # v52：结算画面会被读成全空：取营业结束前至少 2 秒的那次桌态
        # v71：主循环的记录之外，加上任何地方读到的桌态（11-09 12:22 只用主循环记录，拿到 16 秒前的旧桌态，误报落下 1 号桌）
        hist = sorted(getattr(self, "_st_hist", []) + getattr(self.g, "_ts_hist", []), key=lambda h: h[0])
        hist = [h for h in hist if h[0] <= time.time() - 2]
        t_, st = hist[-1] if hist else (time.time(), {})
        self._st_hist = []
        self.g._ts_hist = []
        lt = [m for m, (s_, _) in st.items() if s_ == "laotao"]
        od = [m for m, (s_, _) in st.items() if s_ == "order"]
        age = time.time() - t_
        self.log(f"【落下的客人】没接待 {lt} 没送齐 {od}（{age:.0f} 秒前的桌态）")
        ep = getattr(self, "ep", None)
        if ep is not None:
            ep["left_recv"], ep["left_order"] = len(lt), len(od)

    def _auto_loop(self, end):
        while time.time() < end:
            self.g.check()
            self.g.close_quit_dialog()
            if self.g.business_over():
                self.log("营业结束（进入结算画面），停止服务")
                self.log_left_customers()
                return
            if time.time() - self.last_fireworks > 15 and self.g.fireworks_ready():
                raise FireworksReady()
            self.cook()  # 自己判断到没到时间、在途够不够
            st = self.g.table_states()
            if not self.g.business_over():
                hist = self.__dict__.setdefault("_st_hist", [])
                hist.append((time.time(), st))
                del hist[:-20]
            self.ui("states", st)
            # 记下每桌客人从什么时候开始等接待（等太久客人会走，连续上餐清零）
            since = self.__dict__.setdefault("laotao_since", {})
            for m, (s_, _) in st.items():
                if s_ == "laotao":
                    since.setdefault(m, time.time())
                else:
                    since.pop(m, None)
            if time.time() - getattr(self, "_score_t", 0) >= 10:
                self._score_t = time.time()
                self.score.append(sum(1 for s_, _ in st.values() if s_ in ("laotao", "order")))
                self.log("[桌态] " + " ".join(f"{m}:{s_}" for m, (s_, _) in st.items()))  # v13c：看「待结账」会持续多久
                self.ep_sample(st)
                if self.cfg.get("log_combo"):
                    self.log("[连击] 满堂彩 {}/100 连续 {}".format(*self.g.combo())
                             + "｜桌位 " + " ".join(f"{m}:{s_}{''.join(i[0] for i in it)}" for m, (s_, it) in st.items()))
            partial = set(getattr(self.g, "partial_tables", set()))
            wait = [n for n, (s, _) in st.items() if s == "laotao" and time.time() >= self.fail_until.get(n, 0)]
            wait_all = list(wait)  # v81：late_recv_ok 过滤前的等接待（收尾判断「来不及只拿饮品」要算上）
            if wait and not self.late_recv_ok():
                wait = []  # 快收工了、手上又凑不齐一菜一饮：新客人不接（接了送不上，结算时要扣分）
            self.sync_inv()
            # 顶部桌位栏直接显示还差什么（送到的会叠上笑脸/哭脸，已经排除掉了）
            orders = {n: it for n, (s, it) in st.items() if s == "order" and not self.skip_table(n)}
            det = self.g.hotbar_detail()
            todo = [n for n in self.route(orders) if self.can_serve(n, orders[n], det)
                    and time.time() >= self.fail_until.get(n, 0)]

            # 0. 顺手：在吧台前、吧台上有做好的菜、等着的桌还缺菜、手上有空位 → 拿上
            free_slots = 5 - sum(1 for x in det if x)
            want_dish = sum(it.count("dish") for it in orders.values()) - det.count("dish")
            if self.cfg.get("dish_for_waiting", True):
                # v20：等接待的老饕几乎都要一份菜：也算进去，接待完当场就能送，不用为了一份菜再跑一趟吧台
                #（实测每次只拿 1~2 份，一趟只送 1 桌，回吧台 11~12 次/局）
                want_dish += len(wait)
            if self.cfg.get("dish_topup", True):
                # v22：吧台上有菜就补到 keep_dishes 份（手上多带菜不吃亏；实测吧台上摆着 2 份只拿了 1 份，
                # 接待下一桌要菜时手上没有，又得回吧台——回吧台大多是这个原因）
                want_dish = max(want_dish, int(self.cfg["keep_dishes"]) - det.count("dish"))
            if self.fill_hands():
                want_dish = free_slots  # 满堂彩后不用对着要求拿：空几格拿几份
            #    饮品先拿：人在吧台这头、有桌在等已经知道要什么的饮品 → 先去旁边的饮品台拿（饮品不用等厨房，
            #    送齐一桌靠的就是饮品；以前手上塞满了菜，饮品要等一两分钟才去拿，桌子迟迟送不齐）
            #    只在正站在饮品台时拿：已经往过道方向取过菜了就不再滑回饮品台重新对一次（一趟拿齐，饮品下趟再补）
            if (free_slots > 0 and self.at_drink and self.pos is not None and self.pos[1] < 0.3
                    and time.time() - getattr(self, "_drink_try", 0) > 5):
                tags = self.drink_plan()
                if tags:
                    self._drink_try = time.time()
                    self.get_drinks(tags)
                    continue
            #    吧台上有珍馐（有额外的满堂彩加成）：只在闲着的时候顺手拿（没有客人等接待、没有能送的、手上菜不到 3 份），
            #    有活的时候绕去拿珍馐反而耽误事；要取菜时本来就优先拿珍馐
            if (free_slots > 0 and self.pos is not None and self.pos[1] < 0.3
                    and not wait and not todo and det.count("dish") < int(self.cfg["keep_dishes"])
                    and not (time.time() < getattr(self, "_nodish_until", 0) and "dish" in det)
                    and time.time() - getattr(self, "_rare_try", 0) > 4 and not self.g.boost_active()
                    and any(r for _, r in self.g.takeable_icons())):
                self._rare_try = time.time()
                if self.get_dishes(1):
                    continue
            # v82：收尾刚判「来不及只拿饮品」：8 秒内手上有菜就不再顺手取菜（23:57 拿完饮品又被这里和 topup_at_D 取了 2 份菜，多 4 秒）
            nodish_win = time.time() < getattr(self, "_nodish_until", 0) and "dish" in det
            if nodish_win:
                want_dish = 0
            lf_c = self.g.left_est()
            if (self.cfg.get("closed_dish_exact", True) and lf_c is not None and lf_c < 0 and wait_all
                    and not orders and "dish" in det):
                # v92：打烊后有人等接待、没有待送的桌：菜只补到等接待的人数，不再多拿（07:25:12 多取 1 份菜 4 秒，1 号没送上）
                want_dish = min(want_dish, len(wait_all) - det.count("dish"))
            if self.cfg.get("close_cap_dish", False) and lf_c is not None and lf_c < 5:
                want_dish = min(want_dish, 2 - det.count("dish"))  # v83：收尾手上菜最多 2 份
            if (free_slots > 0 and want_dish > 0 and self.pos is not None and self.pos[1] < 0.3
                    and time.time() - self.last_dish_try > self.cfg["dish_retry"] and self.g.takeable_icons()):
                if not self.get_dishes(min(want_dish, free_slots)):
                    self.last_dish_try = time.time()  # 拿不到（没对准/被挡），过一会儿再试，不原地反复
                continue
            #    手上一份菜都没有、吧台上有在途的菜、有客人要接待：先拿菜（空手去接待，客人点完单还得回来）
            if (wait and "dish" not in det and free_slots > 0 and self.surplus() > 0
                    and time.time() - self.last_dish_try > self.cfg["dish_retry"]):
                if not self.get_dishes(min(int(self.cfg["keep_dishes"]), free_slots)):
                    self.last_dish_try = time.time()
                continue
            #    在饮品台旁边、有桌缺饮品且知道要什么 → 拿上
            if self.at_drink and free_slots > 0 and time.time() - getattr(self, "_drink_try", 0) > 5:
                tags = self.drink_plan()
                if tags:
                    self._drink_try = time.time()
                    self.get_drinks(tags)
                    continue

            # 1. 客人来了先接待（记下要求，手上有对的当场送）；手上有能送的也一起排，哪个近先做哪个（同样近先送）
            if self.fired() and time.time() - getattr(self, "_prep_t", 0) > 8:
                lf = self.g.left_est()
                nd, nk = det.count("dish"), sum(1 for x in det if x in DRINK_TAGS)
                # v51（用户）：最后的客人最好带两菜一饮去接（两样都是菜的点单约 13.5%）；但来不及就不用拿全：
                # 手上有一菜一饮、又有客人在等，就直接去接；手上连一菜一饮都没有，或者没人等着时，才回去补到两菜两饮
                # v55：两菜一饮才稳（最后一位点两样菜时一菜一饮送不齐，12:12 1 号桌差 3 秒）。离结算（倒计时 0 后约 close_grace 秒）
                # 还够回去补一趟（close_trip 秒）就先补到两菜一饮再去接；不够了才带着一菜一饮直接去
                real_left = self.real_left() if lf is not None else None  # v95：打烊后按横幅倒计时校准
                if real_left is None:
                    real_left = (lf if lf is not None else 99) + float(self.cfg.get("close_grace", 24))
                # v88：v87 收尾常备饮品只要 1 瓶（没人等时 drink_plan 不会多拿），这里也按 1 瓶算，不然手上 3 菜 1 饮还反复回去补
                dk = 1 if self.cfg.get("close_stock_less", True) else 2
                need = ((nd < 1 or nk < 1) or ((nd < 2 or nk < dk) and not wait)
                        or (nd < 2 and real_left > float(self.cfg.get("close_trip", 22))))
                if (lf is not None and -float(self.cfg.get("close_prep_min", 15)) < lf < float(self.cfg.get("close_prep_left", 22))
                        and need and not todo):
                    # v43：收尾先把饮品补到 2 瓶、手上要有菜（v50：用户纠正打烊后饮品台照样能拿，倒计时 0 之后 15 秒内也补）
                    self._prep_t = time.time()
                    # v70：离结算不到 close_nodish_left 秒、手上有菜、有人在等：只补饮品不取菜（11-09 11:34 取菜多花 4 秒，5 号桌差一点）
                    # v81：wait 可能被 late_recv_ok 清空（手上凑不齐一菜一饮时），这里用过滤前的（23:49 6 号桌：倒计时 2 秒来的客人，
                    # 被当成没人等，多取了 4 秒菜，到桌刚读完要求就结算）
                    nodish = bool(wait_all) and nd >= 1 and real_left < float(self.cfg.get("close_nodish_left", 30))
                    self.log(f"倒计时 {lf:.0f} 秒、手上 {nd} 菜 {nk} 饮：先回去补"
                             f"{'（来不及了：只拿饮品不取菜）' if nodish else '（收尾带两菜两饮）'}")
                    # v77：收尾时手上的饮品已经够等着的桌（等菜缺饮品的 + 等接待的）：不在饮品台停，直接取菜
                    #（15:20 1 号桌只缺一份菜，回去还在饮品台多拿一瓶果香，差几秒没送上）
                    pend_dr = sum(1 for m, (s_, it) in st.items() if s_ == "order" and "drink" in it) + len(wait)
                    nodrink = (self.cfg.get("close_skip_drink", True) and nd < 2 and nk >= 1 and nk >= pend_dr
                               and lf is not None and lf < 0)
                    if nodrink:
                        self.log(f"手上 {nk} 瓶饮品够等着的 {pend_dr} 桌：这趟只取菜")
                    if nodish:
                        # v98：8→15 秒（走回饮品台就要 7~8 秒，窗口过期后 _trip_topup 又去取菜，11-15 17:12:06）
                        self._nodish_until = time.time() + float(self.cfg.get("nodish_window", 15))
                    self.restock(no_dish=nodish, no_drink=nodrink)
                    continue
            if (self.cfg.get("topup_at_D", True) and (wait or orders) and not nodish_win
                    and (self.at_drink or getattr(self, "wn_at", None) == "D")
                    and (det.count("dish") < 2 or not any(x in DRINK_TAGS for x in det))
                    and sum(1 for x in det if x) < 5 and time.time() - getattr(self, "_topup_t", 0) > 6):
                # v52：人在饮品台、手上不到两菜一饮：顺手补齐再出发（用户看到拿着一道菜就去接，接完还得回来拿饮品）。
                # 原来「路过吧台顺手拿」那段要坐标，新走法没有坐标，一直没生效
                self._topup_t = time.time()
                if self.restock():
                    continue
            tasks = [(m, "deliver") for m in todo] + [(m, "receive") for m in wait]
            if tasks:
                n, kind = min(tasks, key=self._task_key)
                if kind == "deliver":
                    if self.can_serve(n, self.g.table_states()[n][1]):
                        self.deliver(n)
                else:
                    self.receive(n)
                continue

            # 2.4 没有能送的、没人等接待：去结账（每局只来 12 位客人，桌子卡在「待结账」可能是原因；v13 试验）
            if self.cfg.get("do_checkout", True) and self.cfg.get("wang_nav"):
                pay = [m for m, (s_, _) in st.items() if s_ == "pay" and time.time() >= self.fail_until.get(m, 0)]
                if pay:
                    self.checkout(min(pay, key=lambda m: self._task_key_dist(m, "deliver")))
                    continue

            if self.at_table in TABLES:
                # v21 查问题：人在桌边却要回吧台——当时谁在等接待、谁在冷却、手上有什么
                lt = [m for m, (s_, _) in st.items() if s_ == "laotao"]
                cd = {m: round(self.fail_until.get(m, 0) - time.time()) for m in lt if time.time() < self.fail_until.get(m, 0)}
                self.log(f"[回吧台] 等接待 {lt} 冷却 {cd} 手上 {det} 待送 {dict((m, ''.join(i[0] for i in it)) for m, it in orders.items())}")
            # 3. 手上没有能送的、还有桌在等：回去补一趟（饮品按各桌要的香型最多 2 瓶，菜按实际缺的份数，吧台上有就拿）
            if orders:
                got = self.restock()
                if not got and not any(self.can_serve(m, it) for m, it in orders.items()):
                    if any("dish" in it for it in orders.values()):
                        self.wait_dishes_at_counter()  # 缺的是菜：在出餐口等一会儿（有新客人来下一轮会先去接待）
                    else:
                        time.sleep(1.0)
                continue

            # 4. 完全没事（没人等接待、也没东西能送）：回到吧台出餐口（饮品台旁边，菜从这头开始摆）原地等；
            #    已经在吧台前就不再走动，只在出了菜、手上不到 keep_dishes 份时顺手拿
            if self.pos is None or self.pos[1] >= 0.3:
                if self.where != "出餐口等待":
                    self.log("没有要接待和能送的，回吧台出餐口等着")
                self.anchor()
                self._at("出餐口等待")
                continue
            time.sleep(1.0)  # 没人等、没东西要送：在出餐口原地等，不来回走

    def _task_key(self, task):
        """任务排序：离现在所在的排越近越先（在吧台时 1、2 号最近，其次 3、4，最后 5、6）；同一排上菜先于接待"""
        n, kind = task
        # 等太久的桌（接待后超过 urgent_after 秒还没送齐）最先办：客人等到走掉，连续上餐会清零
        t0 = (getattr(self, "ep", None) or {}).get("recv", {}).get(n)
        urgent = 0 if (kind == "deliver" and t0 and time.time() - t0 > self.cfg["urgent_after"]) else 2
        t1 = getattr(self, "laotao_since", {}).get(n)
        if kind == "receive" and t1 and time.time() - t1 > self.cfg["recv_urgent_after"]:
            urgent = 1  # v36：等接待久的排在「接待后久等没送齐」的后面（以前同级按远近，6 号桌接待后等了 259 秒）
        if kind == "deliver" and self.fired() and self.cfg.get("fired_deliver_first", True):
            urgent = 0  # v42：满堂彩后先把送了一半的桌送齐再接新客（04:22 先去接 6 号，5 号缺 1 瓶饮品到结算）
        return (urgent,) + self._task_key_dist(n, kind)

    def _task_key_dist(self, n, kind):
        if self.use_coords() and self.pos is not None:
            # 按坐标估路程：回过道的横移 + 沿过道的前后 + 靠桌的横移
            tx, ty = self.table_spot(n)
            x, y = self.pos
            ax = self.aisle_x()
            dist = abs(x - ax) + abs(y - ty) + abs(tx - ax)
            return (round(dist, 1), 0 if kind == "deliver" else 1, n)
        row = TABLES[n][1]
        cur = TABLES[self.at_table][1] if self.at_table else -1  # -1 = 在吧台这头
        return (abs(row - cur), 0 if kind == "deliver" else 1, n)

    def wait_dishes_at_counter(self):
        """回到饮品台旁边等菜（菜是从饮品台那头开始往过道方向一份份摆的），出现了就去拿（最多等 dish_wait_max 秒）"""
        self.anchor()
        end = time.time() + self.cfg["dish_wait_max"]
        while time.time() < end and not self.g.takeable_icons():
            self.g.check()
            time.sleep(0.5)
        self.last_dish_try = 0.0
        want_dish = sum(it.count("dish") for m, (st_, it) in self.g.table_states().items()
                        if st_ == "order" and not self.skip_table(m))
        inv = self.sync_inv()
        self.get_dishes(min(max(1, want_dish - inv.count("dish")), 5 - len(inv)))

    def _low_stock(self):
        """该先回去补货了：手上 ≤2 样，或者一份菜都没有（客人一定会点菜，只拿着饮品去接待很亏）；
        而且出餐台可能有菜（没在等菜的冷却里）"""
        inv = self.sync_inv()
        left = self.g.left_est()
        if (self.fired() and left is not None and 3 < left < float(self.cfg.get("close_prep_left", 22))
                and inv.count("drink") < 2 and time.time() - self.last_dish_try > 3):
            return True  # v43：倒计时 0 一到饮品台就关，之后还有约 25 秒能接客：趁还开着把饮品补到 2 瓶以上
        if (inv and self.fired() and left is not None and left < self.cfg["end_recv_guard"]
                and "dish" in inv and any(x != "dish" for x in inv)):
            return False  # 收尾、已经不做菜了：手上一菜一饮都有就送，不再来回补货
            # v35：凑不齐一菜一饮时 late_recv_ok 不让接新客人，这里也不让补货 → 以前会空等到结算
        return ((len(inv) < self.cfg["restock_below"] or "dish" not in inv)
                and time.time() - self.last_dish_try > self.cfg["dish_retry"])

    def route(self, tables):
        """按路线排序：沿中间过道从前排（靠吧台）往后排走，同一排先右后左"""
        return sorted(tables, key=lambda n: (TABLES[n][1], n))

    def set_view(self):
        """把视角压到最低（俯视）。鼠标只上下移动，压到底后位置固定，每次都一样"""
        if self.cfg["top_view"] and not self.view_set:
            self.g.look(1500)
            self.view_set = True

    def align(self, lane):
        """俯视画面里找目标一侧最近的桌子，用 W/S 微调到桌子中线和角色平齐"""
        cy0, tol, k = self.cfg["char_y"], self.cfg["align_tol"], self.cfg["align_k"]
        for _ in range(8):
            ts = [t for t in self.g.tables() if (t[0] > 540 if lane == "right" else t[0] < 420)]
            if not ts:
                self.log("画面里没找到这一侧的桌子，跳过对齐")
                return False
            t = min(ts, key=lambda t: abs(t[1] - cy0))
            dy = t[1] - cy0
            if abs(dy) <= tol:
                return True
            # 桌子在角色下方（画面靠下）= 走过头了，往回 S；在上方就往前 W
            self.g.hold(["s" if dy > 0 else "w"], min(0.5, max(0.06, abs(dy) * k)))
        self.log("对齐 8 次还没对上")
        return False

    def _search(self):
        """靠到桌边还没提示（客人坐在桌子另一头）：先往前小步找，再往后找"""
        if self.cfg.get("wang_nav"):
            # 新走法：站位那一步已经按牌子对好、没提示也往桌子挪过了。这里再前后找会留下没记账的位移（还会开关慢走），
            # 实测之后的桌到桌就走反、走丢
            time.sleep(0.3)
            p = self.g.prompt()
            return p if p in TABLE_PROMPTS else None
        step, n = self.cfg["search_step"] / self._slow_on(), int(self.cfg["search_steps"])
        try:
            for key, times in (("w", n), ("s", n * 2), ("w", n)):  # 前 n 步 -> 后 2n 步 -> 回到原位
                for _ in range(times):
                    p = self.g.hold([key], step, stop_on=TABLE_PROMPTS)
                    if p:
                        return p
            return None
        finally:
            self.g.set_slow(False)

    def back_to_aisle(self):
        """从桌边横向退回中间过道，避免往吧台走时被长凳挡住"""
        if self.use_coords() and self.pos is not None:
            if self.pos[1] > 0.3:
                self._lateral_to(self.aisle_x())
            self.side = None
            return
        if self.side:
            key, t, slow = (self.side + (False,))[:3]
            if slow:
                self.g.set_slow(True)
            self.g.hold(["a" if key == "d" else "d"], t)
            self.g.set_slow(False)
            self.side = None

    def go_home(self):
        self.back_to_aisle()
        self.pin_counter()
        self._at("吧台")

    def _arrive(self, ok):
        if ok and self.cfg["press_f"]:
            self.g.press("f")
            self.log("按 F")


# ---------------- 测试通道 ----------------
# 只接受下面这些固定指令，不执行任何代码。指令写在 test_channel/cmd.json，结果写回 result.json + shot.jpg
CHANNEL_DIR = os.path.join(BASE_DIR, "test_channel")
CHANNEL_KEYS = set("wasdfqe12345m") | {"tab", "esc", "space", "ctrl"}
CHANNEL_PARAMS = {"pin_time", "slide_timeout", "lane_center", "side_max", "row1", "row2", "row3",
                  "char_y", "align_tol", "align_k", "below1", "below2", "below_max",
                  "turn_step", "turn_steps", "recover_walk", "max_drinks", "yaw_tol", "yaw_px_per_deg", "yaw_sign", "side_min",
                  "dish_timeout", "search_step", "search_steps"}


class _NotInHall(Exception):
    """MaaEnd 定位到人不在餐馆大厅（在外面大地图上）"""


def maaend_run(args, timeout=240):
    """用 MaaEnd 执行一条命令（子进程，沿用当前的管理员权限），返回它打印的结果行"""
    import subprocess
    script = os.path.join(BASE_DIR, "maaend_bridge.py")
    py = sys.executable.replace("pythonw.exe", "python.exe")
    # v99：以前用 subprocess.run 一直等到 MaaEnd 跑完（最长 5 分钟），期间不看停止信号——
    # 11-15 17:30 用户按了停止，MaaEnd 还在传送/走路，继续抢鼠标。现在每 0.2 秒查一次，要停就把它连同两个辅助程序一起关掉
    import tempfile
    out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="ignore")
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="ignore")
    proc = subprocess.Popen([py, script] + list(args), stdout=out_f, stderr=err_f, cwd=BASE_DIR,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def kill_tree():
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    end = time.time() + timeout
    try:
        while proc.poll() is None:
            ev = _STOP_EVENT
            if (ev is not None and ev.is_set()) or user32.GetAsyncKeyState(VK_STOP) & 0x8000:
                if ev is not None:
                    ev.set()
                kill_tree()
                release_inputs()
                raise Stopped()
            if time.time() > end:
                kill_tree()
                release_inputs()
                return "MaaEnd 超时"
            time.sleep(0.2)
        out_f.seek(0)
        err_f.seek(0)
        out, err = out_f.read(), err_f.read()
    finally:
        out_f.close()
        err_f.close()
    lines = [l for l in (out or "").splitlines() if l.startswith("{")]
    return lines[-1] if lines else ("MaaEnd 没有结果：" + (err or "")[-400:])


def channel_exec(cmd, nav, game, cfg):
    a = cmd.get("action")
    if a == "home":
        nav.go_home()
    elif a == "anchor":
        nav.anchor()
    elif a == "drink":
        nav.go_drink()
    elif a == "dish":
        nav.go_dish()
    elif a == "table":
        n = int(cmd["n"])
        if n not in TABLES:
            raise ValueError("桌号只能是 1-6")
        nav.go_table(n)
    elif a == "hold":
        keys = [str(k).lower() for k in cmd["keys"]]
        if not keys or not set(keys) <= set("wasd"):
            raise ValueError("hold 只能用 w/a/s/d")
        sec = min(float(cmd["sec"]), 5.0)
        game.hold(keys, sec)
        if "w" in keys:  # 手动往里走也要记下深度，回吧台时才退得够
            nav.depth += sec
    elif a == "press":
        key = str(cmd["key"]).lower()
        if key not in CHANNEL_KEYS:
            raise ValueError(f"不允许的按键：{key}")
        game.press(key)
    elif a == "look":
        dy, dx = int(cmd.get("dy", 0)), int(cmd.get("dx", 0))
        if abs(dy) > 1000 or abs(dx) > 5000:
            raise ValueError("look 的 dy 范围是 ±1000，dx 范围是 ±5000")
        game.look(dy, dx)
    elif a == "recover":
        nav.recover()
    elif a == "fixyaw":
        nav.fix_yaw()
    elif a == "cook":
        nav.cook()
    elif a == "head":
        nav.go_head()
    elif a == "start":
        nav.start_business()
    elif a == "run":
        nav.start_business()
        nav.auto(min(float(cmd.get("sec", 290)), 600))
    elif a == "drinks":
        nav.get_drinks(["果香"] * min(int(cmd["k"]), 5))
    elif a == "dishes":
        nav.get_dishes(min(int(cmd["d"]), 5))
    elif a in ("receive", "deliver"):
        n = int(cmd["n"])
        if n not in TABLES:
            raise ValueError("桌号只能是 1-6")
        getattr(nav, a)(n)
    elif a == "auto":
        nav.auto(min(float(cmd["sec"]), 600))
    elif a == "rounds":
        # 连跑几局（最多 3）：上一局结束后人还在原地，先走回吧台再开。
        # fresh：如果还停在上一局（中途停下的、已经乱了的）营业里，先提前结束它、点完结算，再从头开一局
        if cmd.get("fresh", True) and game.find_btn("ui_btn_continue") is not None:
            # 还停在结算画面（上一局被中途停下后结算到一半）：先点完，不然 MaaEnd 回吧台会一直超时
            nav.log("还停在上一局的结算画面，先点完结算")
            nav.settle()
        elif cmd.get("fresh", True):
            # v37/v60：只认图标会漏；料理界面、确认框挡着左上角时也要认出来（still_in_business）
            nav.end_stale_round()
        for i in range(max(1, min(int(cmd.get("n", 1)), 3))):
            if (i > 0 or cmd.get("walk_back")) and not nav.back_to_start():
                nav.teleport_home()
            nav.reset_round()
            nav.start_business()
            try:
                nav.auto(100000)
            except RoundLost:
                nav.log("走丢了：结束这一局，开下一局")  # 用户：走丢就自己结束重开
                nav.abort_round()
            nav.settle()
    elif a == "settle":
        nav.settle()
    elif a == "wang_practice":
        # 练习新寻路（只接待不送菜）。不在营业里就先走回开店点开一局
        if not game.find_ui("ui_in_business", 0.8):
            if not nav.back_to_start():
                nav.teleport_home()
            nav.reset_round()
            nav.start_business()
        try:
            nav.wang_practice(float(cmd.get("sec", 600)), cmd.get("tables"))
        except BusinessOver:
            nav.log("营业结束")
        if game.find_ui("ui_in_business", 0.8) and nav.abort_round():
            nav.settle()
        else:
            nav.settle()
    elif a == "open_only":
        # 只开店不接管：走回开店点开一局，之后交给用户手动操作（演示走法用）
        if not game.find_ui("ui_in_business", 0.8):
            if not nav.back_to_start():
                nav.teleport_home()
            nav.reset_round()
            nav.start_business()
        nav.log("已开店，交给你手动操作")
    elif a == "survey_wang":
        # 营业中采王字路线参考图（会接待客人拍气泡，但不上菜）。不在营业里就先走回开店点开一局
        if not game.find_ui("ui_in_business", 0.8):
            if not nav.back_to_start():
                nav.teleport_home()
            nav.reset_round()
            nav.start_business()
        nav.survey_wang(int(cmd.get("passes", 2)))
        nav.log("采样结束：提前结束这一局")
        if nav.abort_round():
            nav.settle()
    elif a == "survey_biz":
        # 营业中采干净的位置样本（不接待、不按 F）
        nav.survey_biz(int(cmd.get("rounds", 1)))
    elif a == "abort":
        # 提前结束正在进行的这一局并点完结算（这一局已经乱了，不值得接着跑）
        if nav.abort_round():
            nav.settle()
    elif a == "tphome":
        nav.teleport_home()
    elif a == "click":
        # 在游戏窗口里点一下（坐标是 1920x1080 画面坐标）
        x, y = int(cmd["x"]), int(cmd["y"])
        if not (0 <= x < 1920 and 0 <= y < 1080):
            raise ValueError("click 坐标要在 1920x1080 画面内")
        game.click(x, y)
    elif a == "longpress":
        # 在游戏窗口里按住鼠标左键几秒（地图的放大/缩小按钮要长按）
        x, y = int(cmd["x"]), int(cmd["y"])
        if not (0 <= x < 1920 and 0 <= y < 1080):
            raise ValueError("longpress 坐标要在 1920x1080 画面内")
        game.focus()
        ox, oy = game.client_origin()
        pydirectinput.moveTo(ox + x, oy + y)
        time.sleep(0.05)
        pydirectinput.mouseDown()
        time.sleep(min(float(cmd.get("sec", 2)), 5))
        pydirectinput.mouseUp()
    elif a == "wait":
        time.sleep(min(float(cmd.get("sec", 1)), 10))
    elif a == "mode":
        if cmd.get("value") not in ("试营业", "正式经营"):
            raise ValueError("mode 只能是 试营业 / 正式经营")
        cfg["business_mode"] = cmd["value"]
        save_config(cfg)
    elif a == "record":
        n = int(cmd["n"])
        if n not in TABLES:
            raise ValueError("桌号只能是 1-6")
        nav.record_ref(n)
    elif a == "set":
        key, val = cmd["key"], float(cmd["value"])
        if key not in CHANNEL_PARAMS:
            raise ValueError(f"不允许的参数：{key}")
        if key.startswith("row"):
            cfg["rows"][int(key[3]) - 1] = val
        else:
            cfg[key] = val
        save_config(cfg)
    elif a not in ("shot", "quit", "reload", "spawn"):
        raise ValueError(f"未知指令：{a}")


STABLE_PYW = os.path.join(BASE_DIR, "venv", "Scripts", "pythonw.exe")  # 固定位置的 Python 环境（临时文件夹里的会被清理删掉）


def kill_other_instances():
    """关掉别的正在跑的机器人（命令行里带 laotao_bot 的 python 进程，自己和自己的启动器除外）。
    新开的机器人本身是管理员权限，关得掉旧的，不用弹 UAC"""
    import subprocess
    me, parent = os.getpid(), os.getppid()
    # 也关掉上一个机器人留下的 MaaEnd 桥（maaend_bridge.py）和它的 agent（go-service、cpp-algo）：
    # 实测旧机器人被强关后，它开的 MaaEnd 还连着游戏，新的 MaaEnd 回吧台一直超时（5 分钟）
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'laotao_bot' "
          f"-and $_.ProcessId -ne {me} -and $_.ProcessId -ne {parent} }} "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, creationflags=0x08000000)
    except Exception:
        pass


def channel_shot(game, path):
    ox, oy = game.client_origin()
    if not hasattr(game._local, "sct"):
        game._local.sct = mss.mss()
    img = np.array(game._local.sct.grab({"left": ox, "top": oy, "width": 1920, "height": 1080}))
    img = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR), (960, 540), interpolation=cv2.INTER_AREA)
    cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tofile(path)


# ---------------- 界面 ----------------
BG = "#f5f3ef"
FONT = ("Microsoft YaHei UI", 10)
PARAMS = [
    ("pin_time", "贴吧台 S 时长"),
    ("slide_timeout", "找饮品台最长时间"),
    ("lane_center", "饮品台→中间过道 时长"),
    ("side_max", "横向靠桌最长时间"),
    ("below1", "饮品台→1号桌正下方"),
    ("below2", "饮品台→2号桌正下方"),
    ("row1", "吧台→第1排 W 时长"),
    ("row2", "吧台→第2排 W 时长"),
    ("row3", "吧台→第3排 W 时长"),
    ("dish_timeout", "找取菜提示最长时间"),
]


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("咕嘎餐馆")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.cfg = load_config()

        # 使用说明
        tip = tk.Label(self.root, bg=BG, fg="#555", justify="left", font=FONT, text=(
            "使用前：\n"
            "  1. 游戏窗口 1920×1080，人物站在吧台前「开始经营」提示处\n"
            "  2. 把视角转到面朝吧台（背对餐厅），之后不要动鼠标\n"
            "  3. 点「开始」后自动：开局 → 做菜、接待、上菜 → 结算 → 下一局\n"
            "  紧急停止：按键盘 ~ 键（Tab 上面那个）"))
        tip.pack(fill="x", padx=14, pady=(12, 6))

        box = tk.Frame(self.root, bg=BG)
        box.pack(fill="x", padx=14, pady=4)
        tk.Label(box, text="局数", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.rounds = tk.StringVar(value="0")
        tk.Spinbox(box, from_=0, to=999, width=5, textvariable=self.rounds, font=FONT).grid(row=0, column=1, sticky="w", padx=6)
        tk.Label(box, text="（0 = 一直循环）", bg=BG, fg="#888", font=FONT).grid(row=0, column=2, sticky="w")
        tk.Label(box, text="模式", bg=BG, font=FONT).grid(row=1, column=0, sticky="w", pady=4)
        self.mode = tk.StringVar(value=self.cfg["business_mode"])
        for i, m in enumerate(("试营业", "正式经营")):
            tk.Radiobutton(box, text=m, value=m, variable=self.mode, bg=BG, font=FONT).grid(row=1, column=1 + i, sticky="w")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=14, pady=8)
        tk.Button(btns, text="开始", width=12, font=("Microsoft YaHei UI", 12, "bold"), bg="#2e7d32", fg="white",
                  relief="flat", command=self.start_loop).pack(side="left")
        tk.Button(btns, text="停止 (~)", width=10, font=("Microsoft YaHei UI", 12), bg="#c62828", fg="white",
                  relief="flat", command=self.stop).pack(side="left", padx=10)
        self.status = tk.StringVar(value="未运行")
        tk.Label(self.root, textvariable=self.status, bg=BG, fg="#1565c0",
                 font=("Microsoft YaHei UI", 12, "bold")).pack(fill="x", padx=14, pady=(0, 4))

        self.text = scrolledtext.ScrolledText(self.root, width=58, height=9, state="disabled",
                                              font=("Microsoft YaHei UI", 9))
        self.text.pack(fill="both", expand=True, padx=14, pady=(2, 6))

        # ---- 调试（默认收起）：手动走位、参数、Claude 测试通道 ----
        self.adv_shown = False
        tk.Button(self.root, text="调试 ▸", relief="flat", bg=BG, fg="#888", font=("Microsoft YaHei UI", 9),
                  command=self.toggle_adv).pack(anchor="e", padx=14, pady=(0, 8))
        self.adv = tk.Frame(self.root, bg=BG)
        adv = self.adv

        top = tk.Frame(adv, bg=BG)
        top.pack(fill="x", padx=12, pady=(4, 4))
        self.where = tk.StringVar(value="位置：未知")
        self.cur_prompt = tk.StringVar(value="当前提示：—")
        tk.Label(top, textvariable=self.where, bg=BG, fg="#1565c0", font=FONT).pack(side="left")
        tk.Label(top, textvariable=self.cur_prompt, bg=BG, fg="#2e7d32", font=FONT).pack(side="left", padx=20)

        dest = tk.LabelFrame(adv, text="走到", bg=BG, font=FONT)
        dest.pack(fill="x", padx=12, pady=4)
        row1 = tk.Frame(dest, bg=BG)
        row1.pack(fill="x", pady=2)
        for text, fn in [("贴吧台", lambda n: n.go_home()), ("饮品台(锚点)", lambda n: n.go_drink()),
                         ("出餐台取菜", lambda n: n.go_dish())]:
            tk.Button(row1, text=text, width=12, font=FONT, command=lambda f=fn: self.run(f)).pack(side="left", padx=4)
        grid = tk.Frame(dest, bg=BG)
        grid.pack(pady=2)
        for r, (l, rr) in enumerate([(6, 5), (4, 3), (2, 1)]):
            for c, n in enumerate((l, rr)):
                tk.Button(grid, text=f"{n} 号桌", width=10, font=FONT,
                          command=lambda n=n: self.run(lambda nav: nav.go_table(n))).grid(row=r, column=c * 2, padx=30, pady=2)
        row3 = tk.Frame(dest, bg=BG)
        row3.pack(fill="x", pady=2)
        tk.Button(row3, text="全部桌子走一遍", width=14, font=FONT,
                  command=lambda: self.run(self.tour)).pack(side="left", padx=4)
        self.press_f = tk.BooleanVar(value=self.cfg["press_f"])
        tk.Checkbutton(row3, text="到达后按 F", variable=self.press_f, bg=BG, font=FONT).pack(side="left", padx=10)
        self.channel_on = tk.BooleanVar(value=False)
        self.channel_enabled = False  # 供后台线程读取，避免跨线程访问 tk 变量
        tk.Checkbutton(row3, text="开启 Claude 测试通道", variable=self.channel_on, bg=BG, font=FONT,
                       fg="#c62828", command=self.toggle_channel).pack(side="left", padx=10)
        threading.Thread(target=self.channel_loop, daemon=True).start()
        if "--channel" in sys.argv:  # 命令行带 --channel：一打开就开启测试通道（不用手动勾）
            kill_other_instances()  # 只留自己：旧的（通道关了、不响应的）关掉，免得几个机器人抢通道
            self.channel_on.set(True)
            self.root.after(500, self.toggle_channel)

        pf = tk.LabelFrame(adv, text="参数（秒）", bg=BG, font=FONT)
        pf.pack(fill="x", padx=12, pady=4)
        self.vars = {}
        for i, (key, label) in enumerate(PARAMS):
            tk.Label(pf, text=label, bg=BG, font=FONT).grid(row=i // 2, column=(i % 2) * 2, sticky="e", padx=4, pady=2)
            v = tk.StringVar(value=str(self._get(key)))
            tk.Entry(pf, textvariable=v, width=7, font=FONT).grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w")
            self.vars[key] = v
        tk.Button(pf, text="保存参数", font=FONT, command=self.save).grid(row=(len(PARAMS) + 1) // 2, column=3, sticky="e", pady=4)

        self.root.after(100, self.poll)
        self.root.after(500, self.watch_prompt)

    def toggle_adv(self):
        self.adv_shown = not self.adv_shown
        if self.adv_shown:
            self.adv.pack(fill="x", pady=(0, 10))
        else:
            self.adv.pack_forget()

    def start_loop(self):
        try:
            rounds = int(self.rounds.get())
        except ValueError:
            self.log("局数只能填数字")
            return
        self.cfg["business_mode"] = self.mode.get()
        save_config(self.cfg)
        self.run(lambda nav: self.loop(nav, rounds))

    def loop(self, nav, rounds):
        """一局接一局：开局 → 自动服务 → 结算（继续、完成）→ 走回吧台 → 下一局"""
        i = 0
        while rounds <= 0 or i < rounds:
            i += 1
            total = "∞" if rounds <= 0 else str(rounds)
            self.q.put(("status", f"运行中：第 {i} 局 / 共 {total} 局"))
            self.log(f"===== 第 {i} 局开始 =====")
            if i > 1 and not nav.back_to_start():
                nav.teleport_home()
            nav.reset_round()
            nav.start_business()
            try:
                nav.auto(100000)
            except RoundLost:
                nav.log("走丢了：结束这一局，开下一局")  # 用户：走丢就自己结束重开
                nav.abort_round()
            nav.settle()
            self.log(f"===== 第 {i} 局完成 =====")
        self.q.put(("status", f"已完成 {i} 局"))

    def _get(self, key):
        if key.startswith("row"):
            return self.cfg["rows"][int(key[3]) - 1]
        return self.cfg[key]

    def apply_vars(self):
        for key, v in self.vars.items():
            val = float(v.get())
            if key.startswith("row"):
                self.cfg["rows"][int(key[3]) - 1] = val
            else:
                self.cfg[key] = val
        self.cfg["press_f"] = self.press_f.get()

    def save(self):
        try:
            self.apply_vars()
            save_config(self.cfg)
            self.log("参数已保存")
        except ValueError:
            self.log("参数格式不对，只能填数字")

    def log(self, s):
        self.q.put(("log", s))

    def file_log(self, line):
        """每条日志同时追加到 test_channel/run.log（超过 2MB 就换新文件）"""
        try:
            d = os.path.join(BASE_DIR, "test_channel")
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, "run.log")
            if os.path.exists(fp) and os.path.getsize(fp) > 2_000_000:
                try:
                    os.replace(fp, fp + ".old")
                except OSError:
                    pass  # v44：文件被别的程序开着（如 tail）时改名会失败——以前整条日志就丢了，现在照样往下写
            with open(fp, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    line = time.strftime("%H:%M:%S ") + item[1]
                    self.file_log(line)
                    self.text.configure(state="normal")
                    self.text.insert("end", line + "\n")
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif item[0] == "where":
                    self.file_log(time.strftime("%H:%M:%S ") + f"  [位置] {item[1]}")
                    self.where.set(f"位置：{item[1]}")
                elif item[0] == "reload":
                    import subprocess
                    exe = sys.executable
                    if not getattr(sys, "frozen", False) and os.path.exists(STABLE_PYW):
                        exe = STABLE_PYW  # 重载时换到固定位置的环境
                    args = [exe] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)])
                    subprocess.Popen(args + ["--channel"], cwd=BASE_DIR)
                    self.root.destroy()
                    return
                elif item[0] == "quit":
                    self.root.destroy()  # 测试通道发来「退出」：关掉程序（好替换新版 exe）
                    return
                elif item[0] == "channel_off":
                    self.channel_on.set(False)
                    self.toggle_channel()
                elif item[0] == "syncvars":
                    self.sync_vars()
                elif item[0] == "status":
                    self.status.set(item[1])
                elif item[0] == "prompt":
                    self.cur_prompt.set(f"当前提示：{item[1]}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def watch_prompt(self):
        """空闲时每秒识别一次 F 提示，方便核对识别是否准"""
        if not (self.worker and self.worker.is_alive()):
            def job():
                try:
                    if not hasattr(self, "_watch_game"):
                        self._watch_game = Game(self.cfg, self.log, threading.Event())
                    p = self._watch_game.prompt()
                    self.q.put(("prompt", PROMPTS.get(p, "—")))
                except Exception:
                    self.q.put(("prompt", "（没找到游戏窗口）"))
                finally:
                    # v84：每秒一个新线程，线程里的 mss 不关会漏 2 个 GDI 句柄 + 截图缓冲；跑 5000 秒攒满 1 万个上限，
                    # 截图报 CreateDIBSection/CreateCompatibleDC 失败、机器人退出（11-10 01:53，连跑 1 小时 23 分）
                    try:
                        g_ = getattr(self, "_watch_game", None)
                        sct = getattr(g_._local, "sct", None) if g_ else None
                        if sct is not None:
                            sct.close()
                            del g_._local.sct
                    except Exception:
                        pass
            threading.Thread(target=job, daemon=True).start()
        self.root.after(1000, self.watch_prompt)

    def toggle_channel(self):
        self.channel_enabled = self.channel_on.get()
        if self.channel_enabled:
            self.log(f"测试通道已开启：{CHANNEL_DIR}")
            self.root.lower()
        else:
            # 用户取消勾选 = 手动停止：停掉正在跑的这一局/动作；通道 15 秒后自动重新打开，Claude 好接着测（用户：全程自动，
            # 我只在看到问题时手动停）
            self.stop_event.set()
            self.log("测试通道已关闭（手动停止）：正在跑的已停下，15 秒后通道自动重新打开")
            self.root.after(15000, self._channel_back)

    def _channel_back(self):
        if not self.channel_on.get():
            self.channel_on.set(True)
            self.toggle_channel()

    def sync_vars(self):
        for key, v in self.vars.items():
            v.set(str(self._get(key)))

    def channel_loop(self):
        """测试通道：读取 test_channel/cmd.json，执行固定指令，结果写回 result.json"""
        os.makedirs(CHANNEL_DIR, exist_ok=True)
        cmd_path = os.path.join(CHANNEL_DIR, "cmd.json")
        game = nav = None
        while True:
            time.sleep(0.2)
            if not self.channel_enabled or not os.path.exists(cmd_path):
                continue
            if self.worker and self.worker.is_alive():
                continue
            try:
                with open(cmd_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                os.remove(cmd_path)
            except Exception:
                continue
            lines = []

            def log(s):
                lines.append(s)
                self.log("[通道] " + s)
            result = {"id": data.get("id"), "ok": True}
            try:
                self.stop_event.clear()
                if game is None:
                    game = Game(self.cfg, log, self.stop_event)
                    game.find_window()
                    nav = Navigator(game, log, lambda *a: self.q.put(a))
                game.log = nav.log = log
                for cmd in data.get("cmds", [data]):
                    log(f"执行 {json.dumps(cmd, ensure_ascii=False)}")
                    channel_exec(cmd, nav, game, self.cfg)
                result["prompt"] = PROMPTS.get(game.prompt(), None)
                result["where"] = nav.where
                result["pos"] = nav.pos
                result["sign"] = game.which_table()
                result["counter_angle"] = game.counter_angle()
                result["waiting"] = game.waiting_tables()
                result["states"] = {n: s + ("" if not it else ":" + ",".join(it))
                                    for n, (s, it) in game.table_states().items()}
                result["hotbar"] = game.hotbar_count()
                result["inv"] = nav.inv
                channel_shot(game, os.path.join(CHANNEL_DIR, "shot.jpg"))
            except Stopped:
                result.update(ok=False, error="已停止")
                try:
                    channel_shot(game, os.path.join(CHANNEL_DIR, "shot.jpg"))  # 停下时的画面，方便查原因
                except Exception:
                    pass
                if getattr(game, "flag_stop", False):
                    game.flag_stop = False  # 通道自己的急停：停下这一局就行，通道不关（接着收重新加载等指令）
                else:
                    self.q.put(("channel_off",))  # 按了停止/End：关掉通道，后面的指令都不再执行
            except Exception as e:
                result.update(ok=False, error=str(e))
                game = None  # 下次重新找窗口
            finally:
                release_inputs()
                if game is not None and game.slow:
                    game.set_slow(False)  # 停下时如果还在慢走，切回正常速度
            result["log"] = lines
            self.q.put(("syncvars",))
            tmp = os.path.join(CHANNEL_DIR, "result.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(CHANNEL_DIR, "result.json"))
            if any(c.get("action") == "reload" for c in data.get("cmds", [data])):
                self.q.put(("reload",))
            elif any(c.get("action") == "quit" for c in data.get("cmds", [data])):
                self.q.put(("quit",))

    def tour(self, nav):
        for n in (1, 3, 5, 2, 4, 6):
            nav.go_table(n)
            time.sleep(0.5)
        nav.anchor()

    def run(self, fn):
        if self.worker and self.worker.is_alive():
            return
        try:
            self.apply_vars()
        except ValueError:
            self.log("参数格式不对，只能填数字")
            return
        self.stop_event.clear()
        self.root.lower()
        self.worker = threading.Thread(target=self.work, args=(fn,), daemon=True)
        self.worker.start()

    def work(self, fn):
        g = None
        try:
            g = Game(self.cfg, self.log, self.stop_event)
            g.find_window()
            nav = Navigator(g, self.log, lambda *a: self.q.put(a))
            fn(nav)
            self.log("完成")
        except Stopped:
            self.log("已停止")
            self.q.put(("status", "已停止"))
        except Exception as e:
            self.log(f"出错：{e}")
            self.q.put(("status", f"出错停止：{e}"))
        finally:
            release_inputs()
            if g is not None and g.slow:
                g.set_slow(False)  # 停下时如果还在慢走，切回正常速度

    def stop(self):
        """停止当前动作，并关掉测试通道（否则 Claude 发来的下一条指令会接着走）"""
        self.stop_event.set()
        self.status.set("已停止")
        if self.channel_enabled:
            self.channel_on.set(False)
            self.toggle_channel()

    def mainloop(self):
        self.root.mainloop()


if __name__ == "__main__":
    avoid_bad_cpus()  # 本程序和之后启动的子进程都不用坏核心
    App().mainloop()
