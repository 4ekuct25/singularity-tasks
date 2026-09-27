"""`sing.py ready`: сводка готовых к взятию по всем подпроектам. Заглушка, без токена.

Команда появилась после того, как тот же вопрос пять раз решали ручным обходом
`board --project`, и разбор дважды ошибся. Поэтому проверяются ровно те места,
где ошибался ручной обход, а не только «команда что-то печатает»:

  · закрытая задача, унесённая в дневник, но со связкой в «Новые» (живой случай
    20.09) — не готова, хотя её колонка todo и `notReady` у неё пустой;
  · готовность совпадает с `next`: первая готовая задача проекта и `next` в его
    репозитории — одна и та же задача;
  · недобор выборки не превращается в «готовых нет», а помечает проект, и код
    возврата ненулевой;
  · отказ на одном проекте не съедает сводку по остальным;
  · область прежняя: проект вне «ИИ проекты», сам корень и архивный подпроект
    в сводку не попадают;
  · связки с колонками общие на аккаунт — один запрос на команду, а не на проект.

Контрольный красный (обязателен): сломать фильтр в `ready_board` — например,
строить очередь из `tasks` вместо `live` — и убедиться, что краснеет
`test_closed_task_linked_to_todo_is_not_ready`.

Запуск: tests/run.py fast
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

ROOT = "P-root"
ALPHA, BETA, GAMMA = "P-alpha", "P-beta", "P-gamma"
OUTSIDE, ARCHIVED = "P-outside", "P-archived"
UNDER, BROKEN = "P-under", "P-broken"


def cols(pid, roles=("TODO", "IN-PROGRESS", "DONE")):
    """Системные колонки проекта: id детерминированный, как у живого трекера."""
    names = {"TODO": "Новые", "IN-PROGRESS": "В работе", "DONE": "Готово"}
    return [{"id": f"KS-{pid}-{r}", "name": names[r], "projectId": pid} for r in roles]


STATUSES = {
    ALPHA: cols(ALPHA) + [{"id": "KS-a-review", "name": "На проверке", "projectId": ALPHA},
                          {"id": "KS-a-blocked", "name": "Заблокировано",
                           "projectId": ALPHA}],
    BETA: cols(BETA),
    # без колонки очереди: так выглядит доска, где роль todo не найти
    GAMMA: cols(GAMMA, roles=("IN-PROGRESS", "DONE")),
    OUTSIDE: cols(OUTSIDE),
    ARCHIVED: cols(ARCHIVED),
    UNDER: cols(UNDER),
    ROOT: cols(ROOT),
}


def task(tid, pid, title, **kw):
    t = {"id": tid, "projectId": pid, "title": title, "checked": 0, "priority": 1}
    t.update(kw)
    return t


TASKS = [
    # alpha: обе готовые, порядок — по приоритету, как у next
    task("T-a-plain", ALPHA, "обычная готовая", createdDate="2026-09-01"),
    task("T-a-high", ALPHA, "высокий приоритет", priority=0, createdDate="2026-09-02"),
    # живой случай 20.09: закрыта, в дневнике, а связка всё ещё на «Новые»
    task("T-a-closed", ALPHA, "закрыта, но стоит в Новых", checked=1,
         journalDate="2026-09-14T10:00:00.000Z"),
    task("T-a-deferred", ALPHA, "отложенная", deferred=True),
    task("T-a-future", ALPHA, "с датой начала", start="2999-01-01T09:00:00.000Z"),
    task("T-a-recur", ALPHA, "шаблон серии", recurrence={"type": "weekly"}),
    task("T-a-parent", ALPHA, "родитель незакрытой подзадачи"),
    task("T-a-child", ALPHA, "подзадача", parent="T-a-parent"),
    # без связки — так приходит задача из приложения; приложение кладёт её в «Новые»
    task("T-a-orphan", ALPHA, "заведена в приложении"),
    task("T-a-wip", ALPHA, "в работе"),
    task("T-a-review", ALPHA, "на проверке"),
    task("T-a-blocked", ALPHA, "заблокирована"),
    # gamma: задачи есть, а колонки очереди нет
    task("T-g-wip", GAMMA, "в работе у гаммы"),
    # вне области: готовы, но в сводке их быть не должно
    task("T-out", OUTSIDE, "чужая готовая"),
    task("T-root", ROOT, "задача в самом корне"),
    task("T-arch", ARCHIVED, "в архивном подпроекте"),
    task("T-u", UNDER, "проект с недобором"),
]
LINKS = [
    {"id": "L1", "taskId": "T-a-plain", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L2", "taskId": "T-a-high", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L3", "taskId": "T-a-closed", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L4", "taskId": "T-a-deferred", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L5", "taskId": "T-a-future", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L6", "taskId": "T-a-recur", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L7", "taskId": "T-a-parent", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L8", "taskId": "T-a-child", "statusId": f"KS-{ALPHA}-TODO"},
    {"id": "L9", "taskId": "T-a-wip", "statusId": f"KS-{ALPHA}-IN-PROGRESS"},
    {"id": "L10", "taskId": "T-a-review", "statusId": "KS-a-review"},
    {"id": "L11", "taskId": "T-a-blocked", "statusId": "KS-a-blocked"},
    {"id": "L12", "taskId": "T-g-wip", "statusId": f"KS-{GAMMA}-IN-PROGRESS"},
    {"id": "L13", "taskId": "T-out", "statusId": f"KS-{OUTSIDE}-TODO"},
    {"id": "L14", "taskId": "T-root", "statusId": f"KS-{ROOT}-TODO"},
    {"id": "L15", "taskId": "T-arch", "statusId": f"KS-{ARCHIVED}-TODO"},
    {"id": "L16", "taskId": "T-u", "statusId": f"KS-{UNDER}-TODO"},
]
BASE_PROJECTS = [
    {"id": ROOT, "title": "ИИ проекты"},
    {"id": ALPHA, "title": "alpha", "parent": ROOT},
    {"id": BETA, "title": "beta", "parent": ROOT},
    {"id": GAMMA, "title": "gamma", "parent": ROOT},
    {"id": ARCHIVED, "title": "archived", "parent": ROOT,
     "journalDate": "2026-09-01T10:00:00.000Z"},
    {"id": OUTSIDE, "title": "outside"},
]


class Stub(BaseHTTPRequestHandler):
    """Только чтение. `extra` — проекты, которые включает отдельный тест:
    недобор и отказ ломают код возврата всей команды, в общий набор их нельзя."""

    protocol_version = "HTTP/1.1"
    extra = []
    hits = {}

    def log_message(self, *a):
        pass

    def _send(self, payload, code=200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _page(self, key, items, q, lie=0):
        off = int(q.get("offset", ["0"])[0])
        want = int(q.get("maxCount", ["200"])[0])
        self._send({key: items[off:off + want],
                    "pagination": {"total": len(items) + lie}})

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path, q = parts.path, urllib.parse.parse_qs(parts.query)
        Stub.hits[path] = Stub.hits.get(path, 0) + 1
        pid = (q.get("projectId") or [None])[0]
        if path == "/project":
            return self._page("projects", BASE_PROJECTS + list(Stub.extra), q)
        if path == "/kanban-status":
            if pid == BROKEN:
                return self._send({"message": "Internal error"}, 404)
            return self._page("kanbanStatuses", STATUSES.get(pid, []), q)
        if path == "/kanban-task-status":
            return self._page("kanbanTaskStatuses", LINKS, q)
        if path == "/task":
            items = [t for t in TASKS if t["projectId"] == pid
                     and (q.get("includeArchived") or not t.get("journalDate"))]
            # недобор: сервер обещает на одну задачу больше, чем отдаёт
            return self._page("tasks", items, q, lie=1 if pid == UNDER else 0)
        if path == "/tag":
            return self._page("tags", [], q)
        if path == "/task-group":
            return self._page("taskGroups", [], q)
        if path == "/checklist-item":
            return self._page("checklistItems", [], q)
        self._send({"error": f"нет такого пути: {path}"}, 404)


CORE_KEYS = {"id", "title", "column", "columnName", "project", "group", "groupTitle",
             "tags", "priority", "priorityName", "deadline", "done", "journal",
             "deferred", "start", "openChildren", "recurring", "notReady",
             "parent", "url"}


class ReadyTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.nowhere = tempfile.mkdtemp(prefix="zz-ready-unbound-")
        cls.addClassCleanup(shutil.rmtree, cls.nowhere, True)
        cls.alpha_repo = tempfile.mkdtemp(prefix="zz-ready-alpha-")
        cls.addClassCleanup(shutil.rmtree, cls.alpha_repo, True)
        os.makedirs(os.path.join(cls.alpha_repo, ".agents"))
        with open(os.path.join(cls.alpha_repo, ".agents", "singularity.json"), "w") as f:
            json.dump({"projectId": ALPHA, "projectTitle": "alpha",
                       "columns": {"todo": f"KS-{ALPHA}-TODO",
                                   "wip": f"KS-{ALPHA}-IN-PROGRESS",
                                   "review": "KS-a-review", "done": f"KS-{ALPHA}-DONE",
                                   "blocked": "KS-a-blocked"}}, f)

    def setUp(self):
        Stub.extra = []
        Stub.hits = {}

    def run_sing(self, *argv, cwd=None, code=0):
        env = support.clean_env(SINGULARITY_API=self.api, SINGULARITY_TOKEN="stub",
                                SINGULARITY_AGENT="zz-one",
                                SINGULARITY_NO_SYNC_CHECK="1",
                                SINGULARITY_NO_RATE_WAIT="1")
        r = subprocess.run([sys.executable, SING_UNDER_TEST, *argv],
                           cwd=cwd or self.nowhere, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, code,
                         f"sing.py {' '.join(argv)} -> {r.returncode}\n"
                         f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")
        return r

    def ready_json(self, code=0, cwd=None):
        r = self.run_sing("ready", "--json", code=code, cwd=cwd)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout `ready --json` — не JSON: {e}\n{r.stdout}\n{r.stderr}")

    def project(self, data, pid):
        hit = [p for p in data["projects"] if p["id"] == pid]
        self.assertEqual(len(hit), 1, f"{pid} в сводке {len(hit)} раз")
        return hit[0]

    def ready_ids(self, data, pid):
        return [t["id"] for t in self.project(data, pid)["ready"]]

    # --------------------------------------------------------- что считается готовым

    def test_closed_task_linked_to_todo_is_not_ready(self):
        """Ровно эта задача вышла «готовой» у ручного обхода 20.09."""
        self.assertNotIn("T-a-closed", self.ready_ids(self.ready_json(), ALPHA))

    def test_held_back_tasks_are_not_ready_but_are_counted(self):
        a = self.project(self.ready_json(), ALPHA)
        for tid in ("T-a-deferred", "T-a-future", "T-a-recur", "T-a-parent"):
            self.assertNotIn(tid, [t["id"] for t in a["ready"]])
        self.assertEqual(a["held"], {"отложены": 1, "с датой начала": 1,
                                     "шаблоны серий": 1, "ждут подзадач": 1})
        self.assertEqual(a["nextStart"], "2999-01-01")

    def test_ready_set_and_order_are_those_of_next(self):
        """Высокий приоритет первым; подзадача берётся, её родитель — нет; задача
        из приложения без связки стоит в очереди, как её показывает приложение."""
        ids = self.ready_ids(self.ready_json(), ALPHA)
        self.assertEqual(ids[0], "T-a-high")
        self.assertEqual(set(ids), {"T-a-high", "T-a-plain", "T-a-child", "T-a-orphan"})

    def test_first_ready_task_is_what_next_gives_in_that_repo(self):
        """Паритет с next проверяется вызовом next, а не повтором его логики в тесте."""
        nxt = json.loads(self.run_sing("next", "--json", cwd=self.alpha_repo).stdout)
        data = self.ready_json(cwd=self.alpha_repo)
        self.assertEqual(self.ready_ids(data, ALPHA)[0], nxt["id"])
        self.assertTrue(self.project(data, ALPHA)["bound"])

    def test_counts_of_the_rest_of_the_board(self):
        a = self.project(self.ready_json(), ALPHA)
        self.assertEqual(a["counts"], {"wip": 1, "review": 1, "blocked": 1})
        self.assertEqual(a["queue"], 8)

    # ------------------------------------------------------------------- область

    def test_scope_is_unchanged(self):
        data = self.ready_json()
        ids = {p["id"] for p in data["projects"]}
        self.assertEqual(ids, {ALPHA, BETA, GAMMA})
        self.assertEqual(data["archivedSkipped"], 1)
        human = self.run_sing("ready").stdout
        for foreign in ("outside", "задача в самом корне", "archived"):
            self.assertNotIn(foreign, human.replace("Архивных", ""))

    def test_board_without_a_queue_column_is_named_not_dropped(self):
        g = self.project(self.ready_json(), GAMMA)
        self.assertFalse(g["hasQueue"])
        self.assertIn("todo", g["unboundRoles"])
        self.assertIn("нет колонки очереди", self.run_sing("ready").stdout)

    # ------------------------------------------------------------ полнота и отказы

    def test_underfilled_project_is_marked_and_the_exit_code_says_so(self):
        Stub.extra = [{"id": UNDER, "title": "under", "parent": ROOT}]
        data = self.ready_json(code=1)
        self.assertFalse(data["complete"])
        self.assertFalse(self.project(data, UNDER)["complete"])
        self.assertTrue(self.project(data, ALPHA)["complete"])
        human = self.run_sing("ready", code=1).stdout
        line = next(l for l in human.splitlines() if "under" in l)
        self.assertIn("НЕПОЛНАЯ ВЫБОРКА", line)

    def test_one_broken_board_does_not_eat_the_others(self):
        Stub.extra = [{"id": BROKEN, "title": "broken", "parent": ROOT}]
        data = self.ready_json(code=1)
        self.assertTrue(self.project(data, BROKEN)["error"])
        self.assertTrue(self.ready_ids(data, ALPHA))
        self.assertIn("ОШИБКА", self.run_sing("ready", code=1).stdout)

    def test_clean_run_is_complete_with_exit_zero(self):
        data = self.ready_json()
        self.assertTrue(data["complete"])
        self.assertEqual(data["total"], sum(p["readyCount"] for p in data["projects"]))

    # ------------------------------------------------------------ цена и формат

    def test_links_are_fetched_once_not_per_project(self):
        self.ready_json()
        self.assertEqual(Stub.hits.get("/kanban-task-status"), 1, Stub.hits)
        self.assertEqual(Stub.hits.get("/kanban-status"), 3, Stub.hits)

    def test_json_task_objects_follow_the_common_contract(self):
        for t in self.project(self.ready_json(), ALPHA)["ready"]:
            self.assertEqual(set(t) - CORE_KEYS, set(), t)
            self.assertEqual(CORE_KEYS - set(t), set(), t)
            self.assertEqual(t["column"], "todo")
            self.assertEqual(t["columnName"], "Новые")
            self.assertIsNone(t["notReady"])


if __name__ == "__main__":
    unittest.main()
