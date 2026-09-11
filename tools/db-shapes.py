#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db-shapes.py — в каком виде приложение SingularityApp РЕАЛЬНО хранит поле.

Зачем. Контур «записал через API → прочитал через API» не показывает целый класс
ошибок: API принимает и отдаёт обратно форму, которую приложение разобрать не
может (так было с `note` — обёртку {"ops": [...]} API глотает, а карточка рисует
сырой JSON). Единственный независимый свидетель — локальная база приложения.
Одна запись при этом ничего не доказывает: нужна ЧАСТОТНАЯ картина по живым
данным — «массив 719, простой текст 6» читается однозначно, а один пример нет.

Только чтение. Файлы базы открываются в режиме 'rb', ничего не пишется и не
создаётся. Живая база (IndexedDB/LevelDB) НЕ трогается вообще — она залочена
запущенным приложением; читаются ночные снапшоты nedb-backup/AppDatabase_*.json.

Приватность. База личная — в ней весь трекер пользователя, а скилл ограничен
подпроектами «ИИ проекты». Поэтому инструмент физически не умеет печатать
содержимое строк: строки всегда сводятся к классу («id:T», «iso-datetime»,
«json:list[dict{insert}]», «text»), к длине и к частоте. Флага «показать как
есть» нет намеренно — его бы однажды включили.

Команды
    backups                     какие снапшоты есть и насколько свежие
    collections                 коллекции и число записей
    fields <coll>               какие поля встречаются, как часто, каких типов
    shape <coll> <field>...     ЧАСТОТНАЯ КАРТИНА ФОРМ значения поля
    values <coll> <field>...    распределение значений (строки — классом, не текстом)
    delta <coll> <field>        разбор Quill-дельты: обёртка vs голый массив, attributes
    refs <coll> <field>         значение поля — это ссылка? куда и на сколько процентов
    audit                       прогон по полям, которые пишет скилл

Примеры
    tools/db-shapes.py audit
    tools/db-shapes.py shape tasks note title tags priority deadline checked
    tools/db-shapes.py delta notes content
    tools/db-shapes.py refs tasks note
"""

import argparse
import collections
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

# Поля, которые пишет sing.py, и коллекции, где они лежат на самом деле.
AUDIT = [
    ("tasks", ["title", "note", "tags", "priority", "checked", "complete",
               "state", "deadline", "start", "journalDate", "seenToday",
               "group", "projectId", "parent", "isNote",
               "recurrence", "notifies", "_removed"]),
    ("notes", ["containerId", "content"]),
    ("kanbanStatuses", ["projectId", "name", "kanbanOrder", "_removed"]),
    ("kanbanTaskStatuses", ["taskId", "statusId", "kanbanOrder", "_removed"]),
    ("checklists", ["title", "parent", "done"]),
    ("tags", ["title", "parent"]),
    ("taskGroups", ["title", "parent", "fake"]),
]

UUID = (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Идентификатор трекера: один или несколько буквенных префиксов, uuid и,
# у системных сущностей, детерминированный хвост (KS-<projectId>-TODO).
ID_RE = re.compile(r"^(?P<pref>[A-Za-z]{1,4}(?:-[A-Za-z]{1,4})*)-(?P<uuid>" + UUID +
                   r")(?:-(?P<suf>[A-Z0-9_-]+))?$")
UUID_RE = re.compile("^" + UUID + "$")
# Системные идентификаторы без uuid: TODAY, INBOX и т.п.
CONST_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}(?:-[A-Z0-9_]{1,31})*$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


# --------------------------------------------------------------------------- IO

def newest_backup():
    """Самый свежий НЕцензурированный снапшот.

    ⚠ Соседний `*.cens.json` выглядит «безопасным», но в нём буквы заменены на
    '?' ВКЛЮЧАЯ имена ключей JSON: `{"insert": …}` превращается в `{"??????": …}`.
    Разбирать по нему формы нельзя — дельта перестаёт быть дельтой.
    """
    files = [f for f in glob.glob(os.path.join(BACKUP_DIR, "AppDatabase_*.json"))
             if not f.endswith(".cens.json")]
    if not files:
        sys.exit("Снапшотов не найдено в %s\n"
                 "Приложение делает их само раз в сутки; если каталога нет — "
                 "проверьте, что SingularityApp установлен и хотя бы раз запускался."
                 % BACKUP_DIR)
    return max(files, key=os.path.getmtime)


def load(path):
    # 'rb' — намеренно: файл боевой, приложение может быть запущено, писать нельзя.
    with open(path, "rb") as f:
        raw = f.read()
    doc = json.loads(raw.decode("utf-8"))
    return doc


def db(args):
    path = args.db or newest_backup()
    doc = load(path)
    age = (time.time() - os.path.getmtime(path)) / 3600.0
    if not args.quiet:
        print("база:   %s" % path)
        print("снят:   %s  (%.1f ч назад, version=%s)"
              % (doc.get("created"), age, doc.get("version")))
        if age > 36:
            print("⚠ снапшот старше полутора суток — свежие правки в него не попали")
        print()
    return doc["data"], path


def records(data, coll):
    if coll not in data:
        sys.exit("Нет коллекции %r. Доступны: %s" % (coll, ", ".join(sorted(data))))
    return data[coll]


# ------------------------------------------------------------------- классификация

def str_class(s, depth):
    if s == "":
        return "str(empty)"
    t = s.strip()
    if t == "":
        return "str(только пробелы/перевод строки)"
    # t[0], а не t[:1]: пустая строка — подстрока любой, и "" in "[{" даёт True,
    # отчего перевод строки уезжал в «битый JSON».
    if t[0] in "[{":
        try:
            return "str(json:%s)" % shape(json.loads(t), depth + 1)
        except Exception:
            return "str(broken-json)"
    m = ID_RE.match(s)
    if m:
        suf = m.group("suf")
        if suf and suf.isdigit():
            # T-<uuid>-20260701 — экземпляр повторяющейся задачи
            return "str(id:%s-…-YYYYMMDD экземпляр повторения)" % m.group("pref")
        if suf:
            return "str(id:%s-…-%s детерминированный)" % (m.group("pref"), suf)
        return "str(id:%s)" % m.group("pref")
    if UUID_RE.match(s):
        return "str(uuid)"
    if CONST_ID_RE.match(s) and len(s) <= 32:
        return "str(CONST-id:%s)" % s
    if ISO_RE.match(s):
        # Дробная часть НЕ фиксирована: одно и то же поле встречается с 6, 3 и
        # 0 знаками. Классы разводим, иначе ловушка «парсер прибит к .%fZ»
        # растворяется в одном общем «iso-datetime».
        frac = re.search(r"\.(\d+)", s[10:])
        tail = ".%d знаков" % len(frac.group(1)) if frac else " без дробной части"
        if s.endswith("Z"):
            return "str(iso-Z%s)" % tail
        if re.search(r"[+-]\d{2}:?\d{2}$", s):
            return "str(iso-offset%s)" % tail
        return "str(iso-без-зоны%s)" % tail
    if DATE_RE.match(s):
        return "str(date)"
    if NUM_RE.match(s):
        return "str(numeric)"
    if s.startswith("#") and re.match(r"^#[0-9a-fA-F]{3,8}$", s):
        return "str(color)"
    if "<" in s and ">" in s:
        return "str(text/looks-like-html)"
    return "str(text)"


def shape(v, depth=0):
    """Форма значения — без содержимого. Рекурсивно, включая JSON внутри строк."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return str_class(v, depth)
    if isinstance(v, list):
        if not v:
            return "list(empty)"
        if depth > 4:
            return "list[...]"
        inner = sorted({shape(x, depth + 1) for x in v})
        return "list[%s]" % "|".join(inner)
    if isinstance(v, dict):
        if not v:
            return "dict(empty)"
        if depth > 4:
            return "dict{...}"
        ks = sorted(v)
        if len(ks) > 10:
            return "dict{%d ключей}" % len(ks)
        return "dict{%s}" % ",".join(ks)
    return type(v).__name__


def freq(counter, total, indent="  "):
    for k, n in counter.most_common():
        print("%s%6d  %5.1f%%  %s" % (indent, n, 100.0 * n / total, k))


# ------------------------------------------------------------------------ команды

def cmd_backups(args):
    rows = []
    for f in sorted(glob.glob(os.path.join(BACKUP_DIR, "AppDatabase_*"))):
        rows.append((os.path.basename(f), os.path.getsize(f),
                     time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(os.path.getmtime(f)))))
    if not rows:
        sys.exit("Снапшотов нет в %s" % BACKUP_DIR)
    print("каталог: %s\n" % BACKUP_DIR)
    for name, size, mt in rows:
        mark = "  (цензурирован, формы по нему считать НЕЛЬЗЯ)" \
            if name.endswith(".cens.json") else ""
        print("  %-44s %9d Б  %s%s" % (name, size, mt, mark))
    print("\nчитается самый свежий нецензурированный: %s"
          % os.path.basename(newest_backup()))


def cmd_collections(args):
    data, _ = db(args)
    for k in sorted(data, key=lambda x: -len(data[x])):
        print("  %-24s %6d" % (k, len(data[k])))


def cmd_fields(args):
    data, _ = db(args)
    recs = records(data, args.collection)
    total = len(recs)
    print("%s — записей: %d\n" % (args.collection, total))
    present = collections.Counter()
    types = collections.defaultdict(collections.Counter)
    for r in recs:
        for k, v in r.items():
            present[k] += 1
            types[k][shape(v)] += 1
    print("  %-26s %6s  %6s  формы" % ("поле", "есть", "доля"))
    for k, n in present.most_common():
        forms = ", ".join("%s×%d" % (f, c) for f, c in types[k].most_common(4))
        if len(types[k]) > 4:
            forms += ", …"
        print("  %-26s %6d  %5.1f%%  %s" % (k, n, 100.0 * n / total, forms))


def cmd_shape(args):
    data, _ = db(args)
    recs = records(data, args.collection)
    total = len(recs)
    for field in args.fields:
        c = collections.Counter()
        for r in recs:
            c[shape(r[field]) if field in r else "<поля нет>"] += 1
        print("%s.%s  (всего записей %d)" % (args.collection, field, total))
        freq(c, total)
        print()


def cmd_values(args):
    """Распределение значений. Строки НИКОГДА не печатаются как есть."""
    data, _ = db(args)
    recs = records(data, args.collection)
    total = len(recs)
    for field in args.fields:
        c = collections.Counter()
        lens = []
        for r in recs:
            if field not in r:
                c["<поля нет>"] += 1
                continue
            v = r[field]
            if isinstance(v, (bool, int, float)) or v is None:
                c[repr(v)] += 1
            elif isinstance(v, str):
                c[str_class(v, 0)] += 1
                lens.append(len(v))
            else:
                c[shape(v)] += 1
        print("%s.%s  (всего записей %d)" % (args.collection, field, total))
        freq(c, total)
        if lens:
            lens.sort()
            q = lambda p: lens[min(len(lens) - 1, int(len(lens) * p))]
            print("    длина строк: min %d / медиана %d / p90 %d / max %d"
                  % (lens[0], q(0.5), q(0.9), lens[-1]))
        print()


def cmd_delta(args):
    """Разбор Quill-дельты: обёртка или голый массив, какие attributes реально живут."""
    data, _ = db(args)
    recs = records(data, args.collection)
    field = args.field
    forms = collections.Counter()
    op_keys = collections.Counter()
    attrs = collections.Counter()
    attr_val_shapes = collections.defaultdict(collections.Counter)
    attr_vals = collections.defaultdict(collections.Counter)
    embeds = collections.Counter()
    n_with = 0
    for r in recs:
        v = r.get(field)
        if v in (None, ""):
            forms["<пусто/нет>"] += 1
            continue
        n_with += 1
        ops = None
        if isinstance(v, list):
            forms["нативный массив операций"] += 1
            ops = v
        elif isinstance(v, dict):
            if isinstance(v.get("ops"), list):
                forms["нативный объект-обёртка {ops:[…]}"] += 1
                ops = v["ops"]
            else:
                forms["нативный объект без ops"] += 1
        elif isinstance(v, str):
            t = v.strip()
            if not t:
                forms["пустая строка / перевод строки"] += 1
            elif t[0] not in "[{":
                forms["простой текст (legacy)"] += 1
            else:
                try:
                    j = json.loads(t)
                except Exception:
                    forms["строка с битым JSON"] += 1
                    j = None
                if isinstance(j, list):
                    forms["строка-JSON: ГОЛЫЙ МАССИВ операций"] += 1
                    ops = j
                elif isinstance(j, dict) and isinstance(j.get("ops"), list):
                    forms["строка-JSON: объект-обёртка {ops:[…]}"] += 1
                    ops = j["ops"]
                elif j is not None:
                    forms["строка-JSON: что-то ещё (%s)" % shape(j)] += 1
        else:
            forms["иной тип: %s" % type(v).__name__] += 1
        for op in ops or []:
            if not isinstance(op, dict):
                op_keys["<операция не объект: %s>" % type(op).__name__] += 1
                continue
            for k in op:
                op_keys[k] += 1
            ins = op.get("insert")
            if isinstance(ins, dict):
                for k in ins:
                    embeds[k] += 1
            a = op.get("attributes")
            if isinstance(a, dict):
                for k, val in a.items():
                    attrs[k] += 1
                    attr_val_shapes[k][shape(val)] += 1
                    if isinstance(val, (str, int, bool)):
                        attr_vals[k][val] += 1

    # Значения атрибута печатаем, только если это ЗАКРЫТЫЙ СЛОВАРЬ (bullet/ordered,
    # left/center/right): короткие, без пробелов и разделителей, мало разных.
    # Так «как сделать маркированный список» видно, а ссылки и текст не утекают.
    safe = re.compile(r"^[A-Za-z0-9#_.-]{1,24}$")
    attr_enum = {}
    for k, c in attr_vals.items():
        if len(c) <= 12 and all(
                (not isinstance(v, str)) or safe.match(v) for v in c):
            attr_enum[k] = c

    total = len(recs)
    print("%s.%s — формы (записей %d, непустых %d)\n"
          % (args.collection, field, total, n_with))
    freq(forms, total)
    if op_keys:
        print("\n  ключи операций:")
        freq(op_keys, sum(op_keys.values()), indent="    ")
    if embeds:
        print("\n  встраиваемые объекты (insert — объект):")
        freq(embeds, sum(embeds.values()), indent="    ")
    if attrs:
        print("\n  attributes — словарь оформления, который приложение реально рисует:")
        for k, n in attrs.most_common():
            vs = ", ".join("%s×%d" % (s, c) for s, c in attr_val_shapes[k].most_common(3))
            print("    %6d  %-16s %s" % (n, k, vs))
            enum = attr_enum.get(k)
            if enum is not None:
                print("            значения: %s"
                      % ", ".join("%s×%d" % (v, c) for v, c in enum.most_common()))


def cmd_refs(args):
    """Значение поля — это текст или ссылка? Проверяем по id всех коллекций."""
    data, _ = db(args)
    recs = records(data, args.collection)
    vals = [r[args.field] for r in recs
            if isinstance(r.get(args.field), str) and r[args.field]]
    if not vals:
        sys.exit("У %s.%s нет непустых строковых значений."
                 % (args.collection, args.field))
    index = {}
    for coll, rows in data.items():
        ids = {r.get("id") for r in rows if isinstance(r, dict) and r.get("id")}
        if ids:
            index[coll] = ids
    print("%s.%s — непустых строк: %d\n" % (args.collection, args.field, len(vals)))
    hit_any = False
    for coll, ids in sorted(index.items()):
        hits = sum(1 for v in vals if v in ids)
        if hits:
            hit_any = True
            print("  %6d  %5.1f%%  совпадает с %s.id"
                  % (hits, 100.0 * hits / len(vals), coll))
    # производный id вида N-<id владельца>
    derived = sum(1 for r in recs
                  if isinstance(r.get(args.field), str)
                  and r[args.field].endswith(str(r.get("id"))))
    if derived:
        print("  %6d  %5.1f%%  значение = <префикс>-<id самой записи> "
              "(детерминированный id, а не содержимое)"
              % (derived, 100.0 * derived / len(vals)))
    if not hit_any and not derived:
        print("  ссылок не обнаружено — поле хранит значение, а не id")


def cmd_audit(args):
    data, path = db(args)
    print("Поля, которые пишет скилл, — как они лежат в базе приложения\n")
    for coll, fields in AUDIT:
        if coll not in data:
            print("%s — нет такой коллекции в снапшоте\n" % coll)
            continue
        recs = data[coll]
        total = len(recs) or 1
        print("=== %s (записей %d) ===" % (coll, len(recs)))
        for f in fields:
            c = collections.Counter()
            for r in recs:
                c[shape(r[f]) if f in r else "<поля нет>"] += 1
            top = ", ".join("%s×%d" % (s, n) for s, n in c.most_common(4))
            if len(c) > 4:
                top += ", …"
            miss = c.get("<поля нет>", 0)
            print("  %-16s заполнено %4d/%d  %s"
                  % (f, len(recs) - miss, len(recs), top))
        print()


def main():
    ap = argparse.ArgumentParser(
        description="В каком виде приложение SingularityApp реально хранит поле "
                    "(только чтение снапшотов nedb-backup).")
    ap.add_argument("--db", help="путь к конкретному AppDatabase_*.json")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="не печатать шапку про снапшот")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backups").set_defaults(fn=cmd_backups)
    sub.add_parser("collections").set_defaults(fn=cmd_collections)

    p = sub.add_parser("fields"); p.add_argument("collection")
    p.set_defaults(fn=cmd_fields)

    p = sub.add_parser("shape"); p.add_argument("collection")
    p.add_argument("fields", nargs="+"); p.set_defaults(fn=cmd_shape)

    p = sub.add_parser("values"); p.add_argument("collection")
    p.add_argument("fields", nargs="+"); p.set_defaults(fn=cmd_values)

    p = sub.add_parser("delta"); p.add_argument("collection")
    p.add_argument("field"); p.set_defaults(fn=cmd_delta)

    p = sub.add_parser("refs"); p.add_argument("collection")
    p.add_argument("field"); p.set_defaults(fn=cmd_refs)

    sub.add_parser("audit").set_defaults(fn=cmd_audit)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
