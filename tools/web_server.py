import os
import sys
import argparse
import logging
from flask import Flask, send_from_directory

app = Flask(__name__)
CHART_DIR = "."

@app.route('/')
def serve_index():
    return send_from_directory(CHART_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(CHART_DIR, path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=6000)
    parser.add_argument('--directory', type=str, default='.')
    args = parser.parse_args()
    
    CHART_DIR = os.path.abspath(args.directory)
    
    # Flask 로깅 최소화 (불필요한 접속 로그로 콘솔이 지저분해지는 것 방지)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    print(f"Starting Flask Web Server for Charts on 0.0.0.0:{args.port} ...")
    try:
        app.run(host='0.0.0.0', port=args.port, threaded=True)
    except KeyboardInterrupt:
        sys.exit(0)
