#!/usr/bin/env python3
"""每日抓取台股融資、官方擔保維持率、台指 VIX，計算維持率與恐慌溫度，輸出到 data/。

只用 Python 標準函式庫。
用法：
    python scripts/update.py            # 補齊上次處理之後到今天的交易日
    python scripts/update.py --backfill 120   # 重新建立最近 120 個交易日的歷史
"""
import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "state.json")
MARKET_FILE = os.path.join(DATA, "market.json")
STOCKS_FILE = os.path.join(DATA, "stocks.json")
TAIEX_FILE = os.path.join(DATA, "taiex.json")
CRASH_FILE = os.path.join(DATA, "crashes.json")

TZ = dt.timezone(dt.timedelta(hours=8))
FIN_RATIO = {"TW": 0.6, "OTC": 0.5}   # 融資成數：上市 6 成、上櫃 5 成
STOCK_HIST = 20                        # 個股保留幾天的估算維持率
MARKET_KEEP = 900                      # 大盤歷史保留天數（約 3.5 年）
HISTORY_YEARS = 3                      # 走勢圖要補齊的年數
TAIEX_FILE_START = dt.date(1999, 10, 1)
BUDGET_MIN = float(os.environ.get("BUDGET_MIN", "95"))   # 每次執行補歷史的時間上限（分鐘）
REQ_GAP = 2.2                          # 每次請求間隔秒數，避免被交易所封鎖

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_ctx = ssl.create_default_context()
try:  # 證交所憑證在新版 Python 嚴格模式下會被拒，放寬這一項檢查
    _ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
except AttributeError:
    pass

_last_req = 0.0
START = time.time()


def log(*a):
    print(*a, flush=True)


def http_get(url, raw=False, tries=3):
    global _last_req
    for i in range(tries):
        wait = REQ_GAP - (time.time() - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=40, context=_ctx) as r:
                body = r.read()
            return body if raw else json.loads(body.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"  ! {url} 第 {i + 1} 次失敗：{e}")
            time.sleep(5 * (i + 1))
    return None


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^0-9.\-]", "", str(s))
    if t in ("", "-", ".", "-."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def ymd(d):
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------- 資料來源

def twse_margin(d):
    j = http_get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd(d)}&selectType=ALL&response=json")
    if not j or j.get("stat") != "OK" or j.get("date") != ymd(d):
        return None
    summary = next((t for t in j["tables"] if "信用交易統計" in t.get("title", "")), None)
    detail = next((t for t in j["tables"] if "彙總" in t.get("title", "")), None)
    if not summary or not detail:
        return None
    amt_row = next(r for r in summary["data"] if "融資金額" in r[0])
    rows = {}
    for r in detail["data"]:
        rows[r[0].strip()] = {"name": r[1].strip(), "buy": num(r[2]) or 0, "prev": num(r[5]) or 0,
                              "bal": num(r[6]) or 0}
    return {"amt": num(amt_row[5]) * 1000, "rows": rows}


def twse_quotes(d):
    j = http_get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd(d)}&type=ALLBUT0999&response=json")
    if not j or j.get("stat") != "OK" or j.get("date") != ymd(d):
        return None
    taiex = None
    for t in j["tables"]:
        for r in t.get("data") or []:
            if r and r[0] == "發行量加權股價指數":
                sign = -1 if "-" in re.sub(r"<[^>]*>", "", r[2]).replace("color", "") else 1
                taiex = {"close": num(r[1]), "chg": sign * (num(r[3]) or 0), "pct": num(r[4])}
                break
        if taiex:
            break
    qt = next((t for t in j["tables"] if "每日收盤行情" in (t.get("title") or "")), None)
    if not qt:
        return None
    f = qt["fields"]
    ic, isg, idf = f.index("收盤價"), f.index("漲跌(+/-)"), f.index("漲跌價差")
    px = {}
    for r in qt["data"]:
        c = num(r[ic])
        sg = re.sub(r"<[^>]*>", "", r[isg]).strip()
        df = num(r[idf]) or 0
        px[r[0].strip()] = (c, -df if sg == "-" else df)
    return {"taiex": taiex, "px": px}


def twse_official(d):
    j = http_get(f"https://www.twse.com.tw/rwd/zh/marginTrading/BFIJ3U?response=json&date={ymd(d)}")
    if not j or j.get("stat") != "OK" or j.get("date") != ymd(d) or j.get("keepRate") is None:
        return None
    return {"keepRate": j.get("keepRate"), "callAcc": j.get("callAccNum"), "callAmt": j.get("callAmt"),
            "exeAcc": j.get("exeAccNum"), "exeAmt": j.get("exeAmt"), "belowAcc": j.get("belowAccNum")}


def _tpex(path, d):
    q = urllib.parse.urlencode({"date": d.strftime("%Y/%m/%d"), "id": "", "response": "json"})
    j = http_get(f"https://www.tpex.org.tw/www/zh-tw/{path}?{q}")
    if not j or str(j.get("stat", "")).lower() != "ok" or not j.get("tables"):
        return None
    jd = re.sub(r"\D", "", str(j.get("date", "")))
    if jd and jd != ymd(d):
        return None
    t = j["tables"][0]
    if not t.get("data"):
        return None
    return t


def tpex_margin(d):
    t = _tpex("margin/balance", d)
    if not t:
        return None
    amt = None
    for r in t.get("summary") or []:
        if any("融資金" in str(c) for c in r):
            amt = num(r[6]) * 1000
    if amt is None:
        return None
    rows = {}
    for r in t["data"]:
        rows[r[0].strip()] = {"name": r[1].strip(), "buy": num(r[3]) or 0, "prev": num(r[2]) or 0,
                              "bal": num(r[6]) or 0}
    return {"amt": amt, "rows": rows}


def tpex_quotes(d):
    t = _tpex("afterTrading/dailyQuotes", d)
    if not t:
        return None
    px = {}
    for r in t["data"]:
        px[r[0].strip()] = (num(r[2]), num(r[3]) or 0)
    return {"px": px}


_vix_cache = {}


def vix_close(d):
    m = d.strftime("%Y%m")
    if m not in _vix_cache:
        vals = {}
        raw = http_get(f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{m}new.txt", raw=True, tries=2)
        if raw:
            for line in raw.decode("big5", errors="ignore").splitlines():
                parts = [p for p in line.split("\t") if p.strip()]
                if len(parts) >= 3 and re.fullmatch(r"\d{8}", parts[0].strip()):
                    v = num(parts[2])
                    if v:
                        vals[parts[0].strip()] = v
        _vix_cache[m] = vals
    v = _vix_cache[m].get(ymd(d))
    return v if v is not None else _vix_daily(d)


def _vix_daily(d):
    v = None
    if d >= dt.datetime.now(TZ).date() - dt.timedelta(days=45):
        raw = http_get(f"https://www.taifex.com.tw/cht/7/getVixData?filesname={ymd(d)}", raw=True, tries=2)
        if raw:
            lines = [ln for ln in raw.decode("big5", errors="ignore").splitlines() if ln.startswith(ymd(d))]
            for ln in reversed(lines):
                parts = [p for p in ln.split("\t") if p.strip()]
                if len(parts) >= 3 and re.fullmatch(r"\d+", parts[1].strip()):
                    v = num(parts[2])
                    break
    return v


def trading_days_between(start, end):
    """用證交所月成交資訊（FMTQIK）找出交易日。"""
    days = []
    m = dt.date(start.year, start.month, 1)
    while m <= end:
        j = http_get(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={ymd(m)}")
        if j and j.get("stat") == "OK":
            for r in j.get("data", []):
                y, mo, da = r[0].split("/")
                day = dt.date(int(y) + 1911, int(mo), int(da))
                if start <= day <= end:
                    days.append(day)
        m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return sorted(days)


# ---------------------------------------------------------------- 計算

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def fear_parts(vol, ratio, chg5, close, ma20, est=False):
    """恐慌溫度 0–100，越高越恐慌。缺的項目自動重新分配權重。"""
    parts = {}
    if vol:
        parts["vix"] = (clamp((vol - 15) / 30 * 100), 0.40)
    if ratio:
        parts["ratio"] = (clamp((170 - ratio) / 40 * 100), 0.25)
    if chg5 is not None:
        parts["margin"] = (clamp(-chg5 / 0.08 * 100), 0.15)
    if close and ma20:
        parts["trend"] = (clamp(-(close / ma20 - 1) / 0.08 * 100), 0.20)
    if "vix" not in parts or len(parts) < 3:
        return None, {}
    w = sum(p[1] for p in parts.values())
    score = sum(p[0] * p[1] for p in parts.values()) / w
    out = {k: round(v[0], 1) for k, v in parts.items()}
    if est:
        out["est"] = 1
    return round(score, 1), out


def fear_score(day, history, closes_before):
    vol, est = (day.get("vix"), False) if day.get("vix") else (day.get("vixEst"), True)
    past = history
    chg5 = None
    if len(past) >= 5 and past[-5].get("allAmt") and day.get("allAmt") and past[-5]["d"] >= ymd(parse_ymd(day["d"]) - dt.timedelta(days=12)):
        chg5 = day["allAmt"] / past[-5]["allAmt"] - 1
    ma20 = None
    if day.get("taiex") and len(closes_before) >= 19:
        ma20 = (sum(closes_before[-19:]) + day["taiex"]) / 20
    return fear_parts(vol, day.get("allRatio"), chg5, day.get("taiex"), ma20, est)


# ---------------------------------------------------------------- 加權指數、歷史波動率、VIX 估算

def update_taiex(taiex, today):
    """data/taiex.json：1999 年以來每日加權指數收盤（來自 FMTQIK 月資料）。"""
    have_months = {k[:6] for k in taiex}
    m = TAIEX_FILE_START
    recent = {ymd(today)[:6], ymd(today.replace(day=1) - dt.timedelta(days=1))[:6]}
    while m <= today:
        key = ymd(m)[:6]
        if key not in have_months or key in recent:
            j = http_get(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={ymd(m)}")
            if j and j.get("stat") == "OK":
                for r in j.get("data", []):
                    y, mo, da = r[0].strip().split("/")
                    c = num(r[4])
                    if c:
                        taiex[ymd(dt.date(int(y) + 1911, int(mo), int(da)))] = c
        m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return dict(sorted(taiex.items()))


def hv20_map(taiex):
    """20 日年化歷史波動率（%）。"""
    import math
    keys = list(taiex)
    rets = [None] + [math.log(taiex[keys[i]] / taiex[keys[i - 1]]) for i in range(1, len(keys))]
    out = {}
    for i in range(21, len(keys)):
        w = rets[i - 19:i + 1]
        mu = sum(w) / 20
        out[keys[i]] = (sum((x - mu) ** 2 for x in w) / 19) ** 0.5 * (252 ** 0.5) * 100
    return out


def fit_vix_model(days, hv):
    pts = [(hv[h["d"]], h["vix"]) for h in days if h.get("vix") and h["d"] in hv]
    if len(pts) < 30:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    b = sxy / sxx if sxx else 0
    a = my - b * mx
    syy = sum((p[1] - my) ** 2 for p in pts)
    r2 = (sxy * sxy) / (sxx * syy) if sxx and syy else 0
    return {"a": round(a, 3), "b": round(b, 3), "n": n, "r2": round(r2, 3)}


def est_vix(model, hvv):
    if not model or hvv is None:
        return None
    return round(max(10.0, model["a"] + model["b"] * hvv), 2)


# ---------------------------------------------------------------- 只算大盤（補長期歷史、股災用）

def market_ratio(mg, qt):
    mv = 0.0
    for code, r in mg["rows"].items():
        c = qt["px"].get(code, (None, 0))[0]
        if c:
            mv += r["bal"] * 1000 * c
    return mv


def light_day(d):
    """只計算大盤融資維持率（不更新個股成本）。"""
    tm = twse_margin(d)
    tq = twse_quotes(d) if tm else None
    if not (tm and tq):
        return None
    om = tpex_margin(d)
    oq = tpex_quotes(d) if om else None
    tw_mv = market_ratio(tm, tq)
    day = {"d": ymd(d), "twRatio": round(tw_mv / tm["amt"] * 100, 2) if tm["amt"] else None, "twAmt": tm["amt"],
           "otcRatio": None, "otcAmt": None, "allRatio": None, "allAmt": None, "light": 1}
    if om and oq:
        otc_mv = market_ratio(om, oq)
        day["otcRatio"] = round(otc_mv / om["amt"] * 100, 2) if om["amt"] else None
        day["otcAmt"] = om["amt"]
        day["allAmt"] = tm["amt"] + om["amt"]
        day["allRatio"] = round((tw_mv + otc_mv) / day["allAmt"] * 100, 2)
    return day


def margin_amt(d):
    tm = twse_margin(d)
    om = tpex_margin(d)
    if tm and om:
        return tm["amt"] + om["amt"]
    return None


def fill_history(days_hist, taiex, today, deadline, save_cb):
    have = {h["d"] for h in days_hist}
    start = ymd(today - dt.timedelta(days=int(365.25 * HISTORY_YEARS) + 5))
    todo = [k for k in reversed(list(taiex)) if k >= start and k not in have and k < ymd(today)]
    if not todo:
        return days_hist
    log(f"補長期歷史：還有 {len(todo)} 天")
    added = 0
    for k in todo:
        if time.time() > deadline:
            log("  到達本次時間上限，下次執行繼續")
            break
        day = light_day(parse_ymd(k))
        if day is None:
            day = {"d": k, "light": 1, "missing": 1}
        days_hist.append(day)
        added += 1
        if added % 20 == 0:
            days_hist.sort(key=lambda h: h["d"])
            save_cb(days_hist)
            log(f"  已補 {added} 天（目前到 {k}）")
    days_hist.sort(key=lambda h: h["d"])
    return days_hist


# ---------------------------------------------------------------- 2000 年後股災

CRASHES = [
    # id, 名稱, 搜尋低點的期間（起, 迄）, 找高點的起始日
    ("2000", "網路泡沫破裂", "2000-03-01", "2000-12-31", "2000-01-01"),
    ("2001", "911 恐怖攻擊", "2001-09-01", "2001-10-15", "2001-06-01"),
    ("2004", "319 槍擊案", "2004-03-19", "2004-03-31", "2004-03-01"),
    ("2008", "全球金融海嘯", "2008-09-01", "2008-12-31", "2008-05-01"),
    ("2011", "美債降評、歐債危機", "2011-08-01", "2011-12-31", "2011-07-01"),
    ("2015", "中國股災", "2015-07-01", "2015-08-31", "2015-04-01"),
    ("2018", "十月股災", "2018-10-01", "2018-10-31", "2018-09-01"),
    ("2020", "新冠疫情", "2020-02-01", "2020-03-31", "2020-01-01"),
    ("2022", "美國升息熊市", "2022-01-01", "2022-10-31", "2022-01-01"),
    ("2024", "日圓套利平倉", "2024-07-11", "2024-08-31", "2024-07-01"),
    ("2025", "美國對等關稅", "2025-04-01", "2025-04-30", "2025-01-01"),
    ("2026-07", "2026 年 7 月修正", "2026-07-01", "2026-08-15", "2026-06-01"),
    ("2026-11", "2026 年 11 月急跌", "2026-10-15", "2099-12-31", "2026-09-15"),
]


def compute_crashes(taiex, hv, model, today, old):
    cache = {e["id"]: e for e in (old or {}).get("events", [])}
    keys = list(taiex)
    out = []
    for cid, name, s, e, pf in CRASHES:
        s, e, pf = s.replace("-", ""), e.replace("-", ""), pf.replace("-", "")
        if s > ymd(today):
            continue
        end = min(e, ymd(today))
        c = cache.get(cid)
        fresh = c and end < ymd(today - dt.timedelta(days=30)) and (c.get("ratio") is not None or c["trough"] < "20040211")
        if fresh:
            out.append(c)
            continue
        win = [k for k in keys if s <= k <= end]
        if not win:
            continue
        trough = min(win, key=lambda k: taiex[k])
        pre = [k for k in keys if pf <= k <= trough]
        peak = max(pre, key=lambda k: taiex[k])
        span = [k for k in keys if peak <= k <= trough]
        one_day = None
        for i in range(1, len(span)):
            r = taiex[span[i]] / taiex[span[i - 1]] - 1
            if one_day is None or r < one_day[1]:
                one_day = (span[i], r)
        ev = {"id": cid, "name": name, "year": cid[:4], "peak": peak, "peakClose": taiex[peak],
              "trough": trough, "troughClose": taiex[trough], "drop": round((taiex[trough] / taiex[peak] - 1) * 100, 1),
              "days": len(span) - 1, "ongoing": e > ymd(today),
              "worstDay": one_day[0] if one_day else None, "worstDayPct": round(one_day[1] * 100, 2) if one_day else None}
        log(f"股災 {cid} {name}：高點 {peak} → 低點 {trough}（{ev['drop']}%）")
        td = parse_ymd(trough)
        day = light_day(td) if trough >= "20010101" else None
        ev["ratio"] = day and (day.get("allRatio") or None)
        ev["twRatio"] = day and day.get("twRatio")
        ev["ratioScope"] = "all" if day and day.get("allRatio") else ("tw" if day and day.get("twRatio") else None)
        vix_real = vix_close(td) if td >= today - dt.timedelta(days=120) else None
        ev["vix"] = vix_real
        ev["vixEst"] = None if vix_real else est_vix(model, hv.get(trough))
        ev["hv20"] = round(hv[trough], 1) if trough in hv else None
        chg5 = None
        i = keys.index(trough)
        if day and day.get("allAmt") and i >= 5:
            a5 = margin_amt(parse_ymd(keys[i - 5]))
            if a5:
                chg5 = day["allAmt"] / a5 - 1
        ma20 = sum(taiex[k] for k in keys[i - 19:i + 1]) / 20 if i >= 19 else None
        ev["fear"], ev["fearParts"] = fear_parts(vix_real or ev["vixEst"], ev["ratio"] or ev["twRatio"], chg5,
                                                 taiex[trough], ma20, est=not vix_real)
        out.append(ev)
    return {"updated": dt.datetime.now(TZ).isoformat(timespec="minutes"), "vixModel": model, "events": out}


def process_day(d, cur):
    """處理一個交易日。cur 是處理前的個股狀態（會被複製後更新）。資料不齊回傳 None。"""
    log(f"處理 {d}")
    tm = twse_margin(d)
    tq = twse_quotes(d) if tm else None
    om = tpex_margin(d) if tq else None
    oq = tpex_quotes(d) if om else None
    if not (tm and tq and om and oq):
        log(f"  {d} 資料尚未齊全（上市融資 {bool(tm)}、上市行情 {bool(tq)}、上櫃融資 {bool(om)}、上櫃行情 {bool(oq)}）")
        return None
    official = twse_official(d)
    vix = vix_close(d)

    state = {k: list(v) for k, v in cur.items()}
    day = {"d": ymd(d)}
    stocks = {}
    for mkt, mg, qt in (("TW", tm, tq), ("OTC", om, oq)):
        f = FIN_RATIO[mkt]
        mv = 0.0
        est_loan = 0.0
        tmp = []
        for code, r in mg["rows"].items():
            key = f"{mkt}:{code}"
            close, chg = qt["px"].get(code, (None, 0))
            st = state.get(key)  # [avg, last_close, [ratio hist]]
            if close is None:
                close = st[1] if st else None
                chg = 0
            if close is None:
                continue
            if st is None or st[0] is None:
                avg = close
                hist = []
            else:
                avg, hist = st[0], st[2]
            base = r["prev"] + r["buy"]
            if base > 0:
                avg = (r["prev"] * avg + r["buy"] * close) / base
            if r["bal"] <= 0:
                avg = None
            state[key] = [avg, close, hist]
            mv += r["bal"] * 1000 * close
            if avg:
                est_loan += r["bal"] * 1000 * avg * f
            tmp.append((key, code, r, close, chg, avg))
        ratio = mv / mg["amt"] * 100 if mg["amt"] else None
        # 用官方融資金額校正個股估算成本，讓個股加總與官方總額一致
        k = mg["amt"] / est_loan if est_loan else 1.0
        for key, code, r, close, chg, avg in tmp:
            hist = state[key][2]
            est = None
            if avg:
                est = round(close / (avg * f * k) * 100, 1)
                hist = (hist + [est])[-STOCK_HIST:]
            state[key][2] = hist
            stocks[code] = [r["name"], mkt, close, round(chg, 2), int(r["bal"]), int(r["bal"] - r["prev"]),
                            round(avg * k, 2) if avg else None, est, hist]
        p = "tw" if mkt == "TW" else "otc"
        day[f"{p}Ratio"] = round(ratio, 2) if ratio else None
        day[f"{p}Amt"] = mg["amt"]
        day[f"{p}Mv"] = mv
        day[f"{p}Cal"] = round(k, 4)
    day["allAmt"] = day["twAmt"] + day["otcAmt"]
    day["allRatio"] = round((day["twMv"] + day["otcMv"]) / day["allAmt"] * 100, 2)
    for p in ("tw", "otc"):
        day.pop(f"{p}Mv")
    day["taiex"] = tq["taiex"]["close"] if tq["taiex"] else None
    day["taiexChg"] = tq["taiex"]["chg"] if tq["taiex"] else None
    day["taiexPct"] = tq["taiex"]["pct"] if tq["taiex"] else None
    day["vix"] = vix
    day["official"] = official
    log(f"  上市 {day['twRatio']}%、上櫃 {day['otcRatio']}%、全市場 {day['allRatio']}%、"
        f"官方 {official and official['keepRate']}、VIX {vix}")
    return day, state, stocks


# ---------------------------------------------------------------- 讀寫

def load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def parse_ymd(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="重新建立最近 N 個交易日")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)

    today = dt.datetime.now(TZ).date()
    state = load(STATE_FILE, None)
    market = load(MARKET_FILE, {"days": []})
    days_hist = market["days"]

    if a.backfill or not state:
        n = a.backfill or 120
        days = trading_days_between(today - dt.timedelta(days=int(n * 1.6) + 20), today)[-n:]
        state = {"date": None, "cur": {}, "prev_date": None, "prev": {}}
        days_hist = []
        log(f"重建歷史：{len(days)} 個交易日")
        backfill = True
    else:
        last = parse_ymd(state["date"])
        # 最後一天重新處理一次（補上較晚公布的官方數據），再往後補到今天
        days = [last]
        d = last + dt.timedelta(days=1)
        while d <= today:
            if d.weekday() < 5:
                days.append(d)
            d += dt.timedelta(days=1)
        backfill = False

    latest_stocks = None
    for d in days:
        key = ymd(d)
        if state["date"] == key:
            base = state["prev"]
        else:
            base = state["cur"]
        res = process_day(d, base)
        if res is None:
            if backfill:
                continue
            if d == days[0]:
                continue
            break
        day, new_state, stocks = res
        if state["date"] != key:
            state["prev"], state["prev_date"] = state["cur"], state["date"]
        state["cur"], state["date"] = new_state, key
        days_hist = [h for h in days_hist if h["d"] != key] + [day]
        days_hist.sort(key=lambda h: h["d"])
        latest_stocks = (key, stocks)

    # 補上先前沒抓到的 VIX（近 4 個月內，期交所只提供這段期間的檔案）
    cutoff = ymd(today - dt.timedelta(days=125))
    for h in days_hist:
        if not h.get("vix") and not h.get("light") and h["d"] >= cutoff:
            h["vix"] = vix_close(parse_ymd(h["d"]))
            if h["vix"]:
                log(f"  補上 {h['d']} VIX {h['vix']}")

    # 加權指數長期資料
    taiex = update_taiex(load(TAIEX_FILE, {}), today)
    save(TAIEX_FILE, taiex)

    def finish(days_list, final=False):
        days_list = [h for h in days_list if not h.get("missing")] if final else days_list
        hv = hv20_map(taiex)
        model = fit_vix_model(days_list, hv)
        keys = list(taiex)
        idx = {k: i for i, k in enumerate(keys)}
        real = [h for h in days_list if not h.get("missing")]
        for i, h in enumerate(real):
            if h["d"] in taiex and h.get("light"):
                h["taiex"] = taiex[h["d"]]
                j = idx[h["d"]]
                if j:
                    prev = taiex[keys[j - 1]]
                    h["taiexChg"] = round(h["taiex"] - prev, 2)
                    h["taiexPct"] = round((h["taiex"] / prev - 1) * 100, 2)
            h["hv20"] = round(hv[h["d"]], 2) if h["d"] in hv else None
            h["vixEst"] = None if h.get("vix") else est_vix(model, hv.get(h["d"]))
            closes_before = [taiex[k] for k in keys[max(0, idx.get(h["d"], 0) - 19):idx.get(h["d"], 0)]] if h["d"] in idx else []
            h["fear"], h["fearParts"] = fear_score(h, real[:i], closes_before)
        return days_list, hv, model

    deadline = START + BUDGET_MIN * 60

    def save_market(days_list):
        save(MARKET_FILE, {"updated": dt.datetime.now(TZ).isoformat(timespec="minutes"),
                           "days": [h for h in days_list if not h.get("missing")][-MARKET_KEEP:]})

    # 股災表（歷史事件結果會快取）
    days_hist, hv, model = finish(days_hist)
    try:
        crashes = compute_crashes(taiex, hv, model, today, load(CRASH_FILE, None))
        save(CRASH_FILE, crashes)
    except Exception as e:  # noqa: BLE001
        log(f"股災表計算失敗：{e}")

    # 補 3 年走勢
    days_hist = fill_history(days_hist, taiex, today, deadline, save_market)
    days_hist, hv, model = finish(days_hist, final=True)
    days_hist = days_hist[-MARKET_KEEP:]
    log(f"VIX 估算模型：{model}")

    save(STATE_FILE, state)
    save_market(days_hist)
    if latest_stocks:
        save(STOCKS_FILE, {"d": latest_stocks[0],
                           "fields": ["name", "mkt", "close", "chg", "bal", "balChg", "avg", "ratio", "hist"],
                           "items": latest_stocks[1]})
    log(f"完成，最新資料日 {state['date']}，共 {len(days_hist)} 天")
    if not days_hist:
        sys.exit(1)


if __name__ == "__main__":
    main()
