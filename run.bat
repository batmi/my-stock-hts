@echo off
setlocal enabledelayedexpansion

:: 1. 스크립트가 위치한 디렉토리로 이동 (경로 의존성 해결)
cd /d "%~dp0"

:: ---------------------------------------------------------
:: 필수 라이브러리 목록
:: ---------------------------------------------------------
set REQUIRED_LIBS=rich yfinance pandas matplotlib openpyxl requests beautifulsoup4 google-generativeai python-dotenv tradingview-screener holidays pytest pytest-xdist
set MISSING_LIBS=

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

:: 3. 미설치 라이브러리 스캔
for %%L in (%REQUIRED_LIBS%) do (
    set "IMPORT_NAME=%%L"
    if "%%L"=="beautifulsoup4" set "IMPORT_NAME=bs4"
    if "%%L"=="google-generativeai" set "IMPORT_NAME=google.generativeai"
    if "%%L"=="python-dotenv" set "IMPORT_NAME=dotenv"
    if "%%L"=="tradingview-screener" set "IMPORT_NAME=tradingview_screener"
    if "%%L"=="pytest-xdist" set "IMPORT_NAME=xdist"
    
    %PYTHON_PATH% -c "import !IMPORT_NAME!" >nul 2>&1
    if errorlevel 1 (
        set MISSING_LIBS=!MISSING_LIBS! %%L
    )
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
        for %%L in (!MISSING_LIBS!) do (
            %PIP_PATH% install %%L
        )
        echo [완료] 모든 라이브러리 설치가 끝났습니다.
    ) else (
        echo [중단] 사용자가 설치를 거절했습니다. 프로그램을 종료합니다.
        exit /b 1
    )
)

:: 5. holidays 패키지 자동 업데이트 (임시공휴일 최신화)
echo   - holidays 패키지 최신 버전 동기화 중...
%PIP_PATH% install --upgrade holidays >nul 2>&1

:: 6. yfinance 캐시 자동 정리 (DB Lock 에러 사전 방지)
echo   - yfinance 캐시 데이터 정리 중...
if exist "%LOCALAPPDATA%\py-yfinance" (
    del /q /s "%LOCALAPPDATA%\py-yfinance\*" >nul 2>&1
)

:: 7. 프로그램 실행 (모든 인자 %* 전달)
echo.
echo --- 프로그램 실행 ---
%PYTHON_PATH% main.py %*
