"""Svpn — движок: настройки, подписки, конфиги Xray и sing-box.

Как устроено:
    sing-box (перехватчик) — ловит трафик выбранных программ и сайтов
        через виртуальную сетевую карту и отдаёт его в Xray.
        Всё остальное идёт напрямую, мимо VPN.
    Xray (VPN) — держит связь с серверами подписок, сам выбирает
        живой узел, а капризные к региону сайты ведёт отдельным
        «европейским» контуром.

Порты Xray (для программ, которые настраиваются вручную):
    127.0.0.1:10877  HTTP-прокси   (Гермес, мост Проявки)
    127.0.0.1:10878  SOCKS5        (Телеграм)
    0.0.0.0:10880    HTTP для айпада (пускает только домашнюю сеть и Tailscale)

Модуль не зависит от Windows: всё, что про процессы и реестр, — в app.py.
"""
import base64
import copy
import json
import os
import re
import urllib.parse
import urllib.request

HTTP_PORT = 10877
SOCKS_PORT = 10878
IPAD_PORT = 10880
UI_PORT = 10870
IPAD_PAC_PORT = 10881
PROBE_URL = "https://www.gstatic.com/generate_204"

# Свои сети никогда не идут через туннель.
PRIVATE_NETS = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255/32",
    "fc00::/7", "fe80::/10", "ff00::/8",
]

# Программы, которым перехват не нужен никогда: сам туннель и Tailscale.
ALWAYS_DIRECT_APPS = [
    "xray.exe", "sing-box.exe", "tailscaled.exe", "tailscale-ipn.exe",
    "tailscale.exe",
]

# Узлы, по названию которых видно, что выход российский или мобильный:
# для «европейского» контура они не годятся.
RU_MARKERS = ("росси", "russia", "\U0001F1F7\U0001F1FA", "wi-fi only",
              "wifi only")
MOBILE_MARKERS = ("4g", "lte", "антизаглушк", "белый список")

SUPPORTED = ("vless", "vmess", "trojan", "shadowsocks", "hysteria")


# ---------------------------------------------------------------- настройки

def default_sites():
    """Готовый список сайтов. geo=True — только через европейский выход."""
    S = lambda name, domains, on=True, geo=False, note="": {
        "name": name, "domains": domains, "on": on, "geo": geo, "note": note}
    return [
        S("Adobe и Creative Cloud",
          ["adobe.com", "adobe.io", "adobelogin.com", "adobelogin.net",
           "adobesc.com", "adobestatic.com", "adobeoobe.com", "adobess.com",
           "adobecc.com", "adobe.net", "adobedc.net", "adobedtm.com",
           "adobeprojectm.com", "creativecloud.com", "acrobat.com",
           "typekit.net", "typekit.com", "behance.net"],
          note="Firefly, генеративная заливка, шрифты"),
        S("Google и Gemini", ["generativelanguage.googleapis.com"], geo=True,
          note="API Gemini для плагинов — только европейский выход"),
        S("Google: Доки, Диск, почта",
          ["google.com", "google.ru", "googleapis.com", "gstatic.com",
           "googleusercontent.com", "gvt1.com", "gvt2.com"]),
        S("YouTube",
          ["youtube.com", "youtu.be", "youtube-nocookie.com",
           "googlevideo.com", "ytimg.com", "ggpht.com"]),
        S("Claude", ["claude.ai", "claude.com", "anthropic.com",
                     "claudeusercontent.com"], geo=True),
        S("OpenRouter", ["openrouter.ai"], geo=True),
        S("Magnific и Freepik",
          ["magnific.ai", "magnific.com", "freepik.com", "freepiklabs.com",
           "cdnpk.net"], geo=True),
        S("Higgsfield",
          ["higgsfield.ai", "d8j0ntlcm91z4.cloudfront.net",
           "d2ol7oe51mr4n9.cloudfront.net", "dqv0cqkoy5oj7.cloudfront.net"]),
        S("Плагины AlkaidVision и AI Lab",
          ["alkaidvision.com", "alkaidart.com", "picture-instruments.com"]),
        S("fal.ai", ["fal.run", "fal.ai", "fal.media"]),
        S("Hugging Face", ["huggingface.co", "hf.co", "hf.space"]),
        S("Tailscale", ["tailscale.com", "tailscale.io"]),
        S("Проверка выхода", ["api.country.is", "ifconfig.co"], geo=True,
          note="служебное: показывает страну выхода"),
    ]


def default_apps():
    A = lambda name, exe=(), paths=(), on=True, note="": {
        "name": name, "exe": list(exe), "paths": list(paths), "on": on,
        "note": note}
    return [
        A("Photoshop", ["Photoshop.exe"],
          note="весь трафик Фотошопа, включая плагины"),
        A("Creative Cloud",
          ["Creative Cloud.exe", "Adobe Desktop Service.exe",
           "CCXProcess.exe", "CCLibrary.exe", "CoreSync.exe",
           "AdobeIPCBroker.exe", "Adobe Crash Processor.exe"]),
        A("Telegram", ["Telegram.exe"],
          note="лечит медленную отправку файлов"),
        A("Google Chrome", ["chrome.exe"], on=False,
          note="весь браузер целиком; обычно хватает списка сайтов"),
    ]


def default_settings():
    return {
        "version": 1,
        "enabled": True,
        "autostart": True,
        "subscriptions": {"main": [], "geo": []},
        "sites": default_sites(),
        "apps": default_apps(),
        "ipad": False,
        "theme": "night",
        "glass": True,
    }


def load_settings(path):
    s = default_settings()
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        s.update({k: v for k, v in saved.items() if k in s})
    except (OSError, ValueError):
        pass
    return s


def save_settings(path, s):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clean_domain(d):
    """Привести «https://www.Site.com/path», «*.site.com» к «site.com»."""
    d = (d or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split(":")[0]
    d = d.lstrip("*.").strip(".")
    if d.startswith("www."):
        d = d[4:]
    return d if re.fullmatch(r"[a-z0-9.\-]+\.[a-z0-9\-]+", d) else ""


def active_domains(s, geo=None):
    out = []
    for site in s.get("sites", []):
        if not site.get("on"):
            continue
        if geo is not None and bool(site.get("geo")) != geo:
            continue
        for d in site.get("domains", []):
            d = clean_domain(d)
            if d and d not in out:
                out.append(d)
    return out


def active_apps(s):
    names, paths = [], []
    for a in s.get("apps", []):
        if not a.get("on"):
            continue
        for e in a.get("exe", []):
            if e and e not in names:
                names.append(e)
        for p in a.get("paths", []):
            if p and p not in paths:
                paths.append(p)
    return names, paths


# ---------------------------------------------------------------- подписки

def fetch_url(url, proxy=None, timeout=30):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "v2rayNG/1.8.0"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def parse_subscription(raw):
    """Подписка → список (название, outbound Xray).

    Понимает оба формата, которые отдают провайдеры:
      * JSON — массив готовых конфигов Xray (как у GoVPN и Iron Curtain);
      * текст или base64 — ссылки vless:// vmess:// trojan:// ss:// hy2://.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    raw = raw.strip()
    out = []
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        for i, cfg in enumerate(data):
            if not isinstance(cfg, dict):
                continue
            for ob in cfg.get("outbounds", []):
                if ob.get("tag") == "proxy" and ob.get("protocol") in SUPPORTED:
                    ob = copy.deepcopy(ob)
                    ob.pop("tag", None)
                    out.append((str(cfg.get("remarks") or f"узел {i + 1}"), ob))
        return out
    text = raw
    if "://" not in text:
        try:
            text = base64.b64decode(text + "=" * (-len(text) % 4)).decode(
                "utf-8", errors="ignore")
        except Exception:
            return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = parse_link(line)
        except Exception:
            r = None
        if r:
            out.append(r)
    return out


def _q(qs, k, d=""):
    v = qs.get(k)
    return v[0] if v else d


def _stream(net, sec, qs, host_default):
    """Собрать streamSettings Xray из параметров ссылки."""
    net = net or "tcp"
    st = {"network": net}
    if net == "ws":
        st["wsSettings"] = {"path": _q(qs, "path", "/"),
                            "headers": {"Host": _q(qs, "host")} if _q(qs, "host") else {}}
    elif net == "grpc":
        st["grpcSettings"] = {"serviceName": _q(qs, "serviceName"),
                              "multiMode": _q(qs, "mode") == "multi"}
    elif net in ("xhttp", "splithttp"):
        st["network"] = "xhttp"
        st["xhttpSettings"] = {"path": _q(qs, "path", "/"),
                               "host": _q(qs, "host"),
                               "mode": _q(qs, "mode", "auto")}
    elif net in ("httpupgrade",):
        st["httpupgradeSettings"] = {"path": _q(qs, "path", "/"),
                                     "host": _q(qs, "host")}
    if sec == "reality":
        st["security"] = "reality"
        st["realitySettings"] = {
            "serverName": _q(qs, "sni"), "publicKey": _q(qs, "pbk"),
            "shortId": _q(qs, "sid"), "fingerprint": _q(qs, "fp", "chrome"),
            "spiderX": _q(qs, "spx")}
    elif sec == "tls":
        st["security"] = "tls"
        tls = {"serverName": _q(qs, "sni", host_default),
               "fingerprint": _q(qs, "fp", "chrome")}
        if _q(qs, "alpn"):
            tls["alpn"] = _q(qs, "alpn").split(",")
        if _q(qs, "allowInsecure") in ("1", "true"):
            tls["allowInsecure"] = True
        st["tlsSettings"] = tls
    else:
        st["security"] = "none"
    return st


def parse_link(link):
    u = urllib.parse.urlsplit(link)
    name = urllib.parse.unquote(u.fragment) or u.hostname or "узел"
    qs = urllib.parse.parse_qs(u.query)
    scheme = u.scheme.lower()
    if scheme == "vless":
        user = {"id": urllib.parse.unquote(u.username or ""),
                "encryption": _q(qs, "encryption", "none"),
                "flow": _q(qs, "flow")}
        ob = {"protocol": "vless",
              "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443,
                                      "users": [user]}]},
              "streamSettings": _stream(_q(qs, "type", "tcp"),
                                        _q(qs, "security", "none"), qs, u.hostname)}
        return name, ob
    if scheme == "trojan":
        ob = {"protocol": "trojan",
              "settings": {"servers": [{"address": u.hostname, "port": u.port or 443,
                                        "password": urllib.parse.unquote(u.username or "")}]},
              "streamSettings": _stream(_q(qs, "type", "tcp"),
                                        _q(qs, "security", "tls"), qs, u.hostname)}
        return name, ob
    if scheme == "vmess":
        body = link[len("vmess://"):].split("#")[0]
        j = json.loads(base64.b64decode(body + "=" * (-len(body) % 4)))
        q = {"path": [j.get("path", "")], "host": [j.get("host", "")],
             "sni": [j.get("sni", "") or j.get("host", "")],
             "fp": [j.get("fp", "chrome")], "alpn": [j.get("alpn", "")],
             "serviceName": [j.get("path", "")]}
        ob = {"protocol": "vmess",
              "settings": {"vnext": [{"address": j["add"], "port": int(j["port"]),
                                      "users": [{"id": j["id"],
                                                 "alterId": int(j.get("aid", 0)),
                                                 "security": j.get("scy", "auto")}]}]},
              "streamSettings": _stream(j.get("net", "tcp"),
                                        "tls" if j.get("tls") == "tls" else "none",
                                        q, j["add"])}
        return j.get("ps") or name, ob
    if scheme == "ss":
        userinfo = urllib.parse.unquote(u.username or "")
        if ":" not in userinfo:
            userinfo = base64.urlsafe_b64decode(
                userinfo + "=" * (-len(userinfo) % 4)).decode()
        method, password = userinfo.split(":", 1)
        ob = {"protocol": "shadowsocks",
              "settings": {"servers": [{"address": u.hostname, "port": u.port,
                                        "method": method, "password": password}]}}
        return name, ob
    if scheme in ("hy2", "hysteria2"):
        tls = {"serverName": _q(qs, "sni", u.hostname), "alpn": ["h3"]}
        if _q(qs, "insecure") in ("1", "true"):
            tls["allowInsecure"] = True
        ob = {"protocol": "hysteria",
              "settings": {"address": u.hostname, "port": u.port or 443, "version": 2},
              "streamSettings": {"network": "hysteria",
                                 "hysteriaSettings": {"version": 2,
                                                      "auth": urllib.parse.unquote(u.username or "")},
                                 "security": "tls", "tlsSettings": tls}}
        return name, ob
    return None


def is_ru(name):
    low = name.lower()
    return any(m.lower() in low for m in RU_MARKERS)


def is_mobile(name):
    low = name.lower()
    return any(m in low for m in MOBILE_MARKERS)


# ---------------------------------------------------------------- Xray

TEST_PORT_BASE = 10900   # 127.0.0.1:10900+N — отдельный вход на каждый сервер для проверок
MAX_NODES = 60

# Что проверяем на каждом сервере. strict=True — сервис капризен к стране:
# сервер, не прошедший такую проверку, не попадёт в «европейский» контур.
SERVICE_CHECKS = [
    {"id": "gemini", "name": "Gemini", "strict": True,
     "url": "https://generativelanguage.googleapis.com/v1beta/models?key=check"},
    {"id": "claude", "name": "Claude", "strict": True,
     "url": "https://api.anthropic.com/v1/models"},
    {"id": "openrouter", "name": "OpenRouter", "strict": True,
     "url": "https://openrouter.ai/api/v1/models"},
]


def node_key(ob):
    """Устойчивый ключ сервера: не зависит от порядка в подписке."""
    import hashlib
    o = {k: v for k, v in ob.items() if k != "tag"}
    return hashlib.sha1(json.dumps(o, sort_keys=True).encode()).hexdigest()[:12]


def service_verdict(sid, status, body):
    """Пропускает ли сервис этот выход. status — HTTP-код, body — начало ответа."""
    low = (body or "").lower()
    if sid == "gemini":
        # без ключа нормальный ответ — 400 «API key not valid»;
        # из неподдерживаемой страны — «User location is not supported»
        return status in (200, 400, 401, 403) and "location" not in low
    if sid == "claude":
        # без ключа нормальный ответ — 401; из запретной страны — 403
        return status in (200, 401, 404, 405)
    if sid == "openrouter":
        # бывает 403 «Access denied by security policy» на части выходов
        return status == 200
    return 200 <= status < 500


def pick_nodes(nodes, checks):
    """Разложить серверы по контурам.

    checks — результаты проверок {node_key: {...}}. Если проверок ещё не было,
    выбираем по названию: без российских и мобильных узлов.
    Возвращает (основной контур, европейский контур) — списки индексов.
    """
    main, geo = [], []
    for i, (name, ob) in enumerate(nodes):
        c = checks.get(node_key(ob)) if checks else None
        if c is not None:
            if not c.get("alive"):
                continue
            main.append(i)
            if c.get("strict_ok"):
                geo.append(i)
        else:
            if not is_mobile(name):
                main.append(i)
            if not is_ru(name) and not is_mobile(name):
                geo.append(i)
    if not main:
        main = [i for i in range(len(nodes))]
    return main, geo


def build_xray(s, nodes, enabled=True, log_path=None, checks=None):
    """nodes — список (название, outbound) из всех подписок.

    enabled=False: Xray остаётся на тех же портах, но всё пускает напрямую,
    чтобы вручную настроенные программы (Гермес, Телеграм) не отвалились,
    когда Svpn выключен.
    """
    nodes = nodes[:MAX_NODES]
    cfg = {
        "log": {"loglevel": "warning"},
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"], "queryStrategy": "UseIP"},
        "inbounds": [
            {"tag": "http-in", "port": HTTP_PORT, "listen": "127.0.0.1",
             "protocol": "http", "settings": {"allowTransparent": False},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}},
            {"tag": "socks-in", "port": SOCKS_PORT, "listen": "127.0.0.1",
             "protocol": "socks", "settings": {"udp": True, "auth": "noauth"},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}},
        ],
        "outbounds": [],
        "routing": {"domainStrategy": "AsIs", "balancers": [], "rules": []},
    }
    if log_path:
        cfg["log"]["error"] = log_path
    if s.get("ipad"):
        cfg["inbounds"].append(
            {"tag": "ipad-in", "port": IPAD_PORT, "listen": "0.0.0.0",
             "protocol": "http", "settings": {"allowTransparent": False},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}})
    rules = cfg["routing"]["rules"]
    # Айпад пускаем только из домашней сети и Tailscale.
    rules.append({"type": "field", "inboundTag": ["ipad-in"],
                  "source": ["geoip:!private"], "outboundTag": "block"})

    if not enabled or not nodes:
        cfg["outbounds"] = [{"tag": "direct", "protocol": "freedom"},
                            {"tag": "block", "protocol": "blackhole"}]
        rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "direct"})
        return cfg

    obs = []
    for i, (name, ob) in enumerate(nodes):
        ob = copy.deepcopy(ob)
        ob["tag"] = f"n{i}"
        obs.append(ob)
        # отдельный вход на каждый сервер — для проверок в окне «Серверы»
        cfg["inbounds"].append({"tag": f"t{i}", "port": TEST_PORT_BASE + i,
                                "listen": "127.0.0.1", "protocol": "http",
                                "settings": {"allowTransparent": False}})
        rules.append({"type": "field", "inboundTag": [f"t{i}"], "outboundTag": f"n{i}"})
    rules.append({"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"})
    cfg["outbounds"] = obs + [{"tag": "direct", "protocol": "freedom"},
                              {"tag": "block", "protocol": "blackhole"}]

    main_i, geo_i = pick_nodes(nodes, checks)
    main_sel = [f"n{i}" for i in main_i]
    geo_sel = [f"n{i}" for i in geo_i]
    cfg["observatory"] = {"subjectSelector": sorted(set(main_sel + geo_sel)),
                          "probeURL": PROBE_URL, "probeInterval": "30s",
                          "enableConcurrency": True}
    cfg["routing"]["balancers"].append(
        {"tag": "auto", "selector": main_sel, "strategy": {"type": "leastPing"}})
    geo_domains = active_domains(s, geo=True)
    if geo_sel and geo_domains:
        cfg["routing"]["balancers"].append(
            {"tag": "geo", "selector": geo_sel, "strategy": {"type": "leastPing"}})
        rules.append({"type": "field",
                      "domain": ["domain:" + d for d in geo_domains],
                      "balancerTag": "geo"})
    rules.append({"type": "field", "network": "tcp,udp", "balancerTag": "auto"})
    return cfg


# ---------------------------------------------------------------- sing-box

def build_singbox(s, log_path=None):
    """Перехватчик: выбранные программы и сайты → Xray, остальное напрямую."""
    names, paths = active_apps(s)
    domains = active_domains(s)
    rules = [
        # DNS не перехватываем: Windows спрашивает имена как обычно, а
        # сайт узнаём по самому соединению (имя в TLS/QUIC).
        {"action": "sniff"},
        {"process_name": ALWAYS_DIRECT_APPS, "outbound": "direct"},
        {"ip_is_private": True, "outbound": "direct"},
        {"ip_cidr": ["100.64.0.0/10"], "outbound": "direct"},
    ]
    if names:
        rules.append({"process_name": names, "outbound": "vpn"})
    if paths:
        rules.append({"process_path": paths, "outbound": "vpn"})
    if domains:
        rules.append({"domain_suffix": domains, "outbound": "vpn"})
    cfg = {
        "log": {"level": "error", "timestamp": True},
        "dns": {
            "servers": [{"type": "local", "tag": "local"}],
            "final": "local",
            "strategy": "prefer_ipv4",
        },
        "inbounds": [{
            "type": "tun", "tag": "tun-in", "interface_name": "Svpn",
            # Только IPv4: у большинства домашних провайдеров IPv6 нет, и с ним
            # программы сперва стучатся в пустоту (так было у неё 19.10).
            "address": ["172.19.0.1/30"],
            "mtu": 9000, "auto_route": True, "strict_route": False,
            "stack": "mixed",
            "route_exclude_address": PRIVATE_NETS,
        }],
        "outbounds": [
            {"type": "direct", "tag": "direct"},
            {"type": "socks", "tag": "vpn", "server": "127.0.0.1",
             "server_port": SOCKS_PORT, "version": "5"},
        ],
        "route": {
            "rules": rules,
            "final": "direct",
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
        },
    }
    if log_path:
        cfg["log"]["output"] = log_path
    return cfg


# ---------------------------------------------------------------- айпад

def build_ipad_pac(s, proxy_host):
    """Файл правил для айпада: сайты из списка — через туннель компа."""
    doms = active_domains(s)
    lst = ",\n        ".join(json.dumps(d) for d in doms)
    return f"""// Svpn: правила для айпада. Создано автоматически.
function FindProxyForURL(url, host) {{
    var TUNNEL = "PROXY {proxy_host}:{IPAD_PORT}";
    host = host.toLowerCase();
    if (isPlainHostName(host) || /^(10|127|192\\.168|172\\.(1[6-9]|2\\d|3[01]))\\./.test(host)
        || /^100\\.(6[4-9]|[7-9]\\d|1[01]\\d|12[0-7])\\./.test(host)
        || dnsDomainIs(host, ".local") || dnsDomainIs(host, ".ts.net")) {{
        return "DIRECT";
    }}
    var list = [
        {lst}
    ];
    for (var i = 0; i < list.length; i++) {{
        if (host === list[i] || dnsDomainIs(host, "." + list[i])) return TUNNEL;
    }}
    return "DIRECT";
}}
"""
