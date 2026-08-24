"""Google GenAI SDK 연결 확인 도구 — 사용 가능한 모델 목록과 실제 생성 호출을 한 번씩 태운다.

[왜 main() 안에 두나] 예전에는 이 코드가 모듈 최상단에 그대로 있었다. 그래서 `import` 만
해도 Gemini API 로 나가 버렸고, tools/ 전체를 훑는 점검 스크립트가 여기서 네트워크를 물고
멈췄다. 진단 도구는 **부를 때만** 움직여야 한다.
"""
import os


def main():
    # 1. 환경 변수에서 API 키 읽기
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: 환경변수 'GEMINI_API_KEY'가 설정되지 않았습니다.")
        return 1

    # SDK 는 여기서 들여온다 — 키가 없으면 import 비용도 지지 않는다.
    from google import genai
    from google.genai import types

    # 2. Gemini 클라이언트 생성 (신 SDK 는 전역 configure() 가 없다)
    client = genai.Client(api_key=api_key)

    print("--- 사용 가능한 모델 목록 ---")
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                print(f"Model Name: {m.name}")
    except Exception as e:
        print(f"모델 목록 조회 실패: {e}")

    # 3. 모델 설정 (환경변수 GEMINI_MODEL 우선, 미설정 시 최신 Flash 사용)
    model_name = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

    print(f"\n--- 모델 테스트 ({model_name}) ---")
    try:
        response = client.models.generate_content(
            model=model_name,
            contents="현재 주식 시장 분석을 위한 간단한 파이썬 코드를 작성해줘.",
            config=types.GenerateContentConfig(
                temperature=0.2,
                top_p=0.95,
                max_output_tokens=8192,
            ),
        )
        print(response.text)
    except Exception as e:
        print(f"모델 테스트 실패: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
