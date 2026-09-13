#!/usr/bin/env python3
"""Детектор пропажи agent-тегов: снимок состояния и сравнение с предыдущим.

Зачем. Наблюдалось: 16 карточек из 23 разом остались без `agent:*`. Установлено,
что писал не `sing.py` (он правит по одной задаче, а девять карточек получили
одну и ту же метку времени) и что это не общеаккаунтный процесс (в соседнем
проекте теги уцелели). Кто писал — не установлено: локальная база приложения
отдаёт только суточные снапшоты, а событие было днём. Реконструировать задним
числом больше нечего — повтор надо ПОЙМАТЬ.

    tools/watch-tags.py snapshot      # снять базовую линию
    tools/watch-tags.py diff          # что изменилось с прошлого снимка
    tools/watch-tags.py watch -i 300  # цикл: снимать и печатать изменения
    tools/watch-tags.py selftest      # контрольный прогон: детектор обязан краснеть

⚠ ТОЛЬКО ЧТЕНИЕ рабочих данных. Детектор, который сам пишет в трекер,
подмешивает свои правки в то, что измеряет. Единственное исключение —
`selftest`: он заводит СВОЙ черновой проект, портит тег там и убирает за собой.

Состояние снимка — JSON вне репозитория (это данные, а не код), по умолчанию
`~/.singularity-tagwatch/state.json`.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import sing  # noqa: E402

DEFAULT_STATE = os.path.expanduser("~/.singularity-tagwatch/state.json")

# Процесс приложения — единственная зацепка к «кто писал», доступная снаружи:
# сам факт правки API не подписывает. Совпадение «приложение запущено» с пачкой
# одинаковых меток времени — не доказательство, но это ровно то наблюдение,
# которого не хватило, чтобы закрыть вопрос в прошлый раз.
APP_HINT = "ru.sibirix.singularitydesktop"


def app_running():
    try:
        out = subprocess.run(["pgrep", "-fl", APP_HINT], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.strip())


def scope_projects():
    """Подпроекты «ИИ проекты» — область скилла, шире не смотрим."""
    projects = sing.all_projects()
    root = sing.resolve_root(projects)
    return [p for p in projects
            if p["id"] != root["id"]
            and any(x["id"] == root["id"]
                    for x in sing.project_chain(p["id"], projects))]


def take_snapshot():
    titles = {t["id"]: t.get("title") or "" for t in sing.paged("/tag", "tags")}
    tasks = {}
    for p in scope_projects():
        for t in sing.board_tasks(p["id"]):
            tasks[t["id"]] = {
                "project": p.get("title"),
                "title": sing.plain(t.get("title") or "")[:70],
                "mod": str(t.get("modificatedDate") or ""),
                "tags": sorted(titles.get(x, x) for x in (t.get("tags") or [])),
            }
    return {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "app_running": app_running(),
            "tasks": tasks}


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_state(path, snap):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)


def compare(old, new):
    """Что изменилось в тегах. Считаем и пропажу, и появление: без второго
    нельзя отличить «сняли» от «переставили на другой тег»."""
    changes = []
    for tid, now in new["tasks"].items():
        was = old["tasks"].get(tid)
        if was is None:
            continue                       # новая задача — не изменение тегов
        lost = [t for t in was["tags"] if t not in now["tags"]]
        gained = [t for t in now["tags"] if t not in was["tags"]]
        if lost or gained:
            changes.append({"id": tid, "title": now["title"],
                            "project": now["project"], "lost": lost,
                            "gained": gained, "mod": now["mod"],
                            "mod_before": was["mod"]})
    gone = [tid for tid in old["tasks"] if tid not in new["tasks"]]
    return changes, gone


def report(old, new, changes, gone):
    print(f"было:  {old['at']}  (приложение запущено: {old.get('app_running')})")
    print(f"стало: {new['at']}  (приложение запущено: {new.get('app_running')})")
    print(f"задач в области: {len(new['tasks'])}, исчезло из выборки: {len(gone)}")
    if not changes:
        print("\nИЗМЕНЕНИЙ В ТЕГАХ НЕТ")
        return 0
    losses = [c for c in changes if c["lost"]]
    print(f"\n⚠ ИЗМЕНЕНИЙ: {len(changes)}, из них с ПОТЕРЕЙ тега: {len(losses)}")
    for c in sorted(changes, key=lambda x: x["mod"]):
        mark = "ПОТЕРЯ " if c["lost"] else "прибыло"
        print(f"  [{mark}] {c['id'][:14]}  mod {c['mod_before']} → {c['mod']}")
        if c["lost"]:
            print(f"      снято:  {', '.join(c['lost'])}")
        if c["gained"]:
            print(f"      добавлено: {', '.join(c['gained'])}")
        print(f"      «{c['title']}» [{c['project']}]")

    # Подпись пакетной записи: sing.py правит по одной задаче, поэтому несколько
    # задач с ОДНОЙ меткой времени означают, что писал кто-то другой.
    batches = {m: n for m, n in Counter(c["mod"] for c in changes).items() if n > 1}
    if batches:
        print("\n⚠ ПАКЕТНАЯ ЗАПИСЬ — несколько задач с одной меткой времени:")
        for m, n in sorted(batches.items()):
            print(f"      {m}: {n} задач  ← sing.py так не пишет")
    return 1 if losses else 0


def cmd_snapshot(args):
    snap = take_snapshot()
    save_state(args.state, snap)
    with_tags = sum(1 for t in snap["tasks"].values() if t["tags"])
    print(f"снимок {snap['at']}: задач {len(snap['tasks'])}, с тегами {with_tags}"
          f", приложение запущено: {snap['app_running']}")
    print(f"сохранён: {args.state}")
    return 0


def cmd_diff(args):
    old = load_state(args.state)
    if not old:
        sys.exit(f"нет базовой линии: {args.state}\nснять: watch-tags.py snapshot")
    new = take_snapshot()
    code = report(old, new, *compare(old, new))
    if args.update:
        save_state(args.state, new)
    return code


def cmd_watch(args):
    if not load_state(args.state):
        cmd_snapshot(args)
    print(f"слежу с интервалом {args.interval} с, Ctrl-C — выход\n")
    worst = 0
    try:
        while True:
            time.sleep(args.interval)
            old, new = load_state(args.state), take_snapshot()
            changes, gone = compare(old, new)
            if changes:
                print("=" * 70)
                worst = max(worst, report(old, new, changes, gone))
                save_state(args.state, new)
            else:
                print(f"{new['at']}  тихо ({len(new['tasks'])} задач, "
                      f"приложение: {new['app_running']})")
                save_state(args.state, new)
    except KeyboardInterrupt:
        print("\nостановлено")
    return worst


def cmd_selftest(args):
    """Детектор обязан КРАСНЕТЬ. Молчащий неотличим от исправного, поэтому
    пропажу воспроизводим руками на своём черновике и проверяем, что видно."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "zz", os.path.join(HERE, "zz-project.py"))
    zz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zz)

    proj, _ = zz.create_draft(sing, "zz-tagwatch", with_columns=False)
    print(f"черновой проект: {proj['id']}")
    try:
        task = sing.request("POST", "/task", body={"title": "zz-tagwatch: проба",
                                                   "projectId": proj["id"]})
        tid = task["id"]
        tag = sing.ensure_tag(sing.AGENT_TAG_PREFIX + "zz-tagwatch")
        sing.set_task_tags(tid, add=[tag])
        before = take_snapshot()
        assert before["tasks"].get(tid, {}).get("tags"), "проба завелась без тега"
        print(f"проба {tid} с тегом, снимок снят")

        sing.set_task_tags(tid, drop=[tag])          # воспроизводим пропажу
        after = take_snapshot()
        changes, gone = compare(before, after)
        code = report(before, after, changes, gone)
        lost = [c for c in changes if c["id"] == tid and c["lost"]]
        print("\nИТОГ: " + ("ДЕТЕКТОР КРАСНЕЕТ — пропажа видна"
                            if lost and code == 1 else
                            "ДЕТЕКТОР МОЛЧИТ — он бесполезен, чинить"))
        return 0 if lost and code == 1 else 1
    finally:
        zz.delete_draft(sing, proj["id"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=DEFAULT_STATE, help="файл снимка")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="снять базовую линию").set_defaults(fn=cmd_snapshot)
    sp = sub.add_parser("diff", help="сравнить с сохранённым снимком")
    sp.add_argument("--update", action="store_true", help="обновить снимок после сравнения")
    sp.set_defaults(fn=cmd_diff)
    sp = sub.add_parser("watch", help="цикл слежения")
    sp.add_argument("-i", "--interval", type=int, default=300, help="секунд между снимками")
    sp.set_defaults(fn=cmd_watch)
    sub.add_parser("selftest", help="контрольный прогон: детектор обязан краснеть"
                   ).set_defaults(fn=cmd_selftest)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
