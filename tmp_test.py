import sys, os
sys.path.insert(0, os.path.abspath('.'))
from modules.chart import generate_visual_chart
generate_visual_chart('005930', '삼성전자', False, open_file=False, dpi=100, quiet=True, period_type='daily', months=1)
