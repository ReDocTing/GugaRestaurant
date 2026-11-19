"""大厅定位（自建的「小地图」）：营业时屏幕上没有小地图，用画面里固定不动的景物认位置。
对照表在 test_channel/survey/（不营业时用 MaaEnd 定位采集：坐标 + 面朝吧台的截图，见 laotao_bot.survey_grid）。
定位时把当前画面和对照表逐张比，取最像的几张按相似度加权，得到大厅坐标 (x, y)（MaaEnd 的坐标系，缘缘庄一楼）。

只比画面的一部分：营业和不营业时界面不一样（顶部桌位栏、左上信息面板、小地图、右侧图标），都挡掉；
客人和气泡多在画面中下部，所以用边缘图（形状）比，不用颜色。"""
import json
import os

import cv2
import numpy as np

SW, SH = 96, 40  # 特征图大小


def _mask():
    m = np.ones((540, 960), np.float32)
    m[:100] = 0            # 顶部：营业时的桌位栏 / 不营业时的小地图和图标
    m[100:290, :310] = 0   # 左上：信息面板 / 任务追踪
    m[:, 880:] = 0         # 右侧图标
    m[420:] = 0            # 底部物品栏和人物脚下
    m[200:520, 380:580] = 0  # 人物本身（画面正中）
    return m


MASK = _mask()
ROI = (100, 420)  # 只取这几行


def feature(img960):
    """画面 → 特征向量（边缘强度，缩小、遮挡界面、归一化）"""
    g = cv2.cvtColor(img960, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    e = cv2.magnitude(gx, gy) * MASK
    e = e[ROI[0]:ROI[1]]
    f = cv2.resize(e, (SW, SH), interpolation=cv2.INTER_AREA)
    f = np.sqrt(f)  # 压一压很强的边
    f -= f.mean()
    n = np.linalg.norm(f)
    return (f / n).ravel() if n > 0 else f.ravel()


class HallLocator:
    def __init__(self, survey_dir, bounds=(126.0, 144.0, 120.0, 141.0)):
        self.recs, feats = [], []
        x0, x1, y0, y1 = bounds
        ex = os.path.join(survey_dir, "exclude.json")
        bad = set(json.load(open(ex, encoding="utf-8"))) if os.path.exists(ex) else set()  # 镜头被墙挤歪的坏样本
        with open(os.path.join(survey_dir, "index.jsonl"), encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r["file"] in bad or not (x0 <= r["x"] <= x1 and y0 <= r["y"] <= y1):
                    continue  # 大厅外（侧廊）的点不要
                if r.get("cam") is None or abs((r["cam"] - 88 + 180) % 360 - 180) > 4:
                    continue  # 镜头没对正的不要
                img = cv2.imdecode(np.fromfile(os.path.join(survey_dir, r["file"]), dtype=np.uint8), 1)
                if img is None:
                    continue
                self.recs.append(r)
                feats.append(feature(img))
        self.F = np.array(feats, np.float32)
        self.P = np.array([[r["x"], r["y"]] for r in self.recs], np.float32)

    def locate(self, img960, k=4, exclude=None):
        """返回 (x, y, 置信度)。置信度 = 最像那张的相似度（0~1）"""
        s = self.F @ feature(img960)
        if exclude is not None:
            s[exclude] = -1
        idx = np.argsort(-s)[:k]
        w = np.maximum(s[idx] - s[idx[-1]] + 1e-3, 1e-3)  # 越像权重越大
        p = (self.P[idx] * w[:, None]).sum(0) / w.sum()
        return float(p[0]), float(p[1]), float(s[idx[0]])


if __name__ == "__main__":
    # 自测：留一法，每张用其余的来定位，看误差
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_channel", "survey")
    L = HallLocator(d)
    errs = []
    for i in range(len(L.recs)):
        x, y, c = L.locate(cv2.imdecode(np.fromfile(os.path.join(d, L.recs[i]["file"]), dtype=np.uint8), 1), exclude=i)
        errs.append(np.hypot(x - L.P[i, 0], y - L.P[i, 1]))
    errs = np.array(errs)
    print(f"{len(errs)} 张：误差中位 {np.median(errs):.2f}，90% 以内 {np.percentile(errs, 90):.2f}，最大 {errs.max():.2f}")


class OrbLocator:
    """特征点（ORB）版定位：营业时镜头和采集时远近不同，比整张图不行，比特征点能对上。
    返回 (x, y, 内点数)；内点数太少（<15）说明认不出来"""

    def __init__(self, survey_dir):
        base = HallLocator(survey_dir)  # 复用它的筛选（大厅范围、坏样本、镜头对正）
        self.P = base.P
        self.orb = cv2.ORB_create(1500)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.mask = MASK.astype(np.uint8) * 255
        self.refs = []
        for r in base.recs:
            img = cv2.imdecode(np.fromfile(os.path.join(survey_dir, r["file"]), dtype=np.uint8), 1)
            k, d = self.orb.detectAndCompute(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), self.mask)
            self.refs.append((np.float32([p.pt for p in k]) if k else None, d))

    def locate(self, img960):
        k, d = self.orb.detectAndCompute(cv2.cvtColor(img960, cv2.COLOR_BGR2GRAY), self.mask)
        if d is None or len(k) < 20:
            return None
        pts = np.float32([p.pt for p in k])
        scores = np.zeros(len(self.refs))
        for i, (kp2, d2) in enumerate(self.refs):
            if d2 is None or len(d2) < 8:
                continue
            try:
                ms = self.bf.knnMatch(d, d2, k=2)
            except cv2.error:
                continue
            good = [m for m, n in (x for x in ms if len(x) == 2) if m.distance < 0.75 * n.distance]
            if len(good) < 8:
                continue
            src = pts[[m.queryIdx for m in good]]
            dst = kp2[[m.trainIdx for m in good]]
            _, inl = cv2.findHomography(src, dst, cv2.RANSAC, 8.0)
            scores[i] = int(inl.sum()) if inl is not None else 0
        idx = np.argsort(-scores)[:3]
        w = scores[idx] + 1e-3
        p = (self.P[idx] * w[:, None]).sum(0) / w.sum()
        return float(p[0]), float(p[1]), int(scores[idx[0]])

    def yaw_shift(self, img960):
        """和最像的那张采集图比，对上的特征点平均左右差多少像素（参考图 x − 当前 x）。
        采集图都是正对吧台拍的，所以这个差基本就是镜头左右转偏了多少。返回 (像素差, 内点数) 或 None"""
        k, d = self.orb.detectAndCompute(cv2.cvtColor(img960, cv2.COLOR_BGR2GRAY), self.mask)
        if d is None or len(k) < 20:
            return None
        pts = np.float32([p.pt for p in k])
        best = (0, None)
        for kp2, d2 in self.refs:
            if d2 is None or len(d2) < 8:
                continue
            try:
                ms = self.bf.knnMatch(d, d2, k=2)
            except cv2.error:
                continue
            good = [m for m, n in (x for x in ms if len(x) == 2) if m.distance < 0.75 * n.distance]
            if len(good) < 8:
                continue
            src = pts[[m.queryIdx for m in good]]
            dst = kp2[[m.trainIdx for m in good]]
            _, inl = cv2.findHomography(src, dst, cv2.RANSAC, 8.0)
            if inl is None:
                continue
            inl = inl.ravel().astype(bool)
            if inl.sum() > best[0]:
                best = (int(inl.sum()), float(np.median(dst[inl, 0] - src[inl, 0])))
        return None if best[1] is None else (best[1], best[0])
