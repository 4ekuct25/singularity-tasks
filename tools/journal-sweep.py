#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""journal-sweep.py — кто и когда уносит задачи в дневник (он же «архив»).

Зачем. `journalDate` у задачи ставится НЕ в момент закрытия, а позже и пачкой.
Понять это по одной задаче нельзя: нужна картина «значение journalDate → сколько
задач и сколько проектов получили ровно его». Совпадение секунда-в-секунду у
десятков задач из разных проектов — подпись пакетного процесса, а не отдельных
операций скилла или человека.

Источник — ночные снапшоты локальной базы приложения (см. tools/db-shapes.py).
ТОЛЬКО ЧТЕНИЕ: файлы открываются 'rb', живая база не трогается.

Приватность: печатаются только идентификаторы, времена и счётчики — ни одного
заголовка задачи.

Команды
    clusters              пачки: значение journalDate → задач / проектов
    lag                   задержка «закрыл → унесло в дневник» (медиана, квантили)
    project <P-…>         построчно по одному проекту: checked, journalDate, лаг
    live <P-…>            ОДИН GET к API: что с journalDate у задач прямо сейчас
                          (снапшот ночной, сегодняшних закрытий в нём ещё нет)
"""

import argparse
import datetime
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

BACKUP_DIR = os.path.expanduser(
    "~/Library/Containers/ru.sibirix.singularitydesktop/Data/Library/"
    "Application Support/SingularityApp/nedb-backup"
)


def newest_backup():
    files = [f for f in glob.glob(os.path.join(BACKUP_DIR, "AppDatabase_*.json"))
             if not f.endswith(".cens.json")]
    if not files:
        sys.exit("Снапшотов не найдено в %s" % BACKUP_DIR)
    return max(files, key=os.path.getmtime)


def load(path):
    with open(path, "rb") as f:          # 'rb' — намеренно, файл боевой
        return json.loads(f.read().decode("utf-8"))


def parse_iso(s):
    """Полный ISO с Z; дробная часть бывает 0/3/6 знаков — см. references/api.md."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def ms(v):
    if v is None:
        return None
    return datetime.datetime.fromtimestamp(v / 1000.0, datetime.timezone.utc)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def tasks_of(doc):
    return doc["data"]["tasks"]


def head(path, doc):
    print("база:   %s" % path)
    print("снят:   %s  (version=%s)\n" % (doc.get("created"), doc.get("version")))


def cmd_clusters(doc, args):
    ts = [t for t in tasks_of(doc) if t.get("journalDate")]
    by_val = defaultdict(list)
    for t in ts:
        by_val[t["journalDate"][:19]].append(t)
    print("задач с journalDate: %d, различных значений: %d"
          % (len(ts), len(by_val)))
    print("\n%-21s %8s %9s   %s" % ("journalDate", "задач", "проектов", "доля"))
    for val, group in sorted(by_val.items(), key=lambda kv: -len(kv[1]))[:args.top]:
        projects = {t.get("projectId") for t in group}
        print("%-21s %8d %9d   %5.1f%%"
              % (val, len(group), len(projects), 100.0 * len(group) / len(ts)))
    singles = sum(1 for g in by_val.values() if len(g) == 1)
    print("\nзначений, доставшихся ровно одной задаче: %d из %d"
          % (singles, len(by_val)))
    hours = Counter(t["journalDate"][11:13] for t in ts)
    print("по часам UTC: %s" % ", ".join("%s:%d" % kv for kv in sorted(hours.items())))


def cmd_lag(doc, args):
    lags, exact = [], 0
    for t in tasks_of(doc):
        jd = parse_iso(t.get("journalDate"))
        if not jd:
            continue
        m = t.get("modificated") or {}
        wrote = ms(m.get("journalDate"))
        closed = ms(m.get("checked"))
        if wrote and abs((wrote - jd).total_seconds()) < 2:
            exact += 1
        if closed and jd and jd > closed:
            lags.append((jd - closed).total_seconds())
    print("значение journalDate совпадает с моментом записи поля: %d" % exact)
    if not lags:
        print("нет пар «закрыто → унесено»")
        return
    lags.sort()
    q = lambda p: lags[min(len(lags) - 1, int(p * len(lags)))]
    print("лаг «закрыл → унесло», задач %d:" % len(lags))
    print("  мин %s, медиана %s, p90 %s, макс %s"
          % (dur(lags[0]), dur(statistics.median(lags)), dur(q(0.9)), dur(lags[-1])))
    print("  унесено в первую минуту: %d (%.1f%%)"
          % (sum(1 for x in lags if x <= 60), 100.0 * sum(1 for x in lags if x <= 60) / len(lags)))


def dur(sec):
    sec = int(sec)
    if sec < 90:
        return "%d с" % sec
    if sec < 5400:
        return "%d мин" % (sec // 60)
    if sec < 86400 * 2:
        return "%.1f ч" % (sec / 3600.0)
    return "%.1f сут" % (sec / 86400.0)


def cmd_project(doc, args):
    rows = []
    for t in tasks_of(doc):
        if t.get("projectId") != args.project:
            continue
        m = t.get("modificated") or {}
        rows.append((t["id"], t.get("checked"), ms(m.get("checked")),
                     ms(m.get("journalDate")), t.get("journalDate")))
    rows.sort(key=lambda r: r[2] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    print("задач в проекте: %d\n" % len(rows))
    print("%-40s %4s %-20s %-20s %s"
          % ("id", "chk", "checked записан", "journalDate записан", "journalDate"))
    for tid, chk, closed, wrote, jd in rows:
        print("%-40s %4s %-20s %-20s %s"
              % (tid, chk, fmt(closed), fmt(wrote), (jd or "—")[:19]))


def cmd_live(doc, args):
    """Одна выборка через API: снапшот ночной, сегодняшних закрытий в нём нет."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "scripts"))
    import sing  # noqa: E402
    tasks = sing.fetch_tasks(args.project, include_archived=True)
    tasks = [t for t in tasks if not t.get("removed") and not t.get("isNote")]
    closed = [t for t in tasks if int(t.get("checked") or 0) == 1 or t.get("journalDate")]
    closed.sort(key=lambda t: t.get("modificatedDate") or "")
    print("задач в проекте (без удалённых и заметок): %d, закрытых/в дневнике: %d\n"
          % (len(tasks), len(closed)))
    print("%-40s %4s %-24s %s" % ("id", "chk", "journalDate", "modificatedDate"))
    for t in closed:
        print("%-40s %4s %-24s %s"
              % (t["id"], t.get("checked"), (t.get("journalDate") or "—")[:23],
                 (t.get("modificatedDate") or "")[:23]))
    no_jd = [t for t in closed if not t.get("journalDate")]
    print("\nзакрыто, но ещё НЕ в дневнике: %d" % len(no_jd))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="путь к конкретному AppDatabase_*.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("clusters"); p.add_argument("--top", type=int, default=15)
    p.set_defaults(fn=cmd_clusters)
    p = sub.add_parser("lag"); p.set_defaults(fn=cmd_lag)
    p = sub.add_parser("project"); p.add_argument("project")
    p.set_defaults(fn=cmd_project)
    p = sub.add_parser("live"); p.add_argument("project")
    p.set_defaults(fn=cmd_live)
    args = ap.parse_args()
    path = args.db or newest_backup()
    doc = load(path)
    head(path, doc)
    args.fn(doc, args)


if __name__ == "__main__":
    main()
