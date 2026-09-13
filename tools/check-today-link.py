#!/usr/bin/env python3
"""Проверка выбора канбан-связки: доска проекта против системной доски «Сегодня».

У задачи бывает больше одной связки `kanban-task-status`: кроме связки с колонкой
своего проекта существует связка с системной доской «Сегодня» — псевдопроект
`P-TODAY` со своими колонками и id связки `KTS-<taskId>-TODAY`. Если выбирать
первую попавшуюся связку, то `task_column()` вернёт чужую колонку, а
`move_to_column()` PATCH-ем переведёт на колонку проекта связку «Сегодня» —
то есть снимет задачу с доски «Сегодня» пользователя.

Два режима:

  --stub   без сети: подставная задача с ДВУМЯ связками (TODAY идёт первой).
           Ровно тот случай, который через REST API воспроизвести нельзя:
           POST /kanban-task-status не принимает поле `id` и апсертит
           единственную связку `KTS-<taskId>`, а `KTS-<taskId>-TODAY` заводит
           только само приложение.

  --live P-...  на живом API: в указанном ЧЕРНОВОМ проекте заводится задача,
           её единственная связка ставится на колонку `P-TODAY`, после чего
           пишется, какие HTTP-вызовы сделал move_to_column. Старый код правит
           чужую связку PATCH-ем, починенный — заводит связку доски проекта
           POST-ом. Задача удаляется в том же заходе.

Запуск: python3 tools/check-today-link.py --stub
        python3 tools/check-today-link.py --live P-<черновой проект>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import sing  # noqa: E402

FAILED = []


def check(ok, msg, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + msg + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(msg)


# --------------------------------------------------------------------------- stub


class FakeAPI:
    """Минимальный сервер в памяти: задача, колонки двух проектов, связки."""

    def __init__(self, project, task, links, statuses):
        self.project, self.task = project, task
        self.links = links            # список dict как отдаёт API
        self.statuses = statuses      # statusId -> projectId
        self.calls = []               # (method, path)

    def request(self, method, path, query=None, body=None, soft=False):
        self.calls.append((method, path))
        if method == "GET" and path == f"/task/{self.task}":
            return {"id": self.task, "projectId": self.project}
        if method == "GET" and path == "/kanban-status":
            pid = (query or {}).get("projectId")
            return {"kanbanStatuses": [{"id": s, "name": s, "removed": False}
                                       for s, p in self.statuses.items() if p == pid],
                    "pagination": {"total": sum(1 for p in self.statuses.values()
                                                if p == pid)}}
        if method == "GET" and path.startswith("/kanban-status/"):
            sid = path.split("/", 2)[2]
            pid = self.statuses.get(sid)
            return {"id": sid, "projectId": pid} if pid else None
        if method == "GET" and path == "/kanban-task-status":
            q = query or {}
            items = [dict(l) for l in self.links
                     if (not q.get("taskId") or l["taskId"] == q["taskId"])
                     and (not q.get("statusId") or l["statusId"] == q["statusId"])
                     and (q.get("includeRemoved") == "true" or not l.get("removed"))]
            return {"kanbanTaskStatuses": items, "pagination": {"total": len(items)}}
        if method == "PATCH" and path.startswith("/kanban-task-status/"):
            lid = path.split("/", 2)[2]
            for l in self.links:
                if l["id"] == lid:
                    l.update(body or {})
                    return dict(l)
            return None
        if method == "POST" and path == "/kanban-task-status":
            lid = "KTS-" + body["taskId"]          # сервер выводит id из taskId
            for l in self.links:
                if l["id"] == lid:                 # POST апсертит эту же связку
                    l.update({"statusId": body["statusId"], "removed": False})
                    return dict(l)
            l = {"id": lid, "taskId": body["taskId"], "statusId": body["statusId"],
                 "removed": False}
            self.links.append(l)
            return dict(l)
        raise AssertionError(f"необработанный вызов {method} {path}")


def run_stub():
    P, T = "P-proj", "T-task"
    own_todo, own_wip = f"KS-{P}-TODO", f"KS-{P}-IN-PROGRESS"
    today_col = "KS-d3f053e1-today-column"      # колонка псевдопроекта P-TODAY
    statuses = {own_todo: P, own_wip: P, today_col: "P-TODAY"}
    # связка «Сегодня» ПЕРВАЯ — именно её брал live[0]
    links = [
        {"id": f"KTS-{T}-TODAY", "taskId": T, "statusId": today_col, "removed": False},
        {"id": f"KTS-{T}", "taskId": T, "statusId": own_todo, "removed": False},
    ]
    fake = FakeAPI(P, T, links, statuses)
    orig, sing.request = sing.request, fake.request
    sing._PROJECT_STATUS_IDS = getattr(sing, "_PROJECT_STATUS_IDS", {})
    sing._PROJECT_STATUS_IDS.clear()
    try:
        print("stub: задача в обычном проекте + связка с доской «Сегодня» (она первая)")
        col = sing.task_column(T)
        check(col == own_todo, "task_column() отдаёт колонку проекта", col)

        fake.calls.clear()
        res = sing.move_to_column(T, own_wip)
        patched = [p for m, p in fake.calls if m == "PATCH"]
        by_id = {l["id"]: l for l in links}
        check(by_id[f"KTS-{T}-TODAY"]["statusId"] == today_col,
              "связка «Сегодня» не тронута",
              by_id[f"KTS-{T}-TODAY"]["statusId"])
        check(by_id[f"KTS-{T}"]["statusId"] == own_wip,
              "связка доски проекта переставлена", by_id[f"KTS-{T}"]["statusId"])
        check(all(f"-TODAY" not in p for p in patched),
              "PATCH ушёл не в связку «Сегодня»", ", ".join(patched) or "PATCH не было")
        check(res is not None, "move_to_column не упал", str(res))

        # вторая сцена: связки доски проекта нет вовсе, есть только «Сегодня»
        fake.links[:] = [{"id": f"KTS-{T}-TODAY", "taskId": T,
                          "statusId": today_col, "removed": False}]
        sing._PROJECT_STATUS_IDS.clear()
        col = sing.task_column(T)
        check(col is None, "без связки своей доски колонка = None (задача вне колонок)",
              str(col))
        fake.calls.clear()
        sing.move_to_column(T, own_wip)
        check({l["id"] for l in fake.links} == {f"KTS-{T}-TODAY", f"KTS-{T}"},
              "заведена отдельная связка доски проекта",
              ", ".join(sorted(l["id"] for l in fake.links)))
        check(next(l for l in fake.links if l["id"].endswith("-TODAY"))["statusId"]
              == today_col, "связка «Сегодня» уцелела при заведении новой")
    finally:
        sing.request = orig
        if hasattr(sing, "_PROJECT_STATUS_IDS"):
            sing._PROJECT_STATUS_IDS.clear()


# --------------------------------------------------------------------------- live


def run_live(project_id):
    proj = sing.request("GET", f"/project/{project_id}")
    title = (proj.get("project", proj) or {}).get("title", "")
    if not title.startswith("zz-"):
        sing.die(f"{project_id} «{title}» — не черновой проект (ожидается имя zz-*). "
                 "Живой прогон заводит и удаляет задачу, чужой проект для этого не годится.")
    today_col = "KS-P-TODAY-TODO"
    own_todo = sing.system_status_id(project_id, "todo")
    own_wip = sing.system_status_id(project_id, "wip")
    task = sing.request("POST", "/task", body={"title": "zz-check-today-link",
                                               "projectId": project_id})
    tid = task["id"]
    try:
        sing.request("POST", "/kanban-task-status",
                     body={"taskId": tid, "statusId": own_todo})
        # так выглядит задача, которую пользователь положил на доску «Сегодня»:
        # единственная связка указывает на колонку псевдопроекта P-TODAY
        sing.request("POST", "/kanban-task-status",
                     body={"taskId": tid, "statusId": today_col})
        links = sing.task_links(tid, include_removed=True)
        print(f"live: {tid}, связок {len(links)}: "
              + ", ".join(f"{l['id']}→{l['statusId']}" for l in links))
        on_today_before = len(sing.paged("/kanban-task-status", "kanbanTaskStatuses",
                                         {"statusId": today_col}))
        col = sing.task_column(tid)
        check(col != today_col, "task_column() не отдаёт колонку доски «Сегодня»", str(col))

        calls = []
        orig = sing.request

        def traced(method, path, **kw):
            calls.append((method, path))
            return orig(method, path, **kw)

        sing.request = traced
        try:
            sing.move_to_column(tid, own_wip)
        finally:
            sing.request = orig
        writes = [(m, p) for m, p in calls if m in ("PATCH", "POST")]
        print("  запросы move_to_column: " + ", ".join(f"{m} {p}" for m, p in writes))
        check(not any(m == "PATCH" and p.startswith("/kanban-task-status/")
                      for m, p in writes),
              "чужая связка не правится PATCH-ем",
              ", ".join(f"{m} {p}" for m, p in writes))
        check(sing.task_column(tid) == own_wip, "задача встала в колонку своего проекта")
        on_today_after = len(sing.paged("/kanban-task-status", "kanbanTaskStatuses",
                                        {"statusId": today_col}))
        print(f"  на колонке «Сегодня»/Новые связок: было {on_today_before}, "
              f"стало {on_today_after}")
    finally:
        sing.request("DELETE", f"/task/{tid}", soft=True)
        print(f"  убрано: задача {tid} удалена")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stub", action="store_true", help="офлайн-проверка на подставном API")
    ap.add_argument("--live", metavar="P-ID", help="живой прогон в ЧЕРНОВОМ проекте zz-*")
    args = ap.parse_args()
    if not args.stub and not args.live:
        ap.error("нужен --stub и/или --live P-...")
    if args.stub:
        run_stub()
    if args.live:
        run_live(args.live)
    if FAILED:
        print(f"\nПРОВАЛЕНО проверок: {len(FAILED)}")
        for m in FAILED:
            print("  · " + m)
        sys.exit(1)
    print("\nвсе проверки пройдены")


if __name__ == "__main__":
    main()
