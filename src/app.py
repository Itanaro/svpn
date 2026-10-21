"""Svpn — приложение для трея.

Запуск:
    pythonw app.py            обычный запуск (значок в трее)
    pythonw app.py            если уже запущен — просто покажет окно
    python  app.py --setup    одноразовая настройка с правами администратора:
                              задача перехватчика, брандмауэр, отключение
                              старого сторожа. Вызывается само при первом запуске
                              и если Svpn переехал в другую папку.
    pythonw app.py --uninstall  убрать за собой (зовёт установщик при удалении):
                              автозапуск, задачу перехватчика, правила брандмауэра.

Перехватчик (sing-box) работает от имени системы через задачу планировщика
«Svpn Interceptor», поэтому само приложение прав администратора не требует.
"""
import ctypes
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import engine as E

# ---------------------------------------------------------------- пути
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
    UI_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE), "ui")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    UI_DIR = os.path.join(BASE, "ui")
BIN = os.path.join(BASE, "bin")
DATA = os.path.join(os.environ.get("LOCALAPPDATA") or BASE, "Svpn")
if "--data" in sys.argv[:-1]:          # настройка с правами админа: папка данных того,
    DATA = sys.argv[sys.argv.index("--data") + 1]   # кто запустил Svpn, а не админа
os.makedirs(DATA, exist_ok=True)
SETTINGS = os.path.join(DATA, "settings.json")
SUBS_DIR = os.path.join(DATA, "subs")
os.makedirs(SUBS_DIR, exist_ok=True)
XRAY_CFG = os.path.join(DATA, "xray.json")
SB_CFG = os.path.join(DATA, "singbox.json")
CHECKS = os.path.join(DATA, "checks.json")
LOG_FILE = os.path.join(DATA, "svpn.log")
XRAY_LOG = os.path.join(DATA, "xray_error.log")
SB_LOG = os.path.join(DATA, "singbox.log")
XRAY_EXE = os.path.join(BIN, "xray.exe")
SB_EXE = os.path.join(BIN, "sing-box.exe")

TASK = "Svpn Interceptor"
VERSION = "1.1.0"
# Канал обновлений: файлы в репозитории Itanaro/svpn на GitHub (как у Проявки).
UPDATE_URL = "https://raw.githubusercontent.com/Itanaro/svpn/main/manifest.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOWIN = 0x08000000  # CREATE_NO_WINDOW
IS_WIN = os.name == "nt"


def self_cmd(*args):
    """Команда для запуска этого же приложения (скрипт или собранный exe)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"
        if os.path.exists(w):
            exe = w
    return [exe, os.path.abspath(__file__), *args]


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=NOWIN if IS_WIN else 0)
        return p.returncode, (p.stdout + p.stderr).decode("cp866", errors="ignore")
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------- журнал
_log = []
_log_lock = threading.Lock()


def log(msg):
    stamp = datetime.now()
    with _log_lock:
        _log.append((stamp.strftime("%d.%m %H:%M"), msg))
        del _log[:-200]
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 500_000:
                os.replace(LOG_FILE, LOG_FILE + ".old")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp:%Y-%m-%d %H:%M:%S}  {msg}\n")
        except OSError:
            pass


# ---------------------------------------------------------------- Windows
def autostart_cmd():
    return " ".join(f'"{c}"' for c in self_cmd("--autostart"))


def autostart_value():
    if not IS_WIN:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "Svpn")[0]
    except OSError:
        return None


def autostart_get():
    return autostart_value() is not None


def autostart_set(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Svpn", 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, "Svpn")
            except OSError:
                pass


def make_shortcut():
    """Ярлык «Svpn» на рабочем столе с иконкой серии (один раз)."""
    mark = os.path.join(DATA, ".shortcut")
    if os.path.exists(mark) or os.path.exists(os.path.join(BASE, "uninstall.exe")):
        return                     # ярлыки ставит установщик
    cmd = self_cmd()
    args = " ".join(f'"{c}"' for c in cmd[1:]).replace("'", "''")
    ico = os.path.join(UI_DIR, "svpn.ico")
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\Svpn.lnk\");"
          f"$s.TargetPath='{cmd[0]}';$s.Arguments='{args}';"
          f"$s.IconLocation='{ico}';$s.WorkingDirectory='{BASE}';"
          "$s.Description='Svpn — VPN для рабочих программ';$s.Save()")
    code, out = run(["powershell", "-NoProfile", "-Command", ps], 30)
    if code == 0:
        open(mark, "w").close()
        log("на рабочем столе появился ярлык «Svpn»")


def clear_old_pac():
    """Старый туннель раздавал Windows правила через 127.0.0.1:10879.
    Теперь сайты ловит перехватчик, системные правила не нужны."""
    if not IS_WIN:
        return
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            val = winreg.QueryValueEx(k, "AutoConfigURL")[0]
            if "127.0.0.1:10879" in val:
                winreg.DeleteValue(k, "AutoConfigURL")
                ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
                ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)
                log("убрала старые системные правила (PAC) — их заменил перехватчик")
    except OSError:
        pass


def listening_pids(ports):
    code, out = run(["netstat", "-ano", "-p", "TCP"], 30)
    pids = set()
    for line in out.splitlines():
        p = line.split()
        # Состояние в русской Windows пишется по-русски («ПРОСЛУШИВАНИЕ»),
        # поэтому узнаём слушающий порт по пустому удалённому адресу.
        if len(p) >= 5 and p[0].upper() == "TCP" and p[2].endswith(":0"):
            port = p[1].rsplit(":", 1)[-1]
            if port.isdigit() and int(port) in ports and p[-1].isdigit():
                pids.add(int(p[-1]))
    return pids


def process_running(image):
    code, out = run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], 20)
    return image.lower() in out.lower()


def task_exists():
    code, _ = run(["schtasks", "/Query", "/TN", TASK], 20)
    return code == 0


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(args):
    """Запустить этот же файл с правами администратора и дождаться."""
    cmd = self_cmd(*args)
    exe, params = cmd[0], " ".join(f'"{c}"' for c in cmd[1:])
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-11] + "python.exe"
    SEE_MASK_NOCLOSEPROCESS = 0x40

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                    ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                    ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                    ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p),
                    ("hProcess", ctypes.c_void_p)]
    sei = SEI(cbSize=ctypes.sizeof(SEI), fMask=SEE_MASK_NOCLOSEPROCESS,
              lpVerb="runas", lpFile=exe, lpParameters=params, nShow=0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 120_000)
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return code.value == 0


SETUP_MARK = os.path.join(DATA, "setup.json")


def setup_ok():
    """Задача перехватчика есть и смотрит на sing-box из этой папки Svpn."""
    if not task_exists():
        return False
    try:
        with open(SETUP_MARK, encoding="utf-8") as f:
            return json.load(f).get("sb", "").lower() == SB_EXE.lower()
    except (OSError, ValueError):
        pass
    code, out = run(["schtasks", "/Query", "/TN", TASK, "/XML"], 20)
    if code == 0 and SB_EXE.lower() in out.lower():
        mark_setup()
        return True
    return False


def mark_setup():
    try:
        with open(SETUP_MARK, "w", encoding="utf-8") as f:
            json.dump({"sb": SB_EXE, "at": time.time()}, f)
    except OSError:
        pass


def uninstall():
    """Зовёт установщик при удалении Svpn: убираем всё, что Svpn прописал в Windows."""
    autostart_set(False)
    run(["schtasks", "/End", "/TN", TASK], 30)
    run_elevated(["--uninstall-admin"])
    return 0


def uninstall_admin():
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
Stop-ScheduledTask -TaskName '{TASK}'
Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false
Get-NetFirewallRule -DisplayName 'Svpn iPad*' | Remove-NetFirewallRule
'ok'
"""
    code, txt = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], 120)
    return 0


# ---------------------------------------------------------------- setup (админ)
def setup():
    """Одноразовая настройка. Запускается с правами администратора."""
    out = []
    data = DATA
    ps = f"""
$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute '{SB_EXE}' -Argument 'run -c "{SB_CFG}" -D "{data}"' -WorkingDirectory '{data}'
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName '{TASK}' -Action $a -Principal $p -Settings $s -Force | Out-Null
# обычному пользователю — право запускать и останавливать эту задачу
$svc = New-Object -ComObject 'Schedule.Service'; $svc.Connect()
$t = $svc.GetFolder('\\').GetTask('{TASK}')
$t.SetSecurityDescriptor('D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;GRGX;;;AU)', 0)
# айпад: вход в туннель и раздача правил — только домашняя сеть и Tailscale
Get-NetFirewallRule -DisplayName 'Svpn iPad*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'Svpn iPad (10880-10881)' -Direction Inbound -Protocol TCP -LocalPort 10880,10881 -RemoteAddress LocalSubnet,100.64.0.0/10 -Action Allow -Profile Any | Out-Null
$prog = Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -eq '{XRAY_EXE}' }}
foreach ($f in $prog) {{ $r = $f | Get-NetFirewallRule; if ($r.Action -eq 'Block') {{ $r | Remove-NetFirewallRule }} }}
'ok'
"""
    code, txt = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", ps], 120)
    out.append(txt)
    # Старый туннель (сторож из .hermes-vpn) больше не нужен: выключаем его
    # задачи, файлы не трогаем — откат в любой момент.
    code2, csv = run(["schtasks", "/Query", "/FO", "CSV", "/V"], 60)
    for line in csv.splitlines():
        low = line.lower()
        if ".hermes-vpn" in low and any(w in low for w in (
                "watchdog", "xray", "start_hermes_vpn", "pac_server")):
            name = line.split('","')[1] if '","' in line else ""
            if name:
                run(["schtasks", "/Change", "/TN", name, "/DISABLE"], 30)
                out.append("disabled " + name)
    with open(os.path.join(DATA, "setup.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return 0 if code == 0 and "ok" in txt else 1


# ---------------------------------------------------------------- контроллер
class Svpn:
    def __init__(self):
        # Первый запуск: стартовые настройки и подписки, если их положили рядом.
        seed = os.path.join(BASE, "seed")
        if not os.path.exists(SETTINGS) and os.path.exists(os.path.join(seed, "settings.json")):
            import shutil
            shutil.copy(os.path.join(seed, "settings.json"), SETTINGS)
            for kind in ("main", "geo"):
                src = os.path.join(seed, kind + ".json")
                if os.path.exists(src):
                    shutil.copy(src, os.path.join(SUBS_DIR, kind + ".json"))
        self.s = E.load_settings(SETTINGS)
        self.lock = threading.RLock()
        self.xray = None
        self.health = "starting"
        self.problem = ""
        self.exit = ""
        self.fails = 0
        self.last_sub = 0
        self.nodes = {"main": [], "geo": []}
        self.checks = {}
        self.checking = False
        try:
            with open(CHECKS, encoding="utf-8") as f:
                self.checks = json.load(f)
        except (OSError, ValueError):
            pass
        self.token = secrets.token_urlsafe(16)
        self.on_change = lambda: None
        self._load_nodes()

    # --- подписки
    def _load_nodes(self):
        for kind in ("main", "geo"):
            try:
                with open(os.path.join(SUBS_DIR, kind + ".json"), encoding="utf-8") as f:
                    self.nodes[kind] = [tuple(x) for x in json.load(f)]
            except (OSError, ValueError):
                self.nodes[kind] = []

    def refresh_subs(self):
        changed = False
        for kind in ("main", "geo"):
            urls = self.s["subscriptions"].get(kind) or []
            if not urls:
                if self.nodes[kind]:
                    self.nodes[kind] = []
                    changed = True
                continue
            got = []
            for url in urls:
                if "://" in url and not url.startswith("http"):
                    # ключ сервера целиком (vless://…), а не ссылка на подписку
                    try:
                        node = E.parse_link(url)
                    except Exception:
                        node = None
                    if node:
                        got.append(node)
                    else:
                        log("не поняла ключ сервера: " + url[:30] + "…")
                    continue
                raw = None
                vpn = f"http://127.0.0.1:{E.HTTP_PORT}"
                xray_up = self.xray is not None and self.xray.poll() is None
                for proxy in ((vpn, None) if xray_up else (None, vpn)):
                    try:
                        raw = E.fetch_url(url, proxy=proxy)
                        break
                    except Exception as e:
                        err = e
                if raw is None:
                    log(f"подписка не скачалась ({str(err)[:80]}), беру сохранённую")
                    continue
                nodes = E.parse_subscription(raw)
                if not nodes:
                    log("в подписке не нашлось понятных мне серверов")
                got += nodes
            if got:
                self.nodes[kind] = got
                with open(os.path.join(SUBS_DIR, kind + ".json"), "w", encoding="utf-8") as f:
                    json.dump(got, f, ensure_ascii=False)
                changed = True
        self.last_sub = time.time()
        log(f"подписки: серверов {len(self.all_nodes())}")
        return changed

    # --- конфиги и процессы
    def write_configs(self):
        on = self.s["enabled"]
        x = E.build_xray(self.s, self.all_nodes(), enabled=on,
                         log_path=XRAY_LOG, checks=self.checks)
        sb = E.build_singbox(self.s, log_path=SB_LOG)
        changed = []
        for path, cfg in ((XRAY_CFG, x), (SB_CFG, sb)):
            new = json.dumps(cfg, ensure_ascii=False, indent=1)
            try:
                old = open(path, encoding="utf-8").read()
            except OSError:
                old = ""
            if new != old:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
            changed.append(new != old)
        return tuple(changed)          # (поменялся Xray, поменялся перехватчик)

    def start_xray(self):
        self.stop_xray()
        # Чужие процессы на наших портах (например, старый туннель) — снимаем.
        mine = {self.xray.pid} if self.xray else set()
        for pid in listening_pids({E.HTTP_PORT, E.SOCKS_PORT, E.IPAD_PORT}) - mine:
            run(["taskkill", "/F", "/PID", str(pid)], 20)
            log(f"порт туннеля занимала другая программа (PID {pid}) — закрыла её")
        env = dict(os.environ, XRAY_LOCATION_ASSET=BIN)
        self.xray = subprocess.Popen([XRAY_EXE, "run", "-c", XRAY_CFG], cwd=DATA, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=NOWIN)
        time.sleep(1.5)
        if self.xray.poll() is not None:
            log("VPN-движок не запустился — подробности в xray_error.log")
            return False
        return True

    def stop_xray(self):
        if self.xray and self.xray.poll() is None:
            self.xray.terminate()
            try:
                self.xray.wait(5)
            except subprocess.TimeoutExpired:
                self.xray.kill()
        self.xray = None

    def interceptor(self, on):
        running = process_running("sing-box.exe")
        if on and not running:
            # журнал перехватчика пишется без ограничений — подрезаем перед стартом
            try:
                if os.path.getsize(SB_LOG) > 5_000_000:
                    os.replace(SB_LOG, SB_LOG + ".old")
            except OSError:
                pass
            code, out = run(["schtasks", "/Run", "/TN", TASK], 30)
            if code != 0:
                log("не получилось запустить перехватчик: " + out.strip()[:120])
        elif not on and running:
            run(["schtasks", "/End", "/TN", TASK], 30)

    def apply(self, restart_interceptor=False, interceptor=True):
        with self.lock:
            x_changed, sb_changed = self.write_configs()
            restarted = False
            if x_changed or not self.xray or self.xray.poll() is not None:
                self.start_xray()
                restarted = True
            if self.s["enabled"]:
                if interceptor:
                    if (sb_changed or restart_interceptor) and process_running("sing-box.exe"):
                        run(["schtasks", "/End", "/TN", TASK], 30)
                        time.sleep(1)
                    self.interceptor(True)
                if restarted or sb_changed:
                    self.health = "starting"
                    threading.Thread(target=self._quick_check, daemon=True).start()
            else:
                self.interceptor(False)
                self.health = "off"
        self.on_change()

    def _quick_check(self):
        """Сразу после перезапуска: как только связь есть — «работает»."""
        for _ in range(6):
            time.sleep(2)
            if not self.s["enabled"] or self.health != "starting":
                return
            try:
                if self.probe(timeout=6)[0] == 204:
                    self.health = "ok"
                    self.problem = ""
                    self.on_change()
                    return
            except Exception:
                pass

    def save(self):
        E.save_settings(SETTINGS, self.s)

    # --- проверки
    def probe(self, url=E.PROBE_URL, timeout=10):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": f"http://127.0.0.1:{E.HTTP_PORT}",
             "https": f"http://127.0.0.1:{E.HTTP_PORT}"}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(400)

    def all_nodes(self):
        """Серверы всех подписок, без повторов."""
        out, seen = [], set()
        for name, ob in self.nodes["main"] + self.nodes["geo"]:
            k = E.node_key(ob)
            if k not in seen:
                seen.add(k)
                out.append((name, ob))
        return out[:E.MAX_NODES]

    def check_nodes(self):
        """Проверить каждый сервер: жив ли, какая страна, пускают ли
        капризные сервисы. По итогам Svpn сам решает, какие серверы
        годятся для сайтов с отметкой «Европа»."""
        if self.checking:
            return
        if not self.s["enabled"] or not self.xray or self.xray.poll() is not None:
            raise ValueError("Включи Svpn — проверять серверы можно только при работающем VPN")
        self.checking = True
        self.on_change()
        nodes = self.all_nodes()
        log(f"проверяю серверы: {len(nodes)}")

        def one(i):
            name, ob = nodes[i]
            proxy = f"http://127.0.0.1:{E.TEST_PORT_BASE + i}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}))
            r = {"name": name, "alive": False, "at": time.time()}

            def get(url, timeout=12):
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                try:
                    with opener.open(req, timeout=timeout) as resp:
                        return resp.status, resp.read(600).decode("utf-8", "ignore")
                except urllib.error.HTTPError as e:
                    return e.code, e.read(600).decode("utf-8", "ignore")
            try:
                t0 = time.time()
                st, _ = get(E.PROBE_URL, 10)
                r["alive"] = st == 204
                r["ping"] = int((time.time() - t0) * 1000)
            except Exception:
                return r
            if not r["alive"]:
                return r
            try:
                st, body = get("https://api.country.is/")
                r["country"] = json.loads(body).get("country", "")
            except Exception:
                r["country"] = ""
            r["services"] = {}
            for c in E.SERVICE_CHECKS:
                try:
                    st, body = get(c["url"])
                    r["services"][c["id"]] = E.service_verdict(c["id"], st, body)
                except Exception:
                    r["services"][c["id"]] = False
            r["strict_ok"] = all(r["services"].get(c["id"]) for c in E.SERVICE_CHECKS
                                 if c["strict"]) and r.get("country") != "RU"
            return r
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(8) as pool:
                res = list(pool.map(one, range(len(nodes))))
            self.checks = {E.node_key(ob): r for (_, ob), r in zip(nodes, res)}
            with open(CHECKS, "w", encoding="utf-8") as f:
                json.dump(self.checks, f, ensure_ascii=False)
            alive = sum(1 for r in res if r["alive"])
            geo = sum(1 for r in res if r.get("strict_ok"))
            log(f"серверы проверены: живых {alive} из {len(res)}, "
                f"для «Европы» подходят {geo}")
        finally:
            self.checking = False
        self.apply()

    def servers(self):
        out = []
        for name, ob in self.all_nodes():
            c = self.checks.get(E.node_key(ob))
            row = {"name": name, "checked": c is not None}
            if c:
                row.update(alive=c.get("alive", False), ping=c.get("ping"),
                           country=COUNTRIES.get(c.get("country", ""), c.get("country", "")),
                           services=c.get("services", {}), strict_ok=c.get("strict_ok", False))
            out.append(row)
        return out

    def check_all(self):
        res = []
        tests = [
            ("Связь с VPN", E.PROBE_URL, lambda st, b: st == 204, "есть"),
            ("Страна выхода (европейский контур)", "https://api.country.is/", None, None),
            ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/models?key=x",
             None, None),
            ("YouTube", "https://www.youtube.com/generate_204", lambda st, b: st in (200, 204), "открывается"),
        ]
        for name, url, okf, oktext in tests:
            try:
                st, body = self.probe(url, 15)
                if "country.is" in url:
                    c = json.loads(body).get("country", "?")
                    res.append({"name": name, "ok": c != "RU", "result": c})
                else:
                    res.append({"name": name, "ok": okf(st, body), "result": oktext})
            except urllib.error.HTTPError as e:
                body = e.read(400).decode("utf-8", "ignore")
                if "googleapis" in url:
                    ok = "location" not in body.lower()
                    res.append({"name": name, "ok": ok,
                                "result": "доступен" if ok else "регион отклонён"})
                else:
                    res.append({"name": name, "ok": False, "result": f"ошибка {e.code}"})
            except Exception as e:
                res.append({"name": name, "ok": False, "result": "нет связи"})
        res.append({"name": "Перехватчик программ и сайтов",
                    "ok": process_running("sing-box.exe"),
                    "result": "работает" if process_running("sing-box.exe") else "не запущен"})
        return res

    def watch(self):
        """Встроенный сторож: раз в 20 секунд проверяет связь."""
        n = 0
        while True:
            time.sleep(20)
            n += 1
            try:
                self._watch_once(n)
            except Exception as e:
                log(f"сторож споткнулся: {e}")

    def _watch_once(self, n):
        if time.time() - self.last_sub > 6 * 3600:
            if self.refresh_subs():
                self.apply()
        if not self.s["enabled"]:
            return
        if not self.all_nodes():
            prev, self.health = self.health, "bad"
            self.problem = "Добавь ссылку на подписку VPN в Настройках."
            if prev != self.health:
                self.on_change()
            return
        if self.health == "ok" and not self.checking and self.all_nodes():
            keys = {E.node_key(ob) for _, ob in self.all_nodes()}
            stamps = [self.checks[k].get("at", 0) for k in keys if k in self.checks]
            if len(stamps) < len(keys) or time.time() - min(stamps) > 24 * 3600:
                threading.Thread(target=self.check_nodes, daemon=True).start()
        if not self.xray or self.xray.poll() is not None:
            log("VPN-движок остановился — перезапускаю")
            self.apply()
        try:
            st, _ = self.probe()
            ok = st == 204
        except Exception:
            ok = False
        prev = self.health
        if ok:
            if self.fails >= 3:
                log("связь восстановлена")
                self.interceptor(True)
            self.fails = 0
            self.health = "ok"
            self.problem = ""
            if not process_running("sing-box.exe"):
                self.interceptor(True)
            if n % 30 == 1 or not self.exit:
                try:
                    st, body = self.probe("https://api.country.is/", 15)
                    c = json.loads(body).get("country", "")
                    self.exit = COUNTRIES.get(c, c)
                except Exception:
                    pass
        else:
            self.fails += 1
            if self.fails == 3:
                log("нет связи с VPN — перезапускаю движок, пока всё идёт напрямую")
                self.interceptor(False)
                self.health = "bad"
                self.problem = "Перезапускаю VPN. Пока всё идёт напрямую, интернет работает."
                with self.lock:
                    self.start_xray()
            elif self.fails == 6:
                log("перезапуск не помог — обновляю подписки")
                self.refresh_subs()
                self.apply(interceptor=False)
            elif self.fails % 15 == 0:
                with self.lock:
                    self.start_xray()
            if self.fails >= 3:
                self.health = "bad"
                self.problem = ("Серверы VPN не отвечают. Пока всё идёт напрямую. "
                                "Если долго не проходит — проверь подписку в настройках.")
        if prev != self.health:
            self.on_change()

    def state(self):
        return {
            "settings": self.s, "health": self.health if self.s["enabled"] else "off",
            "problem": self.problem, "exit": self.exit, "autostart": autostart_get(),
            "servers": self.servers(), "checking": self.checking,
            "services": [{"id": c["id"], "name": c["name"]} for c in E.SERVICE_CHECKS],
            "pac_url": f"http://{lan_ip()}:{E.IPAD_PAC_PORT}/ipad.pac",
            "log": list(_log[-60:]),
            "glass_ok": getattr(self, "glass_ok", False),
            "version": VERSION,
            "update": ({"version": self.updater.available.get("version"),
                        "notes": self.updater.available.get("notes", ""),
                        "mandatory": bool(self.updater.available.get("mandatory"))}
                       if getattr(self, "updater", None) and self.updater.available else None),
            "update_status": getattr(self, "updater", None) and self.updater.status or "",
        }


COUNTRIES = {"NL": "Нидерланды", "DE": "Германия", "FI": "Финляндия", "BG": "Болгария",
             "GR": "Греция", "US": "США", "PL": "Польша", "SE": "Швеция", "FR": "Франция",
             "GB": "Великобритания", "KZ": "Казахстан", "EE": "Эстония", "LV": "Латвия",
             "RU": "Россия", "TR": "Турция", "AT": "Австрия", "CH": "Швейцария"}


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------- обновления
def vtuple(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Updater:
    """Проверяет, не вышла ли новая версия, и ставит её.

    В выпуске лежит manifest.json: версия, что нового и список файлов с
    контрольными суммами. Скачиваются только изменившиеся файлы, каждый
    сверяется с суммой — битый или подменённый файл не поставится.
    """
    def __init__(self, t):
        self.t = t
        self.available = None
        self.status = ""
        self.busy = False
        self.notify = lambda text: None

    def _get(self, url, timeout=30):
        last = None
        for proxy in (None, f"http://127.0.0.1:{E.HTTP_PORT}"):
            try:
                return E.fetch_url(url, proxy=proxy, timeout=timeout)
            except Exception as e:
                last = e
        raise last

    def check(self, quiet=True):
        self.error = ""
        try:
            m = json.loads(self._get(UPDATE_URL, 20))
        except Exception as e:
            self.error = ("на сервере обновлений пока нет ни одной версии" if "404" in str(e)
                          else str(e)[:80])
            if not quiet:
                log(f"не получилось проверить обновления: {self.error}")
            return None
        if vtuple(m.get("version", "0")) > vtuple(VERSION):
            first = self.available is None or self.available.get("version") != m["version"]
            self.available = m
            if first:
                log(f"вышла новая версия Svpn {m['version']}")
                self.notify(f"Вышла новая версия Svpn {m['version']}. "
                            "Открой окно Svpn, чтобы обновиться.")
            self.t.on_change()
        else:
            self.available = None
        return self.available

    def apply(self):
        m = self.available
        if not m or self.busy:
            return
        self.busy = True
        try:
            stage = os.path.join(DATA, "update")
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
            todo = []
            for f in m.get("files", []):
                rel = f["path"].replace("/", os.sep)
                if ".." in rel.split(os.sep) or os.path.isabs(rel):
                    raise ValueError("странный путь в обновлении: " + f["path"])
                local = os.path.join(BASE, rel)
                if os.path.exists(local) and sha256_file(local) == f["sha256"]:
                    continue
                todo.append((rel, f))
            for n, (rel, f) in enumerate(todo, 1):
                self.status = f"Скачиваю обновление: {n} из {len(todo)}"
                self.t.on_change()
                data = self._get(f["url"], 600)
                import hashlib
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    raise ValueError(f"файл {f['path']} пришёл повреждённым")
                dst = os.path.join(stage, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(data)
            self.status = "Ставлю обновление, Svpn перезапустится"
            self.t.on_change()
            log(f"ставлю версию {m['version']} (файлов: {len(todo)})")
            # Подменить файлы можно только когда Svpn закрыт — это сделает
            # маленький скрипт, он же запустит Svpn обратно.
            cmd = self_cmd()
            restart = "Start-Process -FilePath '{}' -ArgumentList '{}'".format(
                cmd[0], " ".join(f'"{c}"' for c in cmd[1:]).replace("'", "''")) \
                if len(cmd) > 1 else f"Start-Process -FilePath '{cmd[0]}'"
            ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id {os.getpid()} -Timeout 30
Start-Sleep 1
Copy-Item -Path '{stage}\\*' -Destination '{BASE}' -Recurse -Force
Remove-Item -Path '{stage}' -Recurse -Force
{restart}
"""
            script = os.path.join(DATA, "apply-update.ps1")
            with open(script, "w", encoding="utf-8-sig") as fh:
                fh.write(ps)
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-WindowStyle", "Hidden", "-File", script],
                             creationflags=NOWIN | 0x00000008)
            self.t.quit()
        except Exception as e:
            self.status = f"Обновление не встало: {e}"
            log(self.status)
            self.busy = False
            self.t.on_change()

    def loop(self):
        time.sleep(30)
        while True:
            self.check()
            time.sleep(6 * 3600)


# ---------------------------------------------------------------- HTTP: окно и айпад
def make_ui_handler(t):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self.api(None)
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            static = {"/favicon.png": "image/png", "/svpn.png": "image/png"}
            if path in static:
                with open(os.path.join(UI_DIR, path[1:]), "rb") as f:
                    return self.send(200, f.read(), static[path])
            self.send(404, {"error": "нет такой страницы"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            self.api(body)

        def api(self, body):
            if self.headers.get("X-Token") != t.token:
                return self.send(403, {"error": "нет доступа"})
            name = self.path.split("?")[0][5:]
            try:
                if name == "show":
                    t.show_window()
                    return self.send(200, {"ok": True})
                if name == "state":
                    return self.send(200, t.state())
                if name == "check":
                    return self.send(200, t.check_all())
                if name == "update":
                    threading.Thread(target=t.updater.apply, daemon=True).start()
                    return self.send(200, t.state())
                if name == "check_update":
                    t.updater.check(quiet=False)
                    if getattr(t.updater, "error", ""):
                        raise ValueError("Не получилось проверить обновления: " + t.updater.error)
                    return self.send(200, t.state())
                if name == "check_nodes":
                    threading.Thread(target=lambda: _safe(t.check_nodes), daemon=True).start()
                    if not t.s["enabled"]:
                        raise ValueError("Включи Svpn — проверять серверы можно только при работающем VPN")
                    return self.send(200, t.state())
                handle_action(t, name, body or {})
                return self.send(200, t.state())
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:
                log(f"ошибка в окне: {e}")
                return self.send(500, {"error": "что-то пошло не так, смотри журнал"})
    return H


def _safe(fn):
    try:
        fn()
    except Exception as e:
        log(f"проверка серверов: {e}")


def handle_action(t, name, b):
    s = t.s
    restart = False
    if name == "enabled":
        s["enabled"] = bool(b.get("on"))
        log("Svpn включён" if s["enabled"] else "Svpn выключен")
    elif name == "autostart":
        autostart_set(bool(b.get("on")))
        s["autostart"] = bool(b.get("on"))
        t.save()
        return
    elif name == "glass":
        s["glass"] = bool(b.get("on"))
        t.save()
        getattr(t, "on_glass", lambda on: None)(s["glass"])
        return
    elif name == "theme":
        if b.get("name") not in THEMES:
            raise ValueError("нет такой темы")
        s["theme"] = b["name"]
        t.save()
        getattr(t, "on_theme", lambda n: None)(s["theme"])
        return
    elif name == "ipad":
        s["ipad"] = bool(b.get("on"))
    elif name == "site":
        if "add" in b:
            d = E.clean_domain(b["add"])
            if not d:
                raise ValueError("Не похоже на адрес сайта")
            s["sites"].append({"name": d, "domains": [d], "on": True, "geo": False, "note": ""})
            log(f"добавлен сайт {d}")
        else:
            i = int(b["i"])
            site = s["sites"][i]
            if b.get("remove"):
                s["sites"].pop(i)
                log(f"удалён сайт {site['name']}")
            if "on" in b:
                site["on"] = bool(b["on"])
            if "geo" in b:
                site["geo"] = bool(b["geo"])
            if "domains" in b:
                ds = [E.clean_domain(x) for x in b["domains"]]
                ds = [x for x in ds if x]
                if not ds:
                    raise ValueError("Нужен хотя бы один адрес")
                site["domains"] = ds
    elif name == "app":
        if "add" in b:
            v = b["add"].strip().strip('"')
            if not v.lower().endswith(".exe"):
                raise ValueError("Нужно имя файла .exe")
            entry = {"name": os.path.basename(v)[:-4], "exe": [], "paths": [], "on": True, "note": ""}
            (entry["paths"] if ("\\" in v or "/" in v) else entry["exe"]).append(v)
            s["apps"].append(entry)
            log(f"добавлена программа {v}")
        else:
            i = int(b["i"])
            a = s["apps"][i]
            if b.get("remove"):
                s["apps"].pop(i)
            if "on" in b:
                a["on"] = bool(b["on"])
            if "files" in b:
                files = [f.strip().strip('"') for f in b["files"] if f.strip()]
                files = [f for f in files if f.lower().endswith(".exe")]
                if not files:
                    raise ValueError("Нужен хотя бы один файл .exe")
                a["exe"] = [f for f in files if "\\" not in f and "/" not in f]
                a["paths"] = [f for f in files if "\\" in f or "/" in f]
    elif name == "subs":
        ok = lambda u: "://" in u
        s["subscriptions"] = {"main": [u.strip() for u in b.get("main", []) if ok(u)],
                              "geo": [u.strip() for u in b.get("geo", []) if ok(u)]}
        t.save()
        t.refresh_subs()
        restart = True
        t.checks = {}
        threading.Timer(15, lambda: _safe(t.check_nodes)).start()
    else:
        raise ValueError("неизвестное действие")
    t.save()
    t.apply(restart_interceptor=restart)


def make_pac_handler(t):
    import ipaddress

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            ip = ipaddress.ip_address(self.client_address[0])
            if not (ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")):
                self.send_response(403)
                self.end_headers()
                return
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if not t.s.get("ipad") or not t.s["enabled"] or t.health == "bad" \
                    or not all(c.isalnum() or c in ".-" for c in host):
                body = b'function FindProxyForURL(u, h) { return "DIRECT"; }\n'
            else:
                body = E.build_ipad_pac(t.s, host).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    return H


def serve(addr, handler):
    srv = http.server.ThreadingHTTPServer(addr, handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- окно
# Темы окна: цвет фона и заголовка, тёмный ли заголовок, цвет текста заголовка
THEMES = {"night": ("#121014", True, "#f3eff5"),
          "cloud": ("#ebe9f1", False, "#2c2233"),
          "sun": ("#f6eee6", False, "#33241f")}


def colorref(hex_):
    """#rrggbb → число в формате Windows (синий-зелёный-красный)."""
    r, g, b = int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)
    return (b << 16) | (g << 8) | r


class Window:
    """Собственное окно Svpn — pywebview (на Windows внутри WebView2).
    Крестик и сворачивание прячут окно в трей, «Выйти» в трее закрывает всё."""

    def __init__(self, url, hidden, theme="night", glass=True):
        import webview
        self.webview = webview
        self.quitting = False
        self.theme = theme if theme in THEMES else "night"
        self.glass_on = glass
        # Настоящая прозрачность (матовое стекло Acrylic) есть в Windows 11 22H2 и новее
        try:
            self.glass_ok = sys.getwindowsversion().build >= 22621
        except Exception:
            self.glass_ok = False
        self.w = webview.create_window("Svpn", url, width=540, height=800, min_size=(440, 580),
                                       hidden=hidden, background_color=THEMES[self.theme][0])
        self.w.events.before_show += self._setup_glass
        self.w.events.before_show += self._dark_title
        self.w.events.before_show += self._watch_shutdown
        self.w.events.closing += self._on_closing
        self.w.events.minimized += self._on_minimized
        self.on_shutdown = lambda: None

    def _setup_glass(self, window):
        """Готовим окно к матовому стеклу: страница рисуется поверх прозрачного фона,
        а под ней Windows размывает то, что лежит за окном. Насколько окно прозрачно,
        решает сама страница (тумблер «Прозрачное окно» в Настройках)."""
        if not self.glass_ok:
            return
        try:
            from System.Drawing import Color

            class MARGINS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                            ("t", ctypes.c_int), ("b", ctypes.c_int)]
            form = window.native
            form.BackColor = Color.Black                     # чёрное = «стекло» для Windows
            form.browser.webview.DefaultBackgroundColor = Color.Transparent
            h = form.Handle.ToInt32()
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                ctypes.c_void_p(h), ctypes.byref(MARGINS(-1, -1, -1, -1)))
        except Exception as e:
            self.glass_ok = False
            log(f"прозрачность окна не включилась: {e}")

    def _dark_title(self, window):
        """Заголовок окна в цвет темы, под ним матовое стекло (если включено)."""
        try:
            from webview.platforms.winforms import DwmSetWindowAttribute as dwm
            bg, dark, ink = THEMES[self.theme]
            glass = self.glass_ok and self.glass_on
            h = window.native.Handle.ToInt32()
            dwm(h, 20, 1 if dark else 0)  # тёмный или светлый заголовок (Windows 10 и 11)
            if self.glass_ok:
                dwm(h, 38, 3)             # матовое стекло Acrylic (Windows 11)
            # цвет заголовка: прозрачный при стекле, иначе цвет темы (Windows 11)
            dwm(h, 35, 0xFFFFFFFE if glass else colorref(bg))
            dwm(h, 34, colorref(bg))      # цвет рамки (Windows 11)
            dwm(h, 36, colorref(ink))     # цвет текста заголовка (Windows 11)
        except Exception:
            pass

    def set_theme(self, name):
        self.theme = name
        self._dark_title(self.w)

    def set_glass(self, on):
        self.glass_on = on
        self._dark_title(self.w)

    def _watch_shutdown(self, window):
        try:
            from System.Windows.Forms import CloseReason

            def closing(sender, args):
                if args.CloseReason in (CloseReason.WindowsShutDown,
                                        CloseReason.TaskManagerClosing):
                    self.quitting = True
                    args.Cancel = False            # не мешаем выключению Windows
                    try:
                        self.on_shutdown()
                    except Exception:
                        pass
            window.native.FormClosing += closing   # срабатывает после крестика pywebview
        except Exception as e:
            log(f"окно: не получилось следить за выключением Windows ({e})")

    def _on_closing(self):
        if self.quitting:
            return True
        threading.Thread(target=self.w.hide, daemon=True).start()
        return False                      # крестик не закрывает, а прячет

    def _on_minimized(self):
        self.w.hide()

    def show(self):
        """Показать окно из трея. Свёрнутое окно Windows показывает только кнопкой
        на панели задач, поэтому сначала возвращаем ему обычный размер."""
        self.w.show()
        try:
            from System import Func, Type
            from System.Windows.Forms import FormWindowState
            form = self.w.native

            def front():
                if form.WindowState == FormWindowState.Minimized:
                    form.WindowState = FormWindowState.Normal
                form.Activate()
            form.Invoke(Func[Type](front))
            u = ctypes.windll.user32
            h = form.Handle.ToInt32()
            if u.IsIconic(h):
                u.ShowWindow(h, 9)          # SW_RESTORE — на всякий случай напрямую
            u.SetForegroundWindow(h)
        except Exception as e:
            log(f"окно: не получилось вывести на передний план ({e})")
            try:
                self.w.restore()
            except Exception:
                pass
        self._dark_title(self.w)

    def close(self):
        self.quitting = True
        try:
            self.w.destroy()
        except Exception:
            pass

    def start(self):
        self.webview.start(gui="edgechromium", icon=os.path.join(UI_DIR, "svpn.ico"),
                           private_mode=False, storage_path=os.path.join(DATA, "webview"))


def ask_running_to_show():
    """Svpn уже запущен — просим его показать окно (второе окно не плодим)."""
    try:
        tok = open(os.path.join(DATA, ".token"), encoding="utf-8").read().strip()
        req = urllib.request.Request(f"http://127.0.0.1:{E.UI_PORT}/api/show", data=b"{}",
                                     headers={"X-Token": tok, "Content-Type": "application/json"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5).read()
    except Exception:
        pass


# ---------------------------------------------------------------- трей
def make_icon(state):
    """Значок трея — розовая S серии AI Edit с цветной точкой состояния."""
    from PIL import Image
    name = {"ok": "tray-ok", "off": "tray-off"}.get(state, "tray-warn")
    return Image.open(os.path.join(UI_DIR, name + ".png"))


COLORS = {"ok": (46, 122, 87, 255), "starting": (168, 112, 26, 255),
          "bad": (168, 112, 26, 255), "off": (140, 148, 143, 255)}
TITLES = {"ok": "Svpn: работает", "starting": "Svpn: запускается",
          "bad": "Svpn: нет связи с VPN, всё идёт напрямую", "off": "Svpn: выключен"}


def main_app():
    if not IS_WIN:
        raise SystemExit("Svpn работает только на Windows")
    # Уже запущен? Тогда просто открываем окно.
    try:
        with socket.create_connection(("127.0.0.1", E.UI_PORT), timeout=1):
            pass
        ask_running_to_show()
        return
    except OSError:
        pass

    t = Svpn()
    log("Svpn запущен")
    with open(os.path.join(DATA, ".token"), "w", encoding="utf-8") as f:
        f.write(t.token)
    url = f"http://127.0.0.1:{E.UI_PORT}/?t={t.token}"

    if not setup_ok():
        log("настраиваю перехватчик: Windows спросит разрешение — нужно ответить «Да»")
        if run_elevated(["--setup", "--data", DATA]) and task_exists():
            mark_setup()
        else:
            log("настройка без прав администратора не прошла — программы и сайты "
                "перехватываться не будут, работают только ручные прокси")
    make_shortcut()
    # автозапуск должен вести на этот Svpn (после переезда в другую папку — поправить)
    if t.s.get("autostart") and autostart_value() != autostart_cmd():
        autostart_set(True)
    clear_old_pac()

    # Старый сервер правил (10879) и его раздача для айпада (10881) больше не нужны.
    for pid in listening_pids({10879, E.IPAD_PAC_PORT}):
        run(["taskkill", "/F", "/PID", str(pid)], 20)
        log(f"закрыла старый сервер правил (PID {pid})")
    serve(("127.0.0.1", E.UI_PORT), make_ui_handler(t))
    try:
        serve(("0.0.0.0", E.IPAD_PAC_PORT), make_pac_handler(t))
    except OSError:
        log("порт для айпада занят (возможно, старый сервер правил) — айпад пока без туннеля")

    def boot():
        if t.nodes["main"] or t.nodes["geo"]:
            t.apply(restart_interceptor=True)          # сразу на сохранённых серверах
        if t.s["subscriptions"]["main"] or t.s["subscriptions"]["geo"]:
            t.refresh_subs()                           # уже через VPN
        if not (t.nodes["main"] or t.nodes["geo"]):
            log("нет подписок — добавь ссылку в настройках")
            t.problem = "Добавь ссылку на подписку VPN в настройках."
        t.apply(restart_interceptor=not t.xray)
        threading.Thread(target=t.watch, daemon=True).start()
    threading.Thread(target=boot, daemon=True).start()
    t.updater = Updater(t)
    threading.Thread(target=t.updater.loop, daemon=True).start()

    import pystray

    # Окно создаём заранее (спрятанным при автозапуске). Если WebView2 нет —
    # Svpn всё равно работает, а окно откроется в обычном браузере.
    ui = {"win": None}
    try:
        ui["win"] = Window(url, hidden="--autostart" in sys.argv, theme=t.s.get("theme", "night"),
                           glass=t.s.get("glass", True))
        t.on_theme = ui["win"].set_theme
        t.on_glass = ui["win"].set_glass
        t.glass_ok = ui["win"].glass_ok

        def on_shutdown():
            log("Windows выключается — останавливаю Svpn")
            t.interceptor(False)
            t.stop_xray()
        ui["win"].on_shutdown = on_shutdown
    except Exception as e:
        log(f"окно Svpn не создалось ({e}) — открою его в браузере")

    def show():
        if ui["win"]:
            ui["win"].show()
        else:
            os.startfile(url)
    t.show_window = show

    def cur():
        return t.health if t.s["enabled"] else "off"

    def toggle(icon, item):
        threading.Thread(target=handle_action, args=(t, "enabled", {"on": not t.s["enabled"]}),
                         daemon=True).start()

    def quit_(icon, item):
        log("Svpn закрыт, всё идёт напрямую")
        t.interceptor(False)
        t.stop_xray()
        icon.stop()
        if ui["win"]:
            ui["win"].close()
        threading.Timer(4, lambda: os._exit(0)).start()   # страховка, если окно замешкается

    icon = pystray.Icon("Svpn", make_icon(cur()), TITLES[cur()], menu=pystray.Menu(
        pystray.MenuItem("Открыть Svpn", lambda i, it: show(), default=True),
        pystray.MenuItem("Svpn включён", toggle, checked=lambda it: t.s["enabled"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выйти", quit_)))

    def refresh_icon():
        h = cur()
        icon.icon = make_icon(h)
        icon.title = TITLES[h]
        try:
            icon.update_menu()
        except Exception:
            pass
    t.on_change = refresh_icon
    t.quit = lambda: quit_(icon, None)

    def notify(text):
        try:
            icon.notify(text, "Svpn")
        except Exception:
            pass
    t.updater.notify = notify

    # Окну нужен главный поток, поэтому трей живёт в своём (на Windows так можно).
    if ui["win"]:
        tray = threading.Thread(target=icon.run, daemon=True)
        tray.start()
        try:
            ui["win"].start()
        except Exception as e:
            log(f"окно Svpn не запустилось ({e}) — открою его в браузере")
        if not ui["win"].quitting:
            ui["win"] = None
            tray.join()
    else:
        if "--autostart" not in sys.argv:
            threading.Timer(1.5, show).start()
        icon.run()
    os._exit(0)


if __name__ == "__main__":
    if "--setup" in sys.argv:
        sys.exit(setup())
    if "--uninstall-admin" in sys.argv:
        sys.exit(uninstall_admin())
    if "--uninstall" in sys.argv:
        sys.exit(uninstall())
    main_app()
