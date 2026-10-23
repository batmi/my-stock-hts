"""몬테카를로 시뮬레이션 감사(2026-09-20) — 재현성·PF 센티널, 노이즈 모형(2026-10-23 교체).

종전 봉마다 독립인 수준 노이즈는 변동성·ATR 을 부풀리고 평균회귀를 주입했다 → 이어지는 AR(1)
수준 노이즈(backtest.persistent_level_noise)로 바꿨다. 아래 테스트가 그 성질을 고정한다.
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


def test_노이즈는_수준은_1퍼센트로_흔들되_수익률을_거의_부풀리지_않는다():
    rng = np.random.default_rng(7)
    u = np.concatenate([backtest.persistent_level_noise(rng, 2000) for _ in range(20)])
    assert abs(u.std() - backtest.MONTE_CARLO_NOISE_SIGMA) < 0.002        # 수준 이탈은 ±1% 그대로
    du = np.diff(u)
    #  일수익률에 더해지는 몫 ≈ σ·√(2(1−φ)) ≈ 0.32% — 종전 독립 노이즈는 √2·1% ≈ 1.41%
    assert du.std() < 0.004
    #  자기상관 −(1−φ)/2 ≈ −0.025 — 종전 독립 노이즈는 −0.5(평균회귀 주입)
    ac = np.corrcoef(du[1:], du[:-1])[0, 1]
    assert -0.06 < ac < 0.01


def test_노이즈_경로는_길이를_지키고_빈_길이도_받는다():
    rng = np.random.default_rng(1)
    assert len(backtest.persistent_level_noise(rng, 123)) == 123
    assert len(backtest.persistent_level_noise(rng, 0)) == 0


def test_추세_경로의_변동성과_자기상관을_보존한다():
    """실측 대응 — 노이즈를 입힌 경로의 일수익률 σ 와 lag-1 자기상관이 원본에서 크게 벗어나지 않는다."""
    rng = np.random.default_rng(3)
    close = 10000 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 900)))
    r0 = np.diff(np.log(close))
    sig, acs = [], []
    for _ in range(50):
        r = np.diff(np.log(close * (1 + backtest.persistent_level_noise(rng, len(close)))))
        sig.append(r.std() / r0.std())
        acs.append(np.corrcoef(r[1:], r[:-1])[0, 1])
    assert np.mean(sig) < 1.02                      # 종전 독립 노이즈는 약 1.12(σ 2% 기준)
    ac0 = np.corrcoef(r0[1:], r0[:-1])[0, 1]
    assert abs(np.mean(acs) - ac0) < 0.02
