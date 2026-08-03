#!/bin/bash

# 1. 스크립트가 위치한 디렉토리로 이동 (경로 의존성 해결)
cd "$(dirname "$0")"

# 1-1. API 키 등 환경변수를 여기서 직접 읽는다.
#  ~/.htsrc 는 보통 ~/.zshrc 가 읽어 주지만, 그건 **대화형 셸에만** 적용된다.
#  게다가 오래 띄워 둔 터미널은 그 터미널이 열린 시점의 환경을 그대로 들고 있어,
#  나중에 추가한 변수는 재로그인 전까지 프로세스에 전달되지 않는다.
#  (실측 2026-08-03: KIS 키는 있는데 JOURNAL_API_URL/KEY 만 빠져 매매일지 연동이
#   조용히 비활성 — 웹 대시보드에서 봇 하나가 통째로 안 보였다.)
#  여기서 읽으면 실행 방식(터미널·IDE·cron·systemd)과 무관하게 항상 최신 값이 들어간다.
if [ -f "$HOME/.htsrc" ]; then
    set -a                 # 이 구간에서 정의되는 변수를 자동 export
    . "$HOME/.htsrc"
    set +a
fi

# ---------------------------------------------------------
# 필수 라이브러리 목록
# ---------------------------------------------------------
# pykrx / finance-datareader: 국내 일봉을 'KRX 정규장 기준'으로 조회한다(토스 캔들은 NXT 장전·장후
#  체결이 섞여 ATR이 6~15% 부풀고 ADX가 최대 9.45 어긋난다). pykrx 1순위 / FDR 폴백으로 이중화한다.
REQUIRED_LIBS="rich yfinance pandas matplotlib openpyxl requests beautifulsoup4 google-generativeai python-dotenv tradingview-screener tvdatafeed gnureadline holidays pykrx finance-datareader pytest pytest-xdist"
MISSING_LIBS=""

# tvdatafeed는 PyPI 미배포(git 전용)라 일반 pip install로 설치되지 않는다. git URL로 설치한다.
# (토스 모드 코스피200·코스닥150 시세를 TradingView로 조회하는 데 사용)
TVDATAFEED_GIT_URL="git+https://github.com/rongardF/tvdatafeed.git"

# 2. 운영체제 확인 (macOS vs Linux)
OS_NAME=$(uname -s)

# [추가] macOS/Linux 환경에서 'Too many open files' (Errno 24) 네트워크 에러 방지를 위해 파일 개수 한도 증가
ulimit -n 4096 2>/dev/null

# [추가] (Linux/라즈베리파이) 메모리 절약: glibc malloc 아레나 수 제한
#  - 다중 스레드 Python은 (CPU 코어 수 x 8)개까지 malloc 아레나를 생성하여, 실제 데이터가 적어도
#    RSS가 수백 MB까지 부풀어 오른다. RAM 1GB인 라즈베리파이3에서는 OOM(Killed)의 주요 원인이다.
#  - 아레나를 2개로 제한하고, 해제된 메모리를 OS로 적극 반환하도록 trim 임계값을 낮춘다.
#  - (macOS는 glibc가 아니므로 적용하지 않는다.)
if [ "$OS_NAME" = "Linux" ]; then
    export MALLOC_ARENA_MAX=2
    export MALLOC_TRIM_THRESHOLD_=131072
fi

# 3. 실행할 파이썬 및 핍(PIP) 경로 찾기
if [ -d "./.venv" ]; then
    PYTHON_PATH="./.venv/bin/python"
    PIP_PATH="./.venv/bin/pip"
elif [ -d "./venv" ]; then
    PYTHON_PATH="./venv/bin/python"
    PIP_PATH="./venv/bin/pip"
elif command -v python3 > /dev/null 2>&1; then
    PYTHON_PATH="python3"
    PIP_PATH="pip3"
else
    PYTHON_PATH="python"
    PIP_PATH="pip"
fi

echo "--- 환경 확인: $($PYTHON_PATH --version) ---"

echo "  - 패키지 관리자 환경 점검 중..."
# 4. 최신 리눅스 환경의 PEP 668 외부 관리 환경 에러 우회
PIP_FLAGS=""
if [[ "$PYTHON_PATH" != *"venv"* ]]; then
    # pip 설치 옵션에 break-system-packages가 지원되는지 확인 후 동적 추가
    $PIP_PATH help install 2>/dev/null | grep -q "break-system-packages"
    if [ $? -eq 0 ]; then
        PIP_FLAGS="--break-system-packages"
    fi
fi

echo "  - 필수 라이브러리 설치 상태 스캔 중..."
# 6. 미설치 라이브러리 스캔
for lib in $REQUIRED_LIBS; do
    IMPORT_NAME=$lib
    case $lib in
        "beautifulsoup4")
            IMPORT_NAME="bs4"
            ;;
        "google-generativeai")
            IMPORT_NAME="google.generativeai"
            ;;
        "python-dotenv")
            IMPORT_NAME="dotenv"
            ;;
            "tradingview-screener")
                IMPORT_NAME="tradingview_screener"
                ;;
            "tvdatafeed")
                IMPORT_NAME="tvDatafeed"
                ;;
            "gnureadline")
                IMPORT_NAME="gnureadline"
                ;;
        "pytest-xdist")
            IMPORT_NAME="xdist"
            ;;
        "finance-datareader")
            IMPORT_NAME="FinanceDataReader"
            ;;
    esac

    $PYTHON_PATH -c "import $IMPORT_NAME" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        MISSING_LIBS="$MISSING_LIBS $lib"
    fi
done

# 7. 사용자 확인 및 설치 진행
if [ -n "$MISSING_LIBS" ]; then
    echo "[알림] 다음 라이브러리가 설치되어 있지 않습니다: [$MISSING_LIBS ]"
    read -p "설치하시겠습니까? (y/n): " confirm
    
    if [[ "$confirm" == [yY] || "$confirm" == "yes" ]]; then
        echo "[진행] 설치를 시작합니다..."
        for lib in $MISSING_LIBS; do
            if [ "$lib" = "tvdatafeed" ]; then
                # PyPI 미배포 → git URL로 설치
                $PIP_PATH install "$TVDATAFEED_GIT_URL" $PIP_FLAGS
            else
                $PIP_PATH install $lib $PIP_FLAGS
            fi
        done
        echo "[완료] 모든 라이브러리 설치가 끝났습니다."
    else
        echo "[중단] 사용자가 설치를 거절했습니다. 프로그램을 종료합니다."
        exit 1
    fi
fi

# 8. holidays 패키지 자동 업데이트 (임시공휴일 최신화)
echo "  - holidays 패키지 최신 버전 동기화 중..."
$PIP_PATH install --upgrade holidays $PIP_FLAGS > /dev/null 2>&1

# 9. yfinance 캐시 자동 정리 (DB Lock 에러 사전 방지)
echo "  - yfinance 캐시 데이터 정리 중..."
rm -rf ~/.cache/py-yfinance/* > /dev/null 2>&1
rm -rf ~/Library/Caches/py-yfinance/* > /dev/null 2>&1

# 10. 프로그램 실행
echo ""
echo "--- 프로그램 실행 ---"
$PYTHON_PATH main.py "$@"
