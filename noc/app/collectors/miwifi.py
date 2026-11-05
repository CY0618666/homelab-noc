# -*- coding: utf-8 -*-
"""小米路由器（AP 模式）采集：SSID / 信道 / 在线数 / 拓扑 / 无线客户端

两台 AP 均为有线中继，不负责拨号，因此 wan 字段恒为 0，只取无线侧数据。
"""
import json
import html
import threading
import time
import urllib.parse
import urllib.request

import config

_lock = threading.Lock()
_tokens = {}      # host -> {"token": str, "ts": float}


def _post(host, path, payload):
    url = "http://%s%s" % (host, path)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get(host, token, path):
    url = "http://%s/cgi-bin/luci/;stok=%s%s" % (host, token, path)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _token(host, force=False):
    with _lock:
        c = _tokens.get(host)
        if c and not force and time.time() - c["ts"] < 900:
            return c["token"]
    res = _post(host, "/cgi-bin/luci/api/xqsystem/login", {
        "username": config.MIWIFI_USER,
        "password": config.MIWIFI_PASS,
        "logtype": "1",
    })
    tok = res.get("token")
    if not tok:
        raise RuntimeError("小米 AP 登录失败: %s" % str(res)[:100])
    with _lock:
        _tokens[host] = {"token": tok, "ts": time.time()}
    return tok


def _call(host, path):
    for attempt in (0, 1):
        tok = _token(host, force=bool(attempt))
        try:
            return _get(host, tok, path)
        except Exception:
            if attempt:
                raise
            with _lock:
                _tokens.pop(host, None)
    return {}


def _band(ifname):
    return "5G" if ifname in ("wl0", "wifi1") else "2.4G"


# /api/misystem/device_list 的 connectionType：1=2.4G、2=5G、4=有线
# （4 在部分固件里是 eth，5 是 USB 网卡，都按有线处理）
_CT = {1: ("wifi", "2.4G"), 2: ("wifi", "5G"), 3: ("wifi", "5G"),
       4: ("wired", ""), 5: ("wired", "")}


def _kb(v):
    """小米的速率单位统一是 KB/s"""
    try:
        return float(v or 0) * 1024
    except (TypeError, ValueError):
        return 0.0


def _clients_rich(host):
    """主接口：/api/misystem/device_list

    xqsystem/device_list 在 BE 系固件上只吐 1 条（实测主 AP 报 9 台、它只给 1 台），
    misystem 这个能拿全，而且带 connectionType，能分清有线 / 2.4G / 5G —— 
    iKuai 那边看到的所有终端都在 lan1 上，光看 iKuai 是分不出来的。
    """
    d = _call(host, "/api/misystem/device_list")
    out = []
    for x in d.get("devices") or []:
        mac = (x.get("mac") or "").upper()
        if not mac:
            continue
        ct = x.get("connectionType")
        conn, band = _CT.get(ct, ("", ""))
        ev = (x.get("events") or [{}])[0].get("text") or ""
        # events 里的文案比 connectionType 更直白，能读到就用它兜底
        if not conn:
            if "有线" in ev:
                conn = "wired"
            elif "接入" in ev:
                conn, band = "wifi", ("5G" if "5G" in ev else "2.4G")
        out.append({
            "mac": mac,
            "ip": x.get("ip") or "",
            "name": x.get("name") or x.get("originName") or "",
            "conn": conn,
            "band": band,
            "up": _kb(x.get("uSpeed")),
            "down": _kb(x.get("dSpeed")),
            "online": bool(x.get("online")),
            "online_sec": 0,
            "proto": x.get("wifiprotocol") or "",
            "event": ev,
        })
    return out


def _clients_plain(host):
    """兜底：/api/xqsystem/device_list

    子 AP（中继模式）的 misystem 接口返回空，只有这个能拿到 MAC 清单，
    但字段很少：多数只有 mac，主 AP 上才带 statistics（含 dev 接口名）。
    """
    d = _call(host, "/api/xqsystem/device_list")
    out = []
    for x in d.get("list") or []:
        mac = (x.get("mac") or "").upper()
        if not mac:
            continue
        st = x.get("statistics") or {}
        dev = st.get("dev") or ""
        if dev.startswith("wl"):
            conn, band = "wifi", _band(dev)
        elif dev:
            conn, band = "wired", ""
        else:
            # 在 AP 的客户端表里、又不给接口名：按无线算。
            # 子 AP 全走这条，实测挂的都是灯 / 空调 / 打印机这类纯 Wi-Fi 设备
            conn, band = "wifi", ""
        out.append({
            "mac": mac,
            "ip": x.get("ip") or st.get("ip") or "",
            "name": x.get("name") or x.get("hostname") or "",
            "conn": conn,
            "band": band,
            "up": _kb(st.get("upspeed")),
            "down": _kb(st.get("downspeed")),
            "online": True,
            "online_sec": int(float(st.get("online") or 0)),
            "proto": "",
            "event": "",
        })
    return out


def collect_ap(meta):
    host = meta["host"]
    out = {
        "host": host, "label": meta["label"], "model": meta["model"],
        "node": meta["node"], "ok": False,
        "ssid": "", "channels": [], "online": 0, "clients": [],
    }
    try:
        topo = _call(host, "/api/misystem/topo_graph")
        g = topo.get("graph") or {}
        out["ssid"] = html.unescape(g.get("ssid") or "")
        out["locale"] = g.get("locale") or ""
        out["online"] = int(g.get("onlines") or 0)
        # 主 AP 的拓扑里带下挂 AP 的在线数，叶子节点自己查不到
        out["leafs"] = [
            {"ip": lf.get("ip"), "onlines": int(lf.get("onlines") or 0),
             "locale": lf.get("locale") or "", "name": lf.get("name") or "",
             "link": lf.get("link_type") or ""}
            for lf in (g.get("leafs") or [])
        ]

        wifi = _call(host, "/api/xqnetwork/wifi_detail_all")
        seen = set()
        for it in wifi.get("info") or []:
            if it.get("mode") != "Master":
                continue
            band = _band(it.get("ifname", ""))
            if band in seen:
                continue
            seen.add(band)
            ci = it.get("channelInfo") or {}
            # channel 字段是配置值（0=自动），真实信道在 channelInfo 里
            ch = ci.get("channel") or it.get("channel") or 0
            out["channels"].append({
                "band": band,
                "ch": int(ch) if str(ch).isdigit() else 0,
                "width": int(ci.get("bandwidth") or 0),
                "mode": it.get("wifimode") or "",
                "txpwr": it.get("txpwr") or "",
                "ssid": html.unescape(it.get("ssid") or ""),
            })

        # 两份客户端清单合并：misystem 字段全但部分固件（子 AP）为空，
        # xqsystem 字段少但哪台都有，以 misystem 为准、xqsystem 补漏
        rich = _clients_rich(host)
        plain = _clients_plain(host)
        by_mac = {c["mac"]: c for c in rich}
        for c in plain:
            old = by_mac.get(c["mac"])
            if old is None:
                by_mac[c["mac"]] = c
                continue
            # 补上空字段，但不覆盖 misystem 已经判定的接入方式
            for k in ("ip", "name", "proto"):
                if not old.get(k) and c.get(k):
                    old[k] = c[k]
            if not old.get("conn") and c.get("conn"):
                old["conn"] = c["conn"]
                old["band"] = c["band"]
            if not old.get("up") and c.get("up"):
                old["up"] = c["up"]
            if not old.get("down") and c.get("down"):
                old["down"] = c["down"]
        out["clients"] = sorted(
            by_mac.values(),
            key=lambda c: -(max(c.get("up") or 0, c.get("down") or 0)),
        )
        out["client_src"] = {"rich": len(rich), "plain": len(plain)}

        stat = _call(host, "/api/misystem/status")
        # topo_graph 的 onlines 是"登记过的设备总数"，不是当前在线数
        # （实测主 AP 报 14，status 里 count.online 只有 2），这里取准的
        cnt = ((stat.get("count") or {}).get("online"))
        if cnt is not None:
            out["online_cnt"] = int(cnt)
        mem = stat.get("mem") or {}
        if mem.get("total"):
            out["mem_total"] = mem.get("total")
            out["mem_pct"] = round(float(mem.get("usage") or 0) * 100, 1)
        hw = stat.get("hardware") or {}
        out["firmware"] = hw.get("version") or ""
        out["display"] = hw.get("displayName") or ""
        cpu = stat.get("cpu") or {}
        out["cpu_core"] = cpu.get("core")
        out["uptime"] = int(float(stat.get("upTime") or 0))

        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


def collect():
    if not config.MIWIFI_HOSTS:
        raise RuntimeError("未配置小米 AP（config.local.json 的 wifi.hosts）")
    aps = [collect_ap(m) for m in config.MIWIFI_HOSTS]

    # 子 AP 自己报不出在线数，从主 AP 拓扑的 leafs 反查
    by_ip = {a["host"]: a for a in aps}
    for a in aps:
        if a.get("online_cnt"):
            a["online"] = a["online_cnt"]
    for a in aps:
        for lf in a.get("leafs") or []:
            tgt = by_ip.get(lf.get("ip"))
            if tgt is not None and tgt is not a:
                tgt["online"] = lf["onlines"]
                tgt["locale"] = lf.get("locale") or tgt.get("locale")
                tgt["uplink"] = lf.get("link") or ""

    total_online = sum(a.get("online", 0) for a in aps if a.get("ok"))
    clients = []
    seen = set()
    for a in aps:
        for c in a.get("clients") or []:
            if c["mac"] in seen:
                continue
            seen.add(c["mac"])
            c["ap"] = a["label"]
            clients.append(c)
    return {
        "ok": any(a.get("ok") for a in aps),
        "aps": aps,
        "online": total_online,
        "clients": clients,
        "ts": int(time.time()),
    }
