import os
from google import genai

# 1. 환경 변수에서 API 키 읽기
api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise ValueError("환경 변수 'GEMINI_API_KEY'를 찾을 수 없습니다.")

# 2. 클라이언트 초기화 (최신 SDK 방식)
client = genai.Client(api_key=api_key)

print("--- 사용 가능한 모델 목록 ---")

try:
    # 3. 모델 목록 가져오기 (수정된 부분: genai.list_models() -> client.models.list())
    for m in client.models.list():
        # 최신 SDK에서는 m.supported_generation_methods 대신 
        # m.name을 직접 확인하거나 전체 정보를 출력할 수 있습니다.
        print(f"Model Name: {m.name}")
        
    #print("\n--- 특정 모델 테스트 실행 ---")
    # 목록에서 확인된 모델 중 하나를 선택하여 테스트 (예: gemini-2.0-flash)
    # 2026년 기준 사용 가능한 모델명을 여기에 입력하세요.
    #response = client.models.generate_content(
    #    model='gemini-2.5-flash', 
    #    contents="현재 주식 시장 분석을 위한 간단한 파이썬 코드를 작성해줘."
    #)
    #print(response.text)

except Exception as e:
    print(f"에러가 발생했습니다: {e}")
