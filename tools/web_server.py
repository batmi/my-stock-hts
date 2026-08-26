import sys
import os
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

class TimeoutHTTPRequestHandler(SimpleHTTPRequestHandler):
    # 브라우저가 연결(Keep-Alive 세션)을 맺어두고 요청을 보내지 않을 때
    # 5초 뒤에 안 쓰는 세션(소켓)을 자동으로 정리하여 메모리와 자원을 반환합니다.
    timeout = 5

def run(port, directory):
    # Change to the target directory so SimpleHTTPRequestHandler serves it
    os.chdir(directory)
    
    # Use ThreadingHTTPServer to handle multiple concurrent requests without hanging
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, TimeoutHTTPRequestHandler)
    
    # 메인 프로세스 종료 시 백그라운드 세션 스레드들도 함께 즉시 정리되도록 설정
    httpd.daemon_threads = True
    
    print(f"Serving HTTP on 0.0.0.0 port {port} (http://0.0.0.0:{port}/) ...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received, exiting.")
        sys.exit(0)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=6000)
    parser.add_argument('--directory', type=str, default='.')
    args = parser.parse_args()
    
    run(args.port, args.directory)
