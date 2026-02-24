#!/bin/bash

# 1. 스크립트가 위치한 디렉토리로 이동 (경로 의존성 해결)
cd "$(dirname "$0")"

# ---------------------------------------------------------
# 필수 라이브러리 목록
# ---------------------------------------------------------
REQUIRED_LIBS="rich yfinance pandas matplotlib openpyxl requests"
MISSING_LIBS=""

# 2. 실행할 파이썬 및 핍(PIP) 경로 찾기
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

# 3. macOS LibreSSL 충돌 해결 (urllib3 v2.x -> v1.x 강제 적용)
# 라이브러리가 설치되어 있고, 메이저 버전이 2 이상인지 확인합니다.
$PYTHON_PATH -c "import urllib3; import sys; sys.exit(0) if int(urllib3.__version__.split('.')[0]) >= 2 else sys.exit(1)" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "[알림] macOS 환경과의 호환성을 위해 urllib3 라이브러리를 조정(다운그레이드)합니다..."
    $PIP_PATH install "urllib3<2"
fi

# 4. 미설치 라이브러리 스캔
for lib in $REQUIRED_LIBS; do
    $PYTHON_PATH -c "import $lib" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        MISSING_LIBS="$MISSING_LIBS $lib"
    fi
done

# 5. 사용자 확인 및 설치 진행
if [ -n "$MISSING_LIBS" ]; then
    echo "[알림] 다음 라이브러리가 설치되어 있지 않습니다: [$MISSING_LIBS ]"
    read -p "설치하시겠습니까? (y/n): " confirm
    
    if [[ "$confirm" == [yY] || "$confirm" == "yes" ]]; then
        echo "[진행] 설치를 시작합니다..."
        for lib in $MISSING_LIBS; do
            $PIP_PATH install $lib
        done
        echo "[완료] 모든 라이브러리 설치가 끝났습니다."
    else
        echo "[중단] 사용자가 설치를 거절했습니다. 프로그램을 종료합니다."
        exit 1
    fi
fi

# 6. 프로그램 실행
echo "--- 프로그램 실행 ---"
$PYTHON_PATH main.py "$@"
