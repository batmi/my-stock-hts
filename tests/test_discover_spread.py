"""탐색이 뽑은 시총 분포가 실제로 넓은가.

[배경] `_spread_pick`은 "상위부터 채우면 대형주만 남아 감사 조건과 어긋난다"는 이유로
시총 구간에 고르게 뽑는다. 그런데 뽑은 뒤 `_verify_data`가 **앞에서부터 채우다 target
개에서 멈추므로**, 여유분(target×2)을 단순히 뒤에 붙이면 뒤쪽 절반은 영영 안 나온다.
씨드를 바꿔 몇 번을 다시 돌려도 마찬가지다 — 순서가 시총 내림차순으로 고정돼 있어서다.
"""
import random

import pytest

from modules.manage import discover


def _ranks(target, universe=400, seed=0):
    """시총 내림차순 universe 종목에서 target 개를 뽑았을 때 남는 순위."""
    kept = [{"i": i} for i in range(universe)]
    spread = discover._spread_pick(kept, target * 2, random.Random(seed))
    ordered = spread[0::2] + spread[1::2]          # _fetch_candidates 가 돌려주는 순서
    return [c["i"] for c in ordered[:target]]      # _verify_data 가 실제로 남기는 것


def test_the_bottom_half_is_reachable():
    """종전에는 최대 순위가 풀의 절반(약 200위)에서 잘렸다."""
    ranks = _ranks(target=20)
    assert max(ranks) > 300, f"소형주 구간이 통째로 빠졌다: {ranks}"


def test_the_picks_span_the_whole_range():
    """네 구간(사분위)마다 최소 한 종목은 있어야 '고르게'라 할 수 있다."""
    ranks = _ranks(target=20, universe=400)
    quartiles = {r // 100 for r in ranks}
    assert quartiles == {0, 1, 2, 3}, f"빠진 구간이 있다: {sorted(quartiles)}"


@pytest.mark.parametrize("seed", [0, 1, 7, 42])
def test_the_spare_pool_is_also_spread(seed):
    """예비분이 한쪽에 몰려 있으면, 데이터 확인에서 몇 개만 탈락해도 분포가 무너진다."""
    kept = [{"i": i} for i in range(400)]
    spread = discover._spread_pick(kept, 40, random.Random(seed))
    reserve = [c["i"] for c in spread[1::2]]
    assert min(reserve) < 100 and max(reserve) > 300, reserve


def test_candidates_carry_no_dead_rank_field():
    """rank 는 아무 데서도 안 읽히면서 산식만 틀려 있었다(관리종목 제외분 누락)."""
    import inspect
    assert '"rank"' not in inspect.getsource(discover._fetch_candidates)
