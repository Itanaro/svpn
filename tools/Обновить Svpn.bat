@"%LOCALAPPDATA%\Programs\Svpn\runtime\Svpn.exe" -x "%~f0" || pause & goto :eof
# Svpn: ручная установка обновления. Нужна один раз тем, у кого стоит версия
# старше 1.1.5: там кнопка «Обновить» закрывает Svpn, но файлы не подменяет.
# Запускается встроенным в Svpn Python (первая строка — для Windows).
import ctypes, hashlib, json, os, shutil, subprocess, sys, time, urllib.request

BASE = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Svpn")
DATA = os.path.join(os.environ["LOCALAPPDATA"], "Svpn")
RUN = os.path.join(BASE, "runtime", "Svpn.exe")
URL = "https://raw.githubusercontent.com/Itanaro/svpn/main/manifest.json"
NOWIN = 0x08000000


def msg(text, err=False):
    ctypes.windll.user32.MessageBoxW(None, text, "Svpn — обновление",
                                     (0x10 if err else 0x40) | 0x10000)


def get(url):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                               "User-Agent": "Svpn-fix"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def log(m):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "svpn.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + m + "\n")
    except OSError:
        pass


if not os.path.exists(os.path.join(BASE, "app.py")):
    msg("Не нашла Svpn в папке\n" + BASE, True)
    sys.exit(1)

# 1. Скачиваем всё заранее, пока Svpn ещё работает
try:
    m = json.loads(get(URL))
    todo = {}
    for f in m["files"]:
        rel = f["path"].replace("/", os.sep)
        if ".." in rel.split(os.sep) or os.path.isabs(rel):
            continue
        dst = os.path.join(BASE, rel)
        try:
            with open(dst, "rb") as fh:
                if sha(fh.read()) == f["sha256"]:
                    continue
        except OSError:
            pass
        data = get(f["url"])
        if sha(data) != f["sha256"]:
            raise ValueError("файл %s пришёл повреждённым" % f["path"])
        todo[dst] = data
except Exception as e:
    msg("Не получилось скачать обновление:\n%s\n\nПроверь интернет и запусти файл ещё раз." % e, True)
    sys.exit(1)

# 2. Закрываем Svpn (кроме себя самого — мы тоже Svpn.exe) и VPN-движок
me = os.getpid()
subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq Svpn.exe", "/FI", "PID ne %d" % me],
               capture_output=True, creationflags=NOWIN)
time.sleep(1)
subprocess.run(["taskkill", "/F", "/IM", "xray.exe"], capture_output=True, creationflags=NOWIN)
time.sleep(1)

# 3. Подменяем файлы
bad = []
for dst, data in todo.items():
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for i in range(10):
        try:
            with open(dst + ".new", "wb") as fh:
                fh.write(data)
            os.replace(dst + ".new", dst)
            break
        except OSError:
            time.sleep(1)
    else:
        bad.append(os.path.relpath(dst, BASE))

# хвосты от старого способа обновления
shutil.rmtree(os.path.join(DATA, "update"), ignore_errors=True)
try:
    os.remove(os.path.join(DATA, "apply-update.ps1"))
except OSError:
    pass

# 4. Запускаем Svpn обратно
subprocess.Popen([RUN, os.path.join(BASE, "app.py")], cwd=BASE, close_fds=True,
                 creationflags=0x00000008 | 0x00000200)

if bad:
    log("ручное обновление: не получилось заменить " + ", ".join(bad))
    msg("Не получилось заменить файлы:\n" + "\n".join(bad) +
        "\n\nПерезагрузи компьютер и запусти этот файл ещё раз.", True)
    sys.exit(1)
log("ручное обновление: установлена версия %s (файлов: %d)" % (m["version"], len(todo)))
msg("Готово! Svpn обновлён до версии %s и запускается.\n\n"
    "Дальше обновления будут ставиться сами, по кнопке." % m["version"])
