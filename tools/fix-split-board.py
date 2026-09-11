#!/usr/bin/env python3
"""Починить раздвоенную доску: убрать колонки скилла, дублирующие системные.

Раздвоение возникает, когда `init` отработал на проекте, чей канбан ещё не
развёрнут: системных колонок нет, скилл заводит свои, а через минуту приложение
досоздаёт системные — и на доске появляются две «В работе» и две «Готово».
Системную колонку удалить нельзя (`DELETE /kanban-status/<системная>` отвечает
ошибкой, колонка остаётся), поэтому чинится в другую сторону: привязка
переезжает на системные, задачи — за ней, а опустевшие свои удаляются.

Порядок намеренный: сначала задачи, потом привязка, потом удаление. Удалить
колонку с задачами — значит осиротить их: они пропадут с доски, как это уже
было с задачей после оборвавшегося `add`.

    tools/fix-split-board.py            # разбор: что не так и что будет сделано
    tools/fix-split-board.py --apply    # выполнить

Запускать из каталога привязанного репозитория.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import sing  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="выполнить, а не показать план")
    args = ap.parse_args()

    cfg, path = sing.load_config()
    pid = cfg["projectId"]
    live = {s["id"]: s["name"] for s in sing.project_statuses(pid)}
    sysmap = {r: f"KS-{pid}{suf}" for r, suf in sing.SYSTEM_SUFFIX.items()}

    absent = [r for r, sid in sysmap.items() if sid not in live]
    if absent:
        sys.exit("Системных колонок в проекте нет: " + ", ".join(absent)
                 + ".\nЧинить нечего — доска не раздвоена, скилл работает на своих.")

    wrong = {r: sid for r, sid in sysmap.items() if cfg["columns"].get(r) != sid}
    if not wrong:
        print("Привязка уже на системных колонках.")
    cmap = sing.column_map(pid)
    stale = [cid for cid in live if cid not in set(cfg["columns"].values())
             and cid not in set(sysmap.values())]

    plan = []
    for role, sid in wrong.items():
        old = cfg["columns"].get(role)
        n = sum(1 for c in cmap.values() if c == old)
        plan.append(f"ПЕРЕВЕЗТИ {n} задач «{live.get(old)}» -> «{live[sid]}» (роль {role})")
        plan.append(f"ПРИВЯЗАТЬ роль {role} к {sid}")
    for cid in stale:
        plan.append(f"УДАЛИТЬ опустевшую колонку «{live[cid]}» {cid}")
    if not plan:
        print("Доска в порядке.")
        return
    print("План:" if args.apply else "План (ничего не изменено, добавь --apply):")
    for line in plan:
        print("  · " + line)
    if not args.apply:
        return

    for role, sid in wrong.items():
        old = cfg["columns"].get(role)
        for tid, cid in list(cmap.items()):
            if cid == old:
                print(f"  {tid}: {sing.move_to_column(tid, sid)} -> «{live[sid]}»")
        cfg["columns"][role] = sid
        cfg["columnNames"][role] = live[sid]
    sing.save_config(cfg, path)
    print(f"  привязка переписана: {path}")

    cmap = sing.column_map(pid)
    for cid in stale:
        left = [t for t, c in cmap.items() if c == cid]
        if left:
            print(f"  ⚠ «{live[cid]}» НЕ удаляю — в ней {len(left)} задач: {left}")
            continue
        sing.request("DELETE", f"/kanban-status/{cid}", soft=True)
        after = sing.request("GET", f"/kanban-status/{cid}", soft=True)
        gone = after is None or after.get("removed")
        print(f"  «{live[cid]}» удалена: {gone}"
              + ("" if gone else f" — колонка на месте, состояние {after}"))


if __name__ == "__main__":
    main()
