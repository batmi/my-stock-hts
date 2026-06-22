#!/bin/bash

# 1. 스크립트가 위치한 디렉토리로 이동 (경로 의존성 해결)
cd "$(dirname "$0")"

# ---------------------------------------------------------
# 필수 라이브러리 목록
# ---------------------------------------------------------
REQUIRED_LIBS="rich yfinance pandas matplotlib openpyxl requests beautifulsoup4 google-generativeai python-dotenv tradingview-screener gnureadline holidays pytest pytest-xdist"
MISSING_LIBS=""

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
            "gnureadline")
                IMPORT_NAME="gnureadline"
                ;;
        "pytest-xdist")
            IMPORT_NAME="xdist"
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
            $PIP_PATH install $lib $PIP_FLAGS
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
