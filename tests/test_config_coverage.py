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

