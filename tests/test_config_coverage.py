import os
import pytest
from logging.handlers import TimedRotatingFileHandler
from config import _log_namer, CustomTimedRotatingFileHandler, BASE_DIR

def test_log_namer_success():
    name = "logs/mystock.log.20260218"
    result = _log_namer(name)
    assert result == "logs/mystock_20260218.log"

def test_log_namer_exception():
    name = "invalid_name_format"
    result = _log_namer(name)
    assert result == "invalid_name_format"

def test_custom_handler_getFilesToDelete(tmp_path):
    # Create mock log files
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    base_file = log_dir / "mystock.log"
    
    # Create handler
    handler = CustomTimedRotatingFileHandler(str(base_file), when="MIDNIGHT", interval=1, backupCount=2)
    
    # Create mock old log files
    old_files = [
        "mystock_20260216.log",
        "mystock_20260217.log",
        "mystock_20260218.log",
        "mystock_20260219.log",
        "mystock_other.log"  # should be ignored
    ]
    for file in old_files:
        (log_dir / file).touch()
        
    files_to_delete = handler.getFilesToDelete()
    # Total valid log files: 4. Backup count: 2. So we expect to delete the 2 oldest.
    # Sorted: 20260216, 20260217, 20260218, 20260219
    # To delete: 20260216, 20260217
    assert len(files_to_delete) == 2
    assert any("20260216" in f for f in files_to_delete)
    assert any("20260217" in f for f in files_to_delete)

    handler.close()

def test_custom_handler_getFilesToDelete_fewer_than_backup(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    base_file = log_dir / "mystock.log"
    
    handler = CustomTimedRotatingFileHandler(str(base_file), when="MIDNIGHT", interval=1, backupCount=5)
    
    old_files = [
        "mystock_20260218.log",
        "mystock_20260219.log"
    ]
    for file in old_files:
        (log_dir / file).touch()
        
    files_to_delete = handler.getFilesToDelete()
    assert len(files_to_delete) == 0

    handler.close()

from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import config
from config import setup_logging

def test_setup_logging(tmp_path):
    # Mock LOG_DIR to a temporary directory
    original_log_dir = config.LOG_DIR
    config.LOG_DIR = str(tmp_path)
    
    # Create some dummy files
    old_date = (datetime.now() - timedelta(days=config.LOG_RETENTION_DAYS + 10)).strftime("%Y-%m-%d")
    old_date_no_dash = old_date.replace("-", "")
    
    (tmp_path / f"system_trade_{old_date}.log").touch()
    (tmp_path / f"mystock_{old_date_no_dash}.log").touch()
    (tmp_path / "some_other_file.txt").touch()

    try:
        setup_logging()
        
        # Verify old files are deleted
        assert not (tmp_path / f"system_trade_{old_date}.log").exists()
        assert not (tmp_path / f"mystock_{old_date_no_dash}.log").exists()
        # Non-log file should remain
        assert (tmp_path / "some_other_file.txt").exists()
    finally:
        config.LOG_DIR = original_log_dir



# ── 시스템 시작 로그 ──────────────────────────────────────────────────

def _read_log(tmp_path):
    import logging
    for h in logging.getLogger().handlers:
        h.flush()
    logs = [f for f in os.listdir(tmp_path) if f.endswith('.log')]
    assert logs, "로그 파일이 생성되지 않았다"
    return (tmp_path / logs[0]).read_text(encoding='utf-8')


@pytest.mark.parametrize('file_level', ['WARNING', 'ERROR', 'INFO', 'DEBUG'])
def test_system_start_logged_regardless_of_file_level(tmp_path, file_level):
    """시작 표시는 실행 구분선이므로 FILE_DEBUG_LEVEL 이 무엇이든 남아야 한다.

    기본값(WARNING)에서 사라지면 하루치 로그에 섞인 여러 실행을 구분할 수 없다.
    """
    original_log_dir = config.LOG_DIR
    original_level = config.settings.FILE_DEBUG_LEVEL
    config.LOG_DIR = str(tmp_path)
    config.settings.FILE_DEBUG_LEVEL = file_level
    try:
        setup_logging()
        config.log_system_start()

        content = _read_log(tmp_path)
        start_lines = [ln for ln in content.splitlines() if '시스템 시작' in ln]
        assert len(start_lines) == 1, f"시작 로그가 1줄이어야 한다: {start_lines}"
        assert '[INFO]' in start_lines[0]
    finally:
        config.LOG_DIR = original_log_dir
        config.settings.FILE_DEBUG_LEVEL = original_level


def test_system_start_does_not_leak_other_info_logs(tmp_path):
    """시작 로그를 위해 루트 로거 전체를 INFO 로 여는 부작용이 없어야 한다."""
    import logging
    original_log_dir = config.LOG_DIR
    original_level = config.settings.FILE_DEBUG_LEVEL
    config.LOG_DIR = str(tmp_path)
    config.settings.FILE_DEBUG_LEVEL = 'WARNING'
    try:
        setup_logging()
        config.log_system_start()
        logging.info('평범한 INFO 는 WARNING 설정에서 기록되면 안 된다')

        content = _read_log(tmp_path)
        assert '시스템 시작' in content
        assert '평범한 INFO' not in content
    finally:
        config.LOG_DIR = original_log_dir
        config.settings.FILE_DEBUG_LEVEL = original_level
