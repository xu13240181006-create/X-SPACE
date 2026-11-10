#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
恐慌抄底信号器 v3 - 数据更新+自动部署脚本
================================
从 EODHD + FRED 拉取最新数据，重新生成 index.html，
并通过 Netlify API 自动部署（不需要 netlify-cli）。

v3.2 变更：
  - 价格改用 EODHD adjusted_close（含拆股、分红调整）
  - 宏观改为周频时点数据，按每周最后交易日对齐、考虑发布滞后：
      VIXCLS(当日) / T10Y2Y(当日) / NFCI(取一周前已发布值) / SAHMREALTIME(滞后1~2个月)
    删除已截断的 BAMLH0A0HYM2（FRED 只剩近3年）和已停更的 USSLIND
  - SPY/QQQ 每行: [日期, 价格, 情绪, VIX, YC, NFCI, Sahm]；BTC 每行: [日期, 价格, 情绪]

环境变量：
  EODHD_API_KEY     - EODHD 的 API key
  LOCAL_DATA_DIR    - 可选。本地数据缓存目录：存在 eodhd/<代码>.json、fred/<序列>.csv 时直接读取，
                      否则联网下载并写入缓存（用于本地离线复现）
  NETLIFY_TOKEN     - Netlify Personal Access Token
  NETLIFY_SITE_ID   - Netlify 站点名 (例如 sanhudashiqiu)

使用：
  python build_signal_html.py              # 生成+自动部署
  python build_signal_html.py --no-deploy  # 只生成不部署

依赖：pip install requests
"""

import os
import sys
import json
import math
import csv
import hashlib
import time
from io import StringIO
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("✗ 请先安装: pip install requests")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
TEMPLATE_PATH = SCRIPT_DIR / 'template_v32.html'
OUTPUT_PATH = SCRIPT_DIR / 'v32.html'

SPY_START = '1993-01-29'
QQQ_START = '1999-03-10'
BTC_START = '2014-09-17'
FRED_START = '1990-01-01'
SENTIMENT_LOOKBACK = 10
LOCAL_DATA_DIR = os.environ.get('LOCAL_DATA_DIR')


def _cache_path(*parts):
    if not LOCAL_DATA_DIR:
        return None
    p = Path(LOCAL_DATA_DIR).joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def fetch_eodhd_daily(symbol, api_key, start_date):
    """返回 [(日期, 复权收盘价)]。adjusted_close 缺失时退回 close。"""
    print(f"  拉取 {symbol} from {start_date}... ", end='', flush=True)
    cache = _cache_path('eodhd', f'{symbol}.json')
    try:
        if cache and cache.exists():
            data = json.loads(cache.read_text(encoding='utf-8'))
            print("(本地缓存) ", end='')
        else:
            url = f"https://eodhd.com/api/eod/{symbol}"
            params = {'api_token': api_key, 'period': 'd', 'from': start_date, 'fmt': 'json'}
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if cache:
                cache.write_text(json.dumps(data), encoding='utf-8')
        rows = []
        for d in data:
            px = d.get('adjusted_close')
            if px is None:
                px = d.get('close')
            if px is None:
                continue
            rows.append((d['date'], round(float(px), 4)))
        rows.sort(key=lambda x: x[0])
        if rows:
            print(f"✓ {len(rows)} 条 ({rows[0][0]} → {rows[-1][0]})")
        else:
            print("✗ 数据为空")
        return rows
    except Exception as e:
        print(f"✗ 失败: {e}")
        return []


def fetch_fred_series(series_id, start_date):
    """返回按日期排序的 [(YYYY-MM-DD, 值)]，保留原始频率（日/周/月）。"""
    print(f"  拉取 FRED/{series_id}... ", end='', flush=True)
    cache = _cache_path('fred', f'{series_id}.csv')
    try:
        if cache and cache.exists():
            text = cache.read_text(encoding='utf-8')
            print("(本地缓存) ", end='')
        else:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start_date}"
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            text = r.text
            if cache:
                cache.write_text(text, encoding='utf-8')
        out = []
        for row in csv.DictReader(StringIO(text)):
            date_key = next((k for k in row if k and 'DATE' in k.upper()), None)
            val_key = next((k for k in row if k and k != date_key), None)
            if not date_key or not val_key:
                continue
            ds, vs = row[date_key].strip(), row[val_key].strip()
            if not ds or vs in ('.', ''):
                continue
            try:
                out.append((ds[:10], float(vs)))
            except ValueError:
                pass
        out.sort()
        if out:
            print(f"✓ {len(out)} 条 ({out[0][0]} → {out[-1][0]})")
        else:
            print("✗ 数据为空")
        return out
    except Exception as e:
        print(f"✗ 失败: {e}")
        return []


def fetch_vix_daily(start_date):
    """日线 VIX 收盘。优先 CBOE 官方历史文件（当日收盘后即更新）；失败时退回 FRED VIXCLS（晚一天）。"""
    print("  拉取 CBOE/VIX 日线... ", end='', flush=True)
    cache = _cache_path('cboe', 'VIX_History.csv')
    try:
        if cache and cache.exists():
            text = cache.read_text(encoding='utf-8')
            print("(本地缓存) ", end='')
        else:
            r = requests.get('https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv',
                             timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            r.raise_for_status()
            text = r.text
            if cache:
                cache.write_text(text, encoding='utf-8')
        out = []
        for row in csv.DictReader(StringIO(text)):
            try:
                d = datetime.strptime(row['DATE'].strip(), '%m/%d/%Y').strftime('%Y-%m-%d')
                out.append((d, float(row['CLOSE'])))
            except Exception:
                continue
        out = sorted(x for x in out if x[0] >= start_date)
        if out:
            print(f"✓ {len(out)} 条 ({out[0][0]} → {out[-1][0]})")
            return out
        print("✗ 数据为空，改用 FRED")
    except Exception as e:
        print(f"✗ 失败: {e}，改用 FRED")
    return fetch_fred_series('VIXCLS', start_date)


def asof_values(series, dates, lag_days=0):
    """对每个日期 D，取 观测日 ≤ D - lag_days 的最后一个值（时点取值，无前视）。"""
    out, k, last = [], 0, None
    for ds in dates:
        cutoff = (datetime.strptime(ds, '%Y-%m-%d').date() - timedelta(days=lag_days)).isoformat()
        while k < len(series) and series[k][0] <= cutoff:
            last = series[k][1]
            k += 1
        out.append(last)
    return out


def sahm_asof(series, dates):
    """SAHMREALTIME：M 月数据在 M+1 月初随非农公布。每月 10 号前用 M-2 月，之后用 M-1 月。"""
    by_month = {ds[:7]: v for ds, v in series}
    out = []
    for ds in dates:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
        back = 1 if d.day >= 10 else 2
        y, m = d.year, d.month - back
        while m <= 0:
            m += 12
            y -= 1
        out.append(by_month.get(f'{y}-{m:02d}'))
    return out


def build_macro_columns(weekly_dates, fred):
    """返回每周的 (VIX, YC, NFCI, Sahm)。NFCI 的周五数据在 5 天后（周三）发布 → 只取观测日 ≤ D-5 的值。"""
    vix = asof_values(fred.get('VIXCLS', []), weekly_dates)
    yc = asof_values(fred.get('T10Y2Y', []), weekly_dates)
    nfci = asof_values(fred.get('NFCI', []), weekly_dates, lag_days=5)
    sahm = sahm_asof(fred.get('SAHMREALTIME', []), weekly_dates)
    return list(zip(vix, yc, nfci, sahm))


def daily_to_weekly(daily_rows):
    weekly_dict = {}
    for date_str, price in daily_rows:
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
            yr, wk, _ = dt.isocalendar()
            weekly_dict[f"{yr}-{wk:02d}"] = (date_str, price)
        except Exception:
            continue
    return sorted(weekly_dict.values(), key=lambda x: x[0])


def compute_sentiment(weekly, lookback=SENTIMENT_LOOKBACK):
    closes = [w[1] for w in weekly]
    n = len(closes)
    sentiments = [50] * n
    raw_mom = [0.0] * n
    raw_vol = [0.0] * n
    for i in range(n):
        if i < lookback:
            continue
        raw_mom[i] = (closes[i] - closes[i - lookback]) / closes[i - lookback]
        wk_rets = [(closes[j] - closes[j-1]) / closes[j-1] for j in range(i - lookback + 1, i + 1)]
        raw_vol[i] = math.sqrt(sum(r * r for r in wk_rets) / lookback)
    for i in range(lookback, n):
        idx = list(range(lookback, i + 1))
        mom_rank = sum(1 for j in idx if raw_mom[j] < raw_mom[i]) / len(idx) * 100
        vol_rank = sum(1 for j in idx if raw_vol[j] < raw_vol[i]) / len(idx) * 100
        score = mom_rank * 0.6 + (100 - vol_rank) * 0.4
        sentiments[i] = max(0, min(100, round(score)))
    return [(weekly[i][0], weekly[i][1], sentiments[i]) for i in range(n)]


def format_weekly_js(weekly_with_sent):
    return ',\n'.join([f'["{d}",{p},{s}]' for d, p, s in weekly_with_sent])


def _js_num(v, nd):
    return 'null' if v is None else f'{round(v, nd):g}'


def format_stock_js(weekly_with_sent, macro):
    lines = []
    for (d, p, s), (vix, yc, nfci, sahm) in zip(weekly_with_sent, macro):
        lines.append(f'["{d}",{p},{s},{_js_num(vix, 2)},{_js_num(yc, 2)},{_js_num(nfci, 3)},{_js_num(sahm, 2)}]')
    return ',\n'.join(lines)


DAY_EPOCH = date(1990, 1, 1)


def format_daily_js(daily_rows, vix_series):
    """日线数据（用于「日线收盘锁定」判定）：每个交易日 [距1990-01-01的天数, 复权收盘, 当日VIX收盘]。"""
    vix = asof_values(vix_series, [d for d, _ in daily_rows])
    cells = []
    for (d, p), v in zip(daily_rows, vix):
        n = (datetime.strptime(d, '%Y-%m-%d').date() - DAY_EPOCH).days
        cells.append(f'[{n},{p:g},{_js_num(v, 2)}]')
    return ',\n'.join(','.join(cells[i:i + 8]) for i in range(0, len(cells), 8))


def inject_data(template, data_map):
    result = template
    for placeholder, value in data_map.items():
        ms = f'// {{{{{placeholder}_START}}}}'
        me = f'// {{{{{placeholder}_END}}}}'
        if ms in result and me in result:
            s = result.index(ms) + len(ms)
            e = result.index(me)
            result = result[:s] + '\n' + value + '\n' + result[e:]
        else:
            print(f"  ⚠ 模板中未找到占位符: {placeholder}")
    return result


def get_site_id(site_name, token):
    r = requests.get(
        'https://api.netlify.com/api/v1/sites',
        headers={'Authorization': f'Bearer {token}'},
        params={'filter': 'all'},
        timeout=30
    )
    r.raise_for_status()
    for s in r.json():
        if s.get('name') == site_name or s.get('site_id') == site_name:
            return s['site_id']
    return site_name


def deploy_to_netlify(file_path, token, site_name):
    print(f"\n部署到 Netlify...")
    try:
        site_id = get_site_id(site_name, token)
        print(f"  site_id: {site_id}")
    except Exception as e:
        print(f"  ✗ 查站点失败: {e}")
        return False

    content = file_path.read_bytes()
    sha1 = hashlib.sha1(content).hexdigest()

    print(f"  创建部署...")
    r = requests.post(
        f'https://api.netlify.com/api/v1/sites/{site_id}/deploys',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'files': {'/index.html': sha1}},
        timeout=30
    )
    if r.status_code >= 400:
        print(f"  ✗ 失败 {r.status_code}: {r.text[:300]}")
        return False
    deploy = r.json()
    deploy_id = deploy['id']

    if sha1 in deploy.get('required', []):
        print(f"  上传文件...")
        up = requests.put(
            f'https://api.netlify.com/api/v1/deploys/{deploy_id}/files/index.html',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/octet-stream'},
            data=content,
            timeout=60
        )
        if up.status_code >= 400:
            print(f"  ✗ 上传失败 {up.status_code}: {up.text[:300]}")
            return False
    else:
        print(f"  文件已存在服务器，跳过上传")

    print(f"  等待发布...")
    for _ in range(20):
        time.sleep(2)
        chk = requests.get(
            f'https://api.netlify.com/api/v1/deploys/{deploy_id}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15
        )
        if chk.status_code < 400:
            state = chk.json().get('state')
            if state == 'ready':
                url = chk.json().get('ssl_url') or chk.json().get('url')
                print(f"  ✅ 部署成功: {url}")
                return True
            elif state == 'error':
                print(f"  ✗ 部署出错: {chk.json().get('error_message')}")
                return False
    print(f"  ⚠ 超时，请去 Netlify 后台查看")
    return False


def main():
    no_deploy = '--no-deploy' in sys.argv

    print("═" * 60)
    print("  恐慌抄底信号器 · 数据更新+部署脚本")
    print("═" * 60)

    api_key = os.environ.get('EODHD_API_KEY')
    if not api_key:
        print("\n✗ 未找到 EODHD_API_KEY")
        sys.exit(1)
    print(f"✓ EODHD_API_KEY (len={len(api_key)})")

    if not TEMPLATE_PATH.exists():
        print(f"\n✗ 模板不存在: {TEMPLATE_PATH}")
        sys.exit(1)
    print(f"✓ 模板: {TEMPLATE_PATH.name}")

    print(f"\n[1/3] 拉取 EODHD 日线")
    spy_d = fetch_eodhd_daily('SPY.US', api_key, SPY_START)
    qqq_d = fetch_eodhd_daily('QQQ.US', api_key, QQQ_START)
    btc_d = fetch_eodhd_daily('BTC-USD.CC', api_key, BTC_START)
    if not spy_d or not qqq_d or not btc_d:
        sys.exit(1)

    print(f"\n[2/3] 日线转周线 + 算情绪")
    spy_w = compute_sentiment(daily_to_weekly(spy_d))
    qqq_w = compute_sentiment(daily_to_weekly(qqq_d))
    btc_w = compute_sentiment(daily_to_weekly(btc_d))
    print(f"  SPY: {len(spy_w)}周, 最新{spy_w[-1][0]}")
    print(f"  QQQ: {len(qqq_w)}周, 最新{qqq_w[-1][0]}")
    print(f"  BTC: {len(btc_w)}周, 最新{btc_w[-1][0]}")

    print(f"\n[3/3] 拉取 FRED 宏观数据（周频时点对齐）")
    fred = {sid: fetch_fred_series(sid, FRED_START) for sid in ('T10Y2Y', 'NFCI', 'SAHMREALTIME')}
    fred['VIXCLS'] = fetch_vix_daily(FRED_START)
    missing = [sid for sid, rows in fred.items() if not rows]
    if missing:
        print(f"✗ FRED 序列缺失: {missing}，中止以免生成没有宏观数据的页面")
        sys.exit(1)
    spy_m = build_macro_columns([w[0] for w in spy_w], fred)
    qqq_m = build_macro_columns([w[0] for w in qqq_w], fred)
    meta = {
        'built': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'asof': {sid: rows[-1][0] for sid, rows in fred.items()},
        'price': {'SPY': spy_w[-1][0], 'QQQ': qqq_w[-1][0], 'BTC': btc_w[-1][0]},
    }

    print(f"\n生成 index.html")
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    data_map = {
        'SPY_DATA': format_stock_js(spy_w, spy_m),
        'QQQ_DATA': format_stock_js(qqq_w, qqq_m),
        'BTC_DATA': format_weekly_js(btc_w),
        'SPY_DAILY': format_daily_js(spy_d, fred['VIXCLS']),
        'QQQ_DAILY': format_daily_js(qqq_d, fred['VIXCLS']),
        'META':'const META=' + json.dumps(meta, ensure_ascii=False, separators=(',', ':')) + ';',
    }
    result = inject_data(template, data_map)
    OUTPUT_PATH.write_text(result, encoding='utf-8')
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"✓ {OUTPUT_PATH.name} ({size_kb:.1f} KB)")

    if no_deploy:
        print("\n(--no-deploy 跳过部署)")
        return

    token = os.environ.get('NETLIFY_TOKEN')
    site_name = os.environ.get('NETLIFY_SITE_ID', 'sanhudashiqiu')
    if not token:
        print("\n⚠ 未设置 NETLIFY_TOKEN，无法自动部署")
        print("  获取token: https://app.netlify.com/user/applications")
        print("  设置:    setx NETLIFY_TOKEN \"xxx\"")
        return

    ok = deploy_to_netlify(OUTPUT_PATH, token, site_name)
    if ok:
        print(f"\n🎉 全部完成! 打开 https://{site_name}.netlify.app")


if __name__ == '__main__':
    main()
