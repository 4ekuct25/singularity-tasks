#!/usr/bin/env python3
"""Замер: когда у проекта появляются СИСТЕМНЫЕ колонки и можно ли сослаться на их
детерминированный id (`KS-<projectId>-TODO` / `-IN-PROGRESS` / `-DONE`) раньше, чем
они материализованы.

Вопрос, ради которого это написано (карточка T-afedad75): `init` отказывается
работать, если системных колонок нет, и просит человека открыть проект в
приложении и переключить его в канбан. Отказ обоснован только в том случае, если
сослаться на системный id заранее ДЕЙСТВИТЕЛЬНО нельзя. Догадка тут не годится:
API умеет ответить 200, ничего не сделав (AGENTS.md §4).

Что меряется на черновом `zz-`-проекте, заведённом через API:

  1. отдаёт ли `GET /kanban-status?projectId=` системные колонки сразу;
  2. отвечает ли `GET /kanban-status/KS-<pid>-TODO` по id, когда в списке пусто;
  3. принимает ли `POST /kanban-task-status` ссылку на такой id (и появляется ли
     после этого колонка в списке — то есть материализует ли её ссылка);
  4. сколько колонок на доске в конце: раздвоения быть не должно.

Черновик сносится в том же запуске (`--keep` оставляет его для разбора руками).

    tools/check-kanban-lazy.py
    tools/check-kanban-lazy.py --keep
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SING = os.path.join(os.path.dirname(HERE), "scripts", "sing.py")
TITLE = "zz-kanban-lazy"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def show(label, value):
    print(f"  {label}: {value}")


def raw(sing, method, path, body=None):
    """Тот же запрос, но с КОДОМ ответа: `request(soft=True)` отдаёт None и на
    отказе, и на пустом теле, а для протокола нужен именно код."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(sing.API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + sing.get_token())
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, (r.read().decode() or "")[:300]
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="не сносить черновик")
    args = ap.parse_args()

    sing = load("sing", SING)
    zz = load("zz_project", os.path.join(HERE, "zz-project.py"))

    project, _ = zz.create_draft(sing, TITLE)
    pid = project["id"]
    print(f"\nчерновик {pid} «{project.get('title')}»")

    try:
        print("\n1. Список колонок сразу после создания (GET /kanban-status?projectId=)")
        listed = sing.project_statuses(pid)
        show("сколько", len(listed))
        for s in listed:
            show("  колонка", f"{s['id']}  «{s['name']}»  order={s.get('kanbanOrder')}")

        print("\n2. Те же колонки запросом ПО ID (GET /kanban-status/KS-<pid>-…)")
        by_id = {}
        for role, suf in sing.SYSTEM_SUFFIX.items():
            sid = f"KS-{pid}{suf}"
            got = sing.request("GET", f"/kanban-status/{sid}", soft=True)
            by_id[role] = got
            show(role, f"{sid} -> " + (f"«{got.get('name')}»" if got else "НЕТ ОТВЕТА"))

        print("\n3. Ссылка на системный id из задачи (POST /kanban-task-status,"
              " тело как в move_to_column)")
        task = sing.request("POST", "/task", body={"title": f"{TITLE}-проба",
                                                   "projectId": pid})
        tid = task["id"]
        show("задача", tid)
        todo = f"KS-{pid}-TODO"
        t0 = time.time()
        link = sing.request("POST", "/kanban-task-status",
                            body={"taskId": tid, "statusId": todo}, soft=True)
        show("ответ", (json.dumps(link, ensure_ascii=False)[:200] if link
                       else "ОТКАЗ (soft=None)"))
        show("время", "%.2f c" % (time.time() - t0))

        print("\n3a. Принимает ли сервер СВОЙ id колонки (POST /kanban-status с id)")
        # Это и есть развилка карточки: если id колонки можно назначить самому,
        # `init` способен занять детерминированные системные id заранее, и
        # приложению нечего будет создавать вторым экземпляром.
        wanted = f"KS-{pid}-ZZ-PROBE"
        code, text = raw(sing, "POST", "/kanban-status",
                         {"id": wanted, "name": "zz-проба-id",
                          "projectId": pid, "kanbanOrder": 9})
        show("HTTP", f"{code} {text}")
        check = sing.request("GET", f"/kanban-status/{wanted}", soft=True)
        show("перечитано по запрошенному id",
             f"«{check.get('name')}» — id ЗАНЯТ" if check else "НЕТ — id не занят")
        if check:
            sing.request("DELETE", f"/kanban-status/{wanted}", soft=True)

        print("\n3b. Можно ли снести системную колонку (DELETE /kanban-status)")
        victim = f"KS-{pid}-DONE"
        code, text = raw(sing, "DELETE", f"/kanban-status/{victim}")
        show("HTTP", f"{code} {text}")
        back = sing.request("GET", f"/kanban-status/{victim}", soft=True)
        show("после DELETE", f"«{back.get('name')}» removed={back.get('removed')}"
             if back else "колонки нет — снеслась")

        print("\n3c. Ссылка на НЕСУЩЕСТВУЮЩУЮ колонку (POST /kanban-task-status)")
        # Отложенная привязка «запишем системные id, колонки появятся потом»
        # держится только на этом ответе: если ссылку принимают — доску можно
        # не трогать вовсе, если нет — до синхронизации доска нерабочая.
        task2 = sing.request("POST", "/task", body={"title": f"{TITLE}-проба-2",
                                                    "projectId": pid})
        code, text = raw(sing, "POST", "/kanban-task-status",
                         {"taskId": task2["id"], "statusId": f"KS-{pid}-NO-SUCH"})
        show("HTTP", f"{code} {text}")
        sing.request("DELETE", f"/task/{task2['id']}", soft=True)

        print("\n4. Что на доске в конце (перечитано, а не по коду ответа)")
        after = [s for s in sing.project_statuses(pid) if not s.get("removed")]
        show("сколько", len(after))
        for s in after:
            show("  колонка", f"{s['id']}  «{s['name']}»")
        names = [s["name"] for s in after]
        dupes = sorted({n for n in names if names.count(n) > 1})
        show("тёзки", ", ".join(dupes) if dupes else "нет")
        links = [l for l in sing.paged("/kanban-task-status", "kanbanTaskStatuses", {})
                 if l.get("taskId") == tid]
        show("связка задачи", json.dumps(links, ensure_ascii=False)[:300] or "нет")

        sing.request("DELETE", f"/task/{tid}", soft=True)

        print("\n5. Проект, заведённый КАК ПРИЛОЖЕНИЕМ: id генерит клиент")
        # Приложение работает offline-first и id придумывает само. Если сервер
        # такой id принимает и системных колонок при этом не создаёт — это ровно
        # то состояние, из-за которого `init` отказывается работать, и его
        # наконец можно измерить, а не обсуждать.
        pid2 = "P-" + str(uuid.uuid4())
        code, text = raw(sing, "POST", "/project",
                         {"id": pid2, "title": TITLE + "-client",
                          "parent": project["parent"]})
        show("HTTP", f"{code} {text}")
        made2 = json.loads(text) if code < 300 and text else None
        if made2:
            real = (made2.get("project") or made2).get("id")
            show("id", f"{real} — {'ЗАНЯЛ ЗАПРОШЕННЫЙ' if real == pid2 else 'сервер выдал свой'}")
            cols2 = sing.project_statuses(real)
            show("колонок сразу", len(cols2))
            for s in cols2:
                show("  колонка", f"{s['id']}  «{s['name']}»")
            for role, suf in sing.SYSTEM_SUFFIX.items():
                got = sing.request("GET", f"/kanban-status/KS-{real}{suf}", soft=True)
                show(f"по id {role}", f"«{got.get('name')}»" if got else "НЕТ")
            sing.forget_projects()
            try:
                zz.delete_draft(sing, real)
                show("черновик-клон", "снесён")
            except Exception as e:  # noqa: BLE001 — уборка не должна ронять замер
                show("черновик-клон НЕ снесён", f"{e} — убрать руками {real}")
    finally:
        if args.keep:
            print(f"\nчерновик оставлен: {pid}")
        else:
            zz.delete_draft(sing, pid)


if __name__ == "__main__":
    sys.exit(main())
