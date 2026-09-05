import os
import sys
import json
import hashlib
from core import jsonio
from datetime import datetime, timedelta
from rich.prompt import Prompt


def _config():
    """설정 모듈. **호출 시점에** 해석한다 — 최상단에서 import 하면 안 된다.

    config 는 이 모듈의 SessionManager 로 전역 세션 객체를 만든다(`config.session = SessionManager()`).
    그래서 session 이 최상단에서 config 를 부르면 두 모듈이 서로를 물고, 어느 쪽을 먼저
    import 하느냐가 성패를 가른다 — 실제로 `import session` 을 단독 실행하면
    `ImportError: cannot import name 'SessionManager'` 로 죽었다(config 가 먼저 로드되는
    실행 경로에서만 우연히 살아 있었다). 접근자로 미루면 import 순서와 무관해진다.
    (api 패키지의 `_api()`, auto_trade 의 `_pkg()` 와 같은 규약이다.)
    """
    import config
    return config


class SessionManager:
    def __init__(self):
        # 확정된 운용 모드('1'~'3'). is_toss/is_paper 플래그만으로도 대부분
        #  구분되지만, 그 조합을 되짚는 것과 '무엇으로 떴는가'를 그대로 아는 것은 다르다
        #  — 중복 실행 잠금(modules/instance_lock.guard_mode)은 모드 자체를 키로 쓴다.
        self.mode = ""
        self.is_toss = False  # [추가] 토스증권 모드 여부
        # [추가] 관찰(페이퍼 트레이딩) 모드 여부 = mode 1. 시세·지표는 실제 소스(KIS 실전)를
        #  그대로 쓰고 잔고·예수금·주문만 가상으로 처리한다(modules/paper_broker.py).
        #  실주문은 원천 차단된다.
        self.is_paper = False
        self.url_base = ""
        
        # 현재 활성 계좌 정보 (모드에 따라 변경됨)
        self.app_key = ""
        self.app_secret = ""
        self.cano = ""
        self.acnt_prdt_cd = ""
        
        # 실전투자 정보 저장
        self.real_app_key = ""
        self.real_app_secret = ""
        
        # 자동매매 전용 정보
        self.auto_app_key = ""
        self.auto_app_secret = ""
        self.auto_cano = ""
        self.auto_acnt_prdt_cd = ""
        
        # [추가] 토스증권 (TOSS_) 정보
        self.toss_app_key = ""
        self.toss_app_secret = ""
        self.toss_acc_num = ""
        self.toss_account_seq = None  # /accounts 조회로 해석되는 accountSeq

        # [추가] 가상투자(mode 1) 전용 KIS 앱키. 실전 인스턴스와 앱키를 분리하기 위한 것.
        self.virt_app_key = ""
        self.virt_app_secret = ""
        # [표시 전용] VIRT 앱키에 매인 실제 계좌번호. **매매·조회에 쓰지 않는다** —
        #  가상투자의 cano 는 안전장치로 'PAPER'를 유지해야 하고(가로채기를 빠져나간
        #  계좌성 호출이 조용히 실계좌를 건드리지 않도록), 이 값은 알림 꼬리말처럼
        #  '어느 계좌 앞으로 도는 인스턴스인가'를 밝히는 표시에만 쓴다.
        self.virt_cano = ""
        self.virt_acnt_prdt_cd = ""

        # [추가] 실시간 체결통보(WebSocket H0STCNI0/H0STCNI9) 구독키 = HTS 로그인 ID.
        #   환경변수 우선순위: REAL_HTS_ID → 공통(KIS_HTS_ID/HTS_ID).
        #   미설정 시 체결통보 WS는 구독하지 않고 기존 REST 폴링(ConclusionMonitor)으로 폴백한다.
        self.hts_id = ""

        # 토큰 관리 (API 모듈에서 사용)
        self.real_access_token = ""
        self.auto_access_token = ""
        #  [2026-09-05] 메모리 토큰의 만료 시각. 종전에는 문자열만 들고 있어
        #   get_valid_token 의 메모리 경로가 **만료를 보지 않고** 그대로 돌려줬다.
        #   24시간 구동되는 파이에서는 반드시 도달한다(토큰 수명 24시간).
        self.real_token_expired = ""
        self.auto_token_expired = ""

        self.stock_data = {}
        self.exchange_cache = {}

    def initialize(self, mode=None):
        # [추가] 계좌번호 파싱 헬퍼 (XXXX-XX 형식 지원)
        def parse_acc(acc_str):
            if not acc_str: return "", ""
            if '-' in acc_str:
                parts = acc_str.split('-')
                return parts[0].strip(), parts[1].strip()
            return acc_str.strip(), ""

        # 1. 환경변수 로드 (REAL_, AUTO_ 접두사 사용)
        # 실전투자 (REAL_)
        self.real_app_key = os.environ.get("REAL_APP_KEY", "")
        self.real_app_secret = os.environ.get("REAL_APP_SECRET", "")
        
        # 자동투자 (AUTO_)
        self.auto_app_key = os.environ.get("AUTO_APP_KEY", "")
        self.auto_app_secret = os.environ.get("AUTO_APP_SECRET", "")

        # [추가] 토스증권 (TOSS_)
        self.toss_app_key = os.environ.get("TOSS_APP_KEY", "")
        self.toss_app_secret = os.environ.get("TOSS_APP_SECRET", "")
        self.toss_acc_num = os.environ.get("TOSS_ACC_NUM", "").strip()

        # [추가] 가상투자 전용 KIS 앱키 (VIRT_). mode 1이 KIS 실전 서버에서 '시세만' 받을 때 쓴다.
        #  실전 운용 인스턴스와 앱키를 나누는 것이 핵심이다 — KIS의 TPS(20)·웹소켓 동시 연결(1)·
        #  토큰 발급(1분 1회) 제약이 모두 앱키 단위라, 같은 키를 두 프로세스가 쓰면 가상투자가
        #  실계좌의 주문 경로를 갉아먹는다(양쪽 모두 EGW00201에 갇히고 웹소켓은 서로 끊는다).
        self.virt_app_key = os.environ.get("VIRT_APP_KEY", "")
        self.virt_app_secret = os.environ.get("VIRT_APP_SECRET", "")

        # [추가] 체결통보 WebSocket 구독키(HTS ID). 모드별 우선 → 공통 폴백.
        real_hts = os.environ.get("REAL_HTS_ID", "")
        common_hts = os.environ.get("KIS_HTS_ID", "") or os.environ.get("HTS_ID", "")
        
        # 계좌번호 (ACC_NUM)
        real_acc_str = os.environ.get("REAL_ACC_NUM", "")
        auto_acc_str = os.environ.get("AUTO_ACC_NUM", "")
        # VIRT_ACC_NUM 은 **표시 전용**이다(self.virt_cano 주석 참조). 매매·조회 경로는
        #  가상투자에서 cano='PAPER'를 그대로 쓴다.
        virt_acc_str = os.environ.get("VIRT_ACC_NUM", "")

        # 계좌번호 파싱
        real_cano, real_acnt = parse_acc(real_acc_str)
        auto_cano, auto_acnt = parse_acc(auto_acc_str)
        self.virt_cano, self.virt_acnt_prdt_cd = parse_acc(virt_acc_str)
        
        # 자동매매 계좌 설정
        self.auto_cano = auto_cano
        self.auto_acnt_prdt_cd = auto_acnt
        
        # 텔레그램 설정
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if tg_token: _config().TELEGRAM_BOT_TOKEN = tg_token

        tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if tg_chat_id: _config().TELEGRAM_CHAT_ID = tg_chat_id

        tg_inst = os.environ.get("TELEGRAM_INSTANCE_NAME")
        if tg_inst: _config().TELEGRAM_INSTANCE_NAME = tg_inst

        # OpenDART(전자공시) 설정 - KIS/텔레그램 키와 동일하게 런타임 환경변수에서 로드
        # (config.py import 시점뿐 아니라 세션 초기화 시점에도 재로딩하여 일관성 확보)
        dart_key = os.environ.get("DART_API_KEY")
        if dart_key: _config().DART_API_KEY = dart_key

        # [하위호환] 가상투자는 2026-08-26부터 mode 1 이다(옛 mode 4). 외부 스크립트
        #  (cron·systemd·alias)에 남은 `--mode 4` 를 여기서 흡수한다. 흡수하지 않으면
        #  아래 분기에서 알 수 없는 모드가 되어 기동이 중단된다 — 운영이 조용히 멈추는
        #  것보다는 옛 번호를 받아 주고 경고하는 쪽이 낫다.
        if mode is not None and str(mode) == '4':
            _config().console.print(
                "[bold yellow]⚠️ `--mode 4`는 옛 번호입니다. 가상투자는 이제 mode 1 입니다 — "
                "이번 실행은 mode 1로 진행합니다. 실행 스크립트를 갱신하세요.[/bold yellow]")
            mode = '1'

        # 2. 모드 설정 (CLI 인자 -> 사용자 입력)
        if mode is None:
            _config().console.print("\n접속할 서버를 선택하세요:")
            _config().console.print("[1] 가상투자 (KIS Paper Trading)")
            _config().console.print("[2] 한투증권 (KIS Real Trading)")
            _config().console.print("[3] 토스증권 (Toss Real Trading)")
            mode = Prompt.ask("\n선택 (종료: q)", choices=["1", "2", "3", "q"], default="1")
            if mode == 'q': sys.exit()

        # 모드가 확정된 지점. 아래 분기들은 가상투자처럼 중간에 return 하므로 여기서 기록한다.
        self.mode = str(mode)

        # [모드별 설정 프로필] 모드가 정해지는 즉시 설정 파일을 다시 건다.
        #  실전은 dynamic_config.json 만, 그 외 모드는 거기에 자기 프로필 파일을 얹는다.
        #  이 한 줄이 '관찰모드에서 끈 안전장치가 실전으로 넘어오는' 경로를 끊는다
        #  (config.py 모드별 설정 프로필 주석 참조).
        _config().set_config_profile(_config().profile_for_mode(mode))

        if mode == '1':
            # [가상투자] KIS 실전 서버에서 '시세만' 받고, 잔고·예수금·주문은 paper_broker의
            #  가상 계좌로 대체한다. 실주문은 api.place_order 최상단 하드 가드가 원천 차단한다.
            #
            #  [왜 KIS인가] 종전에는 토스를 썼는데, 그러면 mode 2(실전)와 판단 근거가 달라진다 —
            #  체결강도 미제공(매도잔량비로 대체)·지수는 tvDatafeed·일봉은 pykrx/FDR·웹소켓 없음.
            #  검증 조건이 운용 조건과 다르면 검증 결과가 이전되지 않는다. KIS로 붙이면
            #  mode 2와 완전히 같은 데이터 경로를 탄다.
            #
            #  [왜 별도 앱키인가] KIS의 TPS(20)·웹소켓 동시 연결(1)·토큰 발급(1분 1회) 제약이
            #  전부 앱키 단위다. 실전 인스턴스와 키를 공유하면 두 프로세스가 서로를 모른 채
            #  각자 18TPS로 밀어(합계 36) 양쪽 다 EGW00201에 갇히고, 웹소켓은 서로 끊는다.
            #  즉 가상투자가 실계좌의 주문 경로를 갉아먹는다. VIRT_APP_KEY로 분리한다.
            self.is_toss = False
            self.is_paper = True
            self.url_base = _config().REAL_URL

            # 시세 조회용 키 = VIRT_. 토큰 종류는 'REAL'을 쓰므로 real_* 슬롯에도 넣는다
            #  (get_current_token → get_real_access_token → real_app_key 경로).
            self.app_key = self.virt_app_key
            self.app_secret = self.virt_app_secret
            self.real_app_key = self.virt_app_key
            self.real_app_secret = self.virt_app_secret
            self.auto_app_key = self.virt_app_key
            self.auto_app_secret = self.virt_app_secret

            # [중요] 계좌번호는 실계좌를 쓰지 않는다. VIRT 앱키에도 실제 계좌가 매여 있으므로
            #  실 계좌번호를 넣으면 '가상투자'인데 실 잔고를 읽게 된다. 'PAPER'로 두면 혹시
            #  가로채기를 빠져나간 계좌성 호출이 있어도 조용히 성공하지 않고 실패한다(fail-safe).
            self.cano = "PAPER"
            self.acnt_prdt_cd = ""
            self.auto_cano = self.cano
            self.auto_acnt_prdt_cd = self.acnt_prdt_cd
            self.hts_id = ""          # 체결통보 WS 구독 안 함(가상 체결이라 통보 대상이 없다)

            self._activate_paper_mode()

            if not self.virt_app_key or not self.virt_app_secret:
                _config().console.print(
                    "[bold red]⚠️ 경고: 가상투자용 API Key(VIRT_APP_KEY/VIRT_APP_SECRET)가 "
                    "환경변수에 설정되지 않았습니다. 시세 조회가 실패합니다.[/bold red]")
            elif self.real_app_key and self.virt_app_key == os.environ.get("REAL_APP_KEY", ""):
                # 분리의 목적이 사라지는 설정이라 조용히 넘기지 않는다.
                _config().console.print(
                    "[bold yellow]⚠️ 경고: VIRT_APP_KEY가 REAL_APP_KEY와 같습니다. "
                    "실전 인스턴스와 TPS·웹소켓·토큰을 공유하게 되어 양쪽 모두 불안정해집니다.[/bold yellow]")

            key_status = "OK" if self.virt_app_key and self.virt_app_secret else "MISSING"
            _config().console.print("\n[dim cyan][가상투자 · 시세 소스 한국투자증권(실전)] 설정 로드 확인[/dim cyan]")
            _config().console.print(f"[dim]   - VIRT_APP_KEY 상태: {key_status}[/dim]")
            acc_disp = (f"{self.virt_cano}-{self.virt_acnt_prdt_cd}" if self.virt_acnt_prdt_cd
                        else self.virt_cano) if self.virt_cano else "미설정(VIRT_ACC_NUM)"
            _config().console.print("[dim]   - 계좌: 가상(PAPER) — 실계좌 조회·주문 없음[/dim]")
            _config().console.print(f"[dim]   - 표시용 계좌번호: {acc_disp} (알림 꼬리말 식별용)[/dim]")
            return
        elif mode == '3':
            # [추가] 토스증권 모드 (실전). 토스 API가 제공하는 기능만 사용한다.
            self.is_toss = True
            self.is_paper = False
            self.url_base = _config().TOSS_URL

            # 화면 표시용 계좌번호 (accountSeq는 preflight에서 토큰 발급 후 /accounts로 해석)
            self.cano = self.toss_acc_num
            self.acnt_prdt_cd = ""

            # [중요] 토스는 단일 주식계좌만 제공한다. 시스템 트레이딩용 '자동' 계좌 개념이
            # 없으므로, 시스템 트레이딩(메뉴 5) 계좌를 거래 계좌와 동일하게 동기화한다.
            #   - auto_cano 기반 분기가 토스 계좌를 가리키게 함
            #   - auto_cano == cano 이므로 자산/잔고/미체결 화면의 중복 계좌 표시(auto_cano != cano 조건)는 발생하지 않음
            self.auto_cano = self.cano
            self.auto_acnt_prdt_cd = self.acnt_prdt_cd
            self.auto_app_key = ""
            self.auto_app_secret = ""

            _config().console.print("\n[bold magenta]토스증권 환경을 로드했습니다. (실제 자산 거래 주의)[/bold magenta]")

            if not self.toss_app_key or not self.toss_app_secret:
                _config().console.print("[bold red]⚠️ 경고: 토스 API Key(TOSS_APP_KEY) 또는 Secret이 환경변수에 설정되지 않았습니다.[/bold red]")
        elif mode == '2':
            self.url_base = _config().REAL_URL
            # 실전 모드일 경우 기본 키를 실전용으로 교체
            if self.real_app_key:
                self.app_key = self.real_app_key
                self.app_secret = self.real_app_secret
            
            # [수정] 실전투자 계좌 우선 적용
            if real_cano:
                self.cano = real_cano
                self.acnt_prdt_cd = real_acnt

            self.hts_id = real_hts or common_hts  # 체결통보 WS 구독키

            _config().console.print("\n[bold red]한투증권 서버 환경을 로드했습니다. (실제 자산 거래 주의)[/bold red]")
            
            # [추가] 실전투자 키 누락 확인 (환경변수)
            if not self.app_key or not self.app_secret:
                _config().console.print("[bold red]⚠️ 경고: 한투증권용 API Key(REAL_APP_KEY)가 환경변수에 설정되지 않았습니다.[/bold red]")
        else:
            # [fail-closed] 알 수 없는 모드는 **실전으로 떨어뜨리지 않는다**. 종전에는 이 자리가
            #  `else: 한투증권 실전`이어서, 오타나 폐기된 번호(`--mode 4` 등)가 실계좌 주문
            #  경로로 조용히 이어졌다. 모르는 값이면 아무것도 하지 않고 멈추는 쪽이 맞다.
            _config().console.print(
                f"\n[bold red]알 수 없는 투자 모드입니다: {mode!r}[/bold red]\n"
                "[dim]사용 가능한 모드 — 1: 가상투자 · 2: 한투증권(실전) · 3: 토스증권(실전)[/dim]")
            sys.exit(1)


        # [추가] 토스 모드: KIS식 계좌 입력/표시를 건너뛰고 별도 안내
        if self.is_toss:
            key_status = "OK" if self.toss_app_key and self.toss_app_secret else "MISSING"
            # 가상투자(mode 1)는 KIS 실전 시세를 쓰므로 이 분기에 들어오지 않는다(is_toss=False).
            _config().console.print("\n[dim magenta][토스증권] 설정 로드 확인[/dim magenta]")
            _config().console.print(f"[dim]   - TOSS_APP_KEY 상태: {key_status}[/dim]")
            _config().console.print(f"[dim]   - 계좌번호(TOSS_ACC_NUM): {self.toss_acc_num or '(미지정 → 첫 계좌 사용)'}[/dim]")
            return

        # [추가] 계좌 정보 누락 시 사용자 입력 요청
        if not self.cano:
            _config().console.print("\n[bold yellow]⚠️ 계좌번호(CANO)가 설정되지 않았습니다.[/bold yellow]")
            self.cano = Prompt.ask("계좌번호 앞 8자리 입력")

        if not self.acnt_prdt_cd and self.cano:
            self.acnt_prdt_cd = Prompt.ask("계좌 상품코드(2자리) 입력", default="01")

        # [추가] 로드된 설정 정보 확인 메시지 출력
        key_status = "OK" if self.app_key and self.app_secret else "MISSING"
        _config().console.print("\n[dim cyan][한투증권] 설정 로드 확인[/dim cyan]")
        _config().console.print(f"[dim]   - APP_KEY 상태: {key_status}[/dim]")
        _config().console.print(f"[dim]   - 적용 계좌번호: {self.cano}-{self.acnt_prdt_cd}[/dim]")
        if self.auto_cano:
            _config().console.print(f"[dim]   - 자동매매 계좌: {self.auto_cano}-{self.auto_acnt_prdt_cd}[/dim]")

    def _activate_paper_mode(self):
        """관찰 모드 활성화 — DB 분리 · 가상 계좌 개설 · 외부 연동 차단.

        폐기된 KIS 모의투자(2 TPS·3개월 계좌 리셋)를 대신해 장기 관찰을 맡는 모드다.
        시세·차트·지표·스코어링·필터·청산 판정은 토스 실전과 100% 동일하게 돌고,
        잔고·예수금·주문만 paper_broker의 가상 계좌로 대체된다.
        """
        from modules import db_manager, paper_broker

        # 1) 실계좌 DB와 파일 분리 (trailing_stops·half_tp_status 오염 방지)
        db_manager.db.switch_path(_config().PAPER_DB_FILE_PATH)
        paper_broker.init_tables()

        # 2) 매매일지 웹 연동 — 스위치(메뉴 0 → 5-3)가 정한다. 여기서 강제로 내리지 않는다.
        #    설정은 모드별 프로필로 갈리므로(dynamic_config.paper.json) 가상에서 켠 것이
        #    실전으로 새지 않고, 나가는 건은 isSimulated=true · botId `...:paper:PAPER` 로
        #    실려 서버에서 실거래 기록·통계와 분리 보관된다.
        journal_on = bool(getattr(_config().settings, 'JOURNAL_SYNC_USE', False))
        journal_note = ("전송 (isSimulated=true 로 분리 기록)" if journal_on
                        else "미전송 (메뉴 0 → 5-3 에서 켤 수 있음)")

        seed = paper_broker.get_seed()
        cash = paper_broker.get_cash()
        started = paper_broker._get_state('started_at', '-')
        _config().console.print("\n[bold cyan]가상투자(Paper Trading) 환경을 로드했습니다.[/bold cyan]")
        _config().console.print("[dim]   - 시세·지표: 토스증권 실전과 동일[/dim]")
        _config().console.print("[dim]   - 주문: 가상 체결 (실주문 원천 차단)[/dim]")
        _config().console.print(f"[dim]   - 가상 시드: {seed:,.0f}원 / 현재 현금: {cash:,.0f}원 (개설 {started})[/dim]")
        _config().console.print(f"[dim]   - 데이터: {_config().PAPER_DB_FILE_PATH} (실계좌 DB와 분리)[/dim]")
        _config().console.print(f"[dim]   - 매매일지 웹 연동: {journal_note}[/dim]")

    def load_stock_config(self):
        #  jsonio 를 쓴다 — 관심종목도 상태 파일이다. 손상되면 원본을 격리하고 알린 뒤
        #  빈 목록으로 계속한다(그래야 다음 저장이 깨진 원본을 덮지 않는다).
        empty = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
        self.stock_data = jsonio.load_json(_config().STOCK_DATA_FILE, default=None) or empty
            
        # 거래소 캐시 초기화
        self.exchange_cache = {}
        for group in self.stock_data.values():
            for item in group:
                if 'exchange' in item:
                    self.exchange_cache[item['code']] = item['exchange']

    def save_stock_config(self, data):
        self.stock_data = data
        #  원자적 저장(core/jsonio.save_json). 종전에는 파일을 먼저 비우고 써서,
        #  쓰는 도중 프로세스가 죽으면 관심종목이 통째로 반쪽 JSON 이 됐다.
        if not jsonio.save_json(_config().STOCK_DATA_FILE, data):
            _config().console.print("[red]종목 설정 저장 실패 (상세는 로그 참조)[/red]")

    def update_cache_and_save(self, code, exchange):
        self.exchange_cache[code] = exchange
        # 메모리 데이터 업데이트
        updated = False
        for group_key in self.stock_data:
            for item in self.stock_data[group_key]:
                if item['code'] == code:
                    item['exchange'] = exchange
                    updated = True
        # 파일 저장
        if updated:
            self.save_stock_config(self.stock_data)

    # [추가] 토큰 캐시 관리 메서드
    def _load_token_cache(self):
        return jsonio.load_json(_config().TOKEN_CACHE_FILE, default={}) or {}

    def _save_token_cache(self, cache_data):
        jsonio.save_json(_config().TOKEN_CACHE_FILE, cache_data, indent=2)

    def _token_app_key(self, key):
        """토큰 슬롯을 발급한 앱키. 앱키 개념이 없는 슬롯(TOSS)은 None."""
        if key == "REAL": return self.real_app_key
        if key == "AUTO": return self.auto_app_key
        return None

    @staticmethod
    def _app_key_fingerprint(app_key):
        """앱키를 캐시에 남길 수 있는 형태로 축약한다. 키 자체는 절대 파일에 쓰지 않는다."""
        if not app_key: return None
        return hashlib.sha256(app_key.encode('utf-8')).hexdigest()[:16]

    def _check_token_validity(self, token_info, key=None):
        """만료 시각과 **발급 앱키**가 모두 맞아야 유효하다.

        [중요] 만료만 보면 안 된다. token_cache.json은 파일 하나이고 슬롯 이름이
          REAL/AUTO 뿐인데, 가상투자(mode 1)는 real_app_key·auto_app_key를
          VIRT_APP_KEY로 덮어쓴다(아래 load 로직). 즉 가상투자가 VIRT 키로 받은 토큰이
          'REAL' 슬롯에 남는다. 만료 전(최대 24시간)에 mode 2로 바꾸면 그 토큰을
          '아직 유효함'으로 판정해 남의 앱키 토큰을 그대로 쓰고, KIS는 이를 거부한다.
          앱키 지문을 함께 검사하면 슬롯이 겹쳐도 서로의 토큰을 집지 않는다.
          (앱키를 교체했을 때 캐시가 조용히 낡는 문제도 같은 검사로 함께 막힌다)
        """
        if not token_info: return False
        expired_str = token_info.get('token_expired')
        access_token = token_info.get('access_token')
        if not expired_str or not access_token: return False

        if key is not None:
            expected_fp = self._app_key_fingerprint(self._token_app_key(key))
            if expected_fp and token_info.get('app_key_fp') != expected_fp:
                # 지문이 없거나(구버전 캐시) 다르면 남의 토큰으로 보고 재발급시킨다.
                return False
        
        try:
            expired_dt = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")
            # 만료 1분 전까지만 유효한 것으로 간주
            if datetime.now() < (expired_dt - timedelta(minutes=1)):
                return True
        except Exception: return False
        return False

    def get_valid_token(self, key, force_disk_reload=False):
        """메모리 또는 파일 캐시에서 **유효한** 토큰을 반환. 없으면 None.

        [만료를 보게 한다 · 2026-09-05] 종전 메모리 경로는 문자열이 비어 있지 않으면
         그대로 돌려줬다. 파일 경로는 _check_token_validity 로 만료와 앱키 지문을 모두
         보는데, 메모리 경로만 아무것도 안 봤다 — 함수 이름과 독스트링('유효한')이
         구현과 어긋나 있었다.

         한 번 메모리에 담긴 토큰은 프로세스가 사는 동안 영원히 유효로 읽힌다. KIS 토큰
         수명은 24시간이고 운영은 라즈베리파이 24시간 구동이라 **반드시 도달한다**.
         그때 만료 감지는 오직 사후적이다 — API 가 EGW00123 을 돌려주면 그제야
         TOKEN_EXPIRED 플래그가 서고 예외가 난다(api/http). 즉 만료 경계에서 나가던
         호출이 먼저 한 번 실패하고, 하필 그것이 손절 주문이면 그 주문이 실패한다.
         메모리에서도 만료를 보면 그 창이 사라진다(만료 1분 전에 미리 재발급).
        """
        # 1. 메모리 확인 (force_disk_reload가 아닐 때만)
        if not force_disk_reload:
            token = self.real_access_token if key == "REAL" else (
                self.auto_access_token if key == "AUTO" else "")
            if token:
                expired_str = (self.real_token_expired if key == "REAL"
                               else self.auto_token_expired)
                if self._memory_token_alive(expired_str):
                    return token
                #  만료됐다 — 파일 캐시(다른 프로세스가 갱신했을 수 있다)로 내려간다.
                self._update_memory_token(key, "", "")

        # 2. 파일 캐시 확인
        cache = self._load_token_cache()
        token_info = cache.get(key)
        if self._check_token_validity(token_info, key):
            token = token_info['access_token']
            self._update_memory_token(key, token, token_info.get('token_expired'))
            return token
        return None

    @staticmethod
    def _memory_token_alive(expired_str):
        """메모리 토큰이 아직 유효한가. 만료 시각을 모르면 **모른다 = 유효하지 않다**.

        파일 경로와 같은 1분 여유를 둔다 — 지금 유효해도 요청이 나가는 사이에 넘어가면
        같은 실패다.
        """
        if not expired_str:
            return False
        try:
            expired_dt = datetime.strptime(str(expired_str), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return False
        return datetime.now() < (expired_dt - timedelta(minutes=1))

    def set_token(self, key, token, expired):
        """토큰을 메모리와 파일에 저장"""
        self._update_memory_token(key, token, expired)
        
        cache = self._load_token_cache()
        cache[key] = {
            "access_token": token,
            "token_expired": expired,
            "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "app_key_fp": self._app_key_fingerprint(self._token_app_key(key)),
        }
        self._save_token_cache(cache)

    def is_token_recently_issued(self, key, seconds=60):
        """토큰이 지정된 시간(초) 이내에 발급되었는지 확인.

        KIS의 발급 빈도 제한(EGW00133)은 **앱키 단위**다. 다른 앱키가 남긴 발급 시각을
        보고 재발급을 미루면, 정작 이 앱키로는 토큰이 없는 채로 대기하게 된다.
        """
        cache = self._load_token_cache()
        token_info = cache.get(key)
        if not token_info: return False

        expected_fp = self._app_key_fingerprint(self._token_app_key(key))
        if expected_fp and token_info.get('app_key_fp') != expected_fp:
            return False
        
        issued_at_str = token_info.get('issued_at')
        if not issued_at_str: return False
        
        try:
            issued_dt = datetime.strptime(issued_at_str, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - issued_dt).total_seconds() < seconds:
                return True
        except Exception: pass
        return False

    def _update_memory_token(self, key, token, expired=None):
        """메모리 토큰과 **그 만료 시각**을 함께 둔다.

        만료를 같이 두지 않으면 get_valid_token 의 메모리 경로가 만료를 볼 수 없다.
        expired 를 모르면 빈 값으로 둔다 — 그러면 '모름 = 유효하지 않음'이 되어
        다음 조회가 파일 캐시로 내려간다(느려질 뿐 틀리지 않는다).
        """
        if key == "REAL":
            self.real_access_token = token
            self.real_token_expired = expired or ""
        elif key == "AUTO":
            self.auto_access_token = token
            self.auto_token_expired = expired or ""