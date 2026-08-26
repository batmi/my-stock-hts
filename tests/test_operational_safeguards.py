"""실전 투입 전 운영 안전장치 3종 — 이중 실행 / DB 손상 / 알림 두절.

셋 다 '조용히 잘못되는' 부류다. 매매 로직은 정상으로 보이는데 결과만 틀어진다.

  ② 이중 실행: pending_orders 는 프로세스 메모리에 있다. 엔진이 둘이면 서로의 주문을
     모르고 같은 종목에 각자 매수를 낸다. 재기동 복구로도 못 막는다 — 둘 다 거래소
     미체결을 보고 '내 주문'으로 읽기 때문이다.
  ③ DB 손상: 평단·트레일링 최고가·손절 기준이 이 파일에만 있다. 잔고는 증권사에
     있으니 '무엇을 들고 있는지'는 복구되지만 '어디서 자를지'는 복구되지 않는다.
     라즈베리파이 SD카드 + 전원 차단이 실제 위험이다.
  ④ 알림 두절: 전송이 비동기라 호출부가 성공 여부를 모른다. 텔레그램이 죽었다는 걸
     텔레그램으로 알릴 수는 없으므로, '조용함'이 '이상 없음'과 구분되어야 한다.
"""
import os
import sqlite3

import pytest
from unittest.mock import MagicMock, patch

import config
from modules import db_manager, instance_lock, telegram_notify
from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.instance_lock = None
    yield t
    t._release_instance_lock()


# ────────────────────── ② 같은 계좌 이중 실행 ──────────────────────

def test_second_instance_cannot_take_the_same_account_lock(tmp_path):
    """[핵심] 같은 계좌를 두 번 잠글 수 없다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        a = instance_lock.InstanceLock("12345678-01")
        b = instance_lock.InstanceLock("12345678-01")
        try:
            assert a.acquire() is True
            assert b.acquire() is False, "두 번째 인스턴스가 같은 계좌를 잠갔다"
            assert str(os.getpid()) in b.holder, "선점자 정보가 비어 있다"
        finally:
            a.release(); b.release()


def test_a_different_account_is_not_blocked(tmp_path):
    """다른 계좌는 막지 않는다 — 잠금이 너무 넓으면 정상 운용을 방해한다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        a = instance_lock.InstanceLock("12345678-01")
        b = instance_lock.InstanceLock("99999999-01")
        try:
            assert a.acquire() and b.acquire(), "다른 계좌인데 서로를 막았다"
        finally:
            a.release(); b.release()


def test_release_lets_the_next_instance_in(tmp_path):
    """정상 종료 후에는 다음 실행이 들어올 수 있어야 한다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        a = instance_lock.InstanceLock("12345678-01")
        assert a.acquire()
        a.release()
        b = instance_lock.InstanceLock("12345678-01")
        try:
            assert b.acquire() is True, "해제 후에도 잠겨 있다 — 재시작이 영영 막힌다"
        finally:
            b.release()


def test_a_stale_lock_file_does_not_block_restart(tmp_path):
    """[핵심] 비정상 종료(OOM·kill -9)가 남긴 잠금 파일이 재시작을 막으면 안 된다.

    PID 파일 방식의 고질병이다. flock 은 fd 가 닫히면 커널이 자동으로 푼다.
    """
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        dead = instance_lock.InstanceLock("12345678-01")
        assert dead.acquire()
        # 프로세스가 죽어 fd 가 닫힌 상황 — 파일은 내용까지 그대로 남는다.
        os.close(dead._fd)
        dead._fd = None
        assert os.path.exists(dead.path), "전제: 잠금 파일은 남아 있다"

        fresh = instance_lock.InstanceLock("12345678-01")
        try:
            assert fresh.acquire() is True, "죽은 프로세스의 잠금 파일이 재시작을 막는다"
        finally:
            fresh.release()


def test_start_refuses_when_the_account_is_already_locked(trader, tmp_path):
    """[배선] 잠금이 잡히지 않으면 start()는 초기화조차 하지 않는다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")), \
         patch.object(trader, 'initialize') as init, \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        other = instance_lock.InstanceLock(trader._trade_account_key())
        assert other.acquire(), "전제: 선점 잠금을 잡는다"
        try:
            trader.start(interactive=False)
        finally:
            other.release()

    assert not init.called, "이미 잠긴 계좌인데 초기화를 진행했다"
    assert not trader.is_running, "이중 실행을 허용했다"
    assert any("중복 실행" in str(c) for c in tg.call_args_list), "운영자에게 알리지 않았다"


# ────────────────────── ③ DB 무결성 / 백업 ──────────────────────

def _reportable_corruption(path):
    """파일은 **열리는데** integrity_check 가 이상을 보고하는 손상을 만든다.

    이 구분이 중요하다. 파일을 아예 못 여는 손상은 예외 경로로 빠져서, PRAGMA 결과를
    실제로 검사하는지는 검증하지 못한다(결과를 통째로 무시해도 테스트가 통과한다).
    인덱스 페이지의 셀 페이로드만 건드려 페이지 구조는 성하게 두면
    'row N missing from index ix' 를 보고한다.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=1024")
    conn.execute("VACUUM")
    conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    conn.execute("CREATE INDEX ix ON t(b)")
    conn.executemany("INSERT INTO t VALUES (?, ?)",
                     [(i, f"val{i:06d}") for i in range(400)])
    conn.commit()
    conn.close()
    with open(path, "r+b") as f:
        f.seek(1024 * 4 + 900)
        f.write(b"ZZZZ")


def test_integrity_check_reads_the_pragma_result(tmp_path):
    """[핵심] 파일이 열리더라도 PRAGMA가 이상을 보고하면 '정상'이 아니다."""
    good, bad = tmp_path / "good.db", tmp_path / "bad.db"
    sqlite3.connect(str(good)).execute("CREATE TABLE t (a)")
    _reportable_corruption(str(bad))

    # 전제 확인: 이 손상은 파일을 여는 데는 성공한다(= 예외 경로가 아니다).
    rows = sqlite3.connect(str(bad)).execute("PRAGMA integrity_check;").fetchall()
    assert rows and rows[0][0] != 'ok', "전제가 깨졌다 — 이 손상은 PRAGMA로 보고되지 않는다"

    saved = db_manager.db.db_path
    try:
        db_manager.db.db_path = str(good)
        assert db_manager.db.check_integrity()[0] is True, "정상 DB를 이상으로 판정했다"

        db_manager.db.db_path = str(bad)
        ok, detail = db_manager.db.check_integrity()
        assert ok is False, "PRAGMA 결과를 읽지 않고 정상으로 판정했다"
        assert "index" in detail, f"무엇이 문제인지 안 남긴다: {detail}"
    finally:
        db_manager.db.db_path = saved


def test_unopenable_db_is_not_reported_as_healthy(tmp_path):
    """열지도 못하는 파일은 '정상'이 아니다 — 예외를 삼키면 손상이 통과한다."""
    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"this is not a sqlite file at all" * 100)
    saved = db_manager.db.db_path
    try:
        db_manager.db.db_path = str(bad)
        assert db_manager.db.check_integrity()[0] is False
    finally:
        db_manager.db.db_path = saved


def test_backup_produces_a_readable_copy_and_rotates(tmp_path):
    """백업본은 그 자체로 열려야 하고, 개수는 상한을 지켜야 한다(SD카드 용량)."""
    src = tmp_path / "trade_history.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE trades (a)")
    conn.execute("INSERT INTO trades VALUES (1)")
    conn.commit(); conn.close()

    saved = db_manager.db.db_path
    try:
        db_manager.db.db_path = str(src)
        dest = db_manager.db.backup(keep=3)
        assert dest and os.path.exists(dest), "백업 파일이 생기지 않았다"
        rows = sqlite3.connect(dest).execute("SELECT a FROM trades").fetchall()
        assert rows == [(1,)], "백업본이 원본 내용을 담고 있지 않다"

        bdir = os.path.dirname(dest)
        for day in range(1, 6):                     # 과거 백업이 쌓인 상태를 만든다
            open(os.path.join(bdir, f"trade_history_2026070{day}.db"), "w").close()
        assert len(os.listdir(bdir)) == 6, "전제: 회전 전 6개"
        db_manager.db.backup(keep=3)                # 오늘 것은 이미 있으므로 회전만 확인
        os.remove(dest)
        db_manager.db.backup(keep=3)
        assert len(os.listdir(bdir)) <= 3, f"회전이 안 된다: {os.listdir(bdir)}"
    finally:
        db_manager.db.db_path = saved


def test_start_refuses_to_trade_on_a_corrupted_db(trader, tmp_path):
    """[배선] 무결성 실패는 매매를 멈춰야 한다 — 손절 기준을 잃은 채 돌면 안 된다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")), \
         patch.object(db_manager.db, 'check_integrity', return_value=(False, "page 3 corrupt")), \
         patch.object(trader, 'initialize') as init, \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        trader.start(interactive=False)

    assert not init.called and not trader.is_running, "DB가 깨졌는데 매매를 시작했다"
    assert any("DB" in str(c) for c in tg.call_args_list), "운영자에게 알리지 않았다"
    assert trader.instance_lock is None, "시작을 포기했는데 잠금을 붙들고 있다 — 재시작이 막힌다"


def test_backup_failure_does_not_block_trading(trader, tmp_path):
    """백업 실패로 매매를 막지는 않는다 — 백업이 없다고 지금 매매가 틀리지는 않는다."""
    with patch.object(db_manager.db, 'check_integrity', return_value=(True, "ok")), \
         patch.object(db_manager.db, 'backup', return_value=None):
        assert trader._check_db_health() is True, "백업 실패가 매매를 막았다"


# ────────────────────── ④ 알림 두절 ──────────────────────

@pytest.fixture(autouse=True)
def _clean_delivery():
    telegram_notify.reset_delivery_health()
    yield
    telegram_notify.reset_delivery_health()


def _send(status_code=200, boom=False):
    """실제 전송 경로를 동기로 1회 태운다."""
    resp = MagicMock(status_code=status_code, text="err" if status_code != 200 else "ok")
    post = MagicMock(side_effect=OSError("Network is unreachable")) if boom \
        else MagicMock(return_value=resp)
    with patch.object(config, 'TELEGRAM_BOT_TOKEN', 'T'), \
         patch.object(config, 'TELEGRAM_CHAT_ID', 'C'), \
         patch('modules.telegram_notify.requests.post', post), \
         patch('modules.telegram_notify.time.sleep'), \
         patch('modules.telegram_notify.context.is_screen_output_allowed', return_value=False):
        telegram_notify.send_telegram_message("🚨 손절 경보 삼성전자(005930)", sync=True)


def test_a_failed_alert_is_recorded_not_silently_dropped():
    """[핵심] 못 간 메시지가 흔적 없이 사라지면 안 된다."""
    _send(status_code=500)
    h = telegram_notify.get_delivery_health()
    assert h['failed'] == 1 and h['consecutive_failed'] == 1, "실패가 집계되지 않았다"
    assert h['lost'] and "손절 경보" in h['lost'][-1][1], \
        "무슨 알림이 유실됐는지 남지 않았다 — 로그로도 되짚을 수 없다"


def test_network_error_is_recorded_too():
    """HTTP 오류뿐 아니라 네트워크 예외도 실패다(라즈베리파이 무선이 자주 끊긴다)."""
    _send(boom=True)
    assert telegram_notify.get_delivery_health()['failed'] == 1


def test_success_clears_the_failure_streak():
    """복구되면 연속 실패는 0으로 돌아가야 한다 — 안 그러면 경보가 상시가 된다."""
    _send(status_code=500)
    _send(status_code=200)
    h = telegram_notify.get_delivery_health()
    assert h['consecutive_failed'] == 0 and h['sent'] == 1
    assert h['failed'] == 1, "누적 실패 이력까지 지워졌다 — 하루치 유실이 안 보인다"


def test_health_panel_shows_the_outage(trader):
    """[배선] 상태창에 드러나야 한다 — 집계만 하고 안 보여주면 아무도 모른다."""
    assert "발신 이력 없음" in trader._health_telegram_text()
    for _ in range(3):
        _send(status_code=500)
    text = trader._health_telegram_text()
    assert "연속 실패 3건" in text, f"상태창이 두절을 안 보여준다: {text}"
    assert "손절 경보" in text, "무엇이 유실됐는지 안 보여준다"


def test_lost_message_log_is_bounded():
    """유실 목록이 무한히 자라면 안 된다(1GB 라즈베리파이)."""
    for _ in range(telegram_notify.LOST_MESSAGE_KEEP + 15):
        _send(status_code=500)
    assert len(telegram_notify.get_delivery_health()['lost']) == telegram_notify.LOST_MESSAGE_KEEP


# ==========================================================
# [추가 2026-08-09] 앱키 단위 중복 프로세스 감지
# ==========================================================
def test_same_appkey_twice_is_detected(tmp_path):
    """[핵심] 같은 앱키를 쓰는 두 번째 프로세스를 감지한다.

    KIS의 TPS(20)·웹소켓(1)·토큰 발급(1분 1회) 제약은 전부 앱키 단위다. 계좌 단위
    잠금(InstanceLock)은 자동매매 엔진만 잡으므로, 조회 전용 인스턴스를 하나 더 띄우면
    아무것도 걸리지 않고 유량만 반으로 갈린다 — EGW00201의 1순위 후보였는데도 확인할
    방법이 없었다.
    """
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        a = instance_lock.AppKeyLock("PSxxxxAPPKEY")
        b = instance_lock.AppKeyLock("PSxxxxAPPKEY")
        try:
            assert a.acquire() is True
            assert b.acquire() is False, "같은 앱키로 두 번 잠갔다"
            assert str(os.getpid()) in b.holder
        finally:
            a.release(); b.release()


def test_appkey_lock_file_does_not_leak_the_key(tmp_path):
    """잠금 파일명에 앱키 평문이 남으면 안 된다(파일명은 그대로 노출된다)."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        lock = instance_lock.AppKeyLock("PSxxxxSECRETKEY")
        try:
            assert "PSxxxxSECRETKEY" not in lock.path
            assert lock.path.endswith(".lock") and "appkey_" in lock.path
        finally:
            lock.release()


def test_different_appkeys_do_not_block_each_other(tmp_path):
    """앱키가 다르면 막지 않는다 — mode 4(VIRT_APP_KEY) 동시 운용이 정상 흐름이다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        a = instance_lock.AppKeyLock("REAL_KEY")
        b = instance_lock.AppKeyLock("VIRT_KEY")
        try:
            assert a.acquire() and b.acquire(), "다른 앱키인데 서로를 막았다"
        finally:
            a.release(); b.release()


def test_appkey_lock_does_not_collide_with_account_lock(tmp_path):
    """계좌 잠금과 앱키 잠금은 다른 파일을 쓴다(둘이 겹치면 한쪽이 무력해진다)."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        acct = instance_lock.InstanceLock("12345678-01")
        key = instance_lock.AppKeyLock("12345678-01")
        try:
            assert acct.path != key.path
            assert acct.acquire() and key.acquire()
        finally:
            acct.release(); key.release()


def test_duplicate_note_is_quotable_by_the_tps_log(tmp_path):
    """감지 결과는 TPS 경고가 그대로 인용할 수 있는 한 줄이어야 한다."""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        with patch.object(instance_lock, 'APPKEY_DUPLICATE', False):
            assert "없음" in instance_lock.appkey_duplicate_note()
        with patch.object(instance_lock, 'APPKEY_DUPLICATE', True), \
             patch.object(instance_lock, 'APPKEY_HOLDER', "pid=999"):
            note = instance_lock.appkey_duplicate_note()
            assert "감지" in note and "pid=999" in note


@pytest.fixture
def clean_appkey_locks(tmp_path):
    """앱키 잠금 전역을 격리한다(파일 경로도 임시로 돌린다)."""
    from modules import instance_lock
    saved = (list(instance_lock._APPKEY_LOCKS), instance_lock.APPKEY_DUPLICATE,
             instance_lock.APPKEY_HOLDER, instance_lock.APPKEY_DUP_LABEL)
    instance_lock._APPKEY_LOCKS = []
    instance_lock.APPKEY_DUPLICATE = False
    instance_lock.APPKEY_HOLDER = ""
    instance_lock.APPKEY_DUP_LABEL = ""
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        yield instance_lock
    for lk in instance_lock._APPKEY_LOCKS:
        try:
            lk.release()
        except Exception:
            pass
    (instance_lock._APPKEY_LOCKS, instance_lock.APPKEY_DUPLICATE,
     instance_lock.APPKEY_HOLDER, instance_lock.APPKEY_DUP_LABEL) = saved


def test_appkey_guard_holds_both_manual_and_auto_keys(clean_appkey_locks):
    """수동 키와 자동매매 키를 동시에 잠근다.

    TPS 예산이 앱키별로 갈리면서(ThrottledSession의 앱키별 버킷) '어느 키가 중복인가'가
    '어느 트래픽이 영향을 받는가'와 같은 뜻이 됐다. 시스템 트레이딩 트래픽은 전부
    자동매매 키로 나가므로 수동 키만 봐서는 정작 매매에 영향을 주는 중복을 놓친다.

    종전 구현은 잠금 객체를 단일 전역에 담아 두 번째 호출이 첫 잠금을 덮어썼고,
    GC와 함께 첫 키가 조용히 풀렸다.
    """
    il = clean_appkey_locks
    assert il.guard_appkey("GUARD_MAIN_KEY", "수동") is True
    assert il.guard_appkey("GUARD_AUTO_KEY", "자동매매") is True
    assert len(il._APPKEY_LOCKS) == 2, "두 번째 잠금이 첫 잠금을 덮어썼다"

    other = il.AppKeyLock("GUARD_MAIN_KEY")
    assert other.acquire() is False, "수동 키 잠금이 조용히 풀려 있었다"


def test_appkey_duplicate_note_names_the_offending_key(clean_appkey_locks):
    """진단 로그가 어느 키에서 중복이 났는지 밝혀야 한다."""
    il = clean_appkey_locks
    holder = il.AppKeyLock("GUARD_DUP_KEY")
    assert holder.acquire() is True
    try:
        assert il.guard_appkey("GUARD_DUP_KEY", "자동매매") is False
        assert il.APPKEY_DUPLICATE is True
        note = il.appkey_duplicate_note()
        assert "자동매매" in note, f"어느 키가 중복인지 드러나지 않는다: {note}"
    finally:
        holder.release()


def test_appkey_guard_treats_missing_key_as_out_of_scope(clean_appkey_locks):
    """앱키 미설정(자동매매 키 없음)은 중복이 아니라 검사 대상이 아니다."""
    il = clean_appkey_locks
    assert il.guard_appkey("", "자동매매") is True
    assert len(il._APPKEY_LOCKS) == 0
    assert il.APPKEY_DUPLICATE is False


# ==========================================================
# [추가 2026-08-25] 모드 단위 중복 실행 차단
# ==========================================================
# 계좌 잠금은 자동매매 엔진을 켤 때만 걸리고, 앱키 감지는 기본이 경고뿐이라(토스는 검사조차
#  안 한다) 같은 모드로 두 번 띄우면 둘 다 정상 기동했다. 그 사이 텔레그램 폴링은 409 로
#  갈리고, KIS 유량·웹소켓·토큰은 서로를 끊고, mode 1·2·3 은 DB 파일 하나를 함께 쓴다.
@pytest.fixture
def clean_mode_locks(tmp_path):
    """모드 잠금 전역을 격리한다(잠금 파일 경로도 임시로 돌린다).

    앱키 전역까지 함께 되돌린다 — 기동 차단(_enforce_single_instance)이 모드 잠금과 앱키
    감지를 한 번에 부르므로, 모드 쪽만 치우면 앱키 잠금이 fd 를 문 채 다음 테스트로 샌다.
    """
    saved_holder = instance_lock.MODE_HOLDER
    saved_appkey = (list(instance_lock._APPKEY_LOCKS), instance_lock.APPKEY_DUPLICATE,
                    instance_lock.APPKEY_HOLDER, instance_lock.APPKEY_DUP_LABEL)
    instance_lock.release_mode()
    instance_lock._APPKEY_LOCKS = []
    with patch.object(config, 'DB_FILE_PATH', str(tmp_path / "t.db")):
        yield instance_lock
    instance_lock.release_mode()
    for lk in instance_lock._APPKEY_LOCKS:
        try:
            lk.release()
        except Exception:
            pass
    instance_lock.MODE_HOLDER = saved_holder
    (instance_lock._APPKEY_LOCKS, instance_lock.APPKEY_DUPLICATE,
     instance_lock.APPKEY_HOLDER, instance_lock.APPKEY_DUP_LABEL) = saved_appkey


def test_second_process_in_the_same_mode_is_blocked(clean_mode_locks):
    """[핵심] 같은 모드로 두 번째 프로세스가 자리를 잡을 수 없다."""
    il = clean_mode_locks
    holder = il.ModeLock("2")
    assert holder.acquire() is True, "전제: 첫 프로세스가 자리를 잡는다"
    try:
        assert il.guard_mode("2") is False, "같은 모드로 두 번째 프로세스가 떴다"
        assert "pid=" in il.MODE_HOLDER, f"선점자 정보가 비어 있다: {il.MODE_HOLDER!r}"
    finally:
        holder.release()


def test_a_different_mode_is_not_blocked(clean_mode_locks):
    """다른 모드는 막지 않는다 — 실전(2) 운용 + 관찰(4) 동시 기동이 정상 흐름이다."""
    il = clean_mode_locks
    holder = il.ModeLock("2")
    assert holder.acquire()
    try:
        assert il.guard_mode("4") is True, "모드가 다른데 서로를 막았다"
    finally:
        holder.release()


def test_the_holder_note_says_since_when(clean_mode_locks):
    """선점자 안내에 시작 시각이 있어야 한다 — 죽일지 살릴지는 그걸 보고 판단한다."""
    il = clean_mode_locks
    holder = il.ModeLock("2")
    assert holder.acquire()
    try:
        assert il.guard_mode("2") is False
        assert "started=" in il.MODE_HOLDER, f"언제부터 떠 있는지 알 수 없다: {il.MODE_HOLDER!r}"
        assert "mode=2" in il.MODE_HOLDER
    finally:
        holder.release()


def test_the_same_process_does_not_block_itself(clean_mode_locks):
    """[핵심] 자기 잠금에 자기가 걸리면 안 된다.

    flock 은 '열린 파일 기술자' 단위라, 같은 프로세스가 파일을 다시 열어 잠그면 자기
    잠금에 막힌다(POSIX 레코드 잠금과 다른 지점이다). 재진입은 성공으로 봐야 한다.
    """
    il = clean_mode_locks
    assert il.guard_mode("2") is True
    assert il.guard_mode("2") is True, "같은 프로세스가 자기 잠금에 막혔다"


def test_allow_duplicate_does_not_claim_the_seat(clean_mode_locks):
    """[핵심] 손님 인스턴스는 자리를 주장하지 않는다.

    --allow-duplicate 로 띄운 조회 전용 인스턴스가 잠금을 차지해 버리면, 나중에 정규
    인스턴스를 띄울 때 손님에게 막히는 뒤집힌 상황이 된다.
    """
    il = clean_mode_locks
    assert il.guard_mode("2", allow_duplicate=True) is True
    assert not il._MODE_LOCKS, "손님 인스턴스가 잠금을 잡았다"

    later = il.ModeLock("2")
    try:
        assert later.acquire() is True, "정규 인스턴스가 손님에게 막혔다"
    finally:
        later.release()


def test_allow_duplicate_ignores_an_existing_holder(clean_mode_locks):
    """플래그를 준 쪽은 선점자가 있어도 뜬다(그게 이 플래그의 목적이다)."""
    il = clean_mode_locks
    holder = il.ModeLock("2")
    assert holder.acquire()
    try:
        assert il.guard_mode("2", allow_duplicate=True) is True
    finally:
        holder.release()


def test_mode_lock_does_not_collide_with_the_other_locks(clean_mode_locks):
    """모드·계좌·앱키 잠금은 서로 다른 파일을 쓴다(겹치면 한쪽이 무력해진다)."""
    il = clean_mode_locks
    mode, acct, key = il.ModeLock("2"), il.InstanceLock("2"), il.AppKeyLock("2")
    try:
        assert len({mode.path, acct.path, key.path}) == 3
        assert mode.acquire() and acct.acquire() and key.acquire()
    finally:
        mode.release(); acct.release(); key.release()


def test_a_stale_mode_lock_does_not_block_restart(clean_mode_locks):
    """비정상 종료(OOM·kill -9)가 남긴 잠금 파일이 재시작을 막으면 안 된다."""
    il = clean_mode_locks
    dead = il.ModeLock("2")
    assert dead.acquire()
    os.close(dead._fd)          # 프로세스가 죽어 fd 가 닫힌 상황(파일은 그대로 남는다)
    dead._fd = None
    assert os.path.exists(dead.path), "전제: 잠금 파일은 남아 있다"
    assert il.guard_mode("2") is True, "죽은 프로세스의 잠금 파일이 재시작을 막는다"


def test_startup_aborts_with_guidance_when_the_mode_is_taken(clean_mode_locks):
    """[배선] 선점자가 있으면 기동은 안내를 출력하고 종료한다."""
    import main
    il = clean_mode_locks
    holder = il.ModeLock("2")
    assert holder.acquire(), "전제: 선점 잠금을 잡는다"
    try:
        with patch.object(config, 'console') as console:
            with pytest.raises(SystemExit) as exc:
                main._enforce_single_instance("2")
    finally:
        holder.release()

    assert exc.value.code == 1, "종료 코드가 실패가 아니다"
    printed = " ".join(str(c) for c in console.print.call_args_list)
    assert "한투증권" in printed, "어느 모드가 겹쳤는지 밝히지 않는다"
    assert str(os.getpid()) in printed, "선점 프로세스를 짚어 주지 않는다"
    assert "--allow-duplicate" in printed, "빠져나갈 방법을 안내하지 않는다"
    # 운영자가 직접 죽일 수 있어야 한다 — 명령을 찾으러 다른 문서를 뒤지게 하지 않는다.
    assert f"kill {os.getpid()}" in printed, "종료 명령을 주지 않는다"
    assert f"kill -9 {os.getpid()}" in printed, "응답 없는 프로세스를 처리할 방법이 없다"


def test_the_incumbent_process_is_described(clean_mode_locks):
    """선점 pid 가 '무엇이 언제부터'인지까지 보여야 죽일지 말지 판단할 수 있다."""
    import main
    detail = main._describe_process(os.getpid())
    assert detail, "프로세스 정보를 읽지 못했다"
    assert "python" in detail.lower() or "Python" in detail, f"명령줄이 없다: {detail!r}"


def test_process_description_never_breaks_the_guidance(clean_mode_locks):
    """ps 를 못 읽어도 안내 자체는 나가야 한다(조회 실패가 차단을 무르게 하면 안 된다)."""
    import main
    with patch('subprocess.run', side_effect=OSError("no ps")):
        assert main._describe_process(12345) == ""
    assert main._describe_process("") == ""


def test_startup_proceeds_when_the_mode_is_free(clean_mode_locks):
    """자리가 비어 있으면 조용히 통과한다."""
    import main
    with patch.object(config, 'console'):
        main._enforce_single_instance("2")      # SystemExit 이 나면 실패다
    assert "2" in clean_mode_locks._MODE_LOCKS, "통과했는데 자리를 잡지 않았다"


def test_startup_still_aborts_when_only_the_appkey_is_held(clean_mode_locks):
    """[핵심] 선점 프로세스가 모드 잠금이 없던 버전이어도 막는다.

    실제로 겪은 구멍이다(2026-08-25). 무중단으로 새 코드를 올리면 이미 떠 있는
    프로세스는 모드 잠금을 잡지 않은 상태라 자리가 비어 보이고, 두 번째 인스턴스가
    그대로 기동했다. 그 프로세스도 앱키 잠금만은 쥐고 있으므로 그쪽으로 잡아낸다.
    """
    import main
    il = clean_mode_locks
    legacy = il.AppKeyLock("REAL_KEY_A")        # 모드를 남기지 않던 버전의 잠금
    legacy._extra_info = lambda: ""
    assert legacy.acquire(), "전제: 구버전 프로세스가 앱키만 쥐고 있다"
    try:
        with patch.object(config.session, 'is_toss', False), \
             patch.object(config.session, 'app_key', "REAL_KEY_A"), \
             patch.object(config.session, 'auto_app_key', ""), \
             patch.object(config, 'console') as console:
            with pytest.raises(SystemExit) as exc:
                main._enforce_single_instance("2")
    finally:
        legacy.release()

    assert exc.value.code == 1
    printed = " ".join(str(c) for c in console.print.call_args_list)
    assert str(os.getpid()) in printed, "선점 프로세스를 짚어 주지 않는다"


def test_startup_does_not_abort_when_another_mode_shares_the_appkey(clean_mode_locks):
    """모드가 다르면 막지 않는다 — 앱키만 겹친 것은 환경변수 설정 문제다.

    실전(2) 운용 중에 관찰(4)을 띄우는 것은 정상 흐름이고, VIRT_APP_KEY 를 REAL 과 같게
    둔 설정 실수까지 기동 차단으로 갚게 하면 정상 조합을 끊는 쪽이 더 크다.
    """
    import main
    il = clean_mode_locks
    with patch.object(config.session, 'mode', "2"):
        other = il.AppKeyLock("SHARED_KEY")     # mode=2 가 파일에 박힌다
        assert other.acquire()
    try:
        with patch.object(config.session, 'is_toss', False), \
             patch.object(config.session, 'app_key', "SHARED_KEY"), \
             patch.object(config.session, 'auto_app_key', ""), \
             patch.object(config, 'console') as console:
            main._enforce_single_instance("4")   # SystemExit 이 나면 실패다
    finally:
        other.release()

    printed = " ".join(str(c) for c in console.print.call_args_list)
    assert "VIRT_APP_KEY" in printed, "설정을 어떻게 고치는지 안내하지 않는다"


def test_appkey_lock_records_the_mode(clean_mode_locks):
    """앱키 잠금 파일에 모드가 남아야 '같은 모드인가'를 뒤에서 판정할 수 있다."""
    il = clean_mode_locks
    with patch.object(config.session, 'mode', "2"):
        lock = il.AppKeyLock("MODE_STAMP_KEY")
        assert lock.acquire()
    try:
        probe = il.AppKeyLock("MODE_STAMP_KEY")
        assert probe.acquire() is False
        assert il.holder_mode(probe.holder) == "2", f"모드가 안 남았다: {probe.holder!r}"
    finally:
        lock.release()


def test_holder_mode_reads_nothing_from_a_legacy_lock(clean_mode_locks):
    """모드를 남기지 않던 버전의 잠금은 ""로 읽힌다(= 같은 모드로 간주해 막는 쪽)."""
    il = clean_mode_locks
    assert il.holder_mode("pid=999 appkey=abc") == ""
    assert il.holder_mode("") == ""
