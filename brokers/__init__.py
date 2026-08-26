"""증권사 직결 원시 클라이언트 — 우리 시스템의 어휘가 아직 닿지 않는 계층.

여기 있는 모듈은 증권사가 정한 규격(엔드포인트·필드명·토큰·레이트 리밋)을 그대로 말한다.
`api/` 가 '우리가 쓰는 시세·주문'이라면 이쪽은 '저쪽이 주는 시세·주문'이고, 둘 사이의 번역은
api 계층이 한다(예: `api/toss.py` 가 `brokers.toss_api` 를 감싼다).

    realtime    KIS WebSocket 실시간 시세·체결통보 (미커버 시 REST 폴백)
    toss_api    토스증권 Open API — 시세·자산·주문

[왜 api/ 안이 아닌가 — 2026-08-24 실측]
api 는 서브모듈의 이름을 전부 `api.X` 로 평탄화하고, 쓰기를 그 이름을 가진 모든 서브모듈로
전파하는 패키지다. 여기 둘을 그 안에 넣으면 이름이 부딪힌다(toss_api 54개 중 11개, realtime
50개 중 7개). 그중 `get_investor_trend` 는 **KIS 수급과 토스 수급**이라 의미까지 정면으로
충돌한다 — `api.get_investor_trend` 가 어느 쪽인지 모호해지고 `patch.object(api, ...)` 가
엉뚱한 클라이언트를 덮는다. (mode 1 폐기 전에는 `get_access_token` 도 같은 충돌이었는데,
KIS 모의 토큰 발급이 사라지면서 그 이름은 토스 전용이 됐다.) 평탄화가 필요한 계층이 아니므로(호출부는 늘 `toss_api.X` 로
쓴다) 옆에 둔다.

[방향] brokers 는 api/modules/core 를 부르지 않는다. config 만 참조한다.
"""
