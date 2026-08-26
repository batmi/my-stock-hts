import os
import glob
from datetime import datetime

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BATMI Chart Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --border: rgba(255, 255, 255, 0.1);
        }
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(56, 189, 248, 0.05), transparent 25%),
                radial-gradient(circle at 85% 30%, rgba(139, 92, 246, 0.05), transparent 25%);
            color: var(--text-main);
            margin: 0;
            padding: 2rem;
            min-height: 100vh;
        }
        header {
            text-align: center;
            margin-bottom: 3rem;
            animation: fadeInDown 0.8s ease;
        }
        h1 {
            font-size: 2.5rem;
            font-weight: 600;
            margin: 0;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p.subtitle {
            color: var(--text-muted);
            margin-top: 0.5rem;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 1.5rem;
            max-width: 1400px;
            margin: 0 auto;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            backdrop-filter: blur(10px);
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            cursor: pointer;
            animation: fadeIn 1s ease both;
        }
        .card:hover {
            transform: translateY(-8px) scale(1.02);
            box-shadow: 0 15px 30px rgba(0, 0, 0, 0.3);
            border-color: rgba(56, 189, 248, 0.3);
        }
        .card img {
            width: 100%;
            height: 130px;
            object-fit: cover;
            border-bottom: 1px solid var(--border);
            transition: filter 0.3s;
        }
        .card:hover img {
            filter: brightness(1.1);
        }
        .card-info {
            padding: 0.8rem;
        }
        .card-title {
            font-weight: 600;
            font-size: 0.95rem;
            margin: 0 0 0.4rem 0;
            color: var(--text-main);
            word-break: break-all;
        }
        .card-meta {
            font-size: 0.75rem;
            color: var(--text-muted);
            display: flex;
            justify-content: space-between;
        }
        
        /* Lightbox Modal */
        #lightbox {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(15, 23, 42, 0.95);
            backdrop-filter: blur(8px);
            z-index: 1000;
            opacity: 0;
            transition: opacity 0.3s ease;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }
        #lightbox.active {
            display: flex;
            opacity: 1;
        }
        #lightbox.active.zoomed {
            display: block;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 20px 0;
        }
        #lightbox img {
            max-width: 95%;
            max-height: 95vh;
            border-radius: 8px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            transform: scale(0.95);
            transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            cursor: zoom-in;
            display: block;
            margin: 0 auto;
        }
        #lightbox.active img {
            transform: scale(1);
        }
        #lightbox.active.zoomed img {
            max-width: none;
            max-height: none;
            width: 100%;
            height: auto;
            border-radius: 0;
            cursor: zoom-out;
        }
        .close-btn {
            position: absolute;
            top: 20px; right: 30px;
            font-size: 2.5rem;
            color: white;
            cursor: pointer;
            transition: color 0.2s;
            z-index: 1001;
        }
        .close-btn:hover { color: var(--accent); }

        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: scale(0.95); }
            to { opacity: 1; transform: scale(1); }
        }
    </style>
</head>
<body>
    <header>
        <h1>Quantitative Chart Dashboard</h1>
    </header>
    <div class="gallery">
        <!-- INJECT_CARDS -->
    </div>

    <div id="lightbox" onclick="closeLightbox()">
        <span class="close-btn" onclick="closeLightbox()">&times;</span>
        <img id="lightbox-img" src="" alt="Full Chart" onclick="toggleZoom(event)">
    </div>

    <script>
        function toggleZoom(event) {
            event.stopPropagation();
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.toggle('zoomed');
        }
        function openLightbox(src) {
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.remove('zoomed');
            document.getElementById('lightbox-img').src = src;
            lightbox.classList.add('active');
            document.body.style.overflow = 'hidden';
        }
        function closeLightbox() {
            const lightbox = document.getElementById('lightbox');
            lightbox.classList.remove('active');
            lightbox.classList.remove('zoomed');
            setTimeout(() => { document.getElementById('lightbox-img').src = ''; }, 300);
            document.body.style.overflow = 'auto';
        }
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeLightbox();
        });
    </script>
</body>
</html>
"""

def update_chart_index(chart_dir):
    """지정된 차트 디렉토리의 png 파일들을 읽어 index.html 갤러리를 생성합니다."""
    png_files = glob.glob(os.path.join(chart_dir, '*.png'))
    
    # 수정 시간 기준으로 최신순 정렬
    png_files.sort(key=os.path.getmtime, reverse=True)
    
    cards_html = ""
    for idx, f in enumerate(png_files):
        filename = os.path.basename(f)
        mtime = os.path.getmtime(f)
        date_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        
        # 파일명에서 종목코드/이름 및 기간 파싱 (예: analysis_005930_daily.png)
        parts = filename.replace('.png', '').split('_')
        if len(parts) >= 3:
            code = parts[1]
            try:
                import api
                is_overseas = not code.isdigit()
                stock_name = api.get_stock_name_by_code(code, is_overseas)
                
                period_str = f"({parts[2].upper()})"
                if stock_name:
                    alt_text = f"{stock_name} {code} {period_str}"
                    title_html = f"{stock_name} {code}<br><span style='font-size: 0.85em; color: var(--text-muted);'>{period_str}</span>"
                else:
                    alt_text = f"{code} {period_str}"
                    title_html = f"{code}<br><span style='font-size: 0.85em; color: var(--text-muted);'>{period_str}</span>"
            except Exception:
                period_str = f"({parts[2].upper()})"
                alt_text = f"{code} {period_str}"
                title_html = f"{code}<br><span style='font-size: 0.85em; color: var(--text-muted);'>{period_str}</span>"
        else:
            alt_text = filename
            title_html = filename
            
        anim_delay = (idx % 10) * 0.1
        
        cards_html += f'''
        <div class="card" style="animation-delay: {anim_delay}s" onclick="openLightbox('{filename}')">
            <img src="{filename}" alt="{alt_text}" loading="lazy">
            <div class="card-info">
                <h3 class="card-title">{title_html}</h3>
                <div class="card-meta">
                    <span>{date_str}</span>
                </div>
            </div>
        </div>
        '''
        
    html_content = HTML_TEMPLATE.replace('<!-- INJECT_CARDS -->', cards_html)
    
    index_path = os.path.join(chart_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

if __name__ == "__main__":
    # 독립적으로 실행할 때를 대비한 처리
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    chart_dir = os.path.join(base_dir, 'chart')
    update_chart_index(chart_dir)
    print(f"Chart index generated at: {os.path.join(chart_dir, 'index.html')}")
