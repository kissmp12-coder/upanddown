#!/usr/bin/env python3
"""
업앤다운 (Up & Down)
====================
코스피 보통주 중 '다음 거래일에 종가가 오를 확률'이 높게 계산된 10개 종목을 뽑습니다.

동작 순서
  1) 네이버 금융에서 코스피 종목 목록과 종목별 일봉(약 600거래일)을 받습니다.
  2) 수익률·이동평균·거래량·변동성 등 지표를 계산합니다.
  3) 과거 데이터로 "다음날 오를까?"를 학습합니다 (Gradient Boosting).
  4) 최근 120거래일로 백테스트(실전처럼 미래를 모르는 상태에서 검증)를 하고,
     그 결과로 확률을 보정합니다. 화면에 나오는 %는 이 보정된 값입니다.
  5) 결과를 docs/results.json, 추천 기록을 docs/history.json 에 저장합니다.

실행
  python upanddown.py --out docs          # 실제 데이터
  python upanddown.py --out docs --demo   # 인터넷 없이 가상 데이터로 시험

주의: 이 프로그램의 확률은 통계 모델의 추정치이며 투자 권유가 아닙니다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ─────────────────────────── 설정값 ───────────────────────────
KST = ZoneInfo("Asia/Seoul")
TOP_N = 10                  # 보여줄 종목 수
HISTORY_DAYS = 600          # 종목당 받아올 거래일 수
TEST_DAYS = 120             # 백테스트 기간(거래일)
MIN_TRADING_VALUE = 1e9     # 최근 20일 평균 거래대금 하한 (10억 원) — 거래가 너무 적은 종목 제외
MAX_ABS_RET = 0.31          # 하루 ±31% 초과 변동은 액면분할 등 이벤트로 보고 제외 (가격제한폭 ±30%)
CLEAN_DAYS_AFTER_BREAK = 120
HISTORY_KEEP = 90           # 추천 기록 보관 개수
WORKERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

# ETF·ETN 브랜드 (종목 목록에 섞여 들어오는 경우 제외용)
# 'BNK금융지주'처럼 같은 글자로 시작하는 일반 주식이 걸리지 않도록 뒤에 공백을 붙여 비교
ETF_PREFIXES = tuple(p + " " for p in (
    "KODEX", "TIGER", "KBSTAR", "RISE", "ACE", "SOL", "HANARO", "ARIRANG", "KOSEF",
    "PLUS", "KIWOOM", "TIMEFOLIO", "1Q", "BNK", "WON", "UNICORN", "FOCUS", "TREX",
    "VITA", "KoAct", "WOORI", "ITF", "TRUSTON", "SMART", "KTOP", "HK", "마이다스",
    "에셋플러스", "히어로즈", "파워", "마이티", "DAISHIN343", "TRUE", "QV", "N2",
    "신한", "삼성", "미래에셋", "KB", "하나", "메리츠", "한투", "대신", "키움",
))

FEATURES = [
    "r1", "r3", "r5", "r10", "r20", "r60",
    "ma5", "ma20", "ma60", "ma120",
    "vol20", "vol60", "vr1", "vr5", "log_tv20",
    "range1", "clpos", "gap", "body", "upper", "lower",
    "rsi14", "hi60", "lo60",
    "mkt_r1", "mkt_r5", "breadth", "rel_r1", "rel_r5",
    "rk_r1", "rk_r5", "rk_r20", "rk_vr1", "rk_vol20",
]


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


# ─────────────────────────── 데이터 수집 ───────────────────────────
_local = threading.local()


def _session():
    import requests

    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return s


def http_get(url: str, params: dict | None = None, tries: int = 3, timeout: int = 15):
    last = None
    for i in range(tries):
        try:
            r = _session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # 네트워크 오류 재시도
            last = e
        time.sleep(1.0 + i * 1.5)
    raise RuntimeError(f"요청 실패: {url} ({last})")


def is_common_stock(code: str, name: str, kind: str | None) -> bool:
    if kind and str(kind).lower() not in ("stock",):
        return False
    if not re.fullmatch(r"[0-9A-Z]{5}0", code):   # 끝자리 0이 아닌 코드 = 우선주 등
        return False
    if "스팩" in name or "ETN" in name.upper():
        return False
    if name.startswith(ETF_PREFIXES):
        return False
    return True


def _list_from_mobile_api() -> list[tuple[str, str, str | None]]:
    out, page = [], 1
    while page <= 40:
        r = http_get(
            "https://m.stock.naver.com/api/stocks/marketValue/KOSPI",
            params={"page": page, "pageSize": 100},
        )
        js = r.json()
        items = js.get("stocks") or []
        if not items:
            break
        for it in items:
            code = str(it.get("itemCode") or it.get("reutersCode") or "").strip()
            name = str(it.get("stockName") or "").strip()
            if code and name:
                out.append((code, name, it.get("stockEndType")))
        total = int(js.get("totalCount") or 0)
        if total and page * 100 >= total:
            break
        page += 1
    return out


def _list_from_html() -> list[tuple[str, str, str | None]]:
    out, seen = [], set()
    pat = re.compile(r'/item/main\.naver\?code=([0-9A-Z]{6})"[^>]*class="tltle"[^>]*>([^<]+)</a>')
    for page in range(1, 45):
        r = http_get(
            "https://finance.naver.com/sise/sise_market_sum.naver",
            params={"sosok": 0, "page": page},
        )
        r.encoding = "euc-kr"
        found = [(c, n.strip()) for c, n in pat.findall(r.text) if c not in seen]
        if not found:
            break
        for c, n in found:
            seen.add(c)
            out.append((c, n, None))
    return out


def get_kospi_list() -> pd.DataFrame:
    raw: list = []
    for fn in (_list_from_mobile_api, _list_from_html):
        try:
            raw = fn()
            log(f"종목 목록 {len(raw)}개 수신 ({fn.__name__})")
            if len(raw) >= 300:
                break
        except Exception as e:
            log(f"종목 목록 수신 실패 ({fn.__name__}): {e}")
    if len(raw) < 300:
        raise RuntimeError("코스피 종목 목록을 가져오지 못했습니다.")
    rows = {c: n for c, n, k in raw if is_common_stock(c, n, k)}
    log(f"보통주 {len(rows)}개 선별 (우선주·ETF·스팩 제외)")
    return pd.DataFrame({"code": list(rows), "name": list(rows.values())})


def _daily_from_fchart(code: str, count: int) -> pd.DataFrame:
    r = http_get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day", "count": count, "requestType": 0},
    )
    rows = re.findall(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)"', r.text)
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def _daily_from_sisejson(code: str, count: int) -> pd.DataFrame:
    end = datetime.now(KST)
    start = end - timedelta(days=int(count * 1.6) + 30)
    r = http_get(
        "https://api.finance.naver.com/siseJson.naver",
        params={
            "symbol": code, "requestType": 1, "timeframe": "day",
            "startTime": start.strftime("%Y%m%d"), "endTime": end.strftime("%Y%m%d"),
        },
    )
    rows = re.findall(
        r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', r.text
    )
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def fetch_daily(code: str, count: int = HISTORY_DAYS) -> pd.DataFrame:
    df = pd.DataFrame()
    for fn in (_daily_from_fchart, _daily_from_sisejson):
        try:
            df = fn(code, count)
            if len(df):
                break
        except Exception:
            continue
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 거래정지일 등 시가/고가/저가가 0으로 오는 경우 종가로 채움
    for c in ("open", "high", "low"):
        df.loc[df[c] <= 0, c] = df["close"]
    df = df[df["close"] > 0].drop_duplicates("date").sort_values("date")
    df["code"] = code
    return df.tail(count)


def fetch_all(codes: list[str], count: int, workers: int) -> pd.DataFrame:
    frames, fail = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_daily, c, count): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                d = f.result()
                if len(d):
                    frames.append(d)
                else:
                    fail += 1
            except Exception:
                fail += 1
            if i % 100 == 0 or i == len(codes):
                log(f"  시세 수신 {i}/{len(codes)} (실패 {fail})")
    if not frames:
        raise RuntimeError("시세를 하나도 받지 못했습니다.")
    return pd.concat(frames, ignore_index=True)


# ─────────────────────────── 가상 데이터 (--demo) ───────────────────────────
def make_demo_data(n_stocks: int = 300, n_days: int = HISTORY_DAYS, seed: int = 7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp(datetime.now(KST).date()), periods=n_days)
    mkt = rng.normal(0.0003, 0.010, n_days)
    frames, names = [], []
    for i in range(n_stocks):
        code = f"{900000 + i * 10:06d}"
        names.append((code, f"예시종목 {i + 1:03d}"))
        beta = rng.uniform(0.6, 1.4)
        sig = rng.uniform(0.012, 0.03)
        ret = np.zeros(n_days)
        vol = np.exp(rng.normal(13, 0.4, n_days))
        for t in range(1, n_days):
            # 약한 평균회귀 + 거래량 급증 다음날 소폭 상승 경향 (학습이 되는지 보기 위한 가짜 패턴)
            vr = vol[t - 1] / vol[max(0, t - 21):t].mean()
            ret[t] = beta * mkt[t] - 0.08 * ret[t - 1] + 0.002 * (vr > 1.8) + rng.normal(0, sig)
            vol[t] *= 1 + 3 * abs(ret[t])
        close = 10000 * rng.uniform(0.3, 5) * np.cumprod(1 + np.clip(ret, -0.29, 0.29))
        close = np.round(close)
        op = np.round(close / (1 + ret) * (1 + rng.normal(0, sig / 3, n_days)))
        hi = np.maximum(close, op) * (1 + np.abs(rng.normal(0, sig / 2, n_days)))
        lo = np.minimum(close, op) * (1 - np.abs(rng.normal(0, sig / 2, n_days)))
        frames.append(pd.DataFrame({
            "date": dates, "open": op, "high": np.round(hi), "low": np.round(lo),
            "close": close, "volume": np.round(vol), "code": code,
        }))
    return pd.DataFrame(names, columns=["code", "name"]), pd.concat(frames, ignore_index=True)


# ─────────────────────────── 지표 계산 ───────────────────────────
def _roll(df: pd.DataFrame, col: str, n: int, how: str = "mean") -> pd.Series:
    r = df.groupby("code", sort=False)[col].rolling(n, min_periods=n)
    return getattr(r, how)().reset_index(level=0, drop=True)


def build_features(px: pd.DataFrame) -> pd.DataFrame:
    df = px.sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code", sort=False)
    c = df["close"]
    prev = g["close"].shift(1)

    df["r1"] = c / prev - 1
    for n in (3, 5, 10, 20, 60):
        df[f"r{n}"] = c / g["close"].shift(n) - 1
    for n in (5, 20, 60, 120):
        df[f"ma{n}"] = c / _roll(df, "close", n) - 1
    df["vol20"] = _roll(df, "r1", 20, "std")
    df["vol60"] = _roll(df, "r1", 60, "std")

    vma20 = _roll(df, "volume", 20)
    df["vr1"] = df["volume"] / vma20
    df["vr5"] = _roll(df, "volume", 5) / vma20
    df["tv"] = c * df["volume"]
    df["tv20"] = _roll(df, "tv", 20)
    df["log_tv20"] = np.log10(df["tv20"].clip(lower=1))

    rng_ = (df["high"] - df["low"]).replace(0, np.nan)
    df["range1"] = (df["high"] - df["low"]) / prev
    df["clpos"] = (c - df["low"]) / rng_
    df["gap"] = df["open"] / prev - 1
    df["body"] = c / df["open"] - 1
    df["upper"] = (df["high"] - np.maximum(df["open"], c)) / prev
    df["lower"] = (np.minimum(df["open"], c) - df["low"]) / prev

    d = g["close"].diff()
    df["_gain"] = d.clip(lower=0)
    df["_loss"] = (-d).clip(lower=0)
    ag, al = _roll(df, "_gain", 14), _roll(df, "_loss", 14)
    df["rsi14"] = ag / (ag + al).replace(0, np.nan)
    df["hi60"] = c / _roll(df, "high", 60, "max") - 1
    df["lo60"] = c / _roll(df, "low", 60, "min") - 1

    # 액면분할 등 비정상 변동 이후 일정 기간은 제외
    brk = df["r1"].abs() > MAX_ABS_RET
    pos = g.cumcount()
    last_brk = pos.where(brk).groupby(df["code"]).ffill()
    df["since_brk"] = (pos - last_brk).fillna(10**6)

    # 정답(라벨): 다음 거래일 종가가 오늘보다 높으면 1
    nxt = g["close"].shift(-1)
    nvol = g["volume"].shift(-1)
    df["next_date"] = g["date"].shift(-1)
    df["next_ret"] = nxt / c - 1
    ok = nxt.notna() & (nvol > 0) & (df["next_ret"].abs() <= MAX_ABS_RET)
    df["y"] = np.where(ok, (nxt > c).astype(float), np.nan)
    df.loc[~ok, "next_ret"] = np.nan

    # 분석 대상(유동성·데이터 충분·거래 중)만 남김
    valid = (
        (df["volume"] > 0)
        & (df["tv20"] >= MIN_TRADING_VALUE)
        & (df["since_brk"] >= CLEAN_DAYS_AFTER_BREAK)
        & df["ma120"].notna()
    )
    df = df[valid].copy()

    # 시장 전체 흐름 및 같은 날 종목 간 순위
    by = df.groupby("date")
    df["mkt_r1"] = by["r1"].transform("mean")
    df["mkt_r5"] = by["r5"].transform("mean")
    df["breadth"] = by["r1"].transform(lambda s: (s > 0).mean())
    df["rel_r1"] = df["r1"] - df["mkt_r1"]
    df["rel_r5"] = df["r5"] - df["mkt_r5"]
    for src, dst in (("r1", "rk_r1"), ("r5", "rk_r5"), ("r20", "rk_r20"),
                     ("vr1", "rk_vr1"), ("vol20", "rk_vol20")):
        df[dst] = by[src].rank(pct=True)

    df[FEATURES] = df[FEATURES].replace([np.inf, -np.inf], np.nan)
    return df.drop(columns=["_gain", "_loss"])


# ─────────────────────────── 모델 ───────────────────────────
def make_model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        learning_rate=0.05, max_iter=300, max_leaf_nodes=31, min_samples_leaf=400,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.1,
        n_iter_no_change=25, random_state=42,
    )


class Calibrator:
    """모델 점수를 '실제로 오른 비율'에 맞춰 보정 (Platt scaling: 부드럽고 순서를 유지)"""

    def __init__(self):
        from sklearn.linear_model import LogisticRegression

        self.lr = LogisticRegression()

    @staticmethod
    def _logit(p):
        p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
        return np.log(p / (1 - p)).reshape(-1, 1)

    def fit(self, p, y):
        self.lr.fit(self._logit(p), np.asarray(y).astype(int))
        return self

    def predict(self, p):
        return self.lr.predict_proba(self._logit(p))[:, 1]


def backtest(lab: pd.DataFrame, test_days: int):
    """최근 test_days 거래일을 '모르는 미래'로 두고 검증. (지표, 확률보정기) 반환"""
    from sklearn.metrics import roc_auc_score

    dates = np.sort(lab["date"].unique())
    test_days = min(test_days, max(20, len(dates) // 4))
    start = dates[-test_days]
    tr, te = lab[lab["date"] < start], lab[lab["date"] >= start].copy()
    log(f"백테스트: 학습 {len(tr):,}행 / 검증 {len(te):,}행 ({test_days}거래일)")

    m = make_model().fit(tr[FEATURES], tr["y"])
    te["p"] = m.predict_proba(te[FEATURES])[:, 1]

    top = te.sort_values(["date", "p"], ascending=[True, False]).groupby("date").head(TOP_N)
    daily = top.groupby("date").agg(hit=("y", "mean"), ret=("next_ret", "mean"))
    cal = Calibrator().fit(te["p"], te["y"])
    top_cal = cal.predict(top["p"])

    try:
        auc = float(roc_auc_score(te["y"], te["p"]))
    except ValueError:
        auc = float("nan")

    stats = {
        "start": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(dates[-1]).strftime("%Y-%m-%d"),
        "days": int(test_days),
        "rows": int(len(te)),
        "base_rate": round(float(te["y"].mean()) * 100, 1),
        "accuracy": round(float(((te["p"] > 0.5) == (te["y"] > 0.5)).mean()) * 100, 1),
        "auc": round(auc, 3),
        "top_hit": round(float(top["y"].mean()) * 100, 1),
        "top_pred": round(float(np.mean(top_cal)) * 100, 1),
        "top_avg_ret": round(float(top["next_ret"].mean()) * 100, 2),
        "all_avg_ret": round(float(te["next_ret"].mean()) * 100, 2),
        "days_beat_half": round(float((daily["hit"] > 0.5).mean()) * 100, 1),
    }
    return stats, cal


# ─────────────────────────── 추천 기록 ───────────────────────────
def update_history(path: Path, px: pd.DataFrame, new_entry: dict) -> list:
    hist = []
    if path.exists():
        try:
            hist = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    closes = px.set_index(["code", "date"])["close"]
    cal = np.sort(px["date"].unique())

    for h in hist:
        if h.get("result"):
            continue
        base = pd.Timestamp(h["base_date"])
        later = cal[cal > np.datetime64(base)]
        if not len(later):
            continue
        nd = pd.Timestamp(later[0])
        items, rets = [], []
        for p in h["picks"]:
            try:
                c0, c1 = closes.loc[(p["code"], base)], closes.loc[(p["code"], nd)]
                r = float(c1 / c0 - 1)
                rets.append(r)
                items.append({"code": p["code"], "ret": round(r * 100, 2)})
            except KeyError:
                items.append({"code": p["code"], "ret": None})
        if rets:
            h["result"] = {
                "next_date": nd.strftime("%Y-%m-%d"),
                "up": int(sum(r > 0 for r in rets)),
                "total": len(rets),
                "avg_ret": round(float(np.mean(rets)) * 100, 2),
                "items": items,
            }

    # 가상 데이터 기록과 실제 기록이 섞이지 않게 정리
    hist = [h for h in hist if bool(h.get("demo")) == bool(new_entry.get("demo"))]
    hist = [h for h in hist if h["base_date"] != new_entry["base_date"]]
    hist.append(new_entry)
    hist.sort(key=lambda h: h["base_date"])
    return hist[-HISTORY_KEEP:]


# ─────────────────────────── 메인 ───────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="업앤다운 — 코스피 다음날 상승 확률 상위 10")
    ap.add_argument("--out", default="docs", help="결과 저장 폴더 (기본: docs)")
    ap.add_argument("--demo", action="store_true", help="인터넷 없이 가상 데이터로 실행")
    ap.add_argument("--force", action="store_true", help="이미 최신이어도 다시 계산")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    res_path, hist_path = out / "results.json", out / "history.json"
    t0 = time.time()
    now = datetime.now(KST)

    # 1) 데이터
    if args.demo:
        log("가상 데이터 모드(--demo)")
        names, px = make_demo_data()
    else:
        if res_path.exists() and not args.force:
            try:
                prev = json.loads(res_path.read_text(encoding="utf-8"))
                probe = fetch_daily("005930", 5)  # 삼성전자로 최신 거래일 확인
                latest = probe["date"].max().strftime("%Y-%m-%d")
                if prev.get("base_date") == latest and not prev.get("demo") and not prev.get("partial"):
                    log(f"이미 {latest} 기준 결과가 있어 종료합니다. (--force 로 재계산)")
                    return 0
            except Exception as e:
                log(f"최신 여부 확인 실패, 계속 진행: {e}")
        names = get_kospi_list()
        log(f"일봉 수신 시작 ({len(names)}종목 × 최대 {HISTORY_DAYS}일)")
        px = fetch_all(names["code"].tolist(), HISTORY_DAYS, args.workers)

    # 2) 지표
    log("지표 계산 중…")
    feat = build_features(px)
    latest_date = feat["date"].max()
    lab = feat[feat["y"].notna()]
    log(f"분석 대상 {feat['code'].nunique()}종목, 학습 가능 {len(lab):,}행, 기준일 {latest_date:%Y-%m-%d}")
    if len(lab) < 20000:
        raise RuntimeError("학습 데이터가 너무 적습니다.")

    # 3) 백테스트 + 확률 보정
    stats, cal = backtest(lab, TEST_DAYS)
    log(f"백테스트 결과: 상위{TOP_N} 실제 상승비율 {stats['top_hit']}% "
        f"(전체 평균 {stats['base_rate']}%), AUC {stats['auc']}")

    # 4) 전체 데이터로 다시 학습 → 오늘 기준 예측
    log("최종 모델 학습 중…")
    model = make_model().fit(lab[FEATURES], lab["y"])
    today = feat[feat["date"] == latest_date].copy()
    today["raw"] = model.predict_proba(today[FEATURES])[:, 1]
    today["prob"] = cal.predict(today["raw"])
    today = today.merge(names, on="code", how="left")
    top = today.sort_values("raw", ascending=False).head(TOP_N)

    picks = []
    for i, r in enumerate(top.itertuples(), 1):
        picks.append({
            "rank": i,
            "code": r.code,
            "name": r.name if isinstance(r.name, str) else r.code,
            "prob": round(float(r.prob) * 100, 1),
            "raw": round(float(r.raw) * 100, 1),
            "close": int(round(r.close)),
            "change": round(float(r.r1) * 100, 2),
            "vol_ratio": round(float(r.vr1), 2) if pd.notna(r.vr1) else None,
            "tv_eok": round(float(r.tv) / 1e8, 1),
        })

    partial = (not args.demo) and (latest_date.date() == now.date()) and (now.hour * 60 + now.minute < 15 * 60 + 40)
    result = {
        "app": "업앤다운",
        "demo": bool(args.demo),
        "partial": bool(partial),
        "generated_at": now.isoformat(timespec="minutes"),
        "base_date": latest_date.strftime("%Y-%m-%d"),
        "universe": int(len(today)),
        "market_breadth": round(float((today["r1"] > 0).mean()) * 100, 1),
        "avg_prob": round(float(today["prob"].mean()) * 100, 1),
        "picks": picks,
        "backtest": stats,
        "model": {"train_rows": int(len(lab)), "features": len(FEATURES)},
    }

    entry = {
        "base_date": result["base_date"],
        "picks": [{"code": p["code"], "name": p["name"], "prob": p["prob"]} for p in picks],
        "result": None,
        "demo": bool(args.demo),
    }
    hist = update_history(hist_path, px, entry)

    res_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")

    log(f"완료 ({time.time() - t0:.0f}초) → {res_path}")
    for p in picks:
        log(f"  {p['rank']:>2}. {p['name']}({p['code']})  {p['prob']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
