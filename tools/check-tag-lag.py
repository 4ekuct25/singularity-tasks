#!/usr/bin/env python3
"""Перехват задачи на ЗАГРУЖЕННОЙ очереди синхронизации трекера. Живой API.

Проверка для карточки T-fc3a3096. Дефект зависит от нагрузки: на спокойной
очереди зеленеет и сломанный код, поэтому нагрузку надо создать самим, иначе
прогон «проверит» не то (правила проекта §4, §5.1).

    tools/check-tag-lag.py                       # текущий scripts/sing.py
    tools/check-tag-lag.py --sing /tmp/sing-old.py   # контрольный красный
    tools/check-tag-lag.py -r 3 --writers 8      # сильнее и дольше
    tools/check-tag-lag.py --keep                # не удалять черновик

Что делает: заводит черновой подпроект `zz-taglag`, в нём задачу, помечает её
ЧУЖИМ agent-тегом, разгоняет очередь синхронизации параллельными PATCH по
отдельным задачам — и на этом фоне зовёт `sing.py start ... --take-over`.

Что меряется, кроме кода возврата: осталась ли задача ВООБЩЕ БЕЗ agent-метки.
Это и есть худший исход — задача выглядит свободной, хотя её перехватывали;
он хуже честного отказа, и код возврата его не показывает.

Глубина очереди читается даром: `DELETE /tag/{id}` заведомо отвечает
`500 Sync error … number in queue N` (тег удалить нельзя, см. references/api.md),
и это N — и есть длина очереди. Пробой она не нагружает: запрос отвергнут.

⚠ Имена пробных агентов ФИКСИРОВАНЫ и переиспользуют уже заведённые теги
(`zz-first` / `zz-second`): удалить тег API не даёт, а имя «на каждый прогон»
навсегда засоряет общий на аккаунт список.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DRAFT = "zz-taglag"
HOLDER = "zz-first"      # тег уже есть в аккаунте — новых не заводим
TAKER = "zz-second"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sing = _load("sing", os.path.join(REPO, "scripts", "sing.py"))
zz = _load("zz_project", os.path.join(HERE, "zz-project.py"))


def queue_depth():
    """Длина очереди синхронизации или None. Запрос заведомо отвергается."""
    tags = sing.paged("/tag", "tags", limit=1)
    if not tags:
        return None
    req = urllib.request.Request(f"{sing.API}/tag/{tags[0]['id']}", method="DELETE")
    req.add_header("Authorization", "Bearer " + sing.get_token())
    try:
        urllib.request.urlopen(req, timeout=sing.NET_TIMEOUT).read()
        return None                      # вдруг починят — тогда проба негодна
    except urllib.error.HTTPError as e:
        hit = re.search(r"number in queue (\d+)", e.read().decode(errors="replace"))
        return int(hit.group(1)) if hit else None
    except Exception:                    # noqa: BLE001 — проба необязательна
        return None


class Load:
    """Параллельные записи в ту же очередь: каждая — PATCH по своей задаче.

    ⚠ Нагрузку надо держать НИЖЕ порога `429`, а не выше. Наотмашь (6 писателей
    без пауз) первым ложится сам измеритель: throttler отвергает его подготовку,
    и прогон падает, ничего не проверив. Нужна не частота запросов, а глубина
    очереди синхронизации — её даёт пауза между записями при нескольких
    писателях: очередь набирается, а лимит запросов не срабатывает.
    """

    def __init__(self, task_ids, pace=0.0):
        self.task_ids = task_ids
        self.pace = pace
        self.stop = threading.Event()
        self.writes = 0
        self._lock = threading.Lock()
        self.threads = []

    def _spin(self, tid):
        n = 0
        while not self.stop.is_set():
            n += 1
            sing.request("PATCH", f"/task/{tid}",
                         body={"title": f"zz-taglag: шум {n}"}, soft=True)
            with self._lock:
                self.writes += 1
            if self.pace:
                self.stop.wait(self.pace)

    def __enter__(self):
        for tid in self.task_ids:
            t = threading.Thread(target=self._spin, args=(tid,), daemon=True)
            t.start()
            self.threads.append(t)
        time.sleep(3.0)                  # дать очереди набраться до замера
        return self

    def __exit__(self, *a):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=sing.NET_TIMEOUT + 5)
        return False


def agent_marks(tid):
    task = sing.request("GET", f"/task/{tid}")
    return sorted(t for _, t in sing.agent_tags_on(task))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sing", default=os.path.join(REPO, "scripts", "sing.py"),
                    help="какую реализацию проверяем (для контрольного прогона)")
    ap.add_argument("-r", type=int, default=2, help="сколько перехватов подряд")
    ap.add_argument("--writers", type=int, default=4, help="параллельных писателей")
    ap.add_argument("--pace", type=float, default=0.4,
                    help="пауза писателя между записями: держит нагрузку ниже 429")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    proj, cols = zz.create_draft(sing, DRAFT, with_columns=True)
    print(f"черновой проект: {proj['id']}\nпроверяем: {args.sing}\n")
    workdir = os.path.join("/tmp", f"zz-taglag-{os.getpid()}")
    os.makedirs(os.path.join(workdir, ".agents"), exist_ok=True)
    with open(os.path.join(workdir, ".agents", "singularity.json"), "w") as f:
        json.dump({"projectId": proj["id"], "projectTitle": proj["title"],
                   "columns": cols, "columnNames": {r: r for r in cols}}, f)

    holder_tag = sing.ensure_tag(sing.AGENT_TAG_PREFIX + HOLDER)
    rows = []
    try:
        noise = [sing.request("POST", "/task",
                              body={"title": f"zz-taglag: шум {i}",
                                    "projectId": proj["id"]})["id"]
                 for i in range(args.writers)]
        # ⚠ Задачи готовятся ДО нагрузки, а не под ней: иначе throttler отвергает
        # подготовку измерителя, и прогон падает, не дойдя до перехвата.
        targets = []
        for run in range(1, args.r + 1):
            tid = sing.request("POST", "/task",
                               body={"title": f"zz-taglag: перехват {run}",
                                     "projectId": proj["id"]})["id"]
            sing.move_to_column(tid, cols["todo"])
            sing.set_task_tags(tid, add=[holder_tag])
            targets.append(tid)
        print(f"очередь до нагрузки: {queue_depth()}")

        with Load(noise, pace=args.pace) as load:
            depth = queue_depth()
            print(f"очередь под нагрузкой: {depth} ({args.writers} писателей, "
                  f"пауза {args.pace} с)\n")
            for run, tid in enumerate(targets, 1):
                p = subprocess.run(
                    [sys.executable, args.sing, "start", tid, "--take-over",
                     "--plan", "проверка перехвата под нагрузкой"],
                    cwd=workdir, capture_output=True, text=True,
                    env=dict(os.environ, SINGULARITY_AGENT=TAKER))
                marks = agent_marks(tid)
                rows.append((run, p.returncode, marks))
                verdict = ("ЗАХВАЧЕНА" if marks == [f"agent:{TAKER}"] and
                           p.returncode == 0 else
                           "БЕЗ МЕТКИ — выглядит свободной" if not marks else
                           "отказ, держатель на месте" if p.returncode else
                           "код 0, но метки не те")
                print(f"[{run}] код={p.returncode} метки={marks or '—'}  {verdict}")
                if p.returncode:
                    print("    " + (p.stderr.strip() or "—").replace("\n", "\n    "))
            print(f"\nочередь в конце: {queue_depth()} "
                  f"(шумовых записей: {load.writes})")

        took = sum(1 for _, c, m in rows if c == 0 and m == [f"agent:{TAKER}"])
        naked = sum(1 for _, _, m in rows if not m)
        print(f"\nперехватов удалось: {took} из {args.r}")
        print(f"задач осталось БЕЗ agent-метки: {naked} из {args.r}"
              + ("   ← так быть не должно ни при каком исходе" if naked else ""))
        print("\nИТОГ: " + ("ПЕРЕХВАТ ДЕРЖИТ НАГРУЗКУ" if took == args.r and not naked
                            else "ДЕФЕКТ ВОСПРОИЗВЕДЁН"))
        return 0 if took == args.r and not naked else 1
    finally:
        if args.keep:
            print(f"\nпроект оставлен: {proj['id']}")
        else:
            hit, left = zz.delete_draft(sing, proj["id"])
            print(f"\nчерновой проект удалён: {hit['id']} "
                  f"(черновиков zz-* осталось {left})")


if __name__ == "__main__":
    sys.exit(main())
