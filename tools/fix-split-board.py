#!/usr/bin/env python3
"""Починить раздвоенную доску: убрать колонки скилла, дублирующие системные,
и выровнять порядок колонок — одним прогоном.

Раздвоение возникает, когда `init` отработал на проекте, чей канбан ещё не
развёрнут: системных колонок нет, скилл заводит свои, а через минуту приложение
досоздаёт системные — и на доске появляются две «В работе» и две «Готово».
Системную колонку удалить нельзя (`DELETE /kanban-status/<системная>` отвечает
ошибкой, колонка остаётся), поэтому чинится в другую сторону: привязка
переезжает на системные, задачи — за ней, а опустевшие свои колонки — в том
числе со снятых ролей — удаляются в том же прогоне, и порядок ролей
выравнивается тем же механизмом, что `init --apply` (plan_column_orders +
PATCH kanbanOrder, со сверкой перечитыванием).

Почему stale считается от привязки ПОСЛЕ перепривязки: старая колонка
снимаемой роли в момент расчёта ещё в привязке, и по текущей привязке она
«занята» — мимо плана. financial_portfolio (10-24): план без единого УДАЛИТЬ,
три пустые колонки удалялись руками, порядок чинил повторный init --apply.

Порядок намеренный: сначала задачи, потом привязка, потом удаление, потом
порядок. Удалить колонку с задачами — значит осиротить их: они пропадут с
доски, как это уже было с задачей после оборвавшегося `add`.

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


def plan_split_fix(live, cfg_columns, sysmap):
    """(wrong, final, stale) — кого перепривязать, какова будет привязка после
    прогона и какие колонки к тому моменту окажутся сиротами.

    stale считается от привязки ПОСЛЕ перепривязки, а не от текущей: старая
    колонка снимаемой роли в момент расчёта ещё в cfg_columns, и по текущей
    привязке она «занята» — из-за этого она ушла мимо плана (financial_portfolio,
    10-24). Системные колонки в stale не попадают никогда, даже если на них
    не указывает ни одна роль.
    """
    wrong = {r: sid for r, sid in sysmap.items()
             if cfg_columns.get(r) != sid}
    roles = list(sysmap) + [r for r in cfg_columns if r not in sysmap]
    final = {r: (sysmap[r] if r in wrong else cfg_columns.get(r)) for r in roles}
    stale = [cid for cid in live
             if cid not in set(final.values()) and cid not in set(sysmap.values())]
    return wrong, final, stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="выполнить, а не показать план")
    args = ap.parse_args()

    cfg, path = sing.load_config()
    pid = cfg["projectId"]
    statuses = [s for s in sing.project_statuses(pid) if not s.get("removed")]
    live = {s["id"]: s["name"] for s in statuses}
    sysmap = {r: f"KS-{pid}{suf}" for r, suf in sing.SYSTEM_SUFFIX.items()}

    absent = [r for r, sid in sysmap.items() if sid not in live]
    if absent:
        sys.exit("Системных колонок в проекте нет: " + ", ".join(absent)
                 + ".\nЧинить нечего — доска не раздвоена, скилл работает на своих.")

    wrong, final, stale = plan_split_fix(live, cfg["columns"], sysmap)
    cmap = sing.column_map(pid)
    orders, moves = sing.plan_column_orders(final, statuses, [])

    plan = []
    for role, sid in wrong.items():
        old = cfg["columns"].get(role)
        n = sum(1 for c in cmap.values() if c == old)
        plan.append(f"ПЕРЕВЕЗТИ {n} задач «{live.get(old)}» -> «{live[sid]}» (роль {role})")
        plan.append(f"ПРИВЯЗАТЬ роль {role} к {sid}")
    for cid in stale:
        plan.append(f"УДАЛИТЬ опустевшую колонку «{live[cid]}» {cid}")
    for cid, order in moves:
        plan.append(f"СДВИНУТЬ колонку «{live.get(cid, cid)}» -> порядок {order}")
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

    # Порядок: после перепривязки и удаления роли обязаны идти строго
    # todo → wip → review → done → blocked. Раздвигаем соседей так же, как
    # init --apply, и сверяем ПЕРЕЧИТЫВАНИЕМ: этот API отвечает 200 и эхом
    # того, что ему дали, даже когда записал другое.
    fresh = [s for s in sing.project_statuses(pid) if not s.get("removed")]
    orders, moves = sing.plan_column_orders(final, fresh, [])
    for cid, order in moves:
        name = next((s.get("name") for s in fresh if s["id"] == cid), cid)
        sing.request("PATCH", f"/kanban-status/{cid}", body={"kanbanOrder": order})
        print(f"  подвинута колонка «{name}» -> порядок {order}")
    if moves:
        problems = sing.column_order_problems(final, sing.project_statuses(pid))
        if problems:
            for p in problems:
                print(f"  ✗ порядок не выровнялся: {p}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
