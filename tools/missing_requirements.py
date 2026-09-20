"""requirements.txt 에서 현재 OS 에 해당하는 패키지 중 **설치되지 않은** 것을 한 줄에 하나씩 찍는다.

run.bat 이 쓴다(run.sh 는 같은 규칙을 셸로 들고 있다 — `_requirements_list`·`_import_name`
·`_is_installed`). 판정은 import 를 실행하지 않는 find_spec 이다: pykrx 처럼 import 시점에
네트워크를 타는 패키지가 '설치 안 됨'으로 오판되지 않게 한다.
"""
import importlib.util
import os
import re
import sys

IMPORT_NAME = {
    "beautifulsoup4": "bs4", "google-genai": "google.genai", "python-dotenv": "dotenv",
    "tradingview-screener": "tradingview_screener", "tvdatafeed": "tvDatafeed",
    "gnureadline": "gnureadline", "finance-datareader": "FinanceDataReader",
}


def requirements(path):
    plat = sys.platform            # 'win32' | 'darwin' | 'linux'
    for raw in open(path, encoding="utf-8"):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line or line.startswith("-"):
            continue
        if ";" in line:
            line, marker = line.split(";", 1)
            if "darwin" in marker and plat != "darwin":
                continue
            if "linux" in marker and not plat.startswith("linux"):
                continue
            if "win" in marker and plat != "win32":
                continue
        line = line.split(" @ ")[0]
        name = re.split(r"[<>=!~\[]", line)[0].strip()
        if name:
            yield name


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False
    except Exception:          # noqa: BLE001 - 부모 패키지 import 가 예외를 냈다 → 설치는 돼 있다
        return True


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for pkg in requirements(os.path.join(root, "requirements.txt")):
        if not installed(IMPORT_NAME.get(pkg.lower(), pkg)):
            print(pkg)
