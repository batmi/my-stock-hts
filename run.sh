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

# 2. 운영체제 확인 (macOS vs Linux) — 아래 의존성 목록의 환경 마커 판정에 쓰인다.
OS_NAME=$(uname -s)

# ---------------------------------------------------------
# 필수 라이브러리 목록 — requirements.txt 가 단일 소스
# ---------------------------------------------------------
# 종전에는 이 목록이 여기(REQUIRED_LIBS 하드코딩)와 requirements.txt 두 곳에 있었고,
#  실제로 갈라져 있었다 — requirements.txt 쪽은 쓰지도 않는 패키지 목록이라 그걸로
#  설치하면 실패했다. '기동이 확인하는 목록'과 '설치되는 목록'이 어긋나지 않도록
#  이제 requirements.txt 하나만 읽는다. 의존성 추가는 그 파일만 고치면 된다.
#  (PyPI 배포명과 import 이름이 다른 패키지만 아래 `_import_name()` 표에 한 줄 추가)
REQ_FILE="$(pwd)/requirements.txt"
MISSING_LIBS=""

# requirements.txt → 패키지 이름 목록.
#  주석·빈 줄·-r 같은 지시자를 걷어내고, 환경 마커(; sys_platform == "darwin")는
#  현재 OS에 해당하는 줄만 남긴다. 버전 지정자와 'pkg @ git+...' URL 은 이름만 남긴다.
#  (tvdatafeed 는 PyPI 미배포라 git URL 로 적혀 있는데, 설치는 pip 가 requirements.txt
#   를 통째로 읽어서 하므로 여기서는 이름만 뽑아 설치 여부 확인에만 쓴다.)
_requirements_list() {
    [ -f "$REQ_FILE" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/^#.*$//' -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//')
        [ -z "$line" ] && continue
        case "$line" in -*) continue ;; esac
        marker=""
        case "$line" in *\;*) marker="${line#*;}"; line="${line%%;*}" ;; esac
        if [ -n "$marker" ]; then
            case "$marker" in
                *darwin*) [ "$OS_NAME" = "Darwin" ] || continue ;;
                *linux*)  [ "$OS_NAME" = "Linux" ]  || continue ;;
            esac
        fi
        line="${line%% @ *}"
        line=$(printf '%s' "$line" | sed -e 's/[<>=!~].*$//' -e 's/\[.*$//' -e 's/[[:space:]]*$//')
        [ -n "$line" ] && printf '%s\n' "$line"
    done < "$REQ_FILE"
}

# 1-2. 기동 기록 — cron @reboot 로 띄우면 표준출력이 어디에도 남지 않는다.
#  기동 단계에서 죽으면 파이썬 로거(logs/mystock.log)가 아직 없으므로 여기서 따로 남긴다.
BOOT_LOG="$(pwd)/logs/startup.log"
mkdir -p "$(dirname "$BOOT_LOG")" 2>/dev/null
_boot_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$BOOT_LOG"
}

# 패키지 이름 → import 이름. 스캔과 설치 후 재확인이 같은 표를 써야 한다
#  (두 벌로 두면 한쪽만 고쳐져 '설치했는데 여전히 없다'를 놓친다).
_import_name() {
    case "$1" in
        "beautifulsoup4")        echo "bs4" ;;
        "google-generativeai")   echo "google.generativeai" ;;
        "python-dotenv")         echo "dotenv" ;;
        "tradingview-screener")  echo "tradingview_screener" ;;
        "tvdatafeed")            echo "tvDatafeed" ;;
        "gnureadline")           echo "gnureadline" ;;
        "finance-datareader")    echo "FinanceDataReader" ;;
        *)                       echo "$1" ;;
    esac
}


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

# 2-1. (Linux) 시각 동기화 대기 — 부팅 직후 기동(cron @reboot) 대비.
#  라즈베리파이는 RTC가 없어 부팅 시각이 '마지막 종료 시각'으로 복원된다. NTP 동기화가
#  끝나기 전에 뜨면 datetime.now()가 틀린 채로 휴장일(is_holiday_today)과 매매 시간
#  (is_system_market_open)을 판단한다 — 장중인데 쉬거나, 장이 아닌데 주문을 낸다.
#  이미 동기화됐으면 즉시 통과하므로 수동 실행에는 영향이 없다.
if [ "$OS_NAME" = "Linux" ] && command -v timedatectl > /dev/null 2>&1; then
    _synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
    if [ "$_synced" != "yes" ]; then
        _boot_log "시각 미동기화 — NTP 동기화를 최대 120초 대기합니다 (현재 시각: $(date '+%F %T'))."
        for _i in $(seq 1 24); do
            sleep 5
            [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ] && break
        done
        if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
            _boot_log "시각 동기화 완료: $(date '+%F %T')"
        else
            # 막지는 않는다 — 시각이 틀려도 청산 감시는 도는 편이 낫다. 다만 반드시 남긴다.
            _boot_log "경고: 시각 동기화 실패(120초 초과). 틀린 시각으로 매매 시간을 판단할 수 있습니다."
        fi
    fi
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
# 6. 미설치 라이브러리 스캔 (목록은 requirements.txt 에서 뽑는다)
REQUIRED_LIBS="$(_requirements_list | tr '\n' ' ')"
if [ -z "$REQUIRED_LIBS" ]; then
    # requirements.txt 가 없거나 비면 스캔이 통째로 무력화된다 — 조용히 넘어가면
    #  '의존성 확인을 했다'는 착각만 남으므로 반드시 기록한다.
    _boot_log "경고: requirements.txt 를 읽지 못했습니다($REQ_FILE) — 의존성 확인을 건너뜁니다."
fi
for lib in $REQUIRED_LIBS; do
    IMPORT_NAME=$(_import_name "$lib")
    $PYTHON_PATH -c "import $IMPORT_NAME" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        MISSING_LIBS="$MISSING_LIBS $lib"
    fi
done

# 7. 사용자 확인 및 설치 진행
#
# [비대화형 기동] cron @reboot 로 띄우면 stdin 이 없어 read 가 즉시 EOF 를 받는다.
#  종전에는 그 결과 confirm 이 빈 값이 되어 '거절'로 해석되고 exit 1 로 끝났다 —
#  재부팅 후 포지션을 들고 있는데 **손절 감시가 아예 시작되지 않고, cron 출력은
#  어디에도 남지 않아 아무도 모른다**. 이 시스템에서 가장 비싼 실패다.
#  → 사람이 없으면 되묻지 않고 설치를 진행한다. '묻지 않고 설치'의 위험보다
#    '매매 시스템이 안 뜨는' 위험이 크다. 결과는 항상 기록에 남긴다.
if [ -n "$MISSING_LIBS" ]; then
    echo "[알림] 다음 라이브러리가 설치되어 있지 않습니다: [$MISSING_LIBS ]"

    if [ -t 0 ]; then
        read -p "설치하시겠습니까? (y/n): " confirm
    else
        confirm="y"
        _boot_log "비대화형 기동(cron 등) — 되묻지 않고 설치를 진행합니다:$MISSING_LIBS"
    fi

    if [[ "$confirm" == [yY] || "$confirm" == "yes" ]]; then
        echo "[진행] 설치를 시작합니다..."
        # requirements.txt 를 통째로 넘긴다 — 이미 만족한 패키지는 pip 가 건너뛰고,
        #  버전 하한·환경 마커·git URL(tvdatafeed) 해석을 전부 pip 가 맡는다.
        #  (종전에는 여기서 이름만 넘겨 버전 하한이 무시됐고, git 전용 패키지는
        #   스크립트가 따로 특례를 들고 있어야 했다.)
        $PIP_PATH install -r "$REQ_FILE" $PIP_FLAGS
        echo "[완료] 모든 라이브러리 설치가 끝났습니다."

        # 설치했다고 끝난 게 아니다 — 네트워크가 아직 안 올라왔거나 빌드가 실패하면
        #  import 는 여전히 깨져 있고, 그대로 main.py 를 띄우면 기동 도중 죽는다.
        STILL_MISSING=""
        for lib in $MISSING_LIBS; do
            IMPORT_NAME=$(_import_name "$lib")
            $PYTHON_PATH -c "import $IMPORT_NAME" > /dev/null 2>&1 || STILL_MISSING="$STILL_MISSING $lib"
        done
        if [ -n "$STILL_MISSING" ]; then
            _boot_log "설치 후에도 import 실패:$STILL_MISSING — 기동을 중단합니다(네트워크 미준비 가능성)."
            exit 1
        fi
    else
        echo "[중단] 사용자가 설치를 거절했습니다. 프로그램을 종료합니다."
        exit 1
    fi
fi

# 8. (제거됨) holidays 자동 업그레이드
#  종전에는 기동마다 `pip install --upgrade holidays` 를 돌렸다. 그러면 기동 경로가
#  네트워크와 업스트림 릴리스에 묶이고, 무엇보다 **휴장일 판정 라이브러리가 매 기동
#  조용히 바뀐다** — 매매 시간 판단이 사람 모르게 달라질 수 있다는 뜻이다.
#  갱신 자체는 임시공휴일 반영에 필요하므로 버리지 않고 주기 작업으로 분리했다:
#    tools/update_holidays.sh  (주 1회 cron 권장 — 사용법은 스크립트 상단 주석 참조)

# 9. yfinance 캐시 자동 정리 (DB Lock 에러 사전 방지)
echo "  - yfinance 캐시 데이터 정리 중..."
rm -rf ~/.cache/py-yfinance/* > /dev/null 2>&1
rm -rf ~/Library/Caches/py-yfinance/* > /dev/null 2>&1

# 10. 프로그램 실행
echo ""
echo "--- 프로그램 실행 ---"
$PYTHON_PATH main.py "$@"
