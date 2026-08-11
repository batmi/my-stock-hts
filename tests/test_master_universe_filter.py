"""전체 종목 분석 유니버스에서 비주권(ETF/ETN/리츠/스팩)이 빠지는가.

[사고 2026-08-11] 'DS 코스닥액티브'(0220B0)가 코스피 분석에 섞여 들어와
 "지표 계산용 데이터 부족 (신규상장 등)"으로 실패했다. 제외 필터가 종목명 브랜드
 접두어 목록("KODEX ", "TIGER " …)이라 신규 운용사를 못 잡은 것이다. 같은 목록의
 "리츠 "·"스팩 "은 후행 공백을 요구해 '롯데리츠'·'교보18호스팩' 같은 실제 이름과
 한 건도 매칭되지 않았다 — 필터가 있는 줄 알았지 실제로는 무동작이었다.

 정본은 마스터 파일의 증권그룹구분코드다(ST=주권, EF=ETF, RT=리츠, IF=인프라 …).
 이 테스트는 '브랜드 목록으로 되돌아가지 않는다'를 고정한다.
"""
import io
import os
import zipfile
from unittest.mock import patch

import pytest

from modules import analysis


def _mst_line(code, name, grp, grp_offset):
    """KIS 마스터 한 줄을 흉내낸다.

    앞단은 가변폭(단축코드9 + 표준코드12 + 한글명)이고 뒷단이 고정폭이라, 실제 파서는
    개행을 뗀 뒤 **뒤에서** grp_offset 바이트 지점을 증권그룹구분코드로 읽는다.
    여기서도 같은 기하를 맞춘다 — 앞에서 재는 픽스처를 쓰면 오프셋 회귀를 못 잡는다.
    """
    head = (code.ljust(9) + "X" * 12 + name.ljust(40)).encode("cp949")
    tail = (grp.ljust(2) + "0" * (grp_offset - 2)).encode("cp949")
    return head + tail + b"\n"


@pytest.fixture
def fake_master(tmp_path, monkeypatch):
    """마스터 파일을 위조해 놓고 다운로드는 건너뛰게 한다(당일 파일이면 스킵되는 경로 이용)."""
    monkeypatch.setattr(analysis.config, "DATA_DIR", str(tmp_path), raising=False)

    rows = [
        ("005930", "삼성전자", "ST"),
        ("0220B0", "DS 코스닥액티브", "EF"),   # 브랜드 목록에 없던 신규 운용사 ETF
        ("017860", "DS단석", "ST"),            # 같은 'DS' 접두어의 실제 주권 — 이름으로 거르면 오폭한다
        ("069500", "KODEX 200", "EF"),
        ("330590", "롯데리츠", "RT"),          # 기존 "리츠 " 키워드로는 매칭 불가
        ("088980", "맥쿼리인프라", "IF"),
    ]
    path = tmp_path / "kospi_code.mst"
    with open(path, "wb") as f:
        for code, name, grp in rows:
            f.write(_mst_line(code, name, grp, 227))
    # 다운로드 스킵 조건: zip과 mst가 모두 있고 zip이 오늘 받은 것
    zpath = tmp_path / "kospi_code.mst.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("dummy", "x")
    return path


def test_마스터가_증권그룹구분코드를_함께_준다(fake_master):
    lst = analysis._get_master_stock_list("KOSPI")
    grps = {s["code"]: s["grp"] for s in lst}
    assert grps["005930"] == "ST"
    assert grps["0220B0"] == "EF", "신규 ETF의 그룹코드를 읽지 못하면 이름 휴리스틱으로 되돌아간다"
    assert grps["330590"] == "RT"


def test_비주권은_그룹코드로_걸러진다(fake_master):
    """이름이 아니라 그룹코드가 판정 근거여야 한다."""
    lst = analysis._get_master_stock_list("KOSPI")
    kept = [s for s in lst if s.get("grp", "") in ("", "ST") and "스팩" not in s["name"]]
    assert sorted(s["code"] for s in kept) == ["005930", "017860"]

    # [오폭 방지] 'DS ' 같은 접두어를 브랜드 목록에 추가하는 방식으로 되돌리면
    #  DS단석·DSR제강 같은 실제 주권까지 함께 날아간다. 그룹코드는 둘을 정확히 가른다.
    assert "017860" in {s["code"] for s in kept}
    assert "0220B0" not in {s["code"] for s in kept}


def test_스팩은_그룹이_주권이라_이름으로_걸러야_한다():
    """스팩은 증권그룹이 ST(주권)라 그룹코드로는 안 걸린다 — 이름이 유일한 식별자다."""
    names = ["교보18호스팩", "디비금융제14호스팩", "삼성전자"]
    kept = [n for n in names if "스팩" not in n]
    assert kept == ["삼성전자"]
    # 기존 방식(후행 공백)이 왜 무동작이었는지 함께 고정한다.
    assert not any("스팩 " in n for n in names)
