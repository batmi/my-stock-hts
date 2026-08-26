# constants.py

# ==========================================================
# [설정] TR_ID 상수 (API Transaction ID) 통합 관리
# ==========================================================
TR_ID_CONFIG = {
    "domestic": {
        "trade": {
            "buy": "TTTC0012U",
            "sell": "TTTC0011U",
        },
        "modify": {
            "cancel": "TTTC0013U",
            "revise": "TTTC0013U"
        },
        "inquiry": {
            "balance": "TTTC8434R",
            "history": "TTTC8001R",
            # [추가] 주식일별주문체결조회는 3개월 경계로 TR이 갈린다.
            #  위 history(TTTC8001R)는 '3개월 이내'만 조회되고, 그보다 과거는 아래 TR을 써야 한다.
            "history_old": "CTSC9115R",
            "profit": "TTTC8494R",
            "open_orders": "TTTC8036R",
            "deposit": "CTRP6548R",
            "buyable": "TTTC8908R",
            "sellable": "TTTC8434R"
        },
        "quotations": {
            "price": "FHKST01010100",
            "chart": "FHKST03010100",
            "investor": "FHKST01010900",
            # 업종(지수) 현재가 — 실전/모의 동일 TR. 이 매핑이 없으면 call_api가
            # 'TR_ID not found'로 즉시 실패해 지수 차트의 당일 봉 실시간 갱신이 통째로 멈춘다.
            "index_price": "FHPUP02100000",
            "index_investor": "FHPTJ04040000",
            "index_investor_current": "FHKUP01010900",
            "vol_strength": "FHKST01010300"
        }
    },
    "overseas": {
        "trade": {
            "buy": "TTTT1002U",
            "sell": "TTTT1006U",
        },
        "modify": {
            "cancel": "TTTT1004U",
            "revise": "TTTT1004U"
        },
        "inquiry": {
            "balance": "TTTS3012R",
            "open_orders": "TTTS3018R",
            "buyable": "TTTS3007R",
            "sellable": "TTTS3012R"
        },
        "quotations": {
            "price": "HHDFS00000300",
            "detail": "HHDFS76200200",
            "chart": "HHDFS76240000"
        }
    }
}

# ==========================================================
# [설정] API URL 상수
# ==========================================================
API_URLS = {
    "TOKEN": "/oauth2/tokenP",
    "DOMESTIC": {
        "QUOTATIONS": {
            "PRICE": "/uapi/domestic-stock/v1/quotations/inquire-price",
            "CHART": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "TIME_CHART": "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "INVESTOR": "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "VOL_STRENGTH": "/uapi/domestic-stock/v1/quotations/inquire-ccnl",
            "INDEX_PRICE": "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "INDEX_CHART": "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            "INDEX_INVESTOR_CURRENT": "/uapi/domestic-stock/v1/quotations/inquire-index-investor",
            "DAILY_PRICE": "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            # [추가] 국내 선물옵션 시세 (코스피200 선물 주간 F / 야간 CM 겸용)
            "FUT_PRICE": "/uapi/domestic-futureoption/v1/quotations/inquire-price",
            "FUT_CHART": "/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice"
        },
        "TRADING": {
            "BUY": "/uapi/domestic-stock/v1/trading/order-cash",
            "SELL": "/uapi/domestic-stock/v1/trading/order-cash",
            "REVISE_CANCEL": "/uapi/domestic-stock/v1/trading/order-rvsecncl"
        },
        "INQUIRY": {
            "BALANCE": "/uapi/domestic-stock/v1/trading/inquire-balance",
            "PROFIT": "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            "BUYABLE": "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            "SELLABLE": "/uapi/domestic-stock/v1/trading/inquire-psbl-sell",
            "HISTORY": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            "OPEN_ORDERS": "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            "DEPOSIT": "/uapi/domestic-stock/v1/trading/inquire-account-balance"
        }
    },
    "OVERSEAS": {
        "QUOTATIONS": {
            "PRICE": "/uapi/overseas-price/v1/quotations/price",
            "DETAIL": "/uapi/overseas-price/v1/quotations/price-detail",
            "CHART": "/uapi/overseas-price/v1/quotations/dailyprice"
        },
        "TRADING": {
            "ORDER": "/uapi/overseas-stock/v1/trading/order",
            "REVISE_CANCEL": "/uapi/overseas-stock/v1/trading/order-rvsecncl"
        },
        "INQUIRY": {
            "BALANCE": "/uapi/overseas-stock/v1/trading/inquire-balance",
            "BUYABLE": "/uapi/overseas-stock/v1/trading/inquire-psamount",
            "HISTORY": "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            "OPEN_ORDERS": "/uapi/overseas-stock/v1/trading/inquire-nccs"
        }
    }
}

# ==========================================================
# [설정] 데이터 필드 매핑 (한글 명칭 변환용)
# ==========================================================
FIELD_MAP_DOMESTIC = {
    "marg_rate": "증거금 비율", "rprs_mrkt_kor_name": "대표 시장", "new_hgpr_lwpr_cls_code": "신 고가 저가 구분 코드",
    "bstp_kor_isnm": "업종", "temp_stop_yn": "임시 정지 여부", "oprc_rang_cont_yn": "시가 범위 연장 여부",
    "clpr_rang_cont_yn": "종가 범위 연장 여부", "crdt_able_yn": "신용 가능 여부", "grmn_rate_cls_code": "보증금 비율 구분 코드",
    "elw_pblc_yn": "ELW 발행 여부", "stck_prpr": "주식 현재가", "prdy_vrss": "전일 대비", "prdy_vrss_sign": "전일 대비 부호",
    "prdy_ctrt": "전일 대비율", "acml_tr_pbmn": "누적 거래 대금", "acml_vol": "누적 거래량", "prdy_vrss_vol_rate": "전일 대비 거래량 비율",
    "stck_oprc": "주식 시가", "stck_hgpr": "주식 최고가", "stck_lwpr": "주식 최저가", "stck_mxpr": "주식 상한가",
    "stck_llam": "주식 하한가", "stck_sdpr": "주식 기준가", "wghn_avrg_stck_prc": "가중 평균 주식 가격", "hts_frgn_ehrt": "HTS 외국인 소진율",
    "frgn_ntby_qty": "외국인 순매수 수량", "pgtr_ntby_qty": "프로그램매매 순매수 수량", "pvt_scnd_dmrs_prc": "피벗 2차 디저항 가격",
    "pvt_frst_dmrs_prc": "피벗 1차 디저항 가격", "pvt_pont_val": "피벗 포인트 값", "pvt_frst_dmsp_prc": "피벗 1차 디지지 가격",
    "pvt_scnd_dmsp_prc": "피벗 2차 디지지 가격", "dmrs_val": "디저항 값", "dmsp_val": "디지지 값", "cpfn": "자본금",
    "rstc_wdth_prc": "제한 폭 가격", "stck_fcam": "주식 액면가", "stck_sspr": "주식 대용가", "aspr_unit": "호가단위",
    "hts_deal_qty_unit_val": "HTS 매매 수량 단위 값", "lstn_stcn": "상장 주수", "hts_avls": "HTS 시가총액", "per": "PER",
    "pbr": "PBR", "stac_month": "결산 월", "vol_tnrt": "거래량 회전율", "eps": "EPS", "bps": "BPS", "d250_hgpr": "250일 최고가",
    "d250_hgpr_date": "250일 최고가 일자", "d250_hgpr_vrss_prpr_rate": "250일 최고가 대비 현재가 비율", "d250_lwpr": "250일 최저가",
    "d250_lwpr_date": "250일 최저가 일자", "d250_lwpr_vrss_prpr_rate": "250일 최저가 대비 현재가 비율", "stck_dryy_hgpr": "주식 연중 최고가",
    "dryy_hgpr_vrss_prpr_rate": "연중 최고가 대비 현재가 비율", "dryy_hgpr_date": "연중 최고가 일자", "stck_dryy_lwpr": "주식 연중 최저가",
    "dryy_lwpr_vrss_prpr_rate": "연중 최저가 대비 현재가 비율", "dryy_lwpr_date": "연중 최저가 일자", "w52_hgpr": "52주일 최고가",
    "w52_hgpr_vrss_prpr_ctrt": "52주일 최고가 대비 현재가 대비", "w52_hgpr_date": "52주일 최고가 일자", "w52_lwpr": "52주일 최저가",
    "w52_lwpr_vrss_prpr_ctrt": "52주일 최저가 대비 현재가 대비", "w52_lwpr_date": "52주일 최저가 일자", "whol_loan_rmnd_rate": "전체 융자 잔고 비율",
    "ssts_yn": "공매도가능여부", "stck_shrn_iscd": "주식 단축 종목코드", "fcam_cnnm": "액면가 통화명", "cpfn_cnnm": "자본금 통화명",
    "apprch_rate": "접근도", "frgn_hldn_qty": "외국인 보유 수량", "vi_cls_code": "VI적용구분코드", "ovtm_vi_cls_code": "시간외단일가VI적용구분코드",
    "last_ssts_cntg_qty": "최종 공매도 체결 수량", "invt_caful_yn": "투자유의여부", "mrkt_warn_cls_code": "시장경고코드",
    "short_over_yn": "단기과열여부", "sltr_yn": "정리매매여부", "mang_issu_cls_code": "관리종목여부", "stck_prdy_clpr": "주식 전일 종가",
    "hts_kor_isnm": "HTS 한글 종목명", "prdy_vol": "전일 거래량", "stck_prdy_oprc": "주식 전일 시가", "stck_prdy_hgpr": "주식 전일 최고가",
    "stck_prdy_lwpr": "주식 전일 최저가", "askp": "매도호가", "bidp": "매수호가", "prdy_vrss_vol": "전일 대비 거래량",
    "stck_bsop_date": "주식 영업 일자", "stck_clpr": "주식 종가", "flng_cls_code": "락 구분 코드", "prtt_rate": "분할 비율",
    "mod_yn": "변경 여부", "revl_issu_reas": "재평가사유코드", "iscd_stat_cls_code": "종목상태", "nday_vol_tnrt": "N일거래량회전율",
    "ovtm_uni_prpr": "시간외단일가현재가", "ovtm_uni_prdy_vrss": "시간외단일가전일대비", "ovtm_uni_prdy_vrss_sign": "시간외단일가대비부호",
    "ovtm_uni_prdy_ctrt": "시간외단일가등락률", "ovtm_uni_vol": "시간외단일가거래량", "ovtm_uni_tr_pbmn": "시간외단일가거래대금",
    "dmrs_val_grp_code": "신용위험등급", "idx_bztp_mcls_cd": "지수업종중분류", "tr_stop_yn": "거래정지여부", "kisu": "기준가",
    "mang_issu_yn": "관리종목여부"
}

FIELD_MAP_OVERSEAS_DETAIL = {
    "rsym": "실시간조회종목코드", "pvol": "전일거래량", "open": "시가", "high": "고가", "low": "저가", "last": "현재가",
    "base": "전일종가", "tomv": "시가총액", "pamt": "전일거래대금", "uplp": "상한가", "dnlp": "하한가", "h52p": "52주최고가",
    "h52d": "52주최고일자", "l52p": "52주최저가", "l52d": "52주최저일자", "perx": "PER", "pbrx": "PBR", "epsx": "EPS",
    "bpsx": "BPS", "shar": "상장주수", "mcap": "자본금", "curr": "통화", "zdiv": "소수점자리수", "vnit": "매매단위",
    "t_xprc": "원환산당일가격", "t_xdif": "원환산당일대비", "t_xrat": "원환산당일등락", "p_xprc": "원환산전일가격",
    "p_xdif": "원환산전일대비", "p_xrat": "원환산전일등락", "t_rate": "당일환율", "p_rate": "전일환율", "t_xsgn": "원환산당일기호",
    "p_xsng": "원환산전일기호", "e_ordyn": "거래가능여부", "e_hogau": "호가단위", "e_icod": "업종(섹터)", "e_parp": "액면가",
    "tvol": "거래량", "tamt": "거래대금", "etyp_nm": "ETP 분류명", "xymd": "일자(YYYYMMDD)", "clos": "종가", "sign": "대비기호",
    "diff": "대비", "rate": "등락율", "pbid": "매수호가", "vbid": "매수호가잔량", "pask": "매도호가", "vask": "매도호가잔량",
    "nav": "NAV", "kora": "한국자산", "aloa": "자산배분", "etft": "ETF유형", "nav_diff": "NAV대비", "nav_rate": "NAV등락률",
    "jan_rate": "연초대비수익률", "ovrs_nm": "종목명(영문)", "excd": "거래소"
}