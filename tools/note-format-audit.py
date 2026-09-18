#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note-format-audit.py — как агенты РЕАЛЬНО форматируют свои записи в карточках.

Зачем. Требование «пиши списком, а не стеной» проверяется не мнением, а долей:
сколько записей `ПЛАН`/`РЕЗУЛЬТАТ`/`БЛОКЕР` ушло на доску одним абзацем. Пока
числа нет, спор о формулировке в SKILL.md беспредметен, а после правки нечем
показать, что стало лучше.

Почему по локальной базе, а не по API. Нужен АТРИБУТ операции (`list=bullet`),
а не текст: `sing.py show` склеивает дельту в плоский текст, и маркированный
список там неотличим от строк через перевод строки. Плюс замер по снапшоту
стоит ноль запросов — лимит трекера общий на аккаунт (см. references/api.md).

Только чтение. Снапшот открывается 'rb', живая база не трогается вообще.

Приватность. Инструмент не печатает ни одной строки текста карточек: только
идентификаторы, ярлыки, длины и доли — как `db-shapes.py`. Флага «показать
как есть» нет намеренно.

Команды
    audit [--project P-...] [--limit N]   доля «стен» по записям агентов
    by-agent [--project P-...]            то же в разрезе agent:<имя> из ярлыка

Примеры
    tools/note-format-audit.py audit
    tools/note-format-audit.py audit --wall-chars 300
    tools/note-format-audit.py by-agent
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import time

BACKUP_DIR = os.path.expanduser(
    "~/Library/Containers/ru.sibirix.singularitydesktop/Data/Library/"
    "Application Support/SingularityApp/nedb-backup"
)

# Ярлык записи агента: его пишет note_append() жирной операцией, вида
# «РЕЗУЛЬТАТ (agent:claude-json)» или просто «ПЛАН».
LABEL_RE = re.compile(r"^(ПЛАН|РЕЗУЛЬТАТ|БЛОКЕР|ФАКТ|ВОЗВРАТ|ПРОГРЕСС)"
                      r"(?:\s*\(agent:([^)]+)\))?")

# Порог «стены» по умолчанию — тот же, что зашит в sing.py (WALL_CHARS).
DEFAULT_WALL = 400


def newest_backup():
    """Самый свежий НЕцензурированный снапшот (в `*.cens.json` испорчены имена ключей)."""
    files = [f for f in glob.glob(os.path.join(BACKUP_DIR, "AppDatabase_*.json"))
             if not f.endswith(".cens.json")]
    if not files:
        sys.exit("Снапшотов не найдено в %s\n"
                 "Приложение делает их само раз в сутки." % BACKUP_DIR)
    return max(files, key=os.path.getmtime)


def load_notes(args):
    """Заметки задач выборки: список дельт.

    ⚠ `tasks.note` в базе — НЕ дельта, а ссылка `N-<taskId>` на запись коллекции
    `notes`, где текст лежит в поле `content`. По API то же поле приходит дельтой
    сразу; перепутать легко, и замер тогда молча даёт «записей нет».
    """
    path = args.db or newest_backup()
    with open(path, "rb") as f:
        doc = json.loads(f.read().decode("utf-8"))
    age = (time.time() - os.path.getmtime(path)) / 3600.0
    print("база:  %s" % os.path.basename(path))
    print("снят:  %s (%.1f ч назад)" % (doc.get("created"), age))
    if age > 36:
        print("⚠ снапшот старше полутора суток — свежие записи в него не попали")
    data = doc["data"]
    tasks = data["tasks"]
    if args.project:
        tasks = [t for t in tasks if t.get("projectId") == args.project]
    notes = {n["id"]: n for n in data.get("notes", []) if n.get("id")}
    out = []
    for t in tasks:
        ref = t.get("note")
        ops = None
        if isinstance(ref, str) and ref.startswith("N-"):
            rec = notes.get(ref)
            ops = note_ops(rec.get("content")) if rec else None
        elif ref:
            ops = note_ops(ref)
        if ops:
            out.append((t.get("id"), ops))
    print("задач в выборке: %d, из них с заметкой: %d\n" % (len(tasks), len(out)))
    return out


def note_ops(note):
    """Та же разборка трёх форм значения заметки, что и в scripts/sing.py."""
    if not note:
        return []
    if isinstance(note, list):
        return note
    try:
        d = json.loads(note)
    except (json.JSONDecodeError, TypeError):
        return [{"insert": str(note)}]
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and isinstance(d.get("ops"), list):
        return d["ops"]
    return [{"insert": str(note)}]


def delta_lines(ops):
    """Дельта → строки [(текст, это_маркер_списка)].

    Блочный атрибут в Quill висит на "\\n", а не на тексте строки, поэтому
    маркер списка виден только по операции перевода строки.
    """
    out, buf = [], ""
    for op in ops:
        if not isinstance(op, dict):
            continue
        ins = op.get("insert")
        if not isinstance(ins, str):
            continue
        attrs = op.get("attributes") or {}
        if attrs.get("list") and ins.strip() == "":
            out.append((buf, True))
            buf = ""
            continue
        parts = ins.split("\n")
        buf += parts[0]
        for part in parts[1:]:
            out.append((buf, False))
            buf = part
    if buf:
        out.append((buf, False))
    return out


def agent_records(notes):
    """Записи агентов: ярлык, автор, число маркеров, длина и число строк.

    Текст наружу не отдаётся — только длины и счётчики.
    """
    recs = []
    for task_id, ops in notes:
        cur = None
        for text, bullet in delta_lines(ops):
            m = LABEL_RE.match(text.strip()) if not bullet else None
            if m:
                cur = {"task": task_id, "label": m.group(1),
                       "agent": m.group(2) or "—", "bullets": 0,
                       "lines": 0, "chars": 0}
                recs.append(cur)
                text = text.strip()[m.end():].lstrip(": ").strip()
            if cur is None:
                continue
            if not text.strip() and not bullet:
                continue
            cur["bullets"] += 1 if bullet else 0
            cur["lines"] += 1
            cur["chars"] += len(text.strip())
    return recs


def is_wall(r, wall):
    """Стена — запись без единого маркера, длиннее порога и в одну строку."""
    return r["bullets"] == 0 and r["chars"] > wall and r["lines"] <= 1


def report(recs, wall, title):
    n = len(recs)
    if not n:
        print("%s: записей нет" % title)
        return
    walls = [r for r in recs if is_wall(r, wall)]
    flat = [r for r in recs if r["bullets"] == 0]
    lens = sorted(r["chars"] for r in recs)
    print("%s: записей %d" % (title, n))
    print("  без единого маркера списка: %d (%d%%)" % (len(flat), round(100 * len(flat) / n)))
    print("  стен (>%d симв., ни маркера, ни перевода строки): %d (%d%%)"
          % (wall, len(walls), round(100 * len(walls) / n)))
    print("  длина: медиана %d, макс %d" % (lens[n // 2], lens[-1]))
    for r in sorted(walls, key=lambda x: -x["chars"])[:10]:
        print("    %s  %s  %d симв." % (r["task"], r["label"], r["chars"]))


def cmd_audit(args):
    recs = agent_records(load_notes(args))
    report(recs, args.wall_chars, "ВСЕ записи")
    for label in ("ПЛАН", "РЕЗУЛЬТАТ"):
        print()
        report([r for r in recs if r["label"] == label], args.wall_chars, label)


def cmd_by_agent(args):
    recs = agent_records(load_notes(args))
    by = collections.defaultdict(list)
    for r in recs:
        by[r["agent"]].append(r)
    print("%-22s %5s %7s %7s" % ("агент", "всего", "плоских", "стен"))
    for name, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        flat = sum(1 for r in rs if r["bullets"] == 0)
        walls = sum(1 for r in rs if is_wall(r, args.wall_chars))
        print("%-22s %5d %7d %7d" % (name, len(rs), flat, walls))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="конкретный снапшот вместо самого свежего")
    ap.add_argument("--project", help="ограничить проектом P-...")
    ap.add_argument("--wall-chars", type=int, default=DEFAULT_WALL,
                    help="порог «стены» в символах (по умолчанию %d)" % DEFAULT_WALL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sub.add_parser("by-agent").set_defaults(fn=cmd_by_agent)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
