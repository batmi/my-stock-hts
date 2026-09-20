"""config.<필드> 대입은 settings 로 간다 — 모듈 그림자 금지(2026-09-20).

[배경] 모듈 __getattr__ 은 settings 를 비추지만 대입은 모듈 사전에 그림자를 만들었다.
그림자가 생기면 그 뒤의 읽기가 settings 를 보지 않아 메뉴 설정 변경·프로필 전환이 그 이름만
무시됐고, 테스트 22파일 30곳의 monkeypatch.setattr(config, "<필드>") 이 복원 때 같은 그림자를
남겨 순서 의존 실패(xdist 플래키)를 만들었다.
"""
import config


def test_필드_대입은_settings_에_쓰이고_그림자를_남기지_않는다():
    saved = config.settings.USE_MARKET_FILTER
    try:
        config.USE_MARKET_FILTER = not saved
        assert config.settings.USE_MARKET_FILTER is (not saved)
        assert "USE_MARKET_FILTER" not in vars(config)
    finally:
        config.USE_MARKET_FILTER = saved


def test_monkeypatch_복원_뒤에도_settings_변경이_읽기에_반영된다(monkeypatch):
    saved = config.settings.USE_MARKET_FILTER
    monkeypatch.setattr(config, "USE_MARKET_FILTER", not saved)
    assert config.USE_MARKET_FILTER is (not saved)
    monkeypatch.undo()
    assert config.settings.USE_MARKET_FILTER is saved and "USE_MARKET_FILTER" not in vars(config)
    config.settings.USE_MARKET_FILTER = not saved
    try:
        assert config.USE_MARKET_FILTER is (not saved), "그림자가 남아 settings 변경을 가렸다"
    finally:
        config.settings.USE_MARKET_FILTER = saved


def test_필드가_아닌_이름은_종전대로_모듈_속성이다():
    config._TMP_ROUTING_PROBE = 1
    assert vars(config)["_TMP_ROUTING_PROBE"] == 1
    del config._TMP_ROUTING_PROBE
    assert "_TMP_ROUTING_PROBE" not in vars(config)
