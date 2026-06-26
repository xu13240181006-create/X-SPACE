#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
恐慌抄底信号器 v3 - 数据更新+自动部署脚本
================================
从 EODHD + FRED 拉取最新数据，重新生成 index.html，
并通过 Netlify API 自动部署（不需要 netlify-cli）。

环境变量：
  EODHD_API_KEY     - EODHD 的 API key
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
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("✗ 请先安装: pip install requests")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
TEMPLATE_PATH = SCRIPT_DIR / 'template_v3.html'
OUTPUT_PATH = SCRIPT_DIR / 'index.html'

SPY_START = '1993-01-29'
QQQ_START = '1999-03-10'
BTC_START = '2014-09-17'
FRED_START = '1999-01-01'
SENTIMENT_LOOKBACK = 10


def fetch_eodhd_daily(symbol, api_key, start_date):
    url = f"https://eodhd.com/api/eod/{symbol}"
    params = {'api_token': api_key, 'period': 'd', 'from': start_date, 'fmt': 'json'}
    print(f"  拉取 {symbol} from {start_date}... ", end='', flush=True)
    try:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        rows = [(d['date'], float(d['close'])) for d in data if d.get('close') is not None]
        rows.sort(key=lambda x: x[0])
        if rows:
            print(f"✓ {len(rows)} 条 ({rows[0][0]} → {rows[-1][0]})")
        else:
            print("✗ 数据为空")
        return rows
    except Exception as e:
        print(f"✗ 失败: {e}")
        return []


def fetch_fred_csv(series_id, start_date):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start_date}"
    print(f"  拉取 FRED/{series_id}... ", end='', flush=True)
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(StringIO(r.text))
        data = {}
        for row in reader:
            date_key = next((k for k in row if 'DATE' in k.upper()), None)
            val_key = next((k for k in row if k != date_key and k), None)
            if not date_key or not val_key:
                continue
            date_str, val_str = row[date_key].strip(), row[val_key].strip()
            if not date_str or val_str in ('.', ''):
                continue
            try:
                data[date_str[:7]] = float(val_str)
            except ValueError:
                pass
        print(f"✓ {len(data)} 个月份")
        return data
    except Exception as e:
        print(f"✗ 失败: {e}")
        return {}


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

    print(f"\n[3/3] 拉取 FRED 经济数据")
    yc = fetch_fred_csv('T10Y2Y', FRED_START)
    hy = fetch_fred_csv('BAMLH0A0HYM2', FRED_START)
    lei = fetch_fred_csv('USSLIND', FRED_START)

    print(f"\n生成 index.html")
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    data_map = {
        'SPY_DATA': format_weekly_js(spy_w),
        'QQQ_DATA': format_weekly_js(qqq_w),
        'BTC_DATA': format_weekly_js(btc_w),
        'ECON_YC': 'const ECON_YC=' + json.dumps(yc, separators=(',', ':')) + ';',
        'ECON_HY': 'const ECON_HY=' + json.dumps(hy, separators=(',', ':')) + ';',
        'ECON_LEI': 'const ECON_LEI=' + json.dumps(lei, separators=(',', ':')) + ';',
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
