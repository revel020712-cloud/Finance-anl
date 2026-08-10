# main.py
# Finance.anl 백엔드 — 실제 시세/재무/뉴스 데이터를 제공하는 FastAPI 서버
#
# 실행 방법:
#   pip install -r requirements.txt
#   uvicorn main:app --reload --port 8000
#
# 데이터 소스:
#   - 시세/캔들/지표: Yahoo Finance Chart API를 직접 호출 (query2.finance.yahoo.com/v8/finance/chart)
#   - 재무 지표(PER/배당수익률 등): Yahoo Finance QuoteSummary API를 직접 호출 (v10/finance/quoteSummary)
#     두 경우 모두 yfinance 라이브러리를 거치지 않습니다. yfinance의 Ticker.history()/.info가
#     커스텀 세션을 온전히 사용하지 못하는 문제가 있어, curl_cffi 세션으로 Yahoo API를
#     직접 호출하는 방식으로 완전히 대체했습니다.
#   - 뉴스: Google News RSS (서버에서 호출하므로 브라우저 CORS 제약이 없음)
#
# 주의:
#   - Yahoo Finance의 한국 종목 재무 필드(시가총액/PER/배당수익률 등)는 종목에 따라 일부 누락될 수 있습니다.
#     누락된 값은 null로 반환하고, 프론트엔드에서 "데이터 없음"으로 표시합니다.
#   - 무료 소스이므로 실시간(호가 단위) 시세는 아니며, 통상 15~20분 지연 시세입니다.
#   - 상용 서비스로 확장할 경우, 한국투자증권 Open API 등 국내 정식 라이선스 데이터 소스로 교체를 권장합니다.

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from curl_cffi import requests as curl_requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance-anl")

app = FastAPI(title="Finance.anl API", version="1.0")

# ---------------------------------------------------------------------------
# Yahoo Finance는 2024년 이후 봇 차단을 강화해서, 클라우드 서버(Render 등)의 IP로 오는
# 일반적인 requests 세션은 자주 차단하거나 빈 응답을 돌려줍니다. curl_cffi로 실제
# 브라우저(Chrome)의 TLS 지문을 흉내 내면 이 차단을 대부분 우회할 수 있습니다.
# ---------------------------------------------------------------------------
_yf_session = curl_requests.Session(impersonate="chrome124")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 배포 시에는 실제 프론트엔드 도메인으로 제한하세요.
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 아주 단순한 인메모리 캐시 (Yahoo Finance / Google News 과다 호출 방지)
# ---------------------------------------------------------------------------
_cache: dict = {}
CACHE_TTL_SECONDS = 60


def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL_SECONDS:
        return entry[1]
    return None


def cache_set(key: str, value):
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# 시세 / 재무 / 상관관계
# ---------------------------------------------------------------------------
PERIOD_MAP = {
    "1D": ("1d", "5m"),
    "1W": ("5d", "30m"),
    "1M": ("1mo", "1d"),
    "6M": ("6mo", "1d"),
    "1Y": ("1y", "1d"),
}


def fetch_chart_direct(symbol: str, yf_range: str, interval: str):
    """yfinance 라이브러리를 거치지 않고 Yahoo Finance Chart API를 직접 호출합니다.
    /api/debug/yahoo 에서 이 방식(curl_cffi 세션 + 이 엔드포인트)이 실제로 동작함을 확인했습니다.
    yfinance의 Ticker.history()는 내부적으로 커스텀 세션을 온전히 사용하지 않는 경우가 있어
    이 방식이 더 안정적입니다."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        resp = _yf_session.get(url, params={"range": yf_range, "interval": interval}, timeout=12)
    except Exception as e:
        logger.warning(f"[fetch_chart_direct] {symbol} request raised: {type(e).__name__}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"[fetch_chart_direct] {symbol} status={resp.status_code}")
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    result = (data.get("chart") or {}).get("result")
    if not result:
        logger.warning(f"[fetch_chart_direct] {symbol} no result, error={data.get('chart',{}).get('error')}")
        return None
    r = result[0]
    timestamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs, lows, closes, vols = (
        quote.get("open", []), quote.get("high", []), quote.get("low", []),
        quote.get("close", []), quote.get("volume", []),
    )
    candles = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        candles.append({
            "time": int(ts), "open": round(o, 2), "high": round(h, 2),
            "low": round(l, 2), "close": round(c, 2), "volume": int(v),
        })
    meta = r.get("meta") or {}
    return {"candles": candles, "currency": meta.get("currency"), "longName": meta.get("longName") or meta.get("shortName")}


def resolve_symbol(code: str):
    """KOSPI(.KS)로 먼저 시도하고, 데이터가 없으면 KOSDAQ(.KQ)로 재시도합니다."""
    for suffix in (".KS", ".KQ"):
        symbol = code + suffix
        chart = fetch_chart_direct(symbol, "5d", "1d")
        if chart and chart["candles"]:
            return symbol, chart
    return None, None


# ---------------------------------------------------------------------------
# 재무 지표: Yahoo QuoteSummary API 직접 호출
# 이 API는 종종 crumb(임시 인증 토큰)를 요구하므로, 먼저 쿠키를 확보하고 crumb을 발급받습니다.
# ---------------------------------------------------------------------------
_crumb_cache = {"crumb": None, "ts": 0}


def get_crumb():
    now = time.time()
    if _crumb_cache["crumb"] and now - _crumb_cache["ts"] < 3600:
        return _crumb_cache["crumb"]
    try:
        _yf_session.get("https://fc.yahoo.com", timeout=8)  # 쿠키 확보
        resp = _yf_session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        if resp.status_code == 200 and resp.text and "Too Many Requests" not in resp.text:
            crumb = resp.text.strip()
            if crumb:
                _crumb_cache["crumb"] = crumb
                _crumb_cache["ts"] = now
                return crumb
    except Exception as e:
        logger.warning(f"[get_crumb] failed: {type(e).__name__}: {e}")
    return None


def _raw(d: dict, key: str):
    v = (d or {}).get(key)
    if isinstance(v, dict):
        return v.get("raw")
    return v


def fetch_financials_direct(symbol: str):
    """실패해도 예외를 던지지 않고 빈 dict를 반환합니다 (재무 정보는 best-effort)."""
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    params = {"modules": "summaryDetail,defaultKeyStatistics,financialData,price"}
    crumb = get_crumb()
    if crumb:
        params["crumb"] = crumb
    try:
        resp = _yf_session.get(url, params=params, timeout=10)
    except Exception as e:
        logger.warning(f"[fetch_financials_direct] {symbol} raised: {type(e).__name__}: {e}")
        return {}
    if resp.status_code != 200:
        logger.warning(f"[fetch_financials_direct] {symbol} status={resp.status_code}")
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    results = ((data.get("quoteSummary") or {}).get("result")) or []
    if not results:
        logger.warning(f"[fetch_financials_direct] {symbol} no result, error={data.get('quoteSummary',{}).get('error')}")
        return {}
    r = results[0]
    summary = r.get("summaryDetail") or {}
    stats = r.get("defaultKeyStatistics") or {}
    fin = r.get("financialData") or {}
    price = r.get("price") or {}

    dividend_yield = _raw(summary, "dividendYield")
    return {
        "marketCap": _raw(price, "marketCap") or _raw(summary, "marketCap"),
        "dividendYield": (dividend_yield * 100) if dividend_yield else None,
        "per": _raw(summary, "trailingPE"),
        "eps": _raw(stats, "trailingEps"),
        "netIncome": _raw(stats, "netIncomeToCommon"),
        "revenue": _raw(fin, "totalRevenue"),
        "sharesOutstanding": _raw(stats, "sharesOutstanding"),
        "beta": _raw(stats, "beta"),
        "name": _raw(price, "longName") or _raw(price, "shortName"),
    }


def calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def calc_macd_hist(closes: pd.Series) -> pd.Series:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line - signal


@app.get("/api/stock/{code}")
def get_stock(code: str, period: str = Query("1M")):
    cache_key = f"stock:{code}:{period}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    symbol, _ = resolve_symbol(code)
    if not symbol:
        raise HTTPException(
            status_code=404,
            detail=f"'{code}' 종목의 실시간 시세를 찾을 수 없습니다. 잠시 후 다시 시도해주세요.",
        )

    yf_range, interval = PERIOD_MAP.get(period, ("1mo", "1d"))
    chart = fetch_chart_direct(symbol, yf_range, interval)
    if not chart or not chart["candles"]:
        raise HTTPException(status_code=404, detail="해당 기간의 시세 데이터가 없습니다.")

    candles = chart["candles"]
    closes = pd.Series([c["close"] for c in candles])
    volumes = pd.Series([c["volume"] for c in candles])

    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2]) if len(closes) > 1 else last_close
    change = last_close - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0.0

    rsi_series = calc_rsi(closes)
    macd_hist_series = calc_macd_hist(closes)

    # 재무 지표는 best-effort로 가져옵니다. 실패해도 시세/차트/RSI/MACD/상관관계는
    # 이미 위에서 별도로 확보했으므로 대시보드 핵심 기능에는 영향이 없습니다.
    fin_raw = fetch_financials_direct(symbol)

    financials = {
        "marketCap": fin_raw.get("marketCap"),
        "dividendYield": fin_raw.get("dividendYield"),
        "per": fin_raw.get("per"),
        "eps": fin_raw.get("eps"),
        "netIncome": fin_raw.get("netIncome"),
        "revenue": fin_raw.get("revenue"),
        "sharesOutstanding": fin_raw.get("sharesOutstanding"),
        "beta": fin_raw.get("beta"),
    }

    # 상관관계 히트맵: 실제 과거 데이터에서 계산한 지표 간 상관계수 (모의 데이터 아님)
    returns = closes.pct_change()
    volatility = returns.rolling(5).std()
    ind_df = pd.DataFrame(
        {
            "수익률": returns,
            "RSI": rsi_series,
            "MACD": macd_hist_series,
            "거래량": volumes,
            "변동성": volatility,
        }
    ).dropna()

    correlation = None
    if len(ind_df) >= 8:
        corr = ind_df.corr().round(2)
        correlation = [
            {"a": a, "b": b, "value": float(corr.loc[a, b]) if not np.isnan(corr.loc[a, b]) else 0.0}
            for a in corr.columns
            for b in corr.columns
        ]

    result = {
        "code": code,
        "resolvedTicker": symbol,
        "name": fin_raw.get("name") or chart.get("longName") or code,
        "candles": candles,
        "price": {
            "current": round(last_close, 2),
            "change": round(change, 2),
            "changePercent": round(change_pct, 2),
        },
        "indicators": {
            "rsi14": round(float(rsi_series.iloc[-1]), 1),
            "macdHistogram": [round(float(v), 4) for v in macd_hist_series.tail(12).tolist()],
        },
        "financials": financials,
        "correlation": correlation,
        "fetchedAt": datetime.utcnow().isoformat() + "Z",
    }
    cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# 뉴스 + 감성 분석 (서버 사이드에서 호출하므로 CORS 문제가 없음)
# ---------------------------------------------------------------------------
POS_TERMS = [
    ("상회", 9), ("호실적", 10), ("어닝 서프라이즈", 10), ("흑자전환", 9), ("자사주 매입", 10), ("소각", 8),
    ("배당 확대", 8), ("배당성향 확대", 7), ("최대 실적", 9), ("사상 최대", 9), ("목표주가 상향", 9), ("상향", 6),
    ("개선", 5), ("강세", 6), ("상승", 5), ("수주", 6), ("호조", 6), ("신고가", 8), ("증가", 4), ("성장", 5),
    ("확대", 3), ("안정적", 4), ("긍정적", 6), ("견조", 5), ("순항", 5), ("호평", 6), ("매수", 6), ("투자의견 상향", 8),
]
NEG_TERMS = [
    ("하회", 9), ("적자", 9), ("부실", 9), ("우려", 7), ("급락", 10), ("규제", 6), ("소송", 8), ("횡령", 10),
    ("제재", 8), ("신용등급 강등", 10), ("강등", 8), ("매도", 6), ("약세", 6), ("손실", 7), ("부진", 7), ("둔화", 6),
    ("축소", 4), ("하락", 5), ("위축", 6), ("불확실", 5), ("리스크", 5), ("부담", 4), ("악화", 7), ("적신호", 7),
    ("투자의견 하향", 8), ("하향", 5), ("제한", 3),
]


def score_headline(title: str):
    score = 50
    matched_pos, matched_neg = [], []
    for term, w in POS_TERMS:
        if term in title:
            score += w
            matched_pos.append(term)
    for term, w in NEG_TERMS:
        if term in title:
            score -= w
            matched_neg.append(term)
    score = max(5, min(95, round(score)))

    if matched_pos and matched_neg:
        reason = f"긍정 키워드({', '.join(matched_pos)})와 부정 키워드({', '.join(matched_neg)})가 동시에 감지되어 상쇄된 점수입니다."
    elif matched_pos:
        reason = f'제목에서 긍정 키워드 "{", ".join(matched_pos)}"가 감지되어 긍정 점수를 부여했습니다.'
    elif matched_neg:
        reason = f'제목에서 부정 키워드 "{", ".join(matched_neg)}"가 감지되어 부정 점수를 부여했습니다.'
    else:
        reason = "뚜렷한 감성 키워드가 감지되지 않아 중립(50점) 기준값에 가깝게 평가했습니다."

    return {"score": score, "reason": reason, "keywords": matched_pos + matched_neg}


@app.get("/api/news/{query}")
def get_news(query: str):
    cache_key = f"news:{query}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    rss_url = (
        "https://news.google.com/rss/search?q="
        + requests.utils.quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    try:
        resp = requests.get(rss_url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"뉴스 소스 호출에 실패했습니다: {e}")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        raise HTTPException(status_code=502, detail="뉴스 응답을 파싱하지 못했습니다.")

    items = root.findall(".//item")[:6]
    results = []
    for it in items:
        raw_title = (it.findtext("title") or "").strip()
        parts = raw_title.split(" - ")
        if len(parts) > 1:
            source = parts.pop()
            title = " - ".join(parts)
        else:
            source_el = it.find("source")
            source = source_el.text if source_el is not None else "언론사 미상"
            title = raw_title
        link = it.findtext("link") or "#"
        pub_date = it.findtext("pubDate") or ""
        sentiment = score_headline(title)
        results.append(
            {
                "title": title,
                "source": source,
                "url": link,
                "publishedAt": pub_date,
                **sentiment,
            }
        )

    payload = {"query": query, "items": results}
    cache_set(cache_key, payload)
    return payload


@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}


@app.get("/api/debug/yahoo")
def debug_yahoo():
    """Yahoo Finance에 직접 원시 요청을 보내 응답 상태를 확인하는 진단용 엔드포인트.
    404가 반복될 때 이 엔드포인트로 실제 차단 원인(429/999/타임아웃 등)을 확인할 수 있습니다."""
    url = "https://query2.finance.yahoo.com/v8/finance/chart/105560.KS?range=5d&interval=1d"
    try:
        resp = _yf_session.get(url, timeout=10)
        return {
            "status_code": resp.status_code,
            "headers_sample": {k: v for k, v in list(resp.headers.items())[:5]},
            "body_preview": resp.text[:300],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
