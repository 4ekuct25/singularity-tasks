#!/usr/bin/env python3
"""Воспроизвести гонку: несколько агентов одновременно берут одну задачу.

Проверка для карточки «Закрыть гонку при параллельном взятии одной задачи».
Живой API, черновой подпроект внутри «ИИ проекты», уборка за собой.

    tools/check-claim-race.py            # 2 агента
    tools/check-claim-race.py -n 4       # больше
    tools/check-claim-race.py --keep     # не удалять проект (для разбора)

Годный результат ПОСЛЕ починки: захват ровно один, на задаче ровно один
`agent:*` тег, остальные вышли с кодом 1 и внятной причиной.
Годный результат ДО починки: захватов больше одного — гонка воспроизведена.

Почему именно так: «проверка» одним последовательным вызовом гонку показать не
может в принципе. Процессы стартуют по общей стенной метке времени, иначе первый
успевает закончить раньше, чем второй начнёт, и тест молча зеленеет.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SING = os.path.join(HERE, "..", "scripts", "sing.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import sing  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Черновой проект заводится и сносится одним кодом на все проверки — иначе
# «создай проект и добери колонки» живёт в трёх копиях и разъезжается.
zz = _load("zz_project", os.path.join(HERE, "zz-project.py"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2, help="сколько агентов на одну задачу")
    ap.add_argument("--keep", action="store_true", help="не удалять черновой проект")
    args = ap.parse_args()

    proj, cols = zz.create_draft(sing, "zz-race", with_columns=True)
    print(f"черновой проект: {proj['id']}")
    workdir = tempfile.mkdtemp(prefix="zz-race-")
    os.makedirs(os.path.join(workdir, ".agents"), exist_ok=True)
    with open(os.path.join(workdir, ".agents", "singularity.json"), "w") as f:
        json.dump({"projectId": proj["id"], "projectTitle": proj["title"],
                   "columns": cols, "columnNames": {r: r for r in cols}}, f)

    try:
        task = sing.request("POST", "/task",
                            body={"title": "zz-race: одна задача на всех",
                                  "projectId": proj["id"]})
        tid = task["id"]
        sing.move_to_column(tid, cols["todo"])
        print(f"задача: {tid}\n")

        # общая метка старта: без неё первый успевает закончить до старта второго
        go = time.time() + 3.0
        procs = []
        for i in range(args.n):
            who = f"zz-agent-{i}"
            env = dict(os.environ, SINGULARITY_AGENT=who)
            cmd = (f"python3 -c \"import time;time.sleep(max(0,{go}-time.time()))\" && "
                   f"python3 {SING} start {tid} --plan 'гонка, агент {who}'")
            procs.append((who, subprocess.Popen(cmd, shell=True, cwd=workdir, env=env,
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True)))
        results = []
        for who, p in procs:
            out, _ = p.communicate()
            results.append((who, p.returncode, out.strip()))

        won = [r for r in results if r[1] == 0]
        for who, code, out in sorted(results):
            print(f"[{who}] код={code}\n    " + out.replace("\n", "\n    "))

        fresh = sing.request("GET", f"/task/{tid}")
        titles = {t["id"]: t["title"] for t in sing.paged("/tag", "tags")}
        marks = [titles.get(x, x) for x in (fresh.get("tags") or [])]
        agent_marks = [m for m in marks if m.startswith(sing.AGENT_TAG_PREFIX)]
        print(f"\nзахватов (код 0): {len(won)} из {args.n}")
        print(f"agent-тегов на задаче: {len(agent_marks)} {agent_marks}")
        print(f"колонка задачи: {sing.task_column(tid)}")
        ok = len(won) == 1 and len(agent_marks) == 1
        print("\nИТОГ: " + ("ГОНКА ЗАКРЫТА — ровно один захват" if ok else
                            "ГОНКА ВОСПРОИЗВЕДЕНА — захват не единственный"))
        return 0 if ok else 1
    finally:
        if args.keep:
            print(f"\nпроект оставлен: {proj['id']}")
        else:
            sing.request("DELETE", f"/project/{proj['id']}", soft=True)
            gone = sing.request("GET", f"/project/{proj['id']}", soft=True) is None
            print(f"\nчерновой проект удалён: {gone}")


if __name__ == "__main__":
    sys.exit(main())
