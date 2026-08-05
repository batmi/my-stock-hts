import os
import sys
import json
import jsonio
from datetime import datetime, timedelta
from rich.prompt import Prompt
import config

class SessionManager:
    def __init__(self):
        self.is_simulation = False
        self.is_toss = False  # [추가] 토스증권 모드 여부
        # [추가] 관찰(페이퍼 트레이딩) 모드 여부. 시세·지표는 실제 소스(KIS 실전)를 그대로 쓰고
        #  잔고·예수금·주문만 가상으로 처리한다(modules/paper_broker.py).
        #  모의투자 계좌의 3개월 리셋 없이 장기 관찰하기 위한 모드이며, 실주문은 원천 차단된다.
        self.is_paper = False
        self.url_base = ""
        
        # 현재 활성 계좌 정보 (모드에 따라 변경됨)
        self.app_key = ""
        self.app_secret = ""
        self.cano = ""
        self.acnt_prdt_cd = ""
        
        # 모의투자 정보 저장
        self.sim_app_key = ""
        self.sim_app_secret = ""
        
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

        # [추가] 가상투자(mode 4) 전용 KIS 앱키. 실전 인스턴스와 앱키를 분리하기 위한 것.
        self.virt_app_key = ""
        self.virt_app_secret = ""

        # [추가] 실시간 체결통보(WebSocket H0STCNI0/H0STCNI9) 구독키 = HTS 로그인 ID.
        #   환경변수 우선순위: 모드별(REAL_HTS_ID/SIM_HTS_ID) → 공통(KIS_HTS_ID/HTS_ID).
        #   미설정 시 체결통보 WS는 구독하지 않고 기존 REST 폴링(ConclusionMonitor)으로 폴백한다.
        self.hts_id = ""

        # 토큰 관리 (API 모듈에서 사용)
        self.sim_access_token = ""
        self.real_access_token = ""
        self.auto_access_token = ""

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

        # 1. 환경변수 로드 (SIM_, REAL_, AUTO_ 접두사 사용)
        # 모의투자 (SIM_)
        self.sim_app_key = os.environ.get("SIM_APP_KEY", "")
        self.sim_app_secret = os.environ.get("SIM_APP_SECRET", "")
        
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

        # [추가] 가상투자 전용 KIS 앱키 (VIRT_). mode 4가 KIS 실전 서버에서 '시세만' 받을 때 쓴다.
        #  실전 운용 인스턴스와 앱키를 나누는 것이 핵심이다 — KIS의 TPS(20)·웹소켓 동시 연결(1)·
        #  토큰 발급(1분 1회) 제약이 모두 앱키 단위라, 같은 키를 두 프로세스가 쓰면 가상투자가
        #  실계좌의 주문 경로를 갉아먹는다(양쪽 모두 EGW00201에 갇히고 웹소켓은 서로 끊는다).
        self.virt_app_key = os.environ.get("VIRT_APP_KEY", "")
        self.virt_app_secret = os.environ.get("VIRT_APP_SECRET", "")

        # [추가] 체결통보 WebSocket 구독키(HTS ID). 모드별 우선 → 공통 폴백.
        sim_hts = os.environ.get("SIM_HTS_ID", "")
        real_hts = os.environ.get("REAL_HTS_ID", "")
        common_hts = os.environ.get("KIS_HTS_ID", "") or os.environ.get("HTS_ID", "")
        
        # 계좌번호 (ACC_NUM)
        sim_acc_str = os.environ.get("SIM_ACC_NUM", "")
        real_acc_str = os.environ.get("REAL_ACC_NUM", "")
        auto_acc_str = os.environ.get("AUTO_ACC_NUM", "")
        
        # 계좌번호 파싱
        sim_cano, sim_acnt = parse_acc(sim_acc_str)
        real_cano, real_acnt = parse_acc(real_acc_str)
        auto_cano, auto_acnt = parse_acc(auto_acc_str)
        
        # 자동매매 계좌 설정
        self.auto_cano = auto_cano
        self.auto_acnt_prdt_cd = auto_acnt
        
        # 텔레그램 설정
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if tg_token: config.TELEGRAM_BOT_TOKEN = tg_token

        tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if tg_chat_id: config.TELEGRAM_CHAT_ID = tg_chat_id

        tg_inst = os.environ.get("TELEGRAM_INSTANCE_NAME")
        if tg_inst: config.TELEGRAM_INSTANCE_NAME = tg_inst

        # OpenDART(전자공시) 설정 - KIS/텔레그램 키와 동일하게 런타임 환경변수에서 로드
        # (config.py import 시점뿐 아니라 세션 초기화 시점에도 재로딩하여 일관성 확보)
        dart_key = os.environ.get("DART_API_KEY")
        if dart_key: config.DART_API_KEY = dart_key

        # 2. 모드 설정 (CLI 인자 -> 사용자 입력)
        if mode is None:
            config.console.print("\n접속할 서버를 선택하세요:")
            config.console.print("[1] 모의투자 (Simulation)")
            config.console.print("[2] 한투증권 (KIS, 실전)")
            config.console.print("[3] 토스증권 (Toss, 실전)")
            config.console.print("[4] 가상투자 (Paper Trading)")
            mode = Prompt.ask("\n선택 (종료: q)", choices=["1", "2", "3", "4", "q"], default="1")
            if mode == 'q': sys.exit()

        if mode == '1':
            self.is_simulation = True
            self.url_base = config.SIM_URL
            
            # 모의투자 키 적용
            if self.sim_app_key:
                self.app_key = self.sim_app_key
                self.app_secret = self.sim_app_secret
            
            # [수정] 모의투자 계좌 우선 적용
            if sim_cano:
                self.cano = sim_cano
                self.acnt_prdt_cd = sim_acnt
            
            # [추가] 모의투자 모드: 시스템 트레이딩 계좌 = 모의투자 계좌 강제 동기화
            self.auto_cano = self.cano
            self.auto_acnt_prdt_cd = self.acnt_prdt_cd
            self.auto_app_key = self.app_key
            self.auto_app_secret = self.app_secret
            self.hts_id = sim_hts or common_hts  # 체결통보 WS 구독키

            config.console.print("\n[green]모의투자 서버 환경을 로드했습니다.[/green]")
            
            # [추가] 모의투자 키 누락 확인 (환경변수)
            if not self.app_key or not self.app_secret:
                config.console.print("[bold red]⚠️ 경고: 모의투자용 API Key(SIM_APP_KEY) 또는 Secret이 환경변수에 설정되지 않았습니다.[/bold red]")
        elif mode == '4':
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
            self.is_simulation = False
            self.is_toss = False
            self.is_paper = True
            self.url_base = config.REAL_URL

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
                config.console.print(
                    "[bold red]⚠️ 경고: 가상투자용 API Key(VIRT_APP_KEY/VIRT_APP_SECRET)가 "
                    "환경변수에 설정되지 않았습니다. 시세 조회가 실패합니다.[/bold red]")
            elif self.real_app_key and self.virt_app_key == os.environ.get("REAL_APP_KEY", ""):
                # 분리의 목적이 사라지는 설정이라 조용히 넘기지 않는다.
                config.console.print(
                    "[bold yellow]⚠️ 경고: VIRT_APP_KEY가 REAL_APP_KEY와 같습니다. "
                    "실전 인스턴스와 TPS·웹소켓·토큰을 공유하게 되어 양쪽 모두 불안정해집니다.[/bold yellow]")

            key_status = "OK" if self.virt_app_key and self.virt_app_secret else "MISSING"
            config.console.print("\n[dim cyan][가상투자 · 시세 소스 한국투자증권(실전)] 설정 로드 확인[/dim cyan]")
            config.console.print(f"[dim]   - VIRT_APP_KEY 상태: {key_status}[/dim]")
            config.console.print("[dim]   - 계좌: 가상(PAPER) — 실계좌 조회·주문 없음[/dim]")
            return
        elif mode == '3':
            # [추가] 토스증권 모드 (실전). 토스 API가 제공하는 기능만 사용한다.
            self.is_simulation = False
            self.is_toss = True
            self.is_paper = False
            self.url_base = config.TOSS_URL

            # 화면 표시용 계좌번호 (accountSeq는 preflight에서 토큰 발급 후 /accounts로 해석)
            self.cano = self.toss_acc_num
            self.acnt_prdt_cd = ""

            # [중요] 토스는 단일 주식계좌만 제공한다. 시스템 트레이딩용 '자동' 계좌 개념이
            # 없으므로, 시스템 트레이딩(메뉴 5) 계좌를 거래 계좌와 동일하게 동기화한다.
            #   - auto_cano 기반 분기(target = auto_cano if not is_simulation ...)가 토스 계좌를 가리키게 함
            #   - auto_cano == cano 이므로 자산/잔고/미체결 화면의 중복 계좌 표시(auto_cano != cano 조건)는 발생하지 않음
            # (모의투자 모드와 동일한 단일계좌 동기화 방식)
            self.auto_cano = self.cano
            self.auto_acnt_prdt_cd = self.acnt_prdt_cd
            self.auto_app_key = ""
            self.auto_app_secret = ""

            config.console.print("\n[bold magenta]토스증권 환경을 로드했습니다. (실제 자산 거래 주의)[/bold magenta]")

            if not self.toss_app_key or not self.toss_app_secret:
                config.console.print("[bold red]⚠️ 경고: 토스 API Key(TOSS_APP_KEY) 또는 Secret이 환경변수에 설정되지 않았습니다.[/bold red]")
        else:
            self.is_simulation = False
            self.url_base = config.REAL_URL
            # 실전 모드일 경우 기본 키를 실전용으로 교체
            if self.real_app_key:
                self.app_key = self.real_app_key
                self.app_secret = self.real_app_secret
            
            # [수정] 실전투자 계좌 우선 적용
            if real_cano:
                self.cano = real_cano
                self.acnt_prdt_cd = real_acnt

            self.hts_id = real_hts or common_hts  # 체결통보 WS 구독키

            config.console.print("\n[bold red]한투증권 서버 환경을 로드했습니다. (실제 자산 거래 주의)[/bold red]")
            
            # [추가] 실전투자 키 누락 확인 (환경변수)
            if not self.app_key or not self.app_secret:
                config.console.print("[bold red]⚠️ 경고: 한투증권용 API Key(REAL_APP_KEY)가 환경변수에 설정되지 않았습니다.[/bold red]")
            
        # [추가] 토스 모드: KIS식 계좌 입력/표시를 건너뛰고 별도 안내
        if self.is_toss:
            key_status = "OK" if self.toss_app_key and self.toss_app_secret else "MISSING"
            # mode 4(가상투자)는 KIS 실전 시세를 쓰므로 이 분기에 들어오지 않는다(is_toss=False).
            config.console.print("\n[dim magenta][토스증권] 설정 로드 확인[/dim magenta]")
            config.console.print(f"[dim]   - TOSS_APP_KEY 상태: {key_status}[/dim]")
            config.console.print(f"[dim]   - 계좌번호(TOSS_ACC_NUM): {self.toss_acc_num or '(미지정 → 첫 계좌 사용)'}[/dim]")
            return

        # [추가] 계좌 정보 누락 시 사용자 입력 요청
        if not self.cano:
            config.console.print("\n[bold yellow]⚠️ 계좌번호(CANO)가 설정되지 않았습니다.[/bold yellow]")
            self.cano = Prompt.ask("계좌번호 앞 8자리 입력")

        if not self.acnt_prdt_cd and self.cano:
            self.acnt_prdt_cd = Prompt.ask("계좌 상품코드(2자리) 입력", default="01")

        # [추가] 로드된 설정 정보 확인 메시지 출력
        key_status = "OK" if self.app_key and self.app_secret else "MISSING"
        env_label = "모의투자" if self.is_simulation else "한투증권"
        config.console.print(f"\n[dim cyan][{env_label}] 설정 로드 확인[/dim cyan]")
        config.console.print(f"[dim]   - APP_KEY 상태: {key_status}[/dim]")
        config.console.print(f"[dim]   - 적용 계좌번호: {self.cano}-{self.acnt_prdt_cd}[/dim]")
        if self.auto_cano:
            config.console.print(f"[dim]   - 자동매매 계좌: {self.auto_cano}-{self.auto_acnt_prdt_cd}[/dim]")

    def _activate_paper_mode(self):
        """관찰 모드 활성화 — DB 분리 · 가상 계좌 개설 · 외부 연동 차단.

        모의투자 계좌(mode 1)의 3개월 리셋 없이 장기 관찰하기 위한 모드다.
        시세·차트·지표·스코어링·필터·청산 판정은 토스 실전과 100% 동일하게 돌고,
        잔고·예수금·주문만 paper_broker의 가상 계좌로 대체된다.
        """
        from modules import db_manager, paper_broker

        # 1) 실계좌 DB와 파일 분리 (trailing_stops·half_tp_status 오염 방지)
        db_manager.db.switch_path(config.PAPER_DB_FILE_PATH)
        paper_broker.init_tables()

        # 2) 매매일지 웹 연동 차단 — 가상 체결이 실제 매매 기록에 섞이면 안 된다.
        config.settings.JOURNAL_SYNC_USE = False

        seed = paper_broker.get_seed()
        cash = paper_broker.get_cash()
        started = paper_broker._get_state('started_at', '-')
        config.console.print("\n[bold cyan]가상투자(Paper Trading) 환경을 로드했습니다.[/bold cyan]")
        config.console.print("[dim]   - 시세·지표: 토스증권 실전과 동일[/dim]")
        config.console.print("[dim]   - 주문: 가상 체결 (실주문 원천 차단)[/dim]")
        config.console.print(f"[dim]   - 가상 시드: {seed:,.0f}원 / 현재 현금: {cash:,.0f}원 (개설 {started})[/dim]")
        config.console.print(f"[dim]   - 데이터: {config.PAPER_DB_FILE_PATH} (실계좌 DB와 분리)[/dim]")
        config.console.print("[dim]   - 매매일지 웹 연동: 자동 차단[/dim]")

    def load_stock_config(self):
        if os.path.exists(config.STOCK_DATA_FILE):
            try:
                with open(config.STOCK_DATA_FILE, 'r', encoding='utf-8') as f:
                    self.stock_data = json.load(f)
            except Exception as e:
                config.console.print(f"[red]종목 설정 로드 실패: {e}[/red]")
                self.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
        else:
            self.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
            
        # 거래소 캐시 초기화
        self.exchange_cache = {}
        for group in self.stock_data.values():
            for item in group:
                if 'exchange' in item:
                    self.exchange_cache[item['code']] = item['exchange']

    def save_stock_config(self, data):
        self.stock_data = data
        try:
            with open(config.STOCK_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            config.console.print(f"[red]종목 설정 저장 실패: {e}[/red]")

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
        return jsonio.load_json(config.TOKEN_CACHE_FILE, default={}) or {}

    def _save_token_cache(self, cache_data):
        jsonio.save_json(config.TOKEN_CACHE_FILE, cache_data, indent=2)

    def _check_token_validity(self, token_info):
        if not token_info: return False
        expired_str = token_info.get('token_expired')
        access_token = token_info.get('access_token')
        if not expired_str or not access_token: return False
        
        try:
            expired_dt = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")
            # 만료 1분 전까지만 유효한 것으로 간주
            if datetime.now() < (expired_dt - timedelta(minutes=1)):
                return True
        except Exception: return False
        return False

    def get_valid_token(self, key, force_disk_reload=False):
        """메모리 또는 파일 캐시에서 유효한 토큰을 반환"""
        # 1. 메모리 확인 (force_disk_reload가 아닐 때만)
        if not force_disk_reload:
            if key == "SIMULATION" and self.sim_access_token: return self.sim_access_token
            if key == "REAL" and self.real_access_token: return self.real_access_token
            if key == "AUTO" and self.auto_access_token: return self.auto_access_token

        # 2. 파일 캐시 확인
        cache = self._load_token_cache()
        token_info = cache.get(key)
        if self._check_token_validity(token_info):
            token = token_info['access_token']
            self._update_memory_token(key, token)
            return token
        return None

    def set_token(self, key, token, expired):
        """토큰을 메모리와 파일에 저장"""
        self._update_memory_token(key, token)
        
        cache = self._load_token_cache()
        cache[key] = {
            "access_token": token,
            "token_expired": expired,
            "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self._save_token_cache(cache)

    def is_token_recently_issued(self, key, seconds=60):
        """토큰이 지정된 시간(초) 이내에 발급되었는지 확인"""
        cache = self._load_token_cache()
        token_info = cache.get(key)
        if not token_info: return False
        
        issued_at_str = token_info.get('issued_at')
        if not issued_at_str: return False
        
        try:
            issued_dt = datetime.strptime(issued_at_str, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - issued_dt).total_seconds() < seconds:
                return True
        except Exception: pass
        return False

    def _update_memory_token(self, key, token):
        if key == "SIMULATION": self.sim_access_token = token
        elif key == "REAL": self.real_access_token = token
        elif key == "AUTO": self.auto_access_token = token