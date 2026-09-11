#!/usr/bin/env python3
"""Черновой подпроект для проверок на живом трекере: завести и снести за собой.

AGENTS.md §6 требует убирать тестовые данные в том же заходе, а проверять правки
`sing.py` приходится на живом API. Ручная уборка через приложение забывается,
поэтому и заведение, и удаление — одной командой, с проверкой по факту.

Название обязано начинаться на `zz-`: так черновик видно в списке проектов и
невозможно снести чужой проект опечаткой в id.

    tools/zz-project.py --create zz-board-check
    tools/zz-project.py --list
    tools/zz-project.py --delete zz-board-check

Проект создаётся ТОЛЬКО внутри ROOT_PROJECT_TITLE — ограничение области
скилла тут действует ровно так же, как в самом sing.py.
"""

import argparse
import importlib.util
import os
import sys

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


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--create", metavar="zz-НАЗВАНИЕ")
    g.add_argument("--delete", metavar="zz-НАЗВАНИЕ|P-id")
    g.add_argument("--list", action="store_true", help="какие черновики ещё живы")
    args = p.parse_args()
    sing = load_sing()

    if args.list:
        left = drafts(sing)
        for x in left:
            print(f"{x['id']}  {x['title']}")
        print(f"черновиков {PREFIX}*: {len(left)}")
        return

    if args.create:
        if not args.create.startswith(PREFIX):
            sys.exit(f"название черновика обязано начинаться на {PREFIX}")
        root = sing.resolve_root()
        # cmd_init создать проект не может (в нём `root` перекрыт путём репозитория),
        # поэтому заводим напрямую; колонки потом доберёт `init --apply`.
        created = sing.request("POST", "/project",
                               body={"title": args.create, "parent": root["id"]})
        proj = created.get("project", created)
        sing.assert_allowed(proj["id"], "созданный черновик")
        print(f"создан {proj['id']}  {proj.get('title')}  внутри «{root['title']}»")
        return

    projects = sing.all_projects()
    pool = drafts(sing, projects)
    hit = next((x for x in pool
                if x["id"] == args.delete or x.get("title") == args.delete), None)
    if not hit:
        sys.exit(f"черновик «{args.delete}» не найден среди {PREFIX}*-проектов "
                 f"внутри области скилла — удалять нечего.")
    sing.request("DELETE", f"/project/{hit['id']}")
    # по коду ответа не верим: API умеет отвечать 200, ничего не сделав (AGENTS.md §4)
    left = [x["id"] for x in drafts(sing)]
    if hit["id"] in left:
        sys.exit(f"{hit['id']}: DELETE отработал, но проект на месте — убрать руками.")
    print(f"удалён {hit['id']}  {hit['title']}; черновиков {PREFIX}* осталось: {len(left)}")


if __name__ == "__main__":
    main()
