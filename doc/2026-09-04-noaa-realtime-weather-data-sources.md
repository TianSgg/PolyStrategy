# NOAA 实时气象数据源调研

> 调研日期: 2026-09-04
> 目标: 为 Polymarket 天气市场策略寻找分钟级/秒级实时温度数据源

## 背景

当前系统的天气信号来自 Polymarket 订单簿变化。如果能接入机场气象站的实时温度数据，可以在市场价格反映之前提前感知温度变化，获得信息优势。

ASOS (Automated Surface Observing Systems) 部署在美国约 900 个机场，每 **1 分钟** 采集一次气象数据（温度、露点、风速风向、气压、能见度、降水等）。核心问题是：这些 1 分钟数据如何实时获取。

---

## ASOS 数据流向

```
ASOS 传感器（每1分钟采集）
    │
    ├──→ NOAAPORT 卫星广播 ──→ Unidata LDM/IDD（延迟 ~1-2 分钟）
    │                              需申请，原则上面向学术机构
    │
    ├──→ FAA SWIM ──→ 航空系统用户（延迟 ~1-2 分钟）
    │                    需 FAA 认证，面向航空运营商
    │
    ├──→ NWS 处理管道 ──→ api.weather.gov（延迟 ~19 分钟，每5分钟更新）
    │
    ├──→ METAR 编码 ──→ aviationweather.gov（延迟 ~5 分钟，每1小时 + SPECI）
    │
    └──→ NCEI 归档 ──→ 1-min 文件下载（延迟 ~2 天）
```

---

## 数据源详细对比

### 1. NWS API (api.weather.gov)

- **频率**: 每 5 分钟一条观测
- **延迟**: ~19 分钟（实测）
- **认证**: 免费，无需 API key，仅需设置 `User-Agent` header
- **格式**: GeoJSON
- **覆盖**: 美国所有 ASOS/AWOS 站点

**实测数据 (KSEA, 2026-09-04)**:
```
当前 UTC:  02:18:48
最新观测: 02:00:00  温度: 16°C  延迟: 18.8 min
上一条:   01:55:00  温度: 16°C  延迟: 23.8 min
再上一条: 01:53:00  温度: 15.6°C 延迟: 25.8 min
```

**API 示例**:
```bash
# 最新观测
curl -H "User-Agent: PolyStrategy" \
  "https://api.weather.gov/stations/KSEA/observations/latest"

# 最近 N 条
curl -H "User-Agent: PolyStrategy" \
  "https://api.weather.gov/stations/KSEA/observations?limit=12"
```

**返回字段**:
| 字段 | 说明 | 示例 |
|------|------|------|
| `timestamp` | 观测时间 (ISO 8601) | `2026-09-04T02:00:00+00:00` |
| `temperature.value` | 温度 (°C) | `16` |
| `dewpoint.value` | 露点 (°C) | `12` |
| `windSpeed.value` | 风速 (km/h) | `16.668` |
| `windDirection.value` | 风向 (°) | `260` |
| `barometricPressure.value` | 气压 (Pa) | `101015.98` |
| `visibility.value` | 能见度 (m) | `16093.44` |
| `relativeHumidity.value` | 相对湿度 (%) | `52.3` |

**优点**: 免费、稳定、JSON 格式、覆盖广
**缺点**: 延迟 ~19 分钟，5 分钟粒度

---

### 2. METAR / SPECI (aviationweather.gov)

- **频率**: 整点 METAR (每小时) + 不定时 SPECI (天气剧变时自动触发)
- **延迟**: ~3-5 分钟
- **认证**: 免费，无需认证
- **格式**: JSON / raw text

**SPECI 触发条件** (ASOS 自动发出):
- 温度/露点变化超过阈值
- 风向/风速剧变
- 能见度突破关键阈值
- 云底高度变化
- 开始/停止降水

**实测 (KSEA, 过去6小时)**:
```
[METAR] 02:00 UTC  16/12°C  -RA (小雨)
[SPECI] 01:17 UTC  18/09°C  SCT040CB (积雨云)  ← 天气变化自动触发
[METAR] 01:00 UTC  19/09°C
[METAR] 00:00 UTC  20/08°C
[METAR] 23:00 UTC  19/08°C
[METAR] 22:00 UTC  18/09°C
[METAR] 21:00 UTC  17/09°C
```

**API 示例**:
```bash
# JSON 格式，含 SPECI
curl "https://aviationweather.gov/api/data/metar?ids=KSEA&format=json&hours=6"

# 多站点批量查询
curl "https://aviationweather.gov/api/data/metar?ids=KSEA,KJFK,KORD,KLAX&format=json&hours=2"

# 原始 METAR 文本
curl "https://aviationweather.gov/api/data/metar?ids=KSEA&format=raw&hours=3"
```

**返回关键字段**:
| 字段 | 说明 |
|------|------|
| `metarType` | `METAR` 或 `SPECI` |
| `temp` | 温度 (°C) |
| `dewp` | 露点 (°C) |
| `wdir` / `wspd` | 风向(°) / 风速(kt) |
| `altim` | 气压 (hPa) |
| `rawOb` | 原始报文 |

**优点**: 延迟最小(~5分钟)、SPECI 提供天气突变即时通知
**缺点**: 常规只有每小时一条，SPECI 不可预测

---

### 3. NCEI ASOS 1-Minute 归档

- **频率**: 每 1 分钟
- **延迟**: ~2 天
- **认证**: 免费
- **格式**: 定宽文本文件 (PG1 + PG2)
- **覆盖**: ~900 个 ASOS 站点

**数据地址**:
```
PG1 (风/能见度): https://www.ncei.noaa.gov/data/automated-surface-observing-system-one-minute-pg1/access/{YYYY}/{MM}/
PG2 (气压/温度): https://www.ncei.noaa.gov/data/automated-surface-observing-system-one-minute-pg2/access/{YYYY}/{MM}/
```

**文件命名**: `asos-1min-pg1-KSEA-202609.dat` (按站点按月)

**PG2 数据示例** (KSEA, 2026-09-01):
```
24233KSEA SEA2026090116440044  NP  0000  0.00  29.363  29.365  29.371   64   55
                                                                         ↑    ↑
                                                            温度°F=64  露点°F=55
```

**优点**: 真正的 1 分钟粒度，完整历史
**缺点**: 延迟 ~2 天，不能用于实时交易

---

### 4. Unidata LDM/IDD (NOAAPORT 互联网转发)

- **频率**: 1 分钟 (ASOS 原始数据流)
- **延迟**: ~1-2 分钟
- **认证**: 需向 Unidata 申请 (support-idd@unidata.ucar.edu)
- **格式**: NOAAPORT 原始报文
- **协议**: LDM push 协议 (TCP)

**工作原理**:
NOAA 通过 NOAAPORT 卫星广播所有气象数据。Unidata (NSF 资助) 运营 IDD 网络，将 NOAAPORT 数据通过互联网分发给订阅节点。你需要运行一个 LDM (Local Data Manager) 守护进程，连接到上游 IDD 节点接收数据推送。

**部署需求**:
- Linux 服务器 (24/7 运行)
- 安装 LDM 软件: https://downloads.unidata.ucar.edu/ldm/
- 配置订阅 surface observation feed
- 自行解析 NOAAPORT 报文提取温度

**申请流程**:
- 原则上面向美国学术机构，但非学术机构也可通过 LDM-users 邮件列表申请
- 联系: support-idd@unidata.ucar.edu

**优点**: 延迟最低的 NOAA 数据获取方式，1 分钟粒度
**缺点**: 需要自建接收基础设施，报文解析复杂，申请可能受限

---

### 5. FAA SWIM (System Wide Information Management)

- **频率**: 1 分钟
- **延迟**: ~1-2 分钟
- **认证**: 需要 FAA 审批
- **格式**: XML/FIXM
- **协议**: JMS / SOLACE

FAA 的航空数据分发系统，包含实时 ASOS 数据。主要面向航空运营商、航空公司、机场管理方。非航空业用户难以获得访问权限。

**优点**: 官方实时数据
**缺点**: 审批门槛高，面向航空业

---

### 6. NOAA CRN 5-Minute (气候参考网)

- **频率**: 5 分钟
- **延迟**: 批量归档，非实时
- **认证**: 免费
- **格式**: 文本文件
- **覆盖**: ~140 个气候参考站 (不在机场，而在"原始"环境)

数据地址: `https://www.ncei.noaa.gov/pub/data/uscrn/products/subhourly01/`

站点列表: `https://www.ncei.noaa.gov/pub/data/uscrn/products/stations.tsv`

**优点**: 高质量科研级数据
**缺点**: 站点少、非实时、不在城市/机场

---

## 非 NOAA 商业数据源 (补充对比)

| 数据源 | 最高频率 | 延迟 | 价格 | 数据来源 |
|--------|---------|------|------|----------|
| **Tomorrow.io** | 1 分钟 | ~1-2 min | 免费(有限) / Enterprise | 融合 NOAA + 卫星 + 雷达 |
| **Weather Underground PWS** | 5-16 秒 | <30 秒 | 免费(需注册PWS) | 25万+民间气象站 |
| **OpenWeatherMap** | ~10 分钟 | ~10 min | 免费 / 付费 | 多源融合 |
| **WeatherAPI** | 10-15 分钟 | ~10-15 min | $0-$65/月 | 多源融合 |
| **Visual Crossing** | 15-30 分钟 | 未知 | $0.0001/条 | 多源融合 |

注意: 商业 API 的温度数据通常也来源于 NOAA ASOS，只是它们自建了更快的数据管道。

---

## 推荐方案

### 方案 A: 纯 NOAA，最快可达 (推荐起步)

**NWS API (5分钟/~19分钟延迟) + METAR SPECI (~5分钟延迟)**

```
NWS API ──→ 每5分钟轮询，获取温度趋势
    +
METAR API ──→ 每2分钟轮询，捕获 SPECI 突变信号
    │
    ▼
信号融合 ──→ 交易策略
```

- 零成本，无需申请
- SPECI 在天气剧变时提供接近实时的温度信息
- NWS API 5 分钟粒度足以追踪温度趋势
- 适合验证概念阶段

### 方案 B: NOAA 实时流 (最佳 NOAA 方案)

**Unidata LDM/IDD 订阅 NOAAPORT 数据流**

- 1 分钟粒度，~1-2 分钟延迟
- 需要: Linux 服务器 + LDM 软件 + 向 Unidata 申请
- 需要: 自行解析 NOAAPORT 报文
- 适合长期运营、对延迟敏感的场景

### 方案 C: 商业 API 补充

**Tomorrow.io (1分钟) 或 WU PWS (秒级)**

- Tomorrow.io: 全球覆盖，1 分钟粒度，数据质量有 QC 保障
- WU PWS: 秒级更新，但数据质量参差不齐，需要自行过滤异常值
- 注意: 这些不是 NOAA 数据源，如果 Polymarket 结算以 NOAA 为准，用商业源做交易信号可能存在数据一致性风险

---

## 关键站点 ICAO 代码

Polymarket 天气市场常见城市对应的 ASOS 机场站点:

| 城市 | ICAO | 机场 |
|------|------|------|
| New York | KJFK / KLGA / KEWR | JFK / LaGuardia / Newark |
| Los Angeles | KLAX | LAX |
| Chicago | KORD / KMDW | O'Hare / Midway |
| Houston | KIAH / KHOU | Intercontinental / Hobby |
| Phoenix | KPHX | Sky Harbor |
| Seattle | KSEA | Sea-Tac |
| Denver | KDEN | Denver International |
| Miami | KMIA | Miami International |
| Dallas | KDFW | DFW |
| Atlanta | KATL | Hartsfield-Jackson |

---

## 附录: API 快速测试命令

```bash
# NWS API - 最新观测
curl -s -H "User-Agent: PolyStrategy" \
  "https://api.weather.gov/stations/KSEA/observations/latest" | python3 -m json.tool

# NWS API - 最近1小时 (每5分钟一条，约12条)
curl -s -H "User-Agent: PolyStrategy" \
  "https://api.weather.gov/stations/KSEA/observations?limit=12"

# METAR API - JSON 含 SPECI
curl -s "https://aviationweather.gov/api/data/metar?ids=KSEA&format=json&hours=6"

# METAR API - 多站点批量
curl -s "https://aviationweather.gov/api/data/metar?ids=KSEA,KJFK,KORD,KLAX&format=json&hours=2"

# METAR API - 原始报文
curl -s "https://aviationweather.gov/api/data/metar?ids=KSEA&format=raw&hours=3"

# ASOS 1-min 归档 (PG2 含温度，延迟~2天)
curl -s "https://www.ncei.noaa.gov/data/automated-surface-observing-system-one-minute-pg2/access/2026/09/asos-1min-pg2-KSEA-202609.dat" | tail -20
```
