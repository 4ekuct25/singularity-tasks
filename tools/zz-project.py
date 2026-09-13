#!/usr/bin/env python3
"""Черновой подпроект для проверок на живом трекере: завести и снести за собой.

AGENTS.md §6 требует убирать тестовые данные в том же заходе, а проверять правки
`sing.py` приходится на живом API. Ручная уборка через приложение забывается,
поэтому и заведение, и удаление — одной командой, с проверкой по факту.

Название обязано начинаться на `zz-`: так черновик видно в списке проектов и
невозможно снести чужой проект опечаткой в id.

    tools/zz-project.py --create zz-board-check
    tools/zz-project.py --create zz-board-check --with-columns
    tools/zz-project.py --list
    tools/zz-project.py --delete zz-board-check

Проект создаётся ТОЛЬКО внутри ROOT_PROJECT_TITLE — ограничение области
скилла тут действует ровно так же, как в самом sing.py.

Заведение и снос вынесены в `create_draft`/`delete_draft`, потому что тем же
черновиком пользуются `tools/check-claim-race.py` и `tests/test_live.py`. Две
копии «создай проект и добери колонки» разъезжаются с первой же правки.
"""

import argparse
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SING = os.path.join(os.path.dirname(HERE), "scripts", "sing.py")
PREFIX = "zz-"


def load_sing():
    spec = importlib.util.spec_from_file_location("sing", SING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def drafts(sing, projects=None):
    projects = projects if projects is not None else sing.all_projects()
    root = sing.resolve_root(projects)
    return [p for p in projects
            if p.get("title", "").startswith(PREFIX)
            and any(x["id"] == root["id"]
                    for x in sing.project_chain(p["id"], projects))]


def create_draft(sing, title, with_columns=False):
    """Завести черновик внутри области скилла. Возвращает (проект, колонки|None).

    with_columns=True добирает доску до пяти ролей: три системные колонки
    переиспользуются (создавать свои с теми же именами нельзя — доска раздвоится),
    «На проверке» и «Заблокировано» создаются.
    """
    if not title.startswith(PREFIX):
        raise ValueError(f"название черновика обязано начинаться на {PREFIX}")
    root = sing.resolve_root()
    # cmd_init создать проект не может (в нём `root` перекрыт путём репозитория),
    # поэтому заводим напрямую; колонки потом доберёт `init --apply`.
    created = sing.request("POST", "/project",
                           body={"title": title, "parent": root["id"]})
    proj = created.get("project", created)
    # памятку сбросить обязательно: иначе проверка области ищет свежий проект
    # в списке, снятом до его появления
    sing.forget_projects()
    sing.assert_allowed(proj["id"], "созданный черновик")
    if not with_columns:
        return proj, None

    live = {s["id"] for s in sing.project_statuses(proj["id"])}
    cols = {}
    for role, suffix in sing.SYSTEM_SUFFIX.items():
        sid = f"KS-{proj['id']}{suffix}"
        if sid not in live:
            raise RuntimeError(
                f"{proj['id']}: у чернового проекта нет системной колонки {role} "
                "— проверка на нём негодна")
        cols[role] = sid
    for role in ("review", "blocked"):
        st = sing.request("POST", "/kanban-status",
                          body={"name": sing.DEFAULT_COLUMNS[role],
                                "projectId": proj["id"],
                                "kanbanOrder": sing.COLUMN_ORDER_HINT[role]})
        cols[role] = st["id"]
    return proj, cols


# Сервер принимает запись в очередь синхронизации и отвечает раньше, чем она
# применится: немедленный GET после DELETE ещё отдаёт проект. Сверять надо, но
# по ПЕРВОМУ чтению судить нельзя — это ложная тревога, а не невыполненный снос.
SETTLE_TRIES = 4
SETTLE_PAUSE = 2.0


def delete_draft(sing, ref):
    """Снести черновик и УБЕДИТЬСЯ, что его нет. Возвращает (проект, осталось)."""
    hit = next((x for x in drafts(sing)
                if x["id"] == ref or x.get("title") == ref), None)
    if not hit:
        raise LookupError(f"черновик «{ref}» не найден среди {PREFIX}*-проектов "
                          "внутри области скилла — удалять нечего.")
    sing.request("DELETE", f"/project/{hit['id']}", soft=True)
    for attempt in range(1, SETTLE_TRIES + 1):
        # ⚠ памятка проектов кэшируется на процесс: без сброса «проверка» читает
        # список, снятый ДО удаления, и всегда докладывает «проект на месте».
        # Проверка, которая в принципе не может позеленеть, — не проверка.
        sing.forget_projects()
        left = [x["id"] for x in drafts(sing)]
        if hit["id"] not in left:
            return hit, len(left)
        if attempt < SETTLE_TRIES:
            time.sleep(SETTLE_PAUSE)
    raise RuntimeError(
        f"{hit['id']}: DELETE отработал, но проект на месте после "
        f"{SETTLE_TRIES} перечитываний — убрать руками.")


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--create", metavar="zz-НАЗВАНИЕ")
    g.add_argument("--delete", metavar="zz-НАЗВАНИЕ|P-id")
    g.add_argument("--list", action="store_true", help="какие черновики ещё живы")
    p.add_argument("--with-columns", action="store_true",
                   help="сразу добрать доску до пяти ролей")
    args = p.parse_args()
    sing = load_sing()

    if args.list:
        left = drafts(sing)
        for x in left:
            print(f"{x['id']}  {x['title']}")
        print(f"черновиков {PREFIX}*: {len(left)}")
        return

    if args.create:
        try:
            proj, cols = create_draft(sing, args.create, args.with_columns)
        except (ValueError, RuntimeError) as e:
            sys.exit(str(e))
        root = sing.resolve_root()
        print(f"создан {proj['id']}  {proj.get('title')}  внутри «{root['title']}»")
        if cols:
            print("колонки: " + ", ".join(f"{r}={cols[r]}" for r in sing.COLUMN_ORDER))
        return

    try:
        hit, left = delete_draft(sing, args.delete)
    except (LookupError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"удалён {hit['id']}  {hit['title']}; черновиков {PREFIX}* осталось: {left}")


if __name__ == "__main__":
    main()
