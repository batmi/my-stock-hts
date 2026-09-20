"""몬테카를로 시뮬레이션 감사(2026-09-20) — 재현성·PF 센티널.

노이즈 모형(수준 노이즈)의 편향은 코드가 아니라 화면 문구와 독스트링에 적었다(운용자 결정 사항).
"""
from unittest.mock import patch

import numpy as np
import pandas as pd

from modules import backtest


def _df(n=60):
    rng = np.random.default_rng(1)
    close = 10000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"date": [f"2025{(i // 28) % 12 + 1:02d}{i % 28 + 1:02d}" for i in range(n)],
                         "open": close * 0.99, "high": close * 1.02, "low": close * 0.98,
                         "close": close, "volume": 1000.0})


def _run(seed, gross_loss):
    seen = []

    def fake_sim(noisy_df, *a, **k):
        seen.append(float(noisy_df["close"].iloc[-1]))
        return {"trades": [], "final_asset": 1_000_000, "total_return": 1.0, "mdd": -1.0,
                "win_trades": 1, "loss_trades": 0, "gross_profit": 10.0, "gross_loss": gross_loss,
                "daily_assets": [1_000_000, 1_010_000]}

    with patch.object(backtest, "simulate_strategy", side_effect=fake_sim), \
         patch("rich.prompt.Prompt.ask", return_value="n"), patch("config.console.print") as out:
        backtest.run_monte_carlo_simulation(_df(), 30, 1_000_000, 7.5, 65, False, -7.0, 20.0, 75.0, 5.0,
                                            10.0, 3.0, 10, True, 2.0, True, runs=5, seed=seed)
    return seen, out


def test_같은_seed_는_같은_노이즈_경로를_만들고_다른_seed_는_다르다():
    a, _ = _run(1, 5.0)
    b, _ = _run(1, 5.0)
    c, _ = _run(2, 5.0)
    assert a == b and a != c and len(a) == 5


def test_무손실_시행은_PF_평균을_오염시키지_않는다():
    """종전엔 손실 0 인 시행에 PF=99.9 를 넣어 평균이 튀었다 — 이제 제외하고 중앙값이며 화면에 밝힌다."""
    _, out = _run(1, 0.0)
    texts = []
    for call in out.call_args_list:
        for arg in call.args:
            if hasattr(arg, "rows"):              # rich Table
                texts += [str(c) for r in arg.rows for c in getattr(arg, "columns", [])]
                texts += [" ".join(str(x) for x in col._cells) for col in arg.columns]
            else:
                texts.append(str(arg))
    joined = "\n".join(texts)
    assert "무손실 시행 5회 제외" in joined and "99.9" not in joined
    assert "seed 1" in joined
