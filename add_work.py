#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_work.py — добавляет работы и музыку на сайт-витрину «Карта навыков».

Файлы (картинки, видео, музыка) уходят в облако Cloudflare R2.
Запись о каждой работе попадает в works.json — этот файл ты выкладываешь на GitHub.
Сервер не нужен: скрипт работает только у тебя на компьютере.

Команды (подробности: python3 add_work.py КОМАНДА --help):
  init                    один раз: ввести ключи R2 и адрес публичной ссылки
  check                   проверить подключение к R2
  add [файлы] [--note]    добавить работу (картинка, видео, текст или просто запись)
  music файлы             добавить музыку в плейлист
  list                    показать всё, что уже добавлено
  remove ID               убрать работу (--delete-file удалит и файл из R2)
  remove-music НОМЕР      убрать трек
  sync                    обновить адрес хранилища в works.json по настройкам

Нужен Python 3.8+ и пакет boto3:  pip3 install boto3
"""
import argparse
import datetime
import getpass
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKS = os.path.join(HERE, "works.json")
CONFIG = os.path.join(HERE, "r2_config.json")

# ветка -> (название, [(id навыка, название)])
BRANCHES = {
    "blender": (
        "Blender",
        [
            ("lowpoly", "Low-poly"),
            ("midpoly", "Mid-poly"),
            ("highpoly", "High-poly"),
            ("anim", "Анимация"),
            ("sculpt", "Скульптинг"),
            ("anatomy", "Анатомия"),
        ],
    ),
    "writing": ("Писательство", []),
    "english": ("Английский", [(t, t) for t in ("A1", "A2", "B1", "B2", "C1", "C2")]),
}

# Браузеру нужен «понятный» тип: .mov из Blender отдаём как video/mp4
VIDEO_EXT = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/webm",
    ".ogv": "video/ogg",
    ".avi": "video/x-msvideo",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
IMAGE_TYPES = {".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".log"}
AUDIO_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}
BROWSER_VIDEO_CODECS = {"h264", "hevc", "vp8", "vp9", "av1"}
TEXT_INLINE_LIMIT = 200000  # текст до 200 КБ кладём прямо в works.json

TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        [
            "a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m",
            "n", "o", "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch",
            "", "y", "", "e", "yu", "ya",
        ],
    )
)


# ---------------------------------------------------------------- вспомогательное
def die(msg, code=1):
    print("\nОшибка: " + msg)
    raise SystemExit(code)


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%d %s" % (n, unit)) if unit == "Б" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def slugify(name):
    s = "".join(TRANSLIT.get(ch, ch) for ch in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40]
    return s or "file"


def clean_title(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    return re.sub(r"[_\s]+", " ", stem).strip() or stem


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    return "file"


def content_type(path, kind):
    ext = os.path.splitext(path)[1].lower()
    if kind == "video":
        return VIDEO_EXT.get(ext, "video/mp4")
    if kind == "audio":
        return AUDIO_EXT.get(ext, "audio/mpeg")
    if ext in IMAGE_TYPES:
        return IMAGE_TYPES[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def stamp():
    # время + короткий случайный хвост: ключи в R2 не совпадут, даже если загрузить два файла за секунду
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


# ---------------------------------------------------------------- works.json
def default_site():
    return {
        "title": "Карта навыков",
        "subtitle": "Портфолио",
        "name": "Твоё имя",
        "mediaBase": "",
        "music": [],
        "works": [],
    }


def load_works():
    if not os.path.exists(WORKS):
        return default_site()
    try:
        with open(WORKS, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        die("works.json повреждён (%s). Копия предыдущей версии: works.json.bak" % e)
    for k, v in default_site().items():
        data.setdefault(k, v)
    return data


def save_works(data):
    if os.path.exists(WORKS):
        shutil.copyfile(WORKS, WORKS + ".bak")
    fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, WORKS)


def next_id(data):
    return max([w.get("id", 0) for w in data["works"]] + [0]) + 1


# ---------------------------------------------------------------- R2
def load_cfg(required=True):
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError:
            die("r2_config.json повреждён. Запусти: python3 add_work.py init")
    env = {
        "account_id": "R2_ACCOUNT_ID",
        "access_key_id": "R2_ACCESS_KEY_ID",
        "secret_access_key": "R2_SECRET_ACCESS_KEY",
        "bucket": "R2_BUCKET",
        "public_base_url": "R2_PUBLIC_BASE_URL",
    }
    for key, var in env.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    missing = [k for k in env if not cfg.get(k)]
    if required and missing:
        die("не хватает настроек (%s). Сначала запусти: python3 add_work.py init" % ", ".join(missing))
    return cfg


def make_client(cfg):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        die("не установлен boto3. Выполни в терминале: pip3 install boto3")
    kw = dict(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"})
    try:
        # новые версии boto3 по умолчанию шлют контрольные суммы, которые R2 не принимает
        conf = Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            **kw
        )
    except TypeError:
        conf = Config(**kw)
    return boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % cfg["account_id"],
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
        config=conf,
    )


class Progress(object):
    def __init__(self, total):
        self.total = max(total, 1)
        self.done = 0

    def __call__(self, n):
        self.done += n
        sys.stdout.write(
            "\r  загрузка: %5.1f%%  (%s из %s)" % (100.0 * self.done / self.total, human(self.done), human(self.total))
        )
        sys.stdout.flush()


def public_url(cfg, key):
    return cfg["public_base_url"].rstrip("/") + "/" + key


def check_public(url):
    """Необязательная проверка: открывается ли файл по публичной ссылке."""
    try:
        import urllib.request

        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 add_work-check"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except Exception as e:  # noqa
        return str(e)


def upload(client, cfg, path, key, ctype):
    size = os.path.getsize(path)
    client.upload_file(
        path,
        cfg["bucket"],
        key,
        ExtraArgs={"ContentType": ctype, "CacheControl": "public, max-age=31536000, immutable"},
        Callback=Progress(size),
    )
    print()
    status = check_public(public_url(cfg, key))
    if status == 200:
        print("  открывается по публичной ссылке: %s" % public_url(cfg, key))
    else:
        print("  ! по публичной ссылке пока не открылось (%s)." % status)
        print("    Проверь, что в R2 включён публичный доступ к хранилищу (см. README, шаг 2).")


# ---------------------------------------------------------------- видео
def which(cmd):
    return shutil.which(cmd)


def video_codec(path):
    if not which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip().split(",")[0] or None
    except Exception:  # noqa
        return None


def convert_video(path):
    if not which("ffmpeg"):
        die("для --convert нужен ffmpeg (macOS: brew install ffmpeg)")
    out = os.path.join(tempfile.mkdtemp(), os.path.splitext(os.path.basename(path))[0] + ".mp4")
    print("  перекодирую в MP4 (H.264)… это может занять время")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-preset", "medium",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out,
    ]
    if subprocess.call(cmd) != 0:
        die("ffmpeg не смог перекодировать файл")
    return out


# ---------------------------------------------------------------- выбор ветки
def ask_menu(title, items):
    print("\n" + title)
    for i, (_, label) in enumerate(items, 1):
        print("  %d) %s" % (i, label))
    while True:
        raw = input("Номер: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1][0]
        print("  Введи число из списка.")


def choose_target(args):
    branch = (args.branch or "").lower() or None
    if branch is None:
        if not sys.stdin.isatty():
            die("укажи --branch (blender, writing или english)")
        branch = ask_menu("Куда добавить работу?", [(k, v[0]) for k, v in BRANCHES.items()])
    if branch not in BRANCHES:
        die("ветка должна быть одной из: %s" % ", ".join(BRANCHES))
    subs = BRANCHES[branch][1]
    sub = None
    if subs:
        ids = [s[0] for s in subs]
        sub = args.sub
        if sub:
            match = [i for i in ids if i.lower() == sub.lower()]
            if not match:
                die("навык для %s должен быть одним из: %s" % (branch, ", ".join(ids)))
            sub = match[0]
        else:
            if not sys.stdin.isatty():
                die("укажи --sub (%s)" % ", ".join(ids))
            sub = ask_menu("Какой навык?", subs)
    return branch, sub


# ---------------------------------------------------------------- команды
def sync_base(data, cfg):
    if cfg and cfg.get("public_base_url"):
        base = cfg["public_base_url"].rstrip("/") + "/"
        if data.get("mediaBase") != base:
            data["mediaBase"] = base
            return True
    return False


def push_if_needed(args, message):
    if getattr(args, "push", False):
        for cmd in (["git", "add", "works.json"], ["git", "commit", "-m", message], ["git", "push"]):
            if subprocess.call(cmd, cwd=HERE) != 0:
                die("команда не сработала: %s (это точно папка репозитория?)" % " ".join(cmd))
        print("Выложено на GitHub.")
    else:
        print('\nОсталось выложить на GitHub:\n  git add works.json && git commit -m "%s" && git push' % message)
        print("(или добавляй к команде флаг --push)")


def cmd_add(args):
    files = args.files or []
    if not files and not args.note:
        die("укажи файл или хотя бы --note «текст записи»")
    for f in files:
        if not os.path.isfile(f):
            die("файл не найден: %s" % f)
    branch, sub = choose_target(args)
    date = args.date or datetime.date.today().isoformat()
    cfg = load_cfg(required=not args.dry_run)
    data = load_works()
    sync_base(data, cfg)
    client = None
    target = branch + ("-" + sub if sub else "")
    added = []

    if not files:
        work = {"id": next_id(data), "branch": branch, "sub": sub, "note": args.note, "date": date}
        work = dict((k, v) for k, v in work.items() if v is not None)
        data["works"].append(work)
        added.append(work)

    for path in files:
        print("\n• %s" % os.path.basename(path))
        kind = kind_of(path)
        base = {"id": next_id(data), "branch": branch, "note": args.note or "", "date": date}
        if sub:
            base["sub"] = sub
        name = os.path.basename(path)
        size = os.path.getsize(path)

        if kind == "text" and size <= TEXT_INLINE_LIMIT:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            base.update({"kind": "text", "name": name, "text": text})
            print("  текст (%s) сохраняю прямо в works.json" % human(size))
            if not args.dry_run:
                data["works"].append(base)
            added.append(base)
            continue

        upload_path = path
        if kind == "video":
            codec = video_codec(path)
            if args.convert:
                upload_path = convert_video(path)
                name = os.path.splitext(name)[0] + ".mp4"
            elif codec and codec not in BROWSER_VIDEO_CODECS:
                print("  ! кодек «%s» браузеры обычно не проигрывают. Добавь --convert, чтобы перекодировать в H.264." % codec)
            elif codec is None and os.path.splitext(path)[1].lower() in (".avi", ".mkv"):
                print("  ! контейнер %s часто не играет в браузерах. Лучше MP4 или MOV с H.264 (или --convert)." % os.path.splitext(path)[1])
        if size > 200 * 1024 * 1024:
            print("  ! файл большой (%s). Для сайта лучше сжать видео." % human(size))

        ext = os.path.splitext(upload_path)[1].lower()
        key = "media/%s/%s-%s%s" % (target, stamp(), slugify(os.path.splitext(name)[0]), ext)
        ctype = content_type(upload_path, kind)
        print("  тип: %s · ключ в R2: %s" % (ctype, key))
        if args.dry_run:
            print("  (пробный запуск: ничего не загружаю)")
            continue
        if client is None:
            client = make_client(cfg)
        upload(client, cfg, upload_path, key, ctype)
        base.update({"kind": kind, "file": key, "name": name, "type": ctype})
        data["works"].append(base)
        added.append(base)

    if args.dry_run:
        print("\nПробный запуск завершён, works.json не изменён.")
        return
    save_works(data)
    print("\nГотово: добавлено работ — %d. Всего в works.json: %d." % (len(added), len(data["works"])))
    push_if_needed(args, "Добавлена работа: %s" % (args.note or ", ".join(os.path.basename(f) for f in files)))


def cmd_music(args):
    for f in args.files:
        if not os.path.isfile(f):
            die("файл не найден: %s" % f)
        if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
            die("%s: нужен аудиофайл (%s)" % (f, ", ".join(sorted(AUDIO_EXT))))
    if args.title and len(args.files) > 1:
        die("--title можно указать только для одного файла")
    cfg = load_cfg(required=not args.dry_run)
    data = load_works()
    sync_base(data, cfg)
    client = None
    for path in args.files:
        print("\n♪ %s" % os.path.basename(path))
        key = "music/%s-%s%s" % (stamp(), slugify(os.path.splitext(os.path.basename(path))[0]), os.path.splitext(path)[1].lower())
        ctype = content_type(path, "audio")
        print("  тип: %s · ключ в R2: %s" % (ctype, key))
        if args.dry_run:
            continue
        if client is None:
            client = make_client(cfg)
        upload(client, cfg, path, key, ctype)
        data["music"].append({"title": args.title or clean_title(path), "file": key})
    if args.dry_run:
        print("\nПробный запуск завершён, works.json не изменён.")
        return
    save_works(data)
    print("\nГотово. Треков в плейлисте: %d." % len(data["music"]))
    push_if_needed(args, "Обновлён плейлист")


def cmd_list(args):
    data = load_works()
    print("Хранилище: %s" % (data.get("mediaBase") or "(не задано)"))
    print("\nПлейлист (%d):" % len(data["music"]))
    for i, t in enumerate(data["music"], 1):
        print("  %d. %s  [%s]" % (i, t.get("title"), t.get("file") or t.get("url")))
    print("\nРаботы (%d):" % len(data["works"]))
    for w in data["works"]:
        where = w["branch"] + ("/" + w["sub"] if w.get("sub") else "")
        what = w.get("file") or ("(текст в works.json)" if w.get("text") is not None else "(только запись)")
        print("  #%-3s %-18s %s  %s — %s" % (w.get("id"), where, w.get("date", ""), w.get("note", ""), what))


def delete_object(cfg, key):
    client = make_client(cfg)
    client.delete_object(Bucket=cfg["bucket"], Key=key)
    print("  файл удалён из R2: %s" % key)


def cmd_remove(args):
    data = load_works()
    found = [w for w in data["works"] if str(w.get("id")) == str(args.id)]
    if not found:
        die("работы с номером %s нет. Посмотри номера: python3 add_work.py list" % args.id)
    w = found[0]
    print("Убираю: #%s %s/%s — %s" % (w["id"], w["branch"], w.get("sub", ""), w.get("note", "")))
    if not args.yes and input("Точно? (y/n): ").strip().lower() != "y":
        die("отменено", 0)
    if args.delete_file and w.get("file"):
        delete_object(load_cfg(), w["file"])
    data["works"] = [x for x in data["works"] if x is not w]
    save_works(data)
    print("Готово.")
    push_if_needed(args, "Убрана работа #%s" % w["id"])


def cmd_remove_music(args):
    data = load_works()
    if not 1 <= args.number <= len(data["music"]):
        die("в плейлисте нет трека №%d" % args.number)
    t = data["music"].pop(args.number - 1)
    print("Убираю трек: %s" % t.get("title"))
    if args.delete_file and t.get("file"):
        delete_object(load_cfg(), t["file"])
    save_works(data)
    print("Готово.")
    push_if_needed(args, "Убран трек")


def cmd_init(args):
    print("Настройка подключения к Cloudflare R2. Всё хранится только у тебя в r2_config.json.\n")
    old = load_cfg(required=False)

    def ask(label, key, secret=False):
        cur = old.get(key, "")
        shown = " [уже задано, Enter — оставить]" if (secret and cur) else (" [%s]" % cur if cur else "")
        raw = (getpass.getpass if secret else input)("%s%s: " % (label, shown)).strip()
        return raw or cur

    cfg = {
        "account_id": ask("Account ID (страница R2 в Cloudflare)", "account_id"),
        "access_key_id": ask("Access Key ID", "access_key_id"),
        "secret_access_key": ask("Secret Access Key", "secret_access_key", secret=True),
        "bucket": ask("Имя хранилища (bucket)", "bucket"),
        "public_base_url": ask("Публичный адрес хранилища (https://pub-….r2.dev)", "public_base_url"),
    }
    for k, v in cfg.items():
        if not v:
            die("поле «%s» не заполнено" % k)
    if not cfg["public_base_url"].startswith("http"):
        die("публичный адрес должен начинаться с https://")
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    data = load_works()
    data["mediaBase"] = cfg["public_base_url"].rstrip("/") + "/"
    for key, label in (("name", "Твоё имя на странице"), ("title", "Заголовок на экране входа"), ("subtitle", "Подзаголовок")):
        raw = input("%s [%s]: " % (label, data.get(key, ""))).strip()
        if raw:
            data[key] = raw
    save_works(data)
    print("\nГотово: r2_config.json и works.json обновлены. Проверь подключение: python3 add_work.py check")


def cmd_check(args):
    cfg = load_cfg()
    client = make_client(cfg)
    try:
        client.head_bucket(Bucket=cfg["bucket"])
        resp = client.list_objects_v2(Bucket=cfg["bucket"], MaxKeys=3)
    except Exception as e:  # noqa
        die("не удалось подключиться к R2: %s" % e)
    print("Подключение работает. Хранилище: %s" % cfg["bucket"])
    print("Объектов в первых строках списка: %d" % resp.get("KeyCount", 0))
    print("Публичный адрес: %s" % cfg["public_base_url"])


def cmd_sync(args):
    cfg = load_cfg()
    data = load_works()
    if sync_base(data, cfg):
        save_works(data)
        print("Адрес хранилища в works.json обновлён: %s" % data["mediaBase"])
        push_if_needed(args, "Новый адрес хранилища")
    else:
        print("Адрес уже актуален.")


def build_parser():
    p = argparse.ArgumentParser(description="Добавление работ и музыки на сайт-витрину (R2 + works.json)")
    sub = p.add_subparsers(dest="cmd")

    def push_flag(sp):
        sp.add_argument("--push", action="store_true", help="сразу сделать git add/commit/push для works.json")

    a = sub.add_parser("add", help="добавить работу")
    a.add_argument("files", nargs="*", help="картинка, видео или текстовый файл (можно несколько)")
    a.add_argument("--branch", help="blender | writing | english")
    a.add_argument("--sub", help="навык: lowpoly, midpoly, highpoly, anim, sculpt, anatomy или уровень A1…C2")
    a.add_argument("--note", help="подпись к работе")
    a.add_argument("--date", help="дата ГГГГ-ММ-ДД (по умолчанию сегодня)")
    a.add_argument("--convert", action="store_true", help="перекодировать видео в MP4/H.264 через ffmpeg")
    a.add_argument("--dry-run", action="store_true", help="показать, что будет сделано, ничего не загружая")
    push_flag(a)
    a.set_defaults(fn=cmd_add)

    m = sub.add_parser("music", help="добавить музыку")
    m.add_argument("files", nargs="+")
    m.add_argument("--title", help="название трека (для одного файла)")
    m.add_argument("--dry-run", action="store_true")
    push_flag(m)
    m.set_defaults(fn=cmd_music)

    sub.add_parser("list", help="показать работы и плейлист").set_defaults(fn=cmd_list)

    r = sub.add_parser("remove", help="убрать работу")
    r.add_argument("id")
    r.add_argument("--delete-file", action="store_true", help="удалить и файл из R2")
    r.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    push_flag(r)
    r.set_defaults(fn=cmd_remove)

    rm = sub.add_parser("remove-music", help="убрать трек из плейлиста")
    rm.add_argument("number", type=int)
    rm.add_argument("--delete-file", action="store_true")
    push_flag(rm)
    rm.set_defaults(fn=cmd_remove_music)

    sub.add_parser("init", help="ввести ключи R2").set_defaults(fn=cmd_init)
    sub.add_parser("check", help="проверить подключение").set_defaults(fn=cmd_check)
    s = sub.add_parser("sync", help="обновить адрес хранилища в works.json")
    push_flag(s)
    s.set_defaults(fn=cmd_sync)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    args.fn(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
