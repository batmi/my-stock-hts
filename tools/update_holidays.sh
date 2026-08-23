#!/bin/bash
# ============================================================================
# holidays 패키지 갱신 — 기동 경로에서 분리된 주기 작업
# ============================================================================
# [왜 따로인가]
#  종전에는 run.sh 가 기동할 때마다 `pip install --upgrade holidays` 를 돌렸다.
#  그 결과 (1) 부팅 경로가 네트워크와 업스트림 릴리스에 묶이고, (2) 휴장일 판정
#  라이브러리가 매 기동 사람 모르게 바뀌었다. 휴장일이 바뀌면 매매 시간 판단
#  (is_holiday_today · is_system_market_open)이 바뀐다 — 기동할 때마다 조용히
#  달라져도 되는 종류의 것이 아니다.
#  그렇다고 고정만 하면 임시공휴일이 반영되지 않으므로, '가끔·명시적으로' 갱신한다.
#
# [사용법]
#   수동:   ./tools/update_holidays.sh
#   주기:   crontab -e 에 아래 한 줄 (매주 일요일 04:10)
#           10 4 * * 0 /home/pi/my-stock-hts/tools/update_holidays.sh >> /home/pi/my-stock-hts/logs/holidays_update.log 2>&1
#
#   갱신 결과(버전 변화 여부)는 표준출력에 남는다. 버전이 바뀌면 그 사실 자체가
#   '휴장일 판정이 달라졌을 수 있다'는 신호이므로, 로그로 남겨 두는 편이 좋다.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || exit 1

if [ -d "./.venv" ]; then
    PYTHON_PATH="./.venv/bin/python"; PIP_PATH="./.venv/bin/pip"
elif [ -d "./venv" ]; then
    PYTHON_PATH="./venv/bin/python"; PIP_PATH="./venv/bin/pip"
else
    PYTHON_PATH="python3"; PIP_PATH="pip3"
fi

PIP_FLAGS=""
if [[ "$PYTHON_PATH" != *"venv"* ]]; then
    $PIP_PATH help install 2>/dev/null | grep -q "break-system-packages" && PIP_FLAGS="--break-system-packages"
fi

_ver() { $PYTHON_PATH -c "import holidays; print(holidays.__version__)" 2>/dev/null || echo "(미설치)"; }

BEFORE="$(_ver)"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] holidays 갱신 시작 (현재 $BEFORE)"
$PIP_PATH install --upgrade holidays $PIP_FLAGS
AFTER="$(_ver)"

if [ "$BEFORE" = "$AFTER" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 변화 없음 ($AFTER)"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 갱신됨: $BEFORE → $AFTER"
    echo "  ※ 휴장일 판정이 달라졌을 수 있습니다. 다음 기동부터 새 달력이 적용됩니다."
fi
