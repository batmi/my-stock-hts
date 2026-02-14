import os
import sys
import json
from datetime import datetime, timedelta
from rich.prompt import Prompt
import config

class SessionManager:
    def __init__(self):
        self.is_simulation = False
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
        
        # 텔레그램 설정
        self.telegram_token = ""
        self.telegram_chat_id = ""
        self.telegram_instance_name = "HTS"
        self.telegram_polling_timeout = 10
        self.enable_telegram = True
        
        # 토큰 관리 (API 모듈에서 사용)
        self.sim_access_token = ""
        self.real_access_token = ""
        self.auto_access_token = ""

        self.stock_data = {}
        self.exchange_cache = {}

    def initialize(self, mode=None):
        # 1. 시크릿 파일 로드 (없으면 환경변수 사용)
        secret_file = os.path.join(config.JSON_DIR, "secret.json")
        secrets = {}
        if os.path.exists(secret_file):
            try:
                with open(secret_file, 'r', encoding='utf-8') as f:
                    secrets = json.load(f)
            except: pass

        # [추가] 계좌번호 파싱 헬퍼 (XXXX-XX 형식 지원)
        def parse_acc(acc_str):
            if not acc_str: return "", ""
            if '-' in acc_str:
                parts = acc_str.split('-')
                return parts[0].strip(), parts[1].strip()
            return acc_str.strip(), ""

        self.app_key = secrets.get("APP_KEY") or os.environ.get("APP_KEY", "")
        self.app_secret = secrets.get("APP_SECRET") or os.environ.get("APP_SECRET", "")
        
        # [수정] 기본 CANO 로드 (Fallback용)
        default_cano = secrets.get("CANO") or os.environ.get("CANO", "")
        default_acnt = secrets.get("ACNT_PRDT_CD") or os.environ.get("ACNT_PRDT_CD", "")
        
        self.real_app_key = secrets.get("REAL_APP_KEY") or os.environ.get("REAL_APP_KEY", "")
        self.real_app_secret = secrets.get("REAL_APP_SECRET") or os.environ.get("REAL_APP_SECRET", "")
        
        self.auto_app_key = secrets.get("AUTO_APP_KEY") or os.environ.get("AUTO_APP_KEY", "")
        self.auto_app_secret = secrets.get("AUTO_APP_SECRET") or os.environ.get("AUTO_APP_SECRET", "")
        
        # [수정] 환경변수 *_ACC_NUM 지원 및 파싱
        sim_acc_str = secrets.get("SIM_ACC_NUM") or os.environ.get("SIM_ACC_NUM", "")
        real_acc_str = secrets.get("REAL_ACC_NUM") or os.environ.get("REAL_ACC_NUM", "")
        auto_acc_str = secrets.get("AUTO_ACC_NUM") or os.environ.get("AUTO_ACC_NUM", "")
        
        sim_cano, sim_acnt = parse_acc(sim_acc_str)
        real_cano, real_acnt = parse_acc(real_acc_str)
        auto_cano_parsed, auto_acnt_parsed = parse_acc(auto_acc_str)
        
        # 자동매매 계좌 설정 (우선순위: AUTO_ACC_NUM > AUTO_CANO)
        if auto_cano_parsed:
            self.auto_cano = auto_cano_parsed
            self.auto_acnt_prdt_cd = auto_acnt_parsed
        else:
            self.auto_cano = secrets.get("AUTO_CANO") or os.environ.get("AUTO_CANO", "")
            self.auto_acnt_prdt_cd = secrets.get("AUTO_ACNT_PRDT_CD") or os.environ.get("AUTO_ACNT_PRDT_CD", "")
        
        self.telegram_token = secrets.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = secrets.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
        
        # [추가] 텔레그램 인스턴스 이름 설정
        self.telegram_instance_name = secrets.get("TELEGRAM_INSTANCE_NAME") or os.environ.get("TELEGRAM_INSTANCE_NAME", "HTS")

        # 2. 모드 설정 (CLI 인자 -> 사용자 입력)
        if mode is None:
            config.console.print("\n접속할 서버를 선택하세요:")
            config.console.print("[1] 모의투자 (Simulation)")
            config.console.print("[2] 실전투자 (Real Trading)")
            mode = Prompt.ask("\n선택 (종료: q)", choices=["1", "2", "q"], default="1")
            if mode == 'q': sys.exit()

        if mode == '1':
            self.is_simulation = True
            self.url_base = config.SIM_URL
            
            # [수정] 모의투자 계좌 우선 적용
            if sim_cano:
                self.cano = sim_cano
                self.acnt_prdt_cd = sim_acnt
            else:
                self.cano = default_cano
                self.acnt_prdt_cd = default_acnt
                
            config.console.print("\n[green]모의투자 서버 환경을 로드했습니다.[/green]")
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
            else:
                self.cano = default_cano
                self.acnt_prdt_cd = default_acnt
                
            config.console.print("\n[bold red]실전투자 서버 환경을 로드했습니다. (실제 자산 거래 주의)[/bold red]")
            
        # [추가] 계좌 정보 누락 시 사용자 입력 요청
        if not self.cano:
            config.console.print("\n[bold yellow]⚠️ 계좌번호(CANO)가 설정되지 않았습니다.[/bold yellow]")
            self.cano = Prompt.ask("계좌번호 앞 8자리 입력")
            
        if not self.acnt_prdt_cd and self.cano:
            self.acnt_prdt_cd = Prompt.ask("계좌 상품코드(2자리) 입력", default="01")

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
        try:
            if not os.path.exists(config.TOKEN_CACHE_FILE): return {}
            with open(config.TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception: return {}

    def _save_token_cache(self, cache_data):
        try:
            with open(config.TOKEN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception: pass

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
        except: return False
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

    def _update_memory_token(self, key, token):
        if key == "SIMULATION": self.sim_access_token = token
        elif key == "REAL": self.real_access_token = token
        elif key == "AUTO": self.auto_access_token = token