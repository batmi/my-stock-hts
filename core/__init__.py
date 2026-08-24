"""최하위 공통 계층 — 프로젝트 도메인을 모르는 코드만 둔다.

여기 있는 모듈은 '무엇을 매매하는가'를 알지 못한다. 날짜·포맷·캐시·JSON IO·스레드 상태·
스레드 풀·상수 표와 지표 수식·거래비용 산식처럼, **입력이 주어지면 답이 정해지는** 것들이다.
금융 지식이 없다는 뜻이 아니라(지표와 비용은 명백히 금융이다) 판단을 내리지 않는다는 뜻이다 —
무엇을 언제 사고팔지는 위층(modules)이 정한다. 그래서 어느 계층에서 불러도 순환이 생기지
않는다(config 만 예외적으로 참조한다 — 로거·콘솔·임계값의 단일 소스).

바깥에서는 `from core import utils` 형태로 쓴다.

[한 곳의 예외] `context._session_token()` 은 세션 만료 판정을 위해 `api` 를 **함수 안에서**
부른다(api 가 context 를 import 하므로 모듈 최상단에 두면 순환한다). 이동 이전부터 있던
의존이며, 여기 말고는 core 가 api/modules 를 부르지 않는다 — 새 코드에서 이 방향을 늘리면
'최하위'라는 성질이 사라진다.

[왜 executors·trading_cost 가 여기 있나 — 2026-08-24]
둘은 modules/ 에 있었지만 도메인 폴더의 의존 그래프에 **없는 순환을 만들어 냈다.**
`theme_analysis → executors` 하나 때문에 research 와 trade 가 서로를 물었고,
`holdings_backfill → trading_cost` 하나 때문에 backtest 와 store 가 서로를 물었다.
정작 executors 는 스레드 풀 넷(concurrent.futures 만 import)이고 trading_cost 는 config 만
읽는 순수 산식이다. 제자리로 옮기자 두 순환이 함께 사라졌다.
"""
