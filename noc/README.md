# Homelab NOC — 全屋状态大屏

一眼看全屋：网络拓扑、**交换机端口级实时流量**、无线覆盖、终端在线、算力水位、存储压力、环境状态、异常事件。

部署在一台常开的 Linux / fnOS 主机上（容器 `homelab-noc`，默认端口 `8940`），浏览器直接访问即可，无需额外前端服务。

---

## 一、它回答什么问题

旧看板是"每个设备怎么样"的卡片列表；这个是"**哪里不对**"的态势面板。

信息分四档，按页签分层（键盘 1–4 直接切）：

| 页签 | 回答什么 | 内容 |
|---|---|---|
| 概览 | 现在有没有事 | 外网曲线（2 分钟 / 1 小时 / 24 小时）+ 事件流 + **网络 / 算力 / 终端三张要点卡** + 竖版物理拓扑 |
| 网络 | 链路哪里堵 | 交换机每口速率与实时收发、无线覆盖与客户端归属、节点 RTT |
| 算力 | 机器累不累 | PVE 宿主机水位、资源趋势曲线、虚拟机与容器明细表 |
| 终端 | 谁挂在网上 | 按分类分组的全屋终端卡片，可按名称 / IP / MAC / 厂商搜索 |

概览不是二级页的缩略图：三张卡只放"要不要点进去"的判断依据——端口在用在几号、
CPU/内存/存储三条水位与各 guest 占用、在线数与分类、无线挂在哪台 AP、当前最占带宽的三台。
点卡片任意位置直接进对应页签，细节都在那边。

顶栏和状态带在所有页签常驻，底部汇总条常驻——切页签不丢全局视角。

布局：

```
顶栏    brand + 页签 + 健康分 + 外网 + 在线 + 连接 + 吞吐 + 时钟
状态带  室内温湿度 · 室外 · 灯光 · 空调 · 门窗 · 无线 · 在家 · HA
─────────────────────────────────────────────────────────
概览    左：外网链路（可切时间窗）/ 事件流   中：网络·算力·终端要点卡   右：竖版拓扑
网络    左上：交换机端口  左下：无线覆盖 + 客户端  右：节点延迟
算力    左：PVE 水位 + 资源趋势              右：虚拟机 / 容器表
终端    全屋终端卡片（按分类分组 + 搜索）
─────────────────────────────────────────────────────────
底栏    全屋终端 30/35 · 各分类台数 · 无线 · 灯光 · 事件库条数
```

排版约定：

- 全局字号按"一两米外能读"定的（正文字号 15px 起），小屏靠 1560 / 1420 / 1300 / 1100 / 860 五档断点收。
- 拓扑固定 viewBox `0 0 470 730`，节点坐标写死在 `app.js` 的 `NODES` 里；字号是**用户单位**，
  viewBox 越小、投到屏幕上越大——想放大拓扑就缩 viewBox，别去调 `font-size`。
- SVG 是 replaced 元素，在 flex 列里给 `flex: 1` 撑不开，必须外层 `.topo-wrap` + `position: absolute; inset: 0`。

### 历史落盘（SQLite）

事件流和关键指标都写进 `/data/noc.db`，容器重建不丢：

- `events` —— 一条事件一行，重启后事件流从库里读回来，不从空白开始
- `metrics` —— 每 `POLL_SAMPLE` 秒一条（默认 60s），存 WAN 上下行 / 在线数 / CPU / 内存，
  用来画 24 小时曲线和回答"昨天这个时候流量多少"
- 只保留最近 `DB_KEEP_DAYS` 天（默认 90），每天清理一次

曲线的时间轴按**真实时间戳**定位，所以刚部署、库里只有十几分钟数据时，横轴会自动
收窄到实际跨度（面板上会写"有数据 15 分钟"），而不是把十几个点摊平成一整天假装有数据。

## 二、数据源（六路）

| 源 | 接口 | 频率 | 拿到什么 |
|---|---|---|---|
| iKuai 主路由 | `POST https://192.168.X.1/Action/call` | 2s | CPU/内存/在线数/连接数/实时流速 |
| iKuai 接口 | `monitor_iface` `iface_stream` | 2s | wan1 / lan1 实时字节流、物理口速率链路 |
| iKuai 终端 | `monitor_lanip` | 5s | 全屋终端列表（实测 33–35 台） |
| PVE | `GET https://192.168.X.10:8006/api2/json/...` | 5s | 宿主机 CPU/内存/rootfs、VM + LXC 明细、每虚拟机网络差分 |
| **水星 SE106P** | `GET http://192.168.X.3/MainRpm.htm` | 3s | **每端口 link 速率 / 实时收发 / PoE 功率** |
| **小米 AP ×2** | `/api/misystem/topo_graph`、`device_list`、`wifi_detail_all` | 10s | SSID、信道、每 AP 在线数、无线客户端 MAC 归属 |
| **Home Assistant** | `GET http://192.168.X.20:8123/api/states` | 15s | 室温湿度、室外天气、空调、灯光、门窗、在家状态、实体健康率 |
| 本机内核 | `/proc/net/dev` | 2s | FN-Lab 网卡吞吐、负载、存储水位 |
| TCP 探测 | socket connect | 10s | 8 个关键节点的 RTT / 抖动 / 丢包 |

全部 **零第三方依赖**（Python 标准库 + 原生 JS），镜像约 60MB。

### 三个设备各自的认证坑（都是实测踩出来的）

| 设备 | 坑 |
|---|---|
| 水星 SE106P | 密码不能明文/md5/base64，要走页面里 `cryp_new.js` 的 `securityEncode`（函数名叫 `hex_md5` 但实际是异或映射表）。表长 255 字符，抄错一个字符就 `logonInfo=1` 登录失败。 |
| 小米 AP | `POST /cgi-bin/luci/api/xqsystem/login` 明文 `logtype=1` 即可拿 stok；真实信道在 `channelInfo.channel`，顶层 `channel` 字段恒为 `"0"`（配置值=自动）。子 AP 自己报不出在线数，要从**主 AP 的 `topo_graph.leafs`** 反查。 |
| Home Assistant | `/auth/login_flow` 的 `handler` 必须是 `["homeassistant", null]`，写成 `["homeassistant","login_flow"]` 直接 **500**。`/auth/token` 必须 **form 编码**，JSON body 会被读成空 → `unsupported_grant_type`。access_token 30 分钟过期，用 refresh_token 静默续期并落盘 `/data/ha_token.json`。 |

## 三、部署

```bash
# 首次：先准备两份配置（仓库里只有模板，没有真实值）
cp .env.example .env                    # 凭据
cp config.example.json config.local.json  # 站点布局

# 起容器
mkdir -p data
docker compose up -d --build

# 改前端（app/ 已只读挂载，刷新浏览器即可，无需重启）
# 改后端（Python）
docker compose restart
```

构建时若镜像源报 401，用 `DOCKER_BUILDKIT=0 docker compose build`。

### 目录

```
noc/
├── app/
│   ├── server.py            HTTP + SSE + 采集调度
│   ├── state.py             快照聚合 / 历史 / 事件 / 健康分
│   ├── assets.py            静态资产表（读 config.local.json）
│   ├── config.py            配置：环境变量 + config.local.json
│   ├── collectors/
│   │   ├── ikuai.py         爱快 API
│   │   ├── pve.py           Proxmox API
│   │   ├── switch.py        交换机端口统计
│   │   ├── miwifi.py        小米 AP 无线侧
│   │   ├── ha.py            Home Assistant 实体
│   │   ├── localnet.py      本机内核读数
│   │   └── pinger.py        TCP RTT 探测
│   └── static/              index.html / style.css / app.js
├── agent/
│   ├── nas_net_agent.py     NAS 侧采集端
│   └── compose-create.example.json
├── config.example.json      站点配置模板
├── config.local.json        站点配置（gitignore，部署时传 data/）
├── .env.example             凭据模板
├── data/                    HA token 缓存 + config.local.json（持久化，勿删）
├── docker-compose.yml
├── Dockerfile
└── .env                     凭据（gitignore）
```

### 配置：两份文件分工

**`.env` —— 连什么、用什么账号**（模板 `.env.example`）

```
IKUAI_URL / IKUAI_USER / IKUAI_PASS
PVE_URL / PVE_USER / PVE_PASS / PVE_NODE
SWITCH_URL / SWITCH_USER / SWITCH_PASS
MIWIFI_USER / MIWIFI_PASS            # 多台 AP 共用一组密码
HA_URL / HA_USER / HA_PASS / HA_TOKEN_FILE
NAS_REPORT_TOKEN / NAS_IP / NAS_STALE
PORT=8940
POLL_FAST=2  POLL_DEVICES=5  POLL_PVE=5  POLL_PING=10
POLL_SWITCH=3  POLL_MIWIFI=10  POLL_HA=15
DEV_GRACE=25  DEV_REPEAT=180
```

**`config.local.json` —— 家里长什么样**（模板 `config.example.json`）

| 段 | 作用 |
|---|---|
| `site` | 站点名、网段（决定探活补位的范围）、NAS 地址 |
| `switch.ports` | 交换机每个口接谁（端口号 → 角色 + 拓扑节点） |
| `wifi.hosts` | 每台 AP 的地址、显示名、型号、对应拓扑节点 |
| `probes` | 要测 RTT 的节点（key 与前端 `NODES` 对应） |
| `devices` | 静态资产表：IP → 名称 / 房间 / 图标 / 分类 |
| `topology` | 拓扑节点上的显示文字（可写自己的 IP 末位） |

两份文件缺失都不会让服务起不来：`config.local.json` 缺失时终端墙为空、拓扑用通用标签；`.env` 里某个地址缺失时，对应采集器直接报「未配置」并跳过，不会去连瞎猜的地址。

`config.local.json` 在容器里读 `/data/config.local.json`（对应 `./data:/data`），也可以用 `NOC_LOCAL_CONFIG` 指定路径。

## 四、接口

| 路径 | 说明 |
|---|---|
| `GET /` | 大屏页面（`?view=overview\|network\|compute\|clients`、`&theme=light\|dark`、`&range=2m\|1h\|24h`、`&sheet=events`） |
| `GET /api/snapshot` | 全量快照（约 33KB），首屏直接拉一次，不等 SSE |
| `GET /api/stream` | SSE，1.5s 一帧 |
| `GET /api/history?minutes=30` | 内存环形缓冲（最长 1 小时，含各端口速率），给"2 分钟 / 1 小时"曲线用 |
| `GET /api/events?limit=50` | 内存事件流（最近 300 条，启动时从库里回填） |
| `GET /api/events/history?hours=24&level=warn&q=xx&limit=200` | **落库事件查询**，按时间窗 / 级别 / 关键字 |
| `GET /api/events/days` | 按天统计条数（含 warn / crit 计数），给历史面板画刻度 |
| `GET /api/metrics?hours=24&step=300` | **落库指标**，按 step 秒聚合，点数太少会自动往细里降 |
| `GET /api/store` | 库本身的状态：路径 / 事件数 / 指标数 / 起止时间 / 体积 |
| `GET /api/health` | 存活检查 |
| `POST /api/nas-report` | 极空间采集端上报（`X-Noc-Token` 鉴权） |

### 截图自检

```bash
python scripts/shot_noc.py                       # 默认概览页
python scripts/shot_noc.py --theme light
python scripts/shot_noc.py --url "http://<host>:8940/?view=compute"
```

脚本会自动加 `?nostream=1`（页面不连 SSE、不起定时器），并配 `--virtual-time-budget`
等首屏数据落地——**带 SSE 时用这个参数会让渲染进程被杀**，这是踩过的坑。

## 五、二层流量的边界（接入交换机后的修订版）

之前结论是"手机 ↔ NAS 的流量完全测不到"，**拿到水星 SE106P 的端口统计后要修正**：

| 流量路径 | 能否计量 | 靠什么 |
|---|---|---|
| 子AP 设备 ↔ NAS / 主AP 设备 | ✅ | 交换机 **口1 / 口2** 的 rx/tx 差值 |
| 任意设备 ↔ 外网 | ✅ | iKuai wan1 + 交换机口5 |
| PVE ↔ 全屋 | ✅ | 交换机 **口4**（发卡弯回程） |
| **同一 AP 下 手机 ↔ NAS** | ❌ | 在 AP 内部直转，不上交换机 |

所以界面拓扑上"二层旁路"那条灰虚线代表的是**最后一种**：同 AP 内部直连。
跨 AP 的流量现在是实测值，光点速度和端口列表里的数字都来自设备。

**要补最后这块**，只能在极空间 Z4Pro 上跑一个读 `/proc/net/dev` 的上报容器
（`NAS 网卡总吞吐 − iKuai 统计的 .30 外网流量 = 同 AP 二层直连流量`）。

## 六、健康分规则

基础 100 分，扣分项：

- WAN 拨号异常 −45
- 关键节点探测失败 −12 / 个
- 路由 CPU > 90% −8，内存 > 92% −10
- PVE 内存 > 92% −10，系统盘 > 90% −8
- 存储池 > 88% −6
- 交换机不可达 −10；**挂了设备的口断开 −9 / 个**
- 小米 AP 不可达 −10；单台 AP 掉线 −8
- HA 采集失败 −6；可用实体率 < 35% −8

≥90 绿，70–89 琥珀，<70 红。正常时安静，异常时顶栏变红并在事件流留痕。

## 七、后续可扩展

- 极空间 NAS agent（补同 AP 二层直连流量）
- 事件持久化到 SQLite，支持 7 天回溯
- HA 场景反向控制（点灯、调空调）
- 手机端竖屏布局（当前 `@media` 已做两档降级）
