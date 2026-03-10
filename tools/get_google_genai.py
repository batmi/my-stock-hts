import os
import google.generativeai as genai

# 1. 환경 변수에서 API 키 읽기
api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    print("Error: 환경변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")
else:
    # 2. Gemini API 설정
    genai.configure(api_key=api_key)

    print("--- 사용 가능한 모델 목록 ---")
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"Model Name: {m.name}")
    except Exception as e:
        print(f"모델 목록 조회 실패: {e}")

    # 3. 모델 설정 (가장 최신인 3.1 Flash-Lite 사용)
    model_name = "gemini-3.1-flash-lite-preview"
    
    print(f"\n--- 모델 테스트 ({model_name}) ---")
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={
                "temperature": 0.2,
                "top_p": 0.95,
                "max_output_tokens": 8192,
            }
        )
        response = model.generate_content("현재 주식 시장 분석을 위한 간단한 파이썬 코드를 작성해줘.")
        print(response.text)
    except Exception as e:
        print(f"모델 테스트 실패: {e}")
