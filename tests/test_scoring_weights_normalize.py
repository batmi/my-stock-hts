"""스코어링 가중치 입력 검증 테스트.

합계가 정확히 10.0점이 아니면 자동 재계산하지 않고 재입력을 요구해야 한다.
합계가 10.0이면 입력값 그대로 저장된다. (4팩터 체계: 추세/모멘텀/강도/시너지)
"""
from unittest.mock import patch

import config
import modules.settings as settings


def test_modify_scoring_weights_requires_exact_10():
    """합계≠10 입력은 거부 후 재입력, 10이 되면 그대로 저장(자동 재계산 없음)."""
    saved = dict(config.SCORING_WEIGHTS)
    # 1차: a→6/3/2/1(=12, 거부) → 메뉴 복귀 → 2차: a→4/2.5/1.5/2(=10, 저장) → b
    seq = ['a', '6', '3', '2', '1',
           'a', '4', '2.5', '1.5', '2',
           'b']
    try:
        with patch("modules.settings.Prompt.ask", side_effect=seq), \
             patch("modules.settings._save_dynamic_config"), \
             patch.object(config, "ENABLE_TELEGRAM", False):
            settings.modify_scoring_weights()
        # 자동 재계산이 아니라 입력값(10점) 그대로 저장
        assert config.SCORING_WEIGHTS["TREND"] == 4.0
        assert config.SCORING_WEIGHTS["MOMENTUM"] == 2.5
        assert config.SCORING_WEIGHTS["STRENGTH"] == 1.5
        assert config.SCORING_WEIGHTS["SYNERGY"] == 2.0
        assert abs(sum(config.SCORING_WEIGHTS.values()) - 10.0) < 0.01
    finally:
        config.SCORING_WEIGHTS.clear()
        config.SCORING_WEIGHTS.update(saved)


def test_modify_scoring_weights_rejects_and_keeps_old():
    """합계≠10만 입력하고 종료(b)하면 기존 가중치가 유지된다(저장 안 됨)."""
    saved = dict(config.SCORING_WEIGHTS)
    seq = ['a', '5', '3', '2', '2', 'b']  # 합계 12 → 거부 → b로 종료
    try:
        with patch("modules.settings.Prompt.ask", side_effect=seq), \
             patch("modules.settings._save_dynamic_config"), \
             patch.object(config, "ENABLE_TELEGRAM", False):
            settings.modify_scoring_weights()
        assert config.SCORING_WEIGHTS == saved  # 변경 없음
    finally:
        config.SCORING_WEIGHTS.clear()
        config.SCORING_WEIGHTS.update(saved)
