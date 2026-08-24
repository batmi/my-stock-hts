"""실전 TPS 예산을 앱키 단위로 분리하는지 검증한다.

[배경] KIS의 유량 한도(REAL_TX_PER_SECOND=20)는 계좌가 아니라 **앱키** 단위다.
mode 2에서 수동(운용자) 계좌와 자동매매 계좌에 서로 다른 앱키(REAL_APP_KEY /
AUTO_APP_KEY)를 쓰면 각각 20 TPS를 따로 받는데, 종전 구현은 실전 요청을 단일
deque 하나로 세어 둘을 합쳐 20으로 눌렀다. 운용자가 메뉴에서 조회를 돌리는 동안
시스템 트레이딩의 판정·주문이 같은 예산을 다투는 구조였고, 손해는 시각이 곧
가격인 매매 쪽이 봤다.

[반대 방향 위험] 앱키가 실제로는 같은데 나눠 세면 합계 40 TPS가 되어 EGW00201을
자초한다. 그래서 분리는 '두 키가 실제로 다를 때'에만 일어나야 한다.
"""
import pytest

import api
import config
from core import context
from api import ThrottledSession


@pytest.fixture
def sess():
    return ThrottledSession()


@pytest.fixture
def separated(monkeypatch):
    """실전 + 수동/자동 앱키가 실제로 다른 환경."""
    s = config.session
    monkeypatch.setattr(s, 'is_simulation', False, raising=False)
    monkeypatch.setattr(s, 'real_app_key', 'MAIN_KEY', raising=False)
    monkeypatch.setattr(s, 'auto_app_key', 'AUTO_KEY', raising=False)
    monkeypatch.setattr(context.trade_context, 'use_auto_account', False, raising=False)
    yield s
    context.trade_context.use_auto_account = False


def _as_auto(flag=True):
    context.trade_context.use_auto_account = flag


def test_separate_appkeys_get_separate_buckets(sess, separated):
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_AUTO
    assert sess._real_buckets[sess.BUCKET_AUTO] is not sess._real_buckets[sess.BUCKET_MANUAL]


def test_identical_appkeys_share_one_bucket(sess, separated, monkeypatch):
    """앱키가 같으면 KIS 카운터도 하나다 — 나눠 세면 합계 40 TPS로 자폭한다."""
    monkeypatch.setattr(config.session, 'auto_app_key', 'MAIN_KEY', raising=False)
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL


def test_missing_auto_appkey_shares_one_bucket(sess, separated, monkeypatch):
    """AUTO_APP_KEY 미설정이면 헤더도 real_app_key로 폴백하므로 버킷도 같아야 한다."""
    monkeypatch.setattr(config.session, 'auto_app_key', '', raising=False)
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL


def test_simulation_never_splits(sess, separated, monkeypatch):
    """모의투자는 앱키가 하나뿐이다(세션 로드가 auto_app_key = app_key로 동기화)."""
    monkeypatch.setattr(config.session, 'is_simulation', True, raising=False)
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL


def test_paper_mode_never_splits(sess, separated, monkeypatch):
    """관찰모드(mode 4)는 앱키가 VIRT 하나뿐이다 — 나누면 한 키에 40 TPS를 쏜다.

    mode 4는 KIS 실전 시세를 쓰므로 is_simulation이 False다. 즉 모의투자 분기로
    걸러지지 않고, 오직 'real_app_key == auto_app_key'라는 사실에 기대어 한 버킷으로
    모인다(session.load_config가 둘 다 virt_app_key로 덮어쓴다). 그 동기화가 깨지면
    버킷이 갈려 각각 20 TPS를 허용하고, 실제로는 같은 키라 EGW00201이 쏟아진다.
    라즈베리파이에서 상시 돌고 있는 모드라 회귀를 여기서 잡는다.
    """
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    monkeypatch.setattr(config.session, 'real_app_key', 'VIRT_KEY', raising=False)
    monkeypatch.setattr(config.session, 'auto_app_key', 'VIRT_KEY', raising=False)
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL


def test_toss_mode_never_splits(sess, separated, monkeypatch):
    """토스(mode 3)는 KIS 앱키를 쓰지 않는다 — 빈 키로 버킷을 가르면 안 된다."""
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    monkeypatch.setattr(config.session, 'real_app_key', '', raising=False)
    monkeypatch.setattr(config.session, 'auto_app_key', '', raising=False)
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL
    _as_auto()
    assert sess._real_bucket_key() == sess.BUCKET_MANUAL


def test_backoff_on_one_key_does_not_punish_the_other(sess, separated):
    """한쪽 앱키의 EGW00201이 다른 키의 예산을 깎으면 안 된다."""
    manual = sess._real_buckets[sess.BUCKET_MANUAL]
    auto = sess._real_buckets[sess.BUCKET_AUTO]
    manual.adaptive_limit = auto.adaptive_limit = 18.0

    _as_auto(False)
    sess._tps_on_rate_limit_real(url="https://x/y", tr_id="FHKST01010100")

    assert manual.adaptive_limit < 18.0, "거부가 난 쪽은 물러나야 한다"
    assert auto.adaptive_limit == 18.0, (
        f"수동 키의 혼잡으로 자동매매 키까지 {auto.adaptive_limit:.2f}로 깎였다")


def test_raise_cadence_is_tracked_per_bucket(sess, separated):
    """가산 증가의 '윈도우당 1회' 제한도 버킷마다 독립이다."""
    manual = sess._real_buckets[sess.BUCKET_MANUAL]
    auto = sess._real_buckets[sess.BUCKET_AUTO]
    manual.adaptive_limit = auto.adaptive_limit = 17.0

    _as_auto(False)
    sess._tps_on_success_real()          # 수동 쪽만 한 칸 올린다
    raised_manual = manual.adaptive_limit
    assert raised_manual > 17.0

    _as_auto(True)
    sess._tps_on_success_real()          # 자동 쪽은 자기 창을 아직 안 썼으므로 올라야 한다
    assert auto.adaptive_limit > 17.0, (
        "수동 키가 방금 올렸다는 이유로 자동매매 키의 상향이 막혔다")


def test_gate_history_is_counted_per_bucket(sess, separated):
    """전송 이력이 섞이면 한쪽 트래픽이 다른 쪽의 창을 채워 버린다."""
    manual = sess._real_buckets[sess.BUCKET_MANUAL]
    auto = sess._real_buckets[sess.BUCKET_AUTO]

    manual.history.extend([1000.0] * 15)
    assert len(auto.history) == 0, "수동 조회 15건이 자동매매 창을 잠식했다"

    _as_auto()
    assert sess._real_bucket().history is auto.history


def test_legacy_attributes_still_address_the_manual_bucket(sess, separated):
    """기존 코드·테스트가 쓰던 평면 속성은 수동 버킷을 가리킨다(하위 호환)."""
    sess.adaptive_limit_real = 12.5
    sess.gate_grants_real = 7
    sess._last_tps_drop = 33.0
    sess._last_tps_raise = 44.0
    sess._last_priority_grant = 55.0

    manual = sess._real_buckets[sess.BUCKET_MANUAL]
    assert (manual.adaptive_limit, manual.grants) == (12.5, 7)
    assert (manual.last_drop, manual.last_raise, manual.last_priority_grant) == (33.0, 44.0, 55.0)
    assert sess.request_history_real is manual.history
