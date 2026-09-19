#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch-app-columns.py — добавляет ли ПРИЛОЖЕНИЕ свои колонки к проекту, который
завёл скилл через REST.

Зачем. Критерий карточки «init на свежем проекте не раздваивает доску» состоит из
двух половин, и вторая — «…ПОСЛЕ открытия проекта в приложении» — по API не
проверяется в принципе: API показывает, что записал сам скилл. Свидетель по
вопросу «что сделало приложение» один — его собственная локальная база и его
собственный лог синхронизации.

Источники (все ТОЛЬКО ЧТЕНИЕ, файлы открываются 'rb'):

  1. `nedb-backup/AppDatabase_*.json` — суточный снапшот, коллекция
     `kanbanStatuses`. Полное состояние, но на полночь: сегодняшний проект в нём
     не появится. Годится как КОНТРОЛЬ метода («вижу ли я колонки вообще»),
     не как наблюдение.
  2. `IndexedDB/sg_renderer_0.indexeddb.leveldb/*.log` — журнал упреждающей
     записи живой базы. Свежие записи лежат в нём НЕсжатыми, и запись колонки
     разбирается целиком: id, projectId, name, kanbanOrder. Это и есть живое
     состояние: что приложение записало у себя за последние минуты.
     Сами `.ldb` сжаты snappy и здесь не читаются — после компакции запись из
     окна наблюдения пропадает, поэтому смотреть надо в том же заходе.
  3. `logs/<дата>-workers_cloud_js.log` — лог синхронизации. `sync <-` это то,
     что приложение ПОЛУЧИЛО, `sync -> len: N` — то, что оно само записало и
     отдаёт в облако. Именно вторая строка отвечает на вопрос карточки:
     колонка, заведённая приложением, обязана уехать в облако и попасть в `->`.

Почему нужны и 2, и 3. Пустой WAL сам по себе не значит «приложение ничего не
делало»: он мог быть скомпактован. Пустой `->` сам по себе не значит того же:
приложение могло до проекта не дойти. Вместе они дают и факт («что записано»),
и то, дошла ли вообще синхронизация до проекта («что получено»).

Приватность: наружу выходят только идентификаторы, счётчики и названия колонок
ЗАПРОШЕННОГО проекта. Названия и содержимое чужих сущностей не печатаются.

В базу не пишется ничего и никогда — см. references/api.md, «Процедура», п. 7.

Команды
    columns <P-id>              состав колонок проекта по суточному снапшоту
    wal <P-id>                  что о колонках проекта лежит в живом WAL прямо сейчас
    sync <P-id>                 события синхронизации по проекту за сегодня
    watch <P-id> [--minutes N] [--every S]
                                окно наблюдения: редкие проверки 2 и 3, сводка в конце

Пример (закрытие второй половины критерия карточки):
    tools/watch-app-columns.py columns P-<известный>     # контроль метода
    tools/watch-app-columns.py watch P-<свежий> --minutes 20 --every 150
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys
import time

APP_DIR = os.path.expanduser(
    "~/Library/Containers/ru.sibirix.singularitydesktop/Data/Library/"
    "Application Support/SingularityApp"
)
BACKUP_DIR = os.path.join(APP_DIR, "nedb-backup")
LEVELDB_DIR = os.path.join(APP_DIR, "IndexedDB", "sg_renderer_0.indexeddb.leveldb")
LOGS_DIR = os.path.join(APP_DIR, "logs")

PID_RE = re.compile(r"^P-[0-9a-fA-F-]{36}$")

# Запись kanbanStatus в WAL: V8-сериализация, ключи — однобайтовые строки,
# значения-идентификаторы тоже, а название колонки двухбайтовое (кириллица).
#
# ⚠ Перед двухбайтовой строкой V8 выравнивает смещение и вставляет `\x00`, если
# оно нечётное. Регулярка без `\x00?` молча теряет ровно те записи, где падинг
# случился: на живом замере это было 2 колонки из 5 — выборка неполная, а
# картина правдоподобная. Отсюда же гейт полноты: `total` против ожидаемого.
REC_RE = re.compile(rb'"\x02id"(?P<idlen>[\x01-\x7f])(?P<id>[A-Za-z0-9\-]{1,127})'
                    rb'"\tprojectId"&(?P<pid>P-[0-9a-f\-]{36})'
                    rb'"\x04name\x00?(?P<tag>[c"])')

SYNC_RE = re.compile(
    r'^\[(?P<ts>[^\]]+)\]\s+\w+ - "sync (?P<dir>->|<-) (?P<tag>[0-9a-f]+)'
    r'(?: promise)? len: (?P<len>\d+); ids: \[(?P<ids>[^\]]*)\]')


def die(msg):
    sys.exit("watch-app-columns: " + msg)


def check_pid(pid):
    if not PID_RE.match(pid or ""):
        die("нужен идентификатор проекта вида P-<uuid>, а не %r" % pid)
    return pid


# ── источник 1: суточный снапшот ────────────────────────────────────────────

def newest_snapshot():
    files = [f for f in glob.glob(os.path.join(BACKUP_DIR, "AppDatabase_*.json"))
             if not f.endswith(".cens.json")]
    if not files:
        die("снапшотов не найдено в %s" % BACKUP_DIR)
    return max(files, key=os.path.getmtime)


def snapshot_columns(pid):
    path = newest_snapshot()
    with open(path, "rb") as f:                      # 'rb' — файл боевой
        doc = json.loads(f.read().decode("utf-8"))
    rows = [s for s in doc["data"].get("kanbanStatuses", [])
            if s.get("projectId") == pid and not s.get("_removed")]
    rows.sort(key=lambda s: s.get("kanbanOrder") or 0)
    return path, doc.get("created"), rows


# ── источник 2: живой WAL ───────────────────────────────────────────────────

def _varint(buf, i):
    val = shift = 0
    while i < len(buf):
        b = buf[i]
        val |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return val, i
        shift += 7
    return val, i


def _read_name(buf, i, tag):
    """После ключа name: тег строки уже прочитан, дальше длина в БАЙТАХ."""
    n, i = _varint(buf, i)
    raw = buf[i:i + n]
    if tag == b"c":
        return raw.decode("utf-16-le", "replace"), i + n
    return raw.decode("latin-1", "replace"), i + n


def wal_columns(pid):
    """Колонки проекта, записанные приложением у себя, по журналу живой базы.

    Одна и та же колонка попадает в WAL столько раз, сколько раз её переписали;
    поэтому по id склеиваем, а считаем различные id.

    Возвращает ещё и ВСЕГО разобранных записей о колонках — по всем проектам.
    Без этого числа ноль у своего проекта неотличим от слепого разбора: ровно так
    и вышло на первом прогоне (группа регулярки — байты, идентификатор — строка,
    сравнение всегда ложно, «тёзок нет» на пустом месте).
    """
    out = {}
    total = 0
    want = pid.encode()          # сравнение с группой регулярки — только в байтах:
    files = sorted(glob.glob(os.path.join(LEVELDB_DIR, "*.log")))
    for path in files:
        with open(path, "rb") as f:                  # 'rb' — живая база, чтение
            data = f.read()
        for m in REC_RE.finditer(data):
            if len(m.group("id")) != m.group("idlen")[0]:
                continue                             # длина не сошлась — не запись
            total += 1
            if m.group("pid") != want:
                continue
            name, j = _read_name(data, m.end(), m.group("tag"))
            # kanbanOrder идёт сразу за name; искать его «где-то рядом» нельзя —
            # поиск по окну цепляет варинт соседней записи и печатает чужой порядок.
            order = None
            if data[j:j + 14] == b'"\x0bkanbanOrderI':
                zz, _ = _varint(data, j + 14)
                order = (zz >> 1) ^ -(zz & 1)
            out[m.group("id").decode()] = (name, order)
    return files, out, total


# ── источник 3: лог синхронизации ───────────────────────────────────────────

def sync_log_path():
    files = sorted(glob.glob(os.path.join(LOGS_DIR, "*-workers_cloud_js.log")))
    if not files:
        die("лога синхронизации не найдено в %s" % LOGS_DIR)
    return files[-1]


def sync_events(pid, known_ids=()):
    """События синхронизации, относящиеся к проекту.

    «Относится» — это id самого проекта, системные колонки `KS-<pid>-*` (в их id
    зашит проект) и любые id, которые мы знаем о нём извне (--ids). Колонку со
    случайным id приложение может завести и не назвать проект в id — поэтому
    отдельно считаются ВСЕ `KS-`, ушедшие в облако за окно: такой id и есть
    кандидат в двойники, его проверяют по составу доски.
    """
    path = sync_log_path()
    known = set(known_ids)
    runs = 0
    ours = []
    pushed_ks = set()
    with open(path, "rb") as f:                      # 'rb' — лог приложения
        for line in f.read().decode("utf-8", "replace").splitlines():
            if "SyncExecutor::execute" in line:
                runs += 1
                continue
            m = SYNC_RE.match(line)
            if not m:
                continue
            ids = [x for x in m.group("ids").split(",") if x]
            mine = [x for x in ids if pid in x or x in known]
            if mine:
                ours.append((m.group("ts"), m.group("dir"), mine))
            if m.group("dir") == "->":
                pushed_ks.update(x for x in ids if x.startswith("KS-"))
    return path, runs, ours, pushed_ks


# ── команды ─────────────────────────────────────────────────────────────────

def cmd_columns(args):
    pid = check_pid(args.project)
    path, created, rows = snapshot_columns(pid)
    print("снапшот: %s (снят %s)" % (os.path.basename(path), created))
    names = [r.get("name") for r in rows]
    twins = sorted({n for n in names if names.count(n) > 1})
    print("колонок у проекта: %d, тёзок: %d%s"
          % (len(rows), len(twins), (" — %s" % twins) if twins else ""))
    for r in rows:
        print("  %-52s order %-7s %s" % (r["id"], r.get("kanbanOrder"), r.get("name")))
    return 0


def cmd_wal(args):
    pid = check_pid(args.project)
    files, cols, total = wal_columns(pid)
    print("WAL: %s" % ", ".join(os.path.basename(p) for p in files))
    print("разобрано записей о колонках всего (по всем проектам): %d" % total)
    names = [v[0] for v in cols.values()]
    twins = sorted({n for n in names if names.count(n) > 1})
    print("записей о колонках проекта: %d, тёзок: %d%s"
          % (len(cols), len(twins), (" — %s" % twins) if twins else ""))
    for cid, (name, order) in sorted(cols.items(), key=lambda kv: kv[1][1] or 0):
        print("  %-52s order %-7s %s" % (cid, order, name))
    return 0


def cmd_sync(args):
    pid = check_pid(args.project)
    path, runs, ours, pushed = sync_events(pid, args.ids or ())
    print("лог: %s" % os.path.basename(path))
    print("прогонов синхронизации за файл: %d" % runs)
    print("событий по проекту: %d" % len(ours))
    for ts, direction, ids in ours[-args.tail:]:
        print("  %s  %s  %s" % (ts, direction, ",".join(ids)))
    print("всего KS-id, УШЕДШИХ в облако за файл: %d%s"
          % (len(pushed), (" — %s" % sorted(pushed)) if pushed else ""))
    return 0


def cmd_watch(args):
    # Построчный сброс: окно наблюдения смотрят ПО ХОДУ, а буферизованный вывод
    # в файл отдаёт всё только в конце — и наблюдение превращается в ожидание.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    pid = check_pid(args.project)
    known = set(args.ids or ())
    deadline = time.time() + args.minutes * 60
    checks = 0
    base_runs = None
    seen_wal = {}
    seen_evt = 0
    pushed0 = None
    wal_total = 0
    while True:
        checks += 1
        _, cols, wal_total = wal_columns(pid)
        _, runs, ours, pushed = sync_events(pid, known)
        if base_runs is None:
            base_runs, pushed0 = runs, set(pushed)
        new_cols = {k: v for k, v in cols.items() if k not in seen_wal}
        seen_wal.update(cols)
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        print("[%s] проверка %d: прогонов синка с начала окна %d; "
              "колонок проекта в WAL %d (новых за проверку %d); "
              "событий по проекту %d (новых %d); новых KS в облако %d"
              % (stamp, checks, runs - base_runs, len(cols), len(new_cols),
                 len(ours), len(ours) - seen_evt, len(pushed - pushed0)))
        for cid, (name, order) in new_cols.items():
            print("      + %s  order %s  %s" % (cid, order, name))
        for ts, direction, ids in ours[seen_evt:]:
            print("      %s %s %s" % (ts, direction, ",".join(ids)))
        seen_evt = len(ours)
        if time.time() >= deadline:
            break
        time.sleep(min(args.every, max(1, deadline - time.time())))
    names = [v[0] for v in seen_wal.values()]
    twins = sorted({n for n in names if names.count(n) > 1})
    print("\nИТОГ за %d мин и %d проверок:" % (args.minutes, checks))
    print("  прогонов синхронизации приложения: %d" % (runs - base_runs))
    print("  записей о колонках, разобранных в WAL всего: %d "
          "(ноль здесь значит, что разбор слеп, а не что колонок нет)" % wal_total)
    print("  различных колонок проекта в WAL: %d, тёзок: %d%s"
          % (len(seen_wal), len(twins), (" — %s" % twins) if twins else ""))
    print("  событий синхронизации по проекту: %d" % seen_evt)
    print("  KS-id, ушедших в облако за окно: %d%s"
          % (len(pushed - pushed0),
             (" — %s" % sorted(pushed - pushed0)) if pushed - pushed0 else ""))
    if not seen_wal and not seen_evt:
        print("  приложение до проекта за окно не дошло — это результат, а не сбой;"
              " вывод о тёзках по нему делать нельзя")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("columns", help="колонки проекта по суточному снапшоту")
    p.add_argument("project")
    p.set_defaults(fn=cmd_columns)

    p = sub.add_parser("wal", help="колонки проекта в живом WAL")
    p.add_argument("project")
    p.set_defaults(fn=cmd_wal)

    p = sub.add_parser("sync", help="события синхронизации по проекту")
    p.add_argument("project")
    p.add_argument("--ids", nargs="*", help="известные id колонок проекта")
    p.add_argument("--tail", type=int, default=20)
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("watch", help="окно наблюдения")
    p.add_argument("project")
    p.add_argument("--ids", nargs="*", help="известные id колонок проекта")
    p.add_argument("--minutes", type=int, default=20)
    p.add_argument("--every", type=int, default=150)
    p.set_defaults(fn=cmd_watch)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
