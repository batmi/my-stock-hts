"""차트 갤러리 웹서버 CLI — 로직은 modules/web_dashboard.py 가 갖는다.

[규약] 실행 중인 기능이 이 스크립트를 subprocess 로 띄우지 않는다. HTS 본체는
같은 프로세스의 데몬 스레드로 서버를 돌린다(web_dashboard.start_web_server).
이 파일은 '차트만 따로 띄워 보고 싶을 때' 쓰는 얇은 사용자다.

  python tools/web_server.py                 # config.WEBCHART_PORT 로 기동
  python tools/web_server.py --port 9096     # 포트 지정
  python tools/web_server.py --host 127.0.0.1
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import web_dashboard


def main():
    parser = argparse.ArgumentParser(description="차트 갤러리 웹서버")
    parser.add_argument('--port', type=int, default=config.WEBCHART_PORT)
    parser.add_argument('--host', type=str, default=config.WEBCHART_HOST)
    parser.add_argument('--directory', type=str, default=config.CHART_DIR)
    parser.add_argument('--no-index', action='store_true',
                        help='기동 시 index.html 을 새로 만들지 않는다')
    args = parser.parse_args()

    config.CHART_DIR = args.directory
    if not args.no_index:
        web_dashboard.update_chart_index(args.directory)

    if not web_dashboard.start_web_server(port=args.port, host=args.host):
        return 1

    print(f"Serving charts from {args.directory} on http://{args.host}:{args.port}/ ... (Ctrl+C 로 종료)")
    try:
        # 서버는 데몬 스레드라 메인이 살아 있어야 한다.
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        web_dashboard.stop_web_server()
    return 0


if __name__ == '__main__':
    sys.exit(main())
