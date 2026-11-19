"""打包用户版：python build.py
生成 dist/咕嘎餐馆_<版本>/（咕嘎餐馆.exe + 识别用的图片和数据 + 使用说明 + LICENSE），可以直接压缩发给别人。
版本号取 guga_app.py 里的 VERSION。需要先 pip install -r requirements.txt（含 pyinstaller）。"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "咕嘎餐馆"
with open(os.path.join(ROOT, "guga_app.py"), encoding="utf-8") as _f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', _f.read(), re.M).group(1)
OUT = os.path.join(ROOT, "dist", f"{NAME}_{VERSION}")


def main():
    os.chdir(ROOT)
    for d in ("build", os.path.join("dist", NAME + ".exe"), OUT):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--uac-admin",  # 游戏是管理员权限运行的，不这样发不了按键
        "--name", NAME, "--icon", os.path.join(ROOT, "guga.ico"),  # 要绝对路径：相对路径会按 build/ 去找
        "--collect-all", "rapidocr_onnxruntime",
        "--hidden-import", "hall_locator", "--hidden-import", "record_file",
        "--distpath", "dist", "--workpath", "build", "--specpath", "build",
        "guga_app.py",
    ])
    os.makedirs(OUT)
    shutil.move(os.path.join(ROOT, "dist", NAME + ".exe"), os.path.join(OUT, NAME + ".exe"))
    for d in ("picture_laotao", "menu"):
        shutil.copytree(d, os.path.join(OUT, d))
    os.makedirs(os.path.join(OUT, "test_channel"))
    for d in ("nodes", "survey", "places_clean"):
        shutil.copytree(os.path.join("test_channel", d), os.path.join(OUT, "test_channel", d))
    shutil.copy(os.path.join("test_channel", "wang_params.json"), os.path.join(OUT, "test_channel"))
    for f in ("laotao_config.json", "使用说明.txt", "LICENSE"):
        shutil.copy(f, OUT)
    print("\n打包完成：", OUT)


if __name__ == "__main__":
    main()
