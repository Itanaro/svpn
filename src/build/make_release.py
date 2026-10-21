"""Выпуск новой версии Svpn в канал обновлений (репозиторий Itanaro/svpn).

    python3 make_release.py <папка сборки> <клон репозитория> <версия> "<что нового>" [--mandatory] [--with-runtime]

<папка сборки> — то же содержимое, что ставит установщик (app.py, engine.py, ui/, bin/, runtime/).
Файлы кладутся в <клон>/files/<путь>, рядом пишется manifest.json со списком
{path, sha256, url}. Svpn у каждого сверяет суммы и качает только изменившиеся файлы.
runtime/ (Python и библиотеки) по умолчанию не выкладывается — только если поменялся
(--with-runtime).
"""
import hashlib
import json
import os
import shutil
import sys

RAW = "https://raw.githubusercontent.com/Itanaro/svpn/main/"


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 4:
        raise SystemExit(__doc__)
    build, repo, version, notes = args[:4]
    runtime = "--with-runtime" in sys.argv
    files = []
    for root, dirs, names in os.walk(build):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        rel_root = os.path.relpath(root, build).replace(os.sep, "/")
        if rel_root.split("/")[0] == "runtime" and not runtime:
            continue
        for n in sorted(names):
            if n.endswith((".bak", ".pyc")) or ".bak-" in n or n == "uninstall.exe":
                continue
            rel = n if rel_root == "." else f"{rel_root}/{n}"
            src = os.path.join(root, n)
            dst = os.path.join(repo, "files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            files.append({"path": rel, "sha256": sha(src), "url": RAW + "files/" + rel})
    manifest = {"version": version, "notes": notes,
                "mandatory": "--mandatory" in sys.argv, "files": files}
    with open(os.path.join(repo, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"версия {version}: файлов {len(files)}")


if __name__ == "__main__":
    main()
