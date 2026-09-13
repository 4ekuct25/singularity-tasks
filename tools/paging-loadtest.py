#!/usr/bin/env python3
"""Нагрузочная проверка пагинации sing.py на черновом подпроекте.

Гейт полноты выборки: СОЗДАНО уникальных объектов ↔ вернул `paged()` ↔
`pagination.total` сервера. Арифметика внутри одного источника ничего не
доказывает — тихая потеря хвоста выглядит правдоподобно.

Черновой проект называется `zz-…` и живёт внутри «ИИ проекты». Он одноразовый:
`cleanup` сносит его целиком (`DELETE /project/{id}` уносит задачи с собой) и
проверяет фактом, что проекта больше нет.

    tools/paging-loadtest.py probe   --project P-...
    tools/paging-loadtest.py create  --title zz-paging
    tools/paging-loadtest.py fill    --project P-... --count 250 [--link]
    tools/paging-loadtest.py measure --project P-...
    tools/paging-loadtest.py cleanup [--project P-...] [--yes]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import sing  # noqa: E402

ZZ_PREFIX = "zz-"


# --------------------------------------------------------------------- HTTP
# Свой request: sing.request() на 429 просто die(), а живой API под пачечной
# заливкой троттлит. Ретраи нужны только заливке, поэтому живут здесь, а не в
# скилле.

def rq(method, path, query=None, body=None, tries=6):
    url = sing.API + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    delay = 1.0
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + sing.get_token())
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                print(f"    HTTP {e.code}, жду {delay:.1f}s", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            sing.die(f"{method} {path} -> HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                print(f"    сеть: {e}, жду {delay:.1f}s", flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            sing.die(f"Сеть недоступна для {method} {path}: {e}")


def raw_page(path, key, query, max_count, offset):
    q = dict(query or {})
    q.update({"maxCount": max_count, "offset": offset, "paginationData": "true"})
    r = rq("GET", path, query=q)
    return r.get(key, []), (r.get("pagination") or {})


def full_scan(path, key, query, page=200):
    """Эталонная выборка: своя, максимально тупая, с отдельным учётом дублей.

    Нужна именно вторая независимая реализация — сверять paged() с самим собой
    бессмысленно.
    """
    seen, order, offset, pages = set(), [], 0, 0
    total = None
    while True:
        batch, pg = raw_page(path, key, query, page, offset)
        pages += 1
        if total is None:
            total = pg.get("total")
        for it in batch:
            order.append(it["id"])
            seen.add(it["id"])
        if not batch:
            break
        offset += page          # шаг по ЗАПРОШЕННОМУ окну, не по отданному
        if len(order) > 20000:
            break
    return order, seen, total, pages


# ------------------------------------------------------------------ команды


def cmd_probe(args):
    pid = args.project
    q = {"projectId": pid, "includeAllRecurrenceInstances": "true"}
    for mc in (5, 100, 200, 201, 500, 1000):
        batch, pg = raw_page("/task", "tasks", q, mc, 0)
        print(f"maxCount={mc:<5} вернул={len(batch):<5} pagination={pg}")
    print("\nбез paginationData:")
    r = rq("GET", "/task", query={**q, "maxCount": 5, "offset": 0})
    print("  ключи:", list(r.keys()), "задач:", len(r.get("tasks", [])))
    print("\noffset за пределом:")
    batch, pg = raw_page("/task", "tasks", q, 50, 100000)
    print(f"  offset=100000 -> вернул={len(batch)} pagination={pg}")


def cmd_create(args):
    projects = sing.all_projects()
    root = sing.resolve_root(projects)
    title = args.title if args.title.startswith(ZZ_PREFIX) else ZZ_PREFIX + args.title
    created = rq("POST", "/project", body={"title": title, "parent": root["id"]})
    proj = created.get("project", created)
    sing.assert_allowed(proj["id"], "черновой проект")
    print(f"{proj['id']}  {title}  (внутри «{root['title']}»)")


def _batch(ops, pause):
    """POST /batch пачкой.

    Тело — объект `{"operations": [...]}`, не голый массив, и не больше
    100 операций за раз (иначе 400 с перечислением всех индексов).
    """
    resp = rq("POST", "/batch", body={"operations": ops})
    time.sleep(pause)
    return resp


def cmd_fill(args):
    pid = args.project
    sing.assert_allowed(pid, "черновой проект")
    proj = rq("GET", f"/project/{pid}")
    title = (proj.get("project") or proj).get("title", "")
    if not title.startswith(ZZ_PREFIX):
        sing.die(f"{pid} называется «{title}» — заливать пачками можно только в zz-проект.")

    start = args.start
    made = []
    n, size = args.count, args.size
    t0 = time.time()
    for base in range(0, n, size):
        chunk = min(size, n - base)
        if args.mode == "plain":
            ids = []
            for i in range(chunk):
                t = rq("POST", "/task",
                       body={"title": f"{args.prefix}{start + base + i:05d}",
                             "projectId": pid})
                ids.append((t.get("task", t))["id"])
                time.sleep(args.pause)
        else:
            ops = [{"method": "POST", "path": "/v2/task",
                    "body": {"title": f"{args.prefix}{start + base + i:05d}",
                             "projectId": pid},
                    "tempId": f"tmp:t{base + i}"}
                   for i in range(chunk)]
            ids = _ids_from_batch(_batch(ops, args.pause))
        made.extend(ids)
        print(f"  создано {len(made)}/{n} (ответ отдал id: {len(ids)}/{chunk}), "
              f"{time.time() - t0:.0f}s", flush=True)
    print(f"создано задач: {len(made)}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(made, f)
        print(f"id записаны в {args.out}")


def _ids_from_batch(resp):
    """Из ответа /batch вытащить id созданных сущностей (форма варьируется)."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            i = x.get("id")
            if isinstance(i, str) and i[:2] in ("T-", "KT"):
                out.append(i)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(resp)
    return out


def cmd_link(args):
    """Поставить задачи проекта в колонку пачками (связка задача↔колонка)."""
    pid = args.project
    sing.assert_allowed(pid, "черновой проект")
    status_id = args.status or sing.system_status_id(pid, "todo")
    if not rq("GET", f"/kanban-status/{status_id}"):
        sing.die(f"колонка {status_id} не найдена")
    ids, _, _, _ = full_scan("/task", "tasks",
                             {"projectId": pid,
                              "includeAllRecurrenceInstances": "true"})
    # связка одна на задачу и её id детерминирован (KTS-<taskId>) — повторный
    # прогон не должен плодить дубли
    links, _, _, _ = full_scan("/kanban-task-status", "kanbanTaskStatuses",
                               {"statusId": status_id})
    have = {lid.replace("KTS-", "", 1) for lid in links}
    print(f"задач в проекте: {len(ids)}, связок уже есть: {len(links)}")
    todo = [i for i in ids if i not in have]
    if args.count:
        todo = todo[:args.count]
    made = 0
    t0 = time.time()
    for base in range(0, len(todo), args.size):
        chunk = todo[base:base + args.size]
        ops = [{"method": "POST", "path": "/v2/kanban-task-status",
                "body": {"taskId": t, "statusId": status_id},
                "tempId": f"tmp:l{base + i}"}
               for i, t in enumerate(chunk)]
        _batch(ops, args.pause)
        made += len(chunk)
        print(f"  связок {made}/{len(todo)}, {time.time() - t0:.0f}s", flush=True)
    print(f"создано связок: {made} в колонке {status_id}")


def _numbers(titles):
    """Номера из заголовков `pg-00042` — независимый от пагинации учёт объектов."""
    out = set()
    for t in titles:
        tail = (t or "").rsplit("-", 1)[-1]
        if tail.isdigit():
            out.add(int(tail))
    return out


def cmd_measure(args):
    pid = args.project
    q = {"projectId": pid, "includeAllRecurrenceInstances": "true"}

    print("=== /task ===")
    order, uniq, total, pages = full_scan("/task", "tasks", q)
    dups = len(order) - len(uniq)
    print(f"эталонный скан:  отдано строк={len(order)}  уникальных={len(uniq)}  "
          f"дублей={dups}  pagination.total={total}  страниц={pages}")

    got = sing.paged("/task", "tasks", q, limit=args.limit) if args.limit \
        else sing.paged("/task", "tasks", q)
    ids = [t["id"] for t in got]

    # третий, независимый от пагинации учёт: номера в заголовках
    ref_titles = {}
    for off in range(0, (total or 0) + 200, 200):
        batch, _ = raw_page("/task", "tasks", q, 200, off)
        for it in batch:
            ref_titles[it["id"]] = it.get("title", "")
        if not batch:
            break
    ref_nums = _numbers(ref_titles.values())
    got_nums = _numbers(t.get("title", "") for t in got)
    holes = sorted(ref_nums - got_nums)
    if ref_nums:
        lo, hi = min(ref_nums), max(ref_nums)
        print(f"номера в заголовках: диапазон {lo}..{hi}, "
              f"есть в фикстуре={len(ref_nums)}, вернул paged()={len(got_nums)}, "
              f"не вернул={len(holes)}"
              + (f" (напр. {holes[:5]})" if holes else ""))
    print(f"paged():         строк={len(ids)}  уникальных={len(set(ids))}  "
          f"дублей={len(ids) - len(set(ids))}")
    miss = uniq - set(ids)
    print(f"ГЕЙТ ПОЛНОТЫ:    paged() потерял {len(miss)} из {len(uniq)} "
          f"({100.0 * len(set(ids) & uniq) / max(len(uniq), 1):.1f}% покрытия)")
    if miss:
        print("  примеры потерянных:", sorted(miss)[:3])

    print("\n=== надстройки ===")
    print(f"fetch_tasks(): {len(sing.fetch_tasks(pid))}")
    print(f"live_tasks():  {len(sing.live_tasks(pid))}")
    print(f"open_tasks():  {len(sing.open_tasks(pid))}")
    cmap = sing.column_map(pid)
    print(f"column_map():  {len(cmap)} связок")
    for st in sing.project_statuses(pid):
        _, links_u, ltotal, _ = full_scan("/kanban-task-status",
                                          "kanbanTaskStatuses",
                                          {"statusId": st["id"]})
        live = sum(1 for v in cmap.values() if v == st["id"])
        mark = "OK " if live == len(links_u) else "РАСХОЖДЕНИЕ"
        print(f"  {mark} колонка «{st['name']}» {st['id']}: "
              f"эталон={len(links_u)} (total={ltotal})  column_map={live}")


def cmd_selftest(args):
    """paged() на заглушках: поведения сервера, которых на живом API не вызвать.

    Живой прогон показывает только то, что сервер делает СЕЙЧАС. Post-фильтр,
    сломанный offset и недобор проверяются заглушкой — иначе они всплывут
    на чужом проекте в неудачный момент.
    """
    import io
    import contextlib

    real = sing.request
    fails = []

    if args.old:
        # Контрольный замер: та же проверка против ПРЕЖНЕЙ реализации. Набор,
        # который прежний код проходит, ничего не доказывает про новый.
        def old_paged(path, key, query=None, limit=1000, page=None):
            items, offset = [], 0
            q = dict(query or {})
            while len(items) < limit:
                q.update({"maxCount": min(200, limit - len(items)),
                          "offset": offset, "paginationData": "true"})
                resp = sing.request("GET", path, query=q)
                batch = resp.get(key, [])
                items.extend(batch)
                total = (resp.get("pagination") or {}).get("total")
                offset += max(len(batch), 1)
                if not batch or (total is not None and offset >= total):
                    break
            return items

        sing.paged = old_paged
        print("  (прогон против ПРЕЖНЕЙ реализации paged)")

    def check(name, ok, detail=""):
        print(f"  {'OK ' if ok else 'ПРОВАЛ'} {name}{'  ' + detail if detail else ''}")
        if not ok:
            fails.append(name)

    # 1. Обычная выборка: 450 объектов страницами по 200.
    data = [{"id": f"X-{i}"} for i in range(450)]

    def srv_plain(method, path, query=None, body=None, soft=False):
        off, mc = int(query["offset"]), int(query["maxCount"])
        page = data[off:off + mc]
        return {"items": page, "pagination": {"total": len(data),
                                              "count": len(page), "offset": off}}

    sing.request = srv_plain
    got = sing.paged("/x", "items")
    check("450 объектов страницами по 200", len(got) == 450, f"вернул {len(got)}")

    # 2. Post-фильтр: сервер берёт окно, потом выбрасывает часть строк.
    #    total считается ДО фильтра, count выходит меньше maxCount.
    def srv_filtered(method, path, query=None, body=None, soft=False):
        off, mc = int(query["offset"]), int(query["maxCount"])
        page = [r for r in data[off:off + mc] if int(r["id"][2:]) % 3]
        return {"items": page, "pagination": {"total": len(data),
                                              "count": len(page), "offset": off}}

    sing.request = srv_filtered
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        got = sing.paged("/x", "items")
    expect = len([r for r in data if int(r["id"][2:]) % 3])
    check("post-фильтр: собраны все уцелевшие строки", len(got) == expect,
          f"вернул {len(got)}, ожидалось {expect}")
    check("post-фильтр: без дублей", len({r['id'] for r in got}) == len(got))
    check("недобор против total замечен вслух", "неполная" in buf.getvalue(),
          repr(buf.getvalue().strip()[:60]))

    # 3. Сервер игнорирует offset — раньше это был бы вечный цикл.
    def srv_stuck(method, path, query=None, body=None, soft=False):
        mc = int(query["maxCount"])
        return {"items": [{"id": f"S-{i}"} for i in range(mc)],
                "pagination": {"total": 10 ** 9, "count": mc, "offset": 0}}

    sing.request = srv_stuck
    died = {}

    def fake_die(msg, code=1):
        died["msg"] = msg
        raise SystemExit(code)

    real_die, sing.die = sing.die, fake_die
    try:
        sing.paged("/x", "items")
        check("сервер не двигает offset — выход по предохранителю", False,
              "цикл не прервался")
    except SystemExit:
        check("сервер не двигает offset — выход по предохранителю",
              "offset" in died.get("msg", ""), died.get("msg", "")[:60])
    finally:
        sing.die = real_die

    # 4. Явный limit по-прежнему режет (на нём стоит doctor).
    sing.request = srv_plain
    got = sing.paged("/x", "items", limit=5)
    check("явный limit=5 отдаёт ровно 5", len(got) == 5, f"вернул {len(got)}")

    sing.request = real
    print("ПРОВАЛОВ:", len(fails) or "нет")
    if fails:
        sys.exit(1)


def cmd_move(args):
    """Перекинуть N связок в другую колонку — чтобы счётчики на доске
    пришлось РАСКЛАДЫВАТЬ, а не просто повторять один общий итог."""
    links, _, _, _ = full_scan("/kanban-task-status", "kanbanTaskStatuses",
                               {"statusId": args.src})
    chosen = links[:args.count]
    print(f"в колонке-источнике связок: {len(links)}, переношу: {len(chosen)}")
    done = 0
    for base in range(0, len(chosen), args.size):
        chunk = chosen[base:base + args.size]
        ops = [{"method": "PATCH", "path": f"/v2/kanban-task-status/{lid}",
                "body": {"statusId": args.dst}} for lid in chunk]
        _batch(ops, args.pause)
        done += len(chunk)
        print(f"  перенесено {done}/{len(chosen)}", flush=True)
    print(f"перенесено связок: {done}")


def cmd_census(args):
    """Стабильность выборки: один и тот же скан несколько раз подряд.

    Нужен до любых выводов о paged(): если сама выборка плывёт между вызовами,
    сравнивать с ней бессмысленно — сначала надо понять, что плывёт.
    """
    pid = args.project
    q = {"projectId": pid, "includeAllRecurrenceInstances": "true"}
    prev = None
    for i in range(args.times):
        for page in args.pages:
            order, uniq, total, pages = full_scan("/task", "tasks", q, page=page)
            tag = f"[{i + 1}] page={page:<4}"
            print(f"{tag} строк={len(order):<5} уникальных={len(uniq):<5} "
                  f"дублей={len(order) - len(uniq):<4} total={total}")
            if prev is not None:
                gone, new = prev - uniq, uniq - prev
                if gone or new:
                    print(f"      относительно предыдущего скана: пропало={len(gone)} "
                          f"появилось={len(new)}")
                    if gone:
                        print("      пример пропавшего:", sorted(gone)[0])
            prev = uniq
        time.sleep(args.pause)
    # чем именно стал пропавший объект
    if args.inspect:
        t = sing.request("GET", f"/task/{args.inspect}", soft=True)
        print("\nGET по id пропавшего:", json.dumps(t, ensure_ascii=False)[:600])


def cmd_cleanup(args):
    projects = sing.all_projects()
    root = sing.resolve_root(projects)
    targets = [p for p in projects
               if p.get("title", "").startswith(ZZ_PREFIX)
               and any(x["id"] == root["id"]
                       for x in sing.project_chain(p["id"], projects))]
    # ⚠ Без --project снести можно ЧУЖУЮ фикстуру: `zz-` — общее пространство
    # имён, в нём одновременно работают несколько агентов. Один такой прогон уже
    # унёс чужие 253 задачи посреди замера.
    if args.project:
        targets = [p for p in targets if p["id"] == args.project] or [
            p for p in projects if p["id"] == args.project]
    elif len(targets) > 1 and not args.all:
        print("zz-проектов несколько — они могут быть не ваши:")
        for p in targets:
            print(f"  {p['id']}  {p['title']}")
        sing.die("Укажите свой: --project P-… (или --all, если точно все ваши).")
    if not targets:
        print("zz-проектов внутри «ИИ проекты» нет — удалять нечего.")
        return
    for p in targets:
        print(f"удаляю {p['id']}  {p['title']}")
        if not args.yes:
            print("  (сухой прогон, добавь --yes)")
            continue
        rq("DELETE", f"/project/{p['id']}")
    if not args.yes:
        return
    # проверка фактом: перечитать список и запросить проект по id
    time.sleep(2)
    left = [p for p in sing.all_projects()
            if p.get("title", "").startswith(ZZ_PREFIX)]
    for p in targets:
        gone = sing.request("GET", f"/project/{p['id']}", soft=True)
        alive = gone and not (gone.get("project", gone) or {}).get("removed")
        print(f"  GET /project/{p['id']} -> "
              f"{'ВСЁ ЕЩЁ ЖИВ' if alive else 'нет (удалён)'}")
        tasks, _, ttotal, _ = full_scan(
            "/task", "tasks",
            {"projectId": p["id"], "includeAllRecurrenceInstances": "true"})
        print(f"  задач в нём осталось: {len(tasks)} (total={ttotal})")
    mine = [p for p in left if p["id"] in {t["id"] for t in targets}]
    print(f"zz-проектов в трекере осталось: {len(left)}"
          + (f" (чужих: {len(left) - len(mine)})" if left else ""))
    if mine:
        sing.die("уборка неполная: " + ", ".join(p["id"] for p in mine))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe")
    sp.add_argument("--project", required=True)
    sp.set_defaults(fn=cmd_probe)

    sp = sub.add_parser("create")
    sp.add_argument("--title", required=True)
    sp.set_defaults(fn=cmd_create)

    sp = sub.add_parser("fill")
    sp.add_argument("--project", required=True)
    sp.add_argument("--count", type=int, required=True)
    sp.add_argument("--start", type=int, default=1)
    sp.add_argument("--size", type=int, default=50)
    sp.add_argument("--pause", type=float, default=0.7)
    sp.add_argument("--prefix", default="zz-task-")
    sp.add_argument("--mode", choices=["batch", "plain"], default="batch")
    sp.add_argument("--out")
    sp.set_defaults(fn=cmd_fill)

    sp = sub.add_parser("link")
    sp.add_argument("--project", required=True)
    sp.add_argument("--status")
    sp.add_argument("--count", type=int)
    sp.add_argument("--size", type=int, default=50)
    sp.add_argument("--pause", type=float, default=0.7)
    sp.set_defaults(fn=cmd_link)

    sp = sub.add_parser("measure")
    sp.add_argument("--project", required=True)
    sp.add_argument("--limit", type=int,
                    help="явный limit для paged() (по умолчанию — как в скилле)")
    sp.set_defaults(fn=cmd_measure)

    sp = sub.add_parser("selftest")
    sp.add_argument("--old", action="store_true",
                    help="прогнать набор против прежней реализации paged (контроль)")
    sp.set_defaults(fn=cmd_selftest)

    sp = sub.add_parser("move")
    sp.add_argument("--src", required=True)
    sp.add_argument("--dst", required=True)
    sp.add_argument("--count", type=int, required=True)
    sp.add_argument("--size", type=int, default=10)
    sp.add_argument("--pause", type=float, default=0.3)
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("census")
    sp.add_argument("--project", required=True)
    sp.add_argument("--times", type=int, default=3)
    sp.add_argument("--pages", type=int, nargs="+", default=[200])
    sp.add_argument("--pause", type=float, default=3.0)
    sp.add_argument("--inspect")
    sp.set_defaults(fn=cmd_census)

    sp = sub.add_parser("cleanup")
    sp.add_argument("--project")
    sp.add_argument("--all", action="store_true",
                    help="снести ВСЕ zz-проекты — только если все они точно ваши")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_cleanup)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
