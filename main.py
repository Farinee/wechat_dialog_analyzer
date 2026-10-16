"""Run the editable desktop app with: D:\\conda\\python.exe main.py."""
from pathlib import Path
from importlib.util import find_spec
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

def check_dependencies():
    required = {"tkinter": "Tkinter", "cryptography": "cryptography", "zstandard": "zstandard",
        "PIL":"Pillow","imageio_ffmpeg":"imageio-ffmpeg","tokenizers":"tokenizers"}
    missing = [label for module, label in required.items() if find_spec(module) is None]
    if missing:
        requirements = PROJECT_ROOT / "requirements.txt"
        message = ("当前 Python 环境缺少依赖：" + ", ".join(missing) +
            f"\n实际解释器：{sys.executable}\n请在 PowerShell 中运行：\n"
            f'& "{sys.executable}" -m pip install -r "{requirements}"\n')
        if "Tkinter" in missing:
            message += "Tkinter 需要通过该 Python 环境的安装器或 Conda 安装。\n"
        raise SystemExit(message)


def launch():
    check_dependencies()
    from app import main
    main()


if __name__ == "__main__":
    launch()
