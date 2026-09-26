@echo off
setlocal enabledelayedexpansion

:: 1. 스크립트가 위치한 디렉토리로 이동 (경로 의존성 해결)
cd /d "%~dp0"

:: 2. 실행할 파이썬 및 핍(PIP) 경로 찾기 (윈도우는 Scripts 폴더 사용)
if exist ".venv\Scripts\python.exe" (
    set PYTHON_PATH=".venv\Scripts\python.exe"
    set PIP_PATH=".venv\Scripts\pip.exe"
) else if exist "venv\Scripts\python.exe" (
    set PYTHON_PATH="venv\Scripts\python.exe"
    set PIP_PATH="venv\Scripts\pip.exe"
) else (
    set PYTHON_PATH=python
    set PIP_PATH=pip
)

echo --- 환경 확인 ---
%PYTHON_PATH% --version

:: 2-1. 파이썬 최소 버전(3.10) 확인 — run.sh 3-1 과 같은 가드(2026-09-26).
::  3.9 이하로 만든 가상환경이면 holidays(>=0.103) 설치가 실패해 목록 전체가 설치되지 않고,
::  설치를 우회해도 기동 도중 문법 오류로 죽는다. 원인이 버전이라는 것을 여기서 바로 알린다.
%PYTHON_PATH% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [중단] Python 3.10 이상이 필요합니다. 경로: %PYTHON_PATH%
    echo   가상환경이 구버전으로 만들어졌다면 지우고 새 파이썬으로 다시 만드세요:
    echo     rmdir /s /q .venv ^&^& py -3.12 -m venv .venv ^&^& run.bat
    exit /b 1
)

:: 3. 미설치 라이브러리 스캔 — requirements.txt 가 단일 소스(run.sh 와 같은 규칙).
::  [2026-09-20] 종전에는 목록이 여기 하드코딩돼 있어 run.sh·requirements.txt 와 갈라졌다:
::   폐기된 google-generativeai 를 요구하고(정본은 google-genai), tvdatafeed·FinanceDataReader
::   ·pykrx 는 빠져 있었다. 판정은 import 실행이 아니라 find_spec 으로 한다(run.sh 주석 참조).
::   환경 마커(; sys_platform == "darwin"/"linux")가 붙은 줄은 윈도우에 해당 없으므로 건너뛴다.
set MISSING_LIBS=
for /f "usebackq delims=" %%L in (`%PYTHON_PATH% tools\missing_requirements.py`) do (
    set MISSING_LIBS=!MISSING_LIBS! %%L
)

:: 4. 사용자 확인 및 설치 진행
if not "!MISSING_LIBS!"=="" (
    echo [알림] 다음 라이브러리가 설치되어 있지 않습니다: [!MISSING_LIBS! ]
    set /p confirm="설치하시겠습니까? (y/n): "

    set do_install=false
    if /i "!confirm!"=="y" set do_install=true
    if /i "!confirm!"=="yes" set do_install=true

    if "!do_install!"=="true" (
        echo [진행] 설치를 시작합니다...
        :: requirements.txt 를 통째로 넘긴다 — 버전 하한·git URL(tvdatafeed) 해석은 pip 가 맡는다.
        %PIP_PATH% install -r requirements.txt
        :: pip 는 해석 단계에서 하나만 실패해도 전부를 설치하지 않는다 — 종료 코드로 판정한다(run.sh 와 같다).
        if errorlevel 1 (
            echo [실패] pip 설치가 오류로 끝났습니다(위 ERROR 참고^).
        ) else (
            echo [완료] 설치가 끝났습니다.
        )
        set STILL_MISSING=
        for /f "usebackq delims=" %%L in (`%PYTHON_PATH% tools\missing_requirements.py`) do (
            set STILL_MISSING=!STILL_MISSING! %%L
        )
        if not "!STILL_MISSING!"=="" (
            echo [중단] 설치 후에도 패키지를 찾지 못했습니다: [!STILL_MISSING! ]
            exit /b 1
        )
    ) else (
        echo [중단] 사용자가 설치를 거절했습니다. 프로그램을 종료합니다.
        exit /b 1
    )
)

:: 5. holidays 갱신은 기동 스크립트가 하지 않는다 — 앱이 7일마다 백그라운드에서 갱신하고
::  갱신 전후 휴장일 차이를 알린다(modules/holiday_calendar_update, 2026-09-20). 수동 실행은:
::    %PIP_PATH% install --upgrade holidays

:: 6. yfinance 캐시 자동 정리 (DB Lock 에러 사전 방지)
echo   - yfinance 캐시 데이터 정리 중...
if exist "%LOCALAPPDATA%\py-yfinance" (
    del /q /s "%LOCALAPPDATA%\py-yfinance\*" >nul 2>&1
)

:: 7. 프로그램 실행 (모든 인자 %* 전달)
echo.
echo --- 프로그램 실행 ---
%PYTHON_PATH% main.py %*
