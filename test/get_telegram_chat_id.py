import requests
import sys

def get_chat_id():
    """
    텔레그램 봇 토큰을 입력받아 Chat ID를 조회하는 유틸리티 스크립트입니다.
    실행 전 텔레그램 앱에서 봇에게 아무 메시지나 하나 보내야 조회가 가능합니다.
    """
    print("=== 텔레그램 Chat ID 조회 도구 ===")
    print("1. 텔레그램 앱에서 생성한 봇에게 'Hello' 등 아무 메시지나 먼저 보내세요.")
    print("2. 아래에 봇 토큰을 입력하세요.")
    
    token = input("\nBot Token 입력: ").strip()
    if not token:
        print("토큰이 입력되지 않았습니다.")
        return

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if not data.get('ok'):
            print(f"\n[오류] API 호출 실패: {data.get('description')}")
            return
            
        results = data.get('result', [])
        if not results:
            print("\n[알림] 수신된 메시지가 없습니다. 봇에게 메시지를 보낸 후 다시 시도해주세요.")
        else:
            # 가장 최근 메시지의 Chat ID 추출
            chat_id = results[-1]['message']['chat']['id']
            user_name = results[-1]['message']['from'].get('first_name', 'Unknown')
            print(f"\n[성공] 감지된 사용자: {user_name}")
            print(f"Your Chat ID: {chat_id}")
            print("\n위 Chat ID를 환경 변수 'TELEGRAM_CHAT_ID'에 설정하세요.")
            
    except Exception as e:
        print(f"\n[오류] 연결 실패: {e}")

if __name__ == "__main__":
    get_chat_id()