"""`--json` у показывающих команд: один формат, чистый stdout. Заглушка, без токена.

Почему заглушка, а не живой трекер: проверять надо РЕДКИЕ состояния доски —
отложенную задачу, задачу с датой начала в будущем, родителя с незакрытой
подзадачей, колонку мимо привязки, роль без колонки. На живой доске они либо не
встречаются, либо их пришлось бы создавать и убирать за собой, а прогон подряд
трекер не держит (429, см. tests/run.py). Здесь они есть всегда — и проверка
может покраснеть.

Что именно проверяется:
  · stdout под `--json` разбирается `json.loads` ЦЕЛИКОМ (ни одной человеческой
    строки сверху) — ровно тот дефект, из-за которого сверку писали регексом;
  · объект задачи одинаков у list, show, board и next — поля сверяются между
    командами, а не только с ожиданием внутри одного теста;
  · каждая текстовая пометка человеческого вывода есть отдельным полем.

Контрольный красный (обязателен — проверка, которая не краснеет, не проверка):

    git show <base>:scripts/sing.py > /tmp/sing-old.py
    SING_UNDER_TEST=/tmp/sing-old.py tests/run.py fast

Запуск: tests/run.py fast
"""

import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

ROOT = "P-root"
PROJ = "P-zz"
COLS = {"todo": "KS-todo", "wip": "KS-wip", "review": "KS-review",
        "done": "KS-done", "blocked": "KS-blocked"}
COL_NAMES = {"KS-todo": "К работе", "KS-wip": "В работе", "KS-review": "На проверке",
             "KS-done": "Готово", "KS-blocked": "Заблокировано",
             "KS-side": "Колонка сбоку"}          # KS-side привязке неизвестна

GROUP = "Q-1"
TAGS = {"TG-a": "agent:zz-one", "TG-b": "срочно"}

# Заметка — Quill-дельта голым массивом, как её хранит приложение.
NOTE = json.dumps([{"insert": "постановка задачи\n"}], ensure_ascii=False)


def task(tid, title, **kw):
    t = {"id": tid, "title": title, "projectId": PROJ, "checked": 0,
         "priority": 1, "group": GROUP}
    t.update(kw)
    return t


# Доска с намеренно редкими состояниями: без них проверять `--json` не на чем.
TASKS = [
    task("T-plain", '<a href="http://x.md">карточка со ссылкой</a>', priority=0,
         deadline="2026-10-15T12:00:00.000Z", tags=["TG-b", "TG-a"], note=NOTE),
    task("T-deferred", "отложенная", deferred=True),
    task("T-future", "с датой начала", start="2999-01-01T09:00:00.000Z"),
    task("T-parent", "родитель незакрытой подзадачи"),
    task("T-child", "подзадача", parent="T-parent"),
    task("T-recur", "шаблон серии", recurrence={"type": "daily"}),
    task("T-side", "в колонке мимо привязки"),
    # без связки вовсе — так приходит задача, заведённая в приложении: при полной
    # привязке она видна в «Новые», при неполной оказывается вне колонок
    task("T-orphan", "заведена в приложении, связки нет"),
    task("T-done", "закрытая и унесённая в дневник", checked=1,
         journalDate="2026-09-01T10:00:00.000Z"),
    task("T-note", "это заметка проекта, а не задача", isNote=True),
]
LINKS = [
    {"id": "KTS-1", "taskId": "T-plain", "statusId": "KS-todo"},
    {"id": "KTS-2", "taskId": "T-deferred", "statusId": "KS-todo"},
    {"id": "KTS-3", "taskId": "T-future", "statusId": "KS-todo"},
    {"id": "KTS-4", "taskId": "T-parent", "statusId": "KS-todo"},
    {"id": "KTS-5", "taskId": "T-child", "statusId": "KS-todo"},
    {"id": "KTS-6", "taskId": "T-recur", "statusId": "KS-todo"},
    {"id": "KTS-7", "taskId": "T-side", "statusId": "KS-side"},
    {"id": "KTS-8", "taskId": "T-done", "statusId": "KS-done"},
    # связка с системной доской «Сегодня» — её показывать нельзя ни там, ни там
    {"id": "KTS-9", "taskId": "T-plain", "statusId": "KS-P-TODAY-TODO"},
]
CHECKLIST = [
    {"id": "CH-1", "parent": "T-plain", "title": "первый пункт", "done": True,
     "parentOrder": 0},
    {"id": "CH-2", "parent": "T-plain", "title": "второй пункт", "done": False,
     "parentOrder": 1},
]
PROJECTS = [
    {"id": ROOT, "title": "ИИ проекты"},
    {"id": PROJ, "title": "zz-json", "parent": ROOT},
]


class TrackerStub(BaseHTTPRequestHandler):
    """Только чтение: `--json` ничего не меняет, кроме `groups --create`."""

    protocol_version = "HTTP/1.1"
    created_groups = []

    def log_message(self, *a):
        pass

    def _send(self, payload, code=200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _page(self, key, items, q):
        off = int(q.get("offset", ["0"])[0])
        want = int(q.get("maxCount", ["200"])[0])
        self._send({key: items[off:off + want], "pagination": {"total": len(items)}})

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path, q = parts.path, urllib.parse.parse_qs(parts.query)
        if path.startswith("/task/"):
            tid = path[len("/task/"):]
            hit = next((t for t in TASKS if t["id"] == tid), None)
            return self._send(hit or {"error": "нет задачи"}, 200 if hit else 404)
        if path == "/task":
            # `includeArchived` — не косметика: без него сервер не отдаёт задачи
            # с journalDate, и `board` не увидит закрытых (см. fetch_tasks).
            items = [t for t in TASKS
                     if q.get("includeArchived") or not t.get("journalDate")]
            if q.get("projectId"):
                items = [t for t in items if t["projectId"] == q["projectId"][0]]
            return self._page("tasks", items, q)
        if path == "/project":
            return self._page("projects", PROJECTS, q)
        if path == "/kanban-status":
            pid = (q.get("projectId") or [PROJ])[0]
            items = [{"id": cid, "name": name, "projectId": pid}
                     for cid, name in COL_NAMES.items()] if pid == PROJ else []
            return self._page("kanbanStatuses", items, q)
        if path == "/kanban-task-status":
            items = LINKS
            if q.get("taskId"):
                items = [l for l in LINKS if l["taskId"] == q["taskId"][0]]
            return self._page("kanbanTaskStatuses", items, q)
        if path == "/tag":
            return self._page("tags", [{"id": i, "title": t}
                                       for i, t in TAGS.items()], q)
        if path == "/task-group":
            items = [{"id": GROUP, "title": "Раздел A", "parent": PROJ,
                      "parentOrder": 0}] + list(self.created_groups)
            return self._page("taskGroups", items, q)
        if path == "/checklist-item":
            items = [c for c in CHECKLIST
                     if not q.get("parent") or c["parent"] == q["parent"][0]]
            return self._page("checklistItems", items, q)
        self._send({"error": f"нет такого пути: {path}"}, 404)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/task-group":
            return self._send({"error": "нет такого пути"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        gid = f"Q-new{len(self.created_groups) + 1}"
        self.created_groups.append({"id": gid, "title": body["title"],
                                    "parent": PROJ, "parentOrder": 9})
        self._send({"id": gid})


# Ключи объекта задачи перечислены здесь ВТОРЫМ списком, отдельно от sing.py.
# Так и задумано: контракт машинного вывода должен ломаться заметно. Сверка с
# `sing.JSON_*` из того же файла подтверждала бы сама себя.
CORE_KEYS = {"id", "title", "column", "columnName", "project", "group", "groupTitle",
             "tags", "priority", "priorityName", "deadline", "done", "journal",
             "deferred", "start", "openChildren", "recurring", "notReady",
             "parent", "url"}


class JsonOutputTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), TrackerStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.repo = cls._repo("bound", dict(COLS))
        # Вторая привязка — БЕЗ роли todo: так выглядит доска с неполной
        # привязкой, и только на ней видно блоки «ролей без колонки» и
        # «вне колонок». С полной привязкой эти ветки недостижимы.
        cls.repo_partial = cls._repo("partial",
                                     {k: v for k, v in COLS.items() if k != "todo"})

    @classmethod
    def _repo(cls, name, columns):
        import tempfile
        d = tempfile.mkdtemp(prefix=f"zz-json-{name}-")
        cls.addClassCleanup(__import__("shutil").rmtree, d, True)
        os.makedirs(os.path.join(d, ".agents"))
        with open(os.path.join(d, ".agents", "singularity.json"), "w") as f:
            json.dump({"projectId": PROJ, "projectTitle": "zz-json",
                       "columns": columns}, f)
        return d

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_sing(self, *argv, cwd=None, code=0):
        env = support.clean_env(SINGULARITY_API=self.api,
                                SINGULARITY_TOKEN="stub",
                                SINGULARITY_AGENT="zz-one",
                                SINGULARITY_NO_SYNC_CHECK="1")
        r = subprocess.run([sys.executable, SING_UNDER_TEST, *argv],
                           cwd=cwd or self.repo, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, code,
                         f"sing.py {' '.join(argv)} -> {r.returncode}\n{r.stderr}")
        return r

    def parsed(self, *argv, **kw):
        """stdout ЦЕЛИКОМ как JSON. Ровно то, чего не хватало: одна человеческая
        строка сверху — и вызывающий получает не данные, а исключение."""
        r = self.run_sing(*argv, **kw)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout `sing.py {' '.join(argv)} --json` — не JSON: {e}\n"
                      f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")

    def by_id(self, items):
        return {t["id"]: t for t in items}

    # ------------------------------------------------------------- один формат
    def test_list_json_parses_whole_stdout(self):
        items = self.parsed("list", "--json")
        self.assertTrue(items, "список задач в колонке todo пуст — проверять нечего")

    def test_every_task_has_every_core_key(self):
        """Ключи есть всегда, даже пустые: «то есть, то нет» заставляет
        обвешивать проверкой каждое обращение — от этого и уходили."""
        for t in self.parsed("list", "--json"):
            self.assertEqual(CORE_KEYS - set(t), set(),
                             f"в {t.get('id')} не хватает полей")

    def test_show_and_list_describe_the_same_task_identically(self):
        """Главное требование карточки: формат ОДИН. Сверяются две команды между
        собой, а не каждая со своим ожиданием."""
        from_list = self.by_id(self.parsed("list", "--json"))["T-plain"]
        from_show = self.parsed("show", "T-plain", "--json")
        for key in sorted(CORE_KEYS):
            self.assertEqual(from_show[key], from_list[key],
                             f"поле {key} у show и list разное")

    def test_board_describes_the_same_task_identically_too(self):
        from_list = self.by_id(self.parsed("list", "--json"))["T-plain"]
        board = self.parsed("board", "--json")
        todo = next(c for c in board["columns"] if c["role"] == "todo")
        from_board = self.by_id(todo["tasks"])["T-plain"]
        for key in sorted(CORE_KEYS):
            self.assertEqual(from_board[key], from_list[key],
                             f"поле {key} у board и list разное")

    def test_next_returns_the_task_object_with_note_and_checklist(self):
        t = self.parsed("next", "--json")
        self.assertEqual(CORE_KEYS - set(t), set())
        self.assertEqual(t["id"], "T-plain", "очередь отдала не верхнюю задачу")
        self.assertIn("постановка задачи", t["note"])
        self.assertEqual([(c["n"], c["done"]) for c in t["checklist"]],
                         [(1, True), (2, False)])

    # ------------------------------------------- пометки человека -> поля машины
    def test_marks_from_human_output_are_fields(self):
        items = self.by_id(self.parsed("list", "--json"))
        self.assertTrue(items["T-deferred"]["deferred"])
        self.assertEqual(items["T-future"]["start"], "2999-01-01")
        self.assertEqual(items["T-parent"]["openChildren"], 1)
        self.assertEqual(items["T-child"]["openChildren"], 0)
        self.assertTrue(items["T-recur"]["recurring"])
        for tid in ("T-deferred", "T-future", "T-parent", "T-recur"):
            self.assertTrue(items[tid]["notReady"],
                            f"{tid}: причина «пока брать нельзя» потеряна")
        self.assertIsNone(items["T-plain"]["notReady"])

    def test_done_and_journal_are_fields(self):
        board = self.parsed("board", "--json")
        done_col = next(c for c in board["columns"] if c["role"] == "done")
        t = self.by_id(done_col["tasks"])["T-done"]
        self.assertTrue(t["done"])
        self.assertTrue(t["journal"], "«(в дневнике)» не стало полем")

    def test_tags_are_titles_and_sorted(self):
        t = self.by_id(self.parsed("list", "--json"))["T-plain"]
        self.assertEqual(t["tags"], ["agent:zz-one", "срочно"])

    def test_no_human_decorations_in_values(self):
        """Ни HTML из заголовка, ни «!» у высокого приоритета: украшения — в
        человеческом выводе, в машинном они мусор."""
        t = self.by_id(self.parsed("list", "--json"))["T-plain"]
        self.assertEqual(t["title"], "карточка со ссылкой")
        self.assertEqual((t["priority"], t["priorityName"]), (0, "высокий"))
        self.assertEqual(t["group"], GROUP)
        self.assertEqual(t["groupTitle"], "Раздел A")

    def test_column_is_the_role_and_the_name_lies_next_to_it(self):
        t = self.by_id(self.parsed("list", "--json"))["T-plain"]
        self.assertEqual((t["column"], t["columnName"]), ("todo", "К работе"))

    def test_column_outside_the_binding_has_a_name_but_no_role(self):
        """Колонка есть, роли нет — это разные вещи, и по JSON они различимы."""
        t = self.parsed("show", "T-side", "--json")
        self.assertIsNone(t["column"])
        self.assertEqual(t["columnName"], "Колонка сбоку")

    # --------------------------------------------------------------- структура
    def test_board_json_keeps_the_warnings_as_data(self):
        board = self.parsed("board", "--json")
        self.assertEqual(board["project"]["id"], PROJ)
        self.assertEqual([c["role"] for c in board["columns"]],
                         ["todo", "wip", "review", "done", "blocked"])
        side = self.by_id(board["unknownColumns"])["KS-side"]
        self.assertEqual((side["name"], side["count"]), ("Колонка сбоку", 1))
        self.assertEqual(board["unboundRoles"], [])
        todo = next(c for c in board["columns"] if c["role"] == "todo")
        self.assertEqual(todo["count"], len(todo["tasks"]))

    def test_board_json_names_the_unbound_role_and_the_loose_tasks(self):
        board = self.parsed("board", "--json", cwd=self.repo_partial)
        self.assertEqual(board["unboundRoles"], ["todo"])
        todo = next(c for c in board["columns"] if c["role"] == "todo")
        self.assertFalse(todo["bound"])
        # count=null, а не 0: про непривязанную роль доска не знает НИЧЕГО, и
        # «0» читалось бы как «в колонке пусто»
        self.assertIsNone(todo["count"])
        self.assertTrue(board["looseTasks"], "задачи вне колонок не показаны")
        for t in board["looseTasks"]:
            self.assertIsNone(t["column"])
            self.assertEqual(CORE_KEYS - set(t), set())

    def test_groups_json(self):
        out = self.parsed("groups", "--json")
        self.assertEqual(out["groups"][0]["id"], GROUP)
        self.assertEqual(out["groups"][0]["openTasks"],
                         len([t for t in TASKS if t["group"] == GROUP
                              and not t.get("checked") and not t.get("isNote")]))
        self.assertEqual(out["outsideGroups"], 0)

    def test_projects_json_is_a_contract_not_an_api_dump(self):
        items = self.parsed("projects", "--json")
        self.assertEqual([p["id"] for p in items], [PROJ])
        self.assertEqual(set(items[0]), {"id", "title", "parent", "depth", "archived"})

    # ------------------------------------------------- stdout только для машины
    def test_empty_queue_is_null_in_stdout_and_words_in_stderr(self):
        r = self.run_sing("next", "--column", "blocked", "--json", code=2)
        self.assertEqual(json.loads(r.stdout), None)
        self.assertIn("Свободных задач нет", r.stderr,
                      "объяснение пропало вовсе — человеку не на что смотреть")
        self.assertNotIn("Свободных", r.stdout)

    def test_created_group_is_confirmed_by_rereading_not_by_a_line_in_stdout(self):
        out = self.parsed("groups", "--create", "zz раздел из теста", "--json")
        self.assertIn("zz раздел из теста", [g["title"] for g in out["groups"]],
                      "создание не подтверждено перечитыванием списка")

    def test_human_output_still_looks_human(self):
        """`--json` не должен был подменить обычный вывод: его читают глазами."""
        r = self.run_sing("list")
        self.assertIn("T-plain", r.stdout)
        self.assertIn("[отложена]", r.stdout)
        self.assertIn("#agent:zz-one", r.stdout)


if __name__ == "__main__":
    unittest.main()
