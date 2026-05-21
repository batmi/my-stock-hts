import os
import sys
import google.generativeai as genai

def run_gemini_tool():
    # 1. 환경변수에서 API 키 불러오기
    api_key = os.environ.get("GEMINI_API_KEY")
    
    if not api_key:
        print("Error: 환경변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")
        print("터미널에서 'export GEMINI_API_KEY=your_key_here'를 실행해 주세요.")
        return

    # 2. Gemini API 설정
    genai.configure(api_key=api_key)

    # 3. 모델 설정 (가장 최신인 3.1 Flash-Lite 사용)
    # 분석 위주의 작업을 위해 최적화된 모델입니다.
    model_name = "gemini-3.1-flash-lite"
    
    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={
                "temperature": 0.2,  # 분석 작업이므로 일관성을 위해 낮게 설정
                "top_p": 0.95,
                "max_output_tokens": 8192,
            }
        )
    except Exception as e:
        print(f"모델 초기화 중 오류 발생: {e}")
        return

    print(f"--- Gemini 분석 툴 실행 중 (모델: {model_name}) ---")
    print("종료하려면 'exit' 또는 'quit'을 입력하세요.\n")

    while True:
        # 4. 프롬프트 입력 받기
        user_input = input("분석할 내용을 입력하세요 >>> ")

        if user_input.lower() in ['exit', 'quit']:
            print("도구를 종료합니다.")
            break

        if not user_input.strip():
            continue

        print("\n[분석 중...]")
        
        try:
            # 5. 콘텐츠 생성 및 출력
            response = model.generate_content(user_input)
            
            print("-" * 30)
            print(response.text)
            print("-" * 30 + "\n")
            
        except Exception as e:
            print(f"응답 생성 중 오류가 발생했습니다: {e}\n")

if __name__ == "__main__":
    run_gemini_tool()
