"""分析指定地址的 BUY 操作在 5 分钟周期内的秒级位置分布。

每条 buy 的时间戳取 offset_seconds = (minute % 5) * 60 + second，
得到 0-299 秒的分布，看 buy 操作集中在 5 分钟窗口的哪个位置。

Usage:
    python analyze_buy_time_distribution.py
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com/activity"
ADDRESS = "0x853593cAAA41bC13eDdcfE446E38542e8a0B25dF"
OUTPUT_DIR = Path(__file__).resolve().parent / "analyze" / "output"

_session = requests.Session()
_session.headers.update({"User-Agent": "PolyStrategy-Analyze/1.0"})


def fetch_all_buys(address: str) -> list[dict]:
    all_buys = []
    offset = 0
    page_size = 100
    empty_streak = 0

    while True:
        url = f"{DATA_API}?user={address}&limit={page_size}&offset={offset}"
        try:
            resp = _session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [ERROR] offset={offset}: {e}")
            break

        if not data:
            break

        prev = len(all_buys)
        for a in data:
            if a.get("type") != "TRADE":
                continue
            if a.get("side") != "BUY":
                continue
            all_buys.append(a)

        new = len(all_buys) - prev
        print(f"  offset={offset:>5d}  page={len(data):>3d}  buys={new:>3d}  total={len(all_buys)}")

        if new == 0:
            empty_streak += 1
            if empty_streak >= 10:
                break
        else:
            empty_streak = 0

        if len(data) < page_size:
            break

        offset += page_size
        time.sleep(0.15)

    return all_buys


def parse_ts(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def offset_in_5min(ts: int) -> int:
    """返回该时间戳在 5 分钟周期内的秒偏移 (0-299)。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt.minute % 5) * 60 + dt.second


def analyze(buys: list[dict]):
    # 按秒统计 (0-299)
    second_counts: dict[int, int] = defaultdict(int)
    second_usdc: dict[int, float] = defaultdict(float)

    for b in buys:
        ts = parse_ts(b.get("timestamp") or b.get("createdAt"))
        if ts is None:
            continue
        off = offset_in_5min(ts)
        second_counts[off] += 1
        second_usdc[off] += float(b.get("usdcSize", 0))

    # 按 10 秒段汇总打印
    print(f"\n5 分钟周期内 10 秒段分布 (共 {len(buys)} 笔 BUY):")
    print(f"  {'段':>10s}  {'次数':>5s}  {'USDC':>10s}  {'分布'}")
    print("  " + "-" * 70)

    seg_counts = []
    for start in range(0, 300, 10):
        c = sum(second_counts[s] for s in range(start, start + 10))
        u = sum(second_usdc[s] for s in range(start, start + 10))
        seg_counts.append((start, c, u))

    max_c = max(c for _, c, _ in seg_counts) if seg_counts else 1
    for start, c, u in seg_counts:
        m, s = divmod(start, 60)
        m2, s2 = divmod(start + 10, 60)
        label = f"{m}:{s:02d}-{m2}:{s2:02d}"
        bar = "█" * int(c / max_c * 40) if c > 0 else ""
        if c > 0:
            print(f"  {label:>10s}  {c:>5d}  {u:>10.2f}  {bar}")

    # 按分钟段 (0-4) 汇总
    print(f"\n按分钟汇总:")
    for m in range(5):
        c = sum(second_counts[s] for s in range(m * 60, (m + 1) * 60))
        u = sum(second_usdc[s] for s in range(m * 60, (m + 1) * 60))
        pct = c / len(buys) * 100 if buys else 0
        bar = "█" * int(pct / 100 * 40)
        print(f"  第 {m} 分钟 ({m}:00-{m}:59)  {c:>5d} 笔  {pct:>5.1f}%  ${u:>10,.2f}  {bar}")

    return second_counts, second_usdc


def write_html(buys: list[dict], second_counts: dict, second_usdc: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    chart_data = []
    for s in range(300):
        chart_data.append({
            "sec": s,
            "count": second_counts.get(s, 0),
            "usdc": round(second_usdc.get(s, 0), 2),
        })

    # 10 秒段数据
    seg_data = []
    for start in range(0, 300, 10):
        c = sum(second_counts.get(s, 0) for s in range(start, start + 10))
        u = sum(second_usdc.get(s, 0) for s in range(start, start + 10))
        m, sec = divmod(start, 60)
        seg_data.append({"start": start, "label": f"{m}:{sec:02d}", "count": c, "usdc": round(u, 2)})

    data_js = json.dumps(chart_data, ensure_ascii=False)
    seg_js = json.dumps(seg_data, ensure_ascii=False)
    total_count = len(buys)
    total_usdc = sum(float(b.get("usdcSize", 0)) for b in buys)

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>BUY 操作 5 分钟周期内位置分布 — {ADDRESS[:10]}...</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, monospace; background: #0d1117; color: #c9d1d9; padding: 24px; }}
h1 {{ font-size: 18px; margin-bottom: 8px; color: #f0f6fc; }}
.subtitle {{ font-size: 12px; color: #8b949e; margin-bottom: 20px; }}
.stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
.stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 18px; }}
.stat .label {{ font-size: 11px; color: #8b949e; }}
.stat .value {{ font-size: 20px; font-weight: 600; color: #58a6ff; }}
.chart-wrap {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
.chart-title {{ font-size: 13px; color: #8b949e; margin-bottom: 8px; }}
.chart {{ display: flex; align-items: flex-end; gap: 0; height: 220px; position: relative; }}
.bar {{ min-width: 1px; flex: 1; cursor: crosshair; transition: background 0.05s; }}
.bar:hover {{ filter: brightness(1.4); }}
.x-axis {{ display: flex; justify-content: space-between; font-size: 10px; color: #8b949e; margin-top: 4px; padding: 0 0; }}
.minute-sep {{ position: absolute; top: 0; bottom: 0; width: 1px; background: #30363d; pointer-events: none; }}
.tooltip {{ position: fixed; background: #1c2128; border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none; display: none; z-index: 10; }}
.toggle {{ display: flex; gap: 8px; margin-bottom: 12px; }}
.toggle button {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }}
.toggle button.active {{ background: #58a6ff; color: #0d1117; border-color: #58a6ff; }}
.tabs {{ display: flex; gap: 0; margin-bottom: 0; }}
.tab {{ padding: 8px 16px; cursor: pointer; border: 1px solid #30363d; border-bottom: none; border-radius: 6px 6px 0 0; background: #0d1117; color: #8b949e; font-size: 13px; }}
.tab.active {{ background: #161b22; color: #f0f6fc; }}
.table-wrap {{ background: #161b22; border: 1px solid #30363d; border-radius: 0 8px 8px 8px; overflow-x: auto; max-height: 500px; overflow-y: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ background: #1c2128; color: #8b949e; text-align: left; padding: 6px 10px; position: sticky; top: 0; }}
td {{ padding: 5px 10px; border-top: 1px solid #21262d; }}
tr:hover td {{ background: #1c2128; }}
</style>
</head>
<body>
<h1>BUY 操作在 5 分钟周期内的位置分布</h1>
<div class="subtitle">地址: {ADDRESS}<br>每条 buy 取 offset = (minute%5)*60 + second，映射到 0-299 秒，看操作集中在 5 分钟窗口的哪个位置</div>

<div class="stats" id="stats"></div>

<div class="toggle">
  <button class="active" onclick="setMetric('count')">按次数</button>
  <button onclick="setMetric('usdc')">按金额</button>
</div>

<div class="chart-wrap">
  <div class="chart-title">逐秒分布 (0-299秒)</div>
  <div class="chart" id="chart-sec"></div>
  <div class="x-axis"><span>0:00</span><span>1:00</span><span>2:00</span><span>3:00</span><span>4:00</span><span>5:00</span></div>
</div>

<div class="chart-wrap">
  <div class="chart-title">10 秒段分布</div>
  <div class="chart" id="chart-seg" style="height:180px;gap:2px;"></div>
  <div class="x-axis"><span>0:00</span><span>1:00</span><span>2:00</span><span>3:00</span><span>4:00</span><span>5:00</span></div>
</div>

<div class="tooltip" id="tooltip"></div>

<div class="tabs">
  <div class="tab active" data-tab="seg10" onclick="switchTab('seg10')">10秒段</div>
  <div class="tab" data-tab="minute" onclick="switchTab('minute')">按分钟</div>
  <div class="tab" data-tab="persec" onclick="switchTab('persec')">逐秒</div>
</div>
<div class="table-wrap">
  <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
</div>

<script>
const SEC = {data_js};
const SEG = {seg_js};
let metric = 'count';
let currentTab = 'seg10';

const totalCount = {total_count};
const totalUsdc = {total_usdc:.2f};

// per-minute stats
const minStats = [0,1,2,3,4].map(m => {{
  const c = SEC.slice(m*60, (m+1)*60).reduce((s,d) => s + d.count, 0);
  const u = SEC.slice(m*60, (m+1)*60).reduce((s,d) => s + d.usdc, 0);
  return {{m, c, u, pct: totalCount > 0 ? (c/totalCount*100).toFixed(1) : '0'}};
}});
const peakMin = minStats.reduce((a,b) => b.c > a.c ? b : a, minStats[0]);

document.getElementById('stats').innerHTML = `
  <div class="stat"><div class="label">总 BUY 次数</div><div class="value">${{totalCount}}</div></div>
  <div class="stat"><div class="label">总金额</div><div class="value">$${{totalUsdc.toLocaleString('en',{{maximumFractionDigits:0}})}}</div></div>
  <div class="stat"><div class="label">最集中分钟</div><div class="value">第 ${{peakMin.m}} 分钟 (${{peakMin.pct}}%)</div></div>
` + minStats.map(ms => `
  <div class="stat"><div class="label">第 ${{ms.m}} 分钟</div><div class="value">${{ms.c}} <span style="font-size:12px;color:#8b949e">(${{ms.pct}}%)</span></div></div>
`).join('');

function val(d) {{ return metric === 'count' ? d.count : d.usdc; }}

function setMetric(m) {{
  metric = m;
  document.querySelectorAll('.toggle button').forEach(b => b.classList.toggle('active', b.textContent.includes(m === 'count' ? '次数' : '金额')));
  renderCharts();
}}

function renderCharts() {{
  // per-second chart
  const maxS = Math.max(...SEC.map(val)) || 1;
  const chart1 = document.getElementById('chart-sec');
  chart1.innerHTML = SEC.map((d, i) => {{
    const v = val(d);
    const h = Math.max(v / maxS * 200, v > 0 ? 1 : 0);
    const color = `hsl(${{210 + (d.sec / 300) * 40}}, 70%, ${{50 + v/maxS*20}}%)`;
    return `<div class="bar" style="height:${{h}}px;background:${{color}}" data-type="sec" data-idx="${{i}}"></div>`;
  }}).join('') + [60,120,180,240].map(s => `<div class="minute-sep" style="left:${{s/300*100}}%"></div>`).join('');

  // 10-second segment chart
  const maxG = Math.max(...SEG.map(val)) || 1;
  const chart2 = document.getElementById('chart-seg');
  chart2.innerHTML = SEG.map((d, i) => {{
    const v = val(d);
    const h = Math.max(v / maxG * 160, v > 0 ? 2 : 0);
    return `<div class="bar" style="height:${{h}}px;background:#58a6ff;border-radius:2px 2px 0 0" data-type="seg" data-idx="${{i}}"></div>`;
  }}).join('') + [6,12,18,24].map(i => `<div class="minute-sep" style="left:${{i/30*100}}%"></div>`).join('');
}}

const tooltip = document.getElementById('tooltip');
document.addEventListener('mousemove', e => {{
  const bar = e.target.closest('.bar');
  if (!bar || !bar.dataset.type) {{ tooltip.style.display = 'none'; return; }}
  const type = bar.dataset.type;
  const idx = +bar.dataset.idx;
  let html = '';
  if (type === 'sec') {{
    const d = SEC[idx];
    const m = Math.floor(d.sec / 60), s = d.sec % 60;
    html = `<b>${{m}}:${{String(s).padStart(2,'0')}}</b> (第 ${{d.sec}} 秒)<br>${{d.count}} 笔 · $${{d.usdc.toLocaleString()}}`;
  }} else {{
    const d = SEG[idx];
    const end = d.start + 10;
    const m2 = Math.floor(end/60), s2 = end%60;
    html = `<b>${{d.label}}-${{m2}}:${{String(s2).padStart(2,'0')}}</b><br>${{d.count}} 笔 · $${{d.usdc.toLocaleString()}}`;
  }}
  tooltip.innerHTML = html;
  tooltip.style.display = 'block';
  tooltip.style.left = (e.clientX + 12) + 'px';
  tooltip.style.top = (e.clientY - 50) + 'px';
}});
document.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  renderTable();
}}

function renderTable() {{
  const maxBar = (arr, key) => Math.max(...arr.map(d => d[key])) || 1;

  if (currentTab === 'seg10') {{
    document.getElementById('thead').innerHTML = '<tr><th>段</th><th>次数</th><th>占比</th><th>USDC</th><th>分布</th></tr>';
    const mc = maxBar(SEG, 'count');
    document.getElementById('tbody').innerHTML = SEG.filter(d => d.count > 0).map(d => {{
      const end = d.start + 10;
      const m2 = Math.floor(end/60), s2 = end%60;
      const pct = totalCount > 0 ? (d.count/totalCount*100).toFixed(1) : '0';
      const bar = '█'.repeat(Math.ceil(d.count / mc * 30));
      return `<tr><td>${{d.label}}-${{m2}}:${{String(s2).padStart(2,'0')}}</td><td>${{d.count}}</td><td>${{pct}}%</td><td>${{d.usdc.toLocaleString()}}</td><td style="color:#58a6ff">${{bar}}</td></tr>`;
    }}).join('');
  }} else if (currentTab === 'minute') {{
    document.getElementById('thead').innerHTML = '<tr><th>分钟</th><th>次数</th><th>占比</th><th>USDC</th><th>分布</th></tr>';
    const mc = Math.max(...minStats.map(m => m.c)) || 1;
    document.getElementById('tbody').innerHTML = minStats.map(ms => {{
      const bar = '█'.repeat(Math.ceil(ms.c / mc * 30));
      return `<tr><td>第 ${{ms.m}} 分钟 (${{ms.m}}:00-${{ms.m}}:59)</td><td>${{ms.c}}</td><td>${{ms.pct}}%</td><td>${{ms.u.toLocaleString()}}</td><td style="color:#58a6ff">${{bar}}</td></tr>`;
    }}).join('');
  }} else {{
    document.getElementById('thead').innerHTML = '<tr><th>秒</th><th>次数</th><th>USDC</th></tr>';
    document.getElementById('tbody').innerHTML = SEC.filter(d => d.count > 0).map(d => {{
      const m = Math.floor(d.sec/60), s = d.sec%60;
      return `<tr><td>${{m}}:${{String(s).padStart(2,'0')}} (第${{d.sec}}秒)</td><td>${{d.count}}</td><td>${{d.usdc.toLocaleString()}}</td></tr>`;
    }}).join('');
  }}
}}

renderCharts();
renderTable();
</script>
</body>
</html>"""

    path = OUTPUT_DIR / "buy_time_distribution.html"
    path.write_text(html, encoding="utf-8")
    print(f"\nHTML 报告: {path}")


def main():
    print(f"获取 {ADDRESS} 的所有 BUY 活动...")
    buys = fetch_all_buys(ADDRESS)
    print(f"\n共 {len(buys)} 条 BUY 记录")

    if not buys:
        print("没有找到买入记录")
        return

    timestamps = [parse_ts(b.get("timestamp") or b.get("createdAt")) for b in buys]
    timestamps = [t for t in timestamps if t]
    if timestamps:
        earliest = datetime.fromtimestamp(min(timestamps), tz=timezone.utc)
        latest = datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
        print(f"时间范围: {earliest:%Y-%m-%d %H:%M:%S} ~ {latest:%Y-%m-%d %H:%M:%S} UTC")

    second_counts, second_usdc = analyze(buys)
    write_html(buys, second_counts, second_usdc)


if __name__ == "__main__":
    main()
