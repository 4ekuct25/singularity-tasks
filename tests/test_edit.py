"""Команды, которые ПРАВЯТ карточку, на заглушке трекера. Без сети и без токена.

Почему заглушка, а не живой трекер: проверять надо то, ради чего эти команды и
написаны, — что PATCH не задел соседние поля. На живом трекере такой дефект
воспроизводится только настоящей порчей данных: чтобы увидеть «заметка пропала»,
её надо потерять. Здесь сервер можно заставить терять заметку или двигать
карточку по доске — и убедиться, что команда это ЛОВИТ и отказывается, а не
рапортует успехом.

Заглушка отвечает так же, как живой API (замер 2026-09-19, задача zz-regroup):
  · `PATCH {"group": null}` и `{"group": ""}` -> `400 Must start with one of: "Q-"`;
  · `{"group": "Q-нет-такой"}` -> `400 Task group not found`;
  · «вне секций» — это id безымянной служебной fake-группы проекта, а не пусто:
    только что созданная задача уже лежит в ней.

Контрольный красный (обязателен — проверка, которая не краснеет, не проверка):

    git show <base>:scripts/sing.py > /tmp/sing-old.py
    SING_UNDER_TEST=/tmp/sing-old.py python3 -m unittest tests.test_edit

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
# Набор ключей объекта задачи перечислен ОДИН раз и отдельно от sing.py — там же,
# где стоит контракт машинного вывода. Второй список рядом разошёлся бы с первым.
from test_json import CORE_KEYS  # noqa: E402

SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

ROOT = "P-root"
PROJ = "P-zz"
OUTSIDE = "P-outside"
COLS = {"todo": "KS-todo", "wip": "KS-wip", "review": "KS-review",
        "done": "KS-done", "blocked": "KS-blocked"}
COL_NAMES = {"KS-todo": "К работе", "KS-wip": "В работе", "KS-review": "На проверке",
             "KS-done": "Готово", "KS-blocked": "Заблокировано"}

FAKE = "Q-fake-zz"          # безымянная служебная группа проекта = «вне секций»
SEC_A = "Q-a"
SEC_B = "Q-b"
NOTE = json.dumps([{"insert": "постановка задачи\n"}], ensure_ascii=False)

PROJECTS = [{"id": ROOT, "title": "ИИ проекты"},
            {"id": PROJ, "title": "zz-edit", "parent": ROOT},
            {"id": OUTSIDE, "title": "чужой проект"}]

# Состояние заглушки: пересобирается перед каждым тестом.
STATE = {}


def reset_state():
    STATE.clear()
    STATE.update({
        "groups": [
            {"id": FAKE, "title": "", "parent": PROJ, "fake": True, "parentOrder": 0},
            {"id": SEC_A, "title": "Раздел A", "parent": PROJ, "parentOrder": 1},
            {"id": SEC_B, "title": "Раздел B", "parent": PROJ, "parentOrder": 2},
            {"id": "Q-fake-outside", "title": "", "parent": OUTSIDE, "fake": True},
        ],
        "tasks": {
            "T-loose": {"id": "T-loose", "title": "вне секций", "projectId": PROJ,
                        "group": FAKE, "checked": 0, "complete": 0, "priority": 1,
                        "parent": "T-родитель", "note": NOTE, "tags": ["TG-a"],
                        "journalDate": None},
            "T-in-a": {"id": "T-in-a", "title": "в разделе A", "projectId": PROJ,
                       "group": SEC_A, "checked": 0, "complete": 0, "priority": 1,
                       "parent": "", "note": NOTE, "tags": [], "journalDate": None},
            # заметка проекта — это задача с isNote (api.md), без связки с доской
            "T-note": {"id": "T-note", "title": "Контекст проекта",
                       "projectId": PROJ, "group": FAKE, "isNote": True,
                       "checked": 0, "complete": 0, "priority": 1, "parent": "",
                       "note": json.dumps([{"insert": "прод через ansible\n"}],
                                          ensure_ascii=False),
                       "tags": [], "journalDate": None},
            "T-alien": {"id": "T-alien", "title": "в чужом проекте",
                        "projectId": OUTSIDE, "group": "Q-fake-outside",
                        "checked": 0, "complete": 0, "priority": 1, "parent": "",
                        "note": NOTE, "tags": [], "journalDate": None},
        },
        "links": {"T-loose": "KS-todo", "T-in-a": "KS-wip"},
        "patches": [],          # тела всех PATCH /task — по ним видно, что ушло
        "drop_note": False,     # сервер «теряет» заметку на любом PATCH
        "move_column": False,   # сервер утаскивает карточку в другую колонку
        "ignore_write": False,  # сервер отвечает 200, не применив поле
        "drop_isnote": False,   # сервер теряет признак заметки
        "next_id": 1,
    })


class TrackerStub(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

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

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path, q = parts.path, urllib.parse.parse_qs(parts.query)
        if path.startswith("/task/"):
            hit = STATE["tasks"].get(path[len("/task/"):])
            return self._send(hit or {"error": "нет задачи"}, 200 if hit else 404)
        if path == "/task":
            items = [t for t in STATE["tasks"].values()
                     if not q.get("projectId") or t["projectId"] == q["projectId"][0]]
            return self._page("tasks", items, q)
        if path == "/project":
            return self._page("projects", PROJECTS, q)
        if path == "/task-group":
            items = [g for g in STATE["groups"]
                     if not q.get("parent") or g["parent"] == q["parent"][0]]
            return self._page("taskGroups", items, q)
        if path == "/kanban-status":
            pid = (q.get("projectId") or [PROJ])[0]
            items = [{"id": cid, "name": name, "projectId": pid}
                     for cid, name in COL_NAMES.items()] if pid == PROJ else []
            return self._page("kanbanStatuses", items, q)
        if path == "/kanban-task-status":
            items = [{"id": f"KTS-{tid}", "taskId": tid, "statusId": sid}
                     for tid, sid in STATE["links"].items()
                     if not q.get("taskId") or tid == q["taskId"][0]]
            return self._page("kanbanTaskStatuses", items, q)
        if path == "/tag":
            return self._page("tags", [{"id": "TG-a", "title": "agent:zz-one"}], q)
        if path == "/checklist-item":
            return self._page("checklistItems", [], q)
        self._send({"error": f"нет такого пути: {path}"}, 404)

    def do_PATCH(self):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/task/"):
            return self._send({"error": "нет такого пути"}, 404)
        tid = path[len("/task/"):]
        task = STATE["tasks"].get(tid)
        if not task:
            return self._send({"error": "нет задачи"}, 404)
        body = self._body()
        STATE["patches"].append(body)
        if "group" in body:
            # Ровно те отказы, что даёт живой сервер (замер в докстроке модуля).
            if not body["group"]:
                return self._send({"statusCode": 400,
                                   "message": ["Must start with one of: \"Q-\""]}, 400)
            if body["group"] not in {g["id"] for g in STATE["groups"]}:
                return self._send({"statusCode": 400,
                                   "message": "Task group not found"}, 400)
        if not STATE["ignore_write"]:
            task.update(body)
        if STATE["drop_note"]:
            task["note"] = None
        if STATE["move_column"]:
            STATE["links"][tid] = "KS-done"
        if STATE["drop_isnote"]:
            task["isNote"] = False
        self._send(task)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        body = self._body()
        if path == "/task-group":
            gid = f"Q-new{len(STATE['groups'])}"
            STATE["groups"].append({"id": gid, "title": body["title"],
                                    "parent": body["parent"], "parentOrder": 9})
            return self._send({"id": gid})
        if path == "/task":
            tid = f"T-new{STATE['next_id']}"
            STATE["next_id"] += 1
            task = {"id": tid, "checked": 0, "complete": 0, "priority": 1,
                    "parent": "", "tags": [], "journalDate": None, "group": FAKE}
            task.update(body)
            if STATE["drop_note"]:
                task["note"] = None
            STATE["tasks"][tid] = task
            return self._send(task)
        self._send({"error": "нет такого пути"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/task/"):
            return self._send({"error": "нет такого пути"}, 404)
        tid = path[len("/task/"):]
        if not STATE["ignore_write"]:
            STATE["tasks"].pop(tid, None)
            STATE["links"].pop(tid, None)
        self._send({"ok": True})


class EditBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), TrackerStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.repo = tempfile.mkdtemp(prefix="zz-edit-")
        cls.addClassCleanup(shutil.rmtree, cls.repo, True)
        os.makedirs(os.path.join(cls.repo, ".agents"))
        with open(os.path.join(cls.repo, ".agents", "singularity.json"), "w") as f:
            json.dump({"projectId": PROJ, "projectTitle": "zz-edit",
                       "columns": dict(COLS)}, f)
        # Каталог БЕЗ привязки: в нём проверяется ограничение области по самой
        # задаче, а не по конфигу репозитория.
        cls.unbound = tempfile.mkdtemp(prefix="zz-edit-unbound-")
        cls.addClassCleanup(shutil.rmtree, cls.unbound, True)

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        reset_state()

    def cli(self, *argv, cwd=None, code=0):
        env = support.clean_env(SINGULARITY_API=self.api,
                                SINGULARITY_TOKEN="stub",
                                SINGULARITY_AGENT="zz-one",
                                SINGULARITY_NO_SYNC_CHECK="1")
        r = subprocess.run([sys.executable, SING_UNDER_TEST, *argv],
                           cwd=cwd or self.repo, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, code,
                         f"sing.py {' '.join(argv)} -> {r.returncode}\n"
                         f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r

    def group_of(self, tid):
        return STATE["tasks"][tid]["group"]


class RegroupTest(EditBase):
    """`--group` был только у `add`: секция задавалась один раз при создании, и
    разложить уже стоящую очередь по разделам было нечем (T-fb720268)."""

    def test_task_moves_into_the_named_section(self):
        r = self.cli("regroup", "T-loose", "Раздел A")
        self.assertEqual(self.group_of("T-loose"), SEC_A,
                         "секция не записалась, а команда отчиталась успехом")
        self.assertIn("вне секций", r.stdout)
        self.assertIn("«Раздел A»", r.stdout)

    def test_section_can_be_named_by_its_id_too(self):
        self.cli("regroup", "T-loose", SEC_B)
        self.assertEqual(self.group_of("T-loose"), SEC_B)

    def test_patch_carries_the_group_and_nothing_else(self):
        """Лишнее поле в PATCH стирает состояние задачи — отсюда и вся проверка."""
        self.cli("regroup", "T-loose", "Раздел A")
        self.assertEqual(STATE["patches"], [{"group": SEC_A}])

    def test_everything_but_the_section_survives(self):
        before = dict(STATE["tasks"]["T-loose"])
        self.cli("regroup", "T-loose", "Раздел A")
        after = STATE["tasks"]["T-loose"]
        for key in ("title", "note", "tags", "parent", "checked", "complete",
                    "journalDate", "projectId"):
            self.assertEqual(after[key], before[key], f"поле {key} уехало")
        self.assertEqual(STATE["links"]["T-loose"], "KS-todo",
                         "перенос в секцию сдвинул карточку по доске")

    def test_clear_returns_the_task_outside_sections(self):
        """«Вне секций» — это id служебной группы, а не null: `PATCH group=null`
        живой сервер отвергает четырёхсотым (замер в докстроке модуля)."""
        r = self.cli("regroup", "T-in-a", "--clear")
        self.assertEqual(self.group_of("T-in-a"), FAKE)
        self.assertEqual(STATE["patches"], [{"group": FAKE}],
                         "в PATCH ушло не id служебной группы")
        self.assertIn("вне секций", r.stdout)

    def test_already_there_writes_nothing(self):
        """No-op обязан быть настоящим: подтверждать перечитыванием тут нечего —
        «поле равно ожидаемому» верно и до записи."""
        r = self.cli("regroup", "T-in-a", "Раздел A")
        self.assertIn("уже", r.stdout)
        self.assertEqual(STATE["patches"], [], "отправлен PATCH на месте no-op")

    def test_clear_is_a_no_op_when_the_task_is_already_loose(self):
        r = self.cli("regroup", "T-loose", "--clear")
        self.assertIn("уже", r.stdout)
        self.assertEqual(STATE["patches"], [])

    def test_unknown_section_is_refused_before_the_request(self):
        p = self.cli("regroup", "T-loose", "Раздела нет", code=1)
        self.assertIn("не найдена", p.stderr)
        self.assertIn("Раздел A", p.stderr, "в отказе нет списка секций")
        self.assertEqual(STATE["patches"], [], "отказ всё-таки что-то записал")
        self.assertEqual(self.group_of("T-loose"), FAKE)

    def test_unknown_section_id_is_refused_too(self):
        """`resolve_group` пропускает любой `Q-…` без проверки: для `add` это
        ошибка сервера при создании, здесь — молча уехавшая карточка."""
        p = self.cli("regroup", "T-loose", "Q-выдуманная", code=1)
        self.assertIn("нет в проекте", p.stderr)
        self.assertEqual(STATE["patches"], [])
        self.assertNotIn("HTTP 400", p.stderr, "наружу утёк ответ API")

    def test_section_and_clear_together_are_refused(self):
        p = self.cli("regroup", "T-loose", "Раздел A", "--clear", code=1)
        self.assertIn("ровно одно", p.stderr)
        self.assertEqual(STATE["patches"], [])

    def test_neither_section_nor_clear_is_refused(self):
        p = self.cli("regroup", "T-loose", code=1)
        self.assertIn("ровно одно", p.stderr)

    def test_task_outside_the_allowed_scope_is_refused(self):
        p = self.cli("regroup", "T-alien", "Раздел A", cwd=self.unbound, code=1)
        self.assertIn("ЗАПРЕЩЕНО", p.stderr)
        self.assertEqual(STATE["patches"], [])

    # --------------------------------------------- сервер ответил 200, не сделав
    def test_silent_refusal_of_the_server_is_caught(self):
        """Ровно тот случай, ради которого AGENTS.md §4: 200 и ничего не сделано."""
        STATE["ignore_write"] = True
        p = self.cli("regroup", "T-loose", "Раздел A", code=1)
        self.assertIn("не применилась", p.stderr)
        self.assertEqual(self.group_of("T-loose"), FAKE)

    def test_a_lost_note_is_caught(self):
        """Заметка в этом скилле — постановка задачи и вся история отчётов.
        `set` её не стережёт, `regroup` обязан (REGROUP_WATCHED)."""
        STATE["drop_note"] = True
        p = self.cli("regroup", "T-loose", "Раздел A", code=1)
        self.assertIn("задела лишнее", p.stderr)
        self.assertIn("note", p.stderr)

    def test_a_card_dragged_across_the_board_is_caught(self):
        """Секция и колонка ортогональны (api.md). Колонка — не поле задачи, и
        снимком до/после внутри PATCH её не поймать: проверяется связка."""
        STATE["move_column"] = True
        p = self.cli("regroup", "T-loose", "Раздел A", code=1)
        self.assertIn("задел доску", p.stderr)

    # ------------------------------------------------------------ машинный вывод
    def test_json_is_the_reread_card_and_stdout_is_only_json(self):
        r = self.cli("regroup", "T-loose", "Раздел A", "--json")
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            self.fail(f"stdout не JSON: {e}\n{r.stdout}")
        self.assertEqual((out["group"], out["groupTitle"]), (SEC_A, "Раздел A"))
        self.assertEqual((out["column"], out["columnName"]), ("todo", "К работе"))
        self.assertIn("вне секций", r.stderr, "человеку не осталось ни строки")

    def test_json_of_a_no_op_shows_the_current_section(self):
        r = self.cli("regroup", "T-in-a", "Раздел A", "--json")
        self.assertEqual(json.loads(r.stdout)["groupTitle"], "Раздел A")
        self.assertEqual(STATE["patches"], [])


class NotesTest(EditBase):
    """У `notes` было только создание: долгоживущий контекст, который SKILL.md велит
    читать перед работой, нельзя было ни перечитать по id, ни поправить, ни убрать
    иначе как руками в приложении (T-ac96c736)."""

    def stored(self, tid):
        """Текст заметки так, как он лежит в трекере."""
        raw = STATE["tasks"][tid]["note"]
        ops = json.loads(raw)
        self.assertIsInstance(ops, list,
                              "заметка записана не голым массивом операций — "
                              "приложение покажет сырой JSON вместо текста")
        return "".join(op.get("insert", "") for op in ops
                       if isinstance(op.get("insert"), str))

    def created_id(self, stdout):
        return stdout.split(":")[0].strip()

    # ------------------------------------------------------------------ чтение
    def test_one_note_is_shown_by_id(self):
        r = self.cli("notes", "--show", "T-note")
        self.assertIn("прод через ansible", r.stdout)
        self.assertIn("Контекст проекта", r.stdout)

    def test_show_refuses_a_task_that_is_not_a_note(self):
        p = self.cli("notes", "--show", "T-loose", code=1)
        self.assertIn("не заметка", p.stderr)
        self.assertIn("sing.py show T-loose", p.stderr, "нет подсказки, куда идти")

    def test_listing_still_prints_every_note(self):
        self.assertIn("прод через ansible", self.cli("notes").stdout)

    # -------------------------------------------------------------- создание
    def test_add_creates_a_note_and_rereads_it(self):
        out = self.cli("notes", "--add", "Соглашения",
                       "--text", "не трогать прод по пятницам").stdout
        tid = self.created_id(out)
        self.assertTrue(STATE["tasks"][tid]["isNote"], "создана задача, а не заметка")
        self.assertIn("не трогать прод по пятницам", self.stored(tid))

    def test_add_fails_when_the_server_swallows_the_text(self):
        """Ответ на POST — не доказательство: карточка перечитывается."""
        STATE["drop_note"] = True
        p = self.cli("notes", "--add", "Пропадёт", "--text", "текст", code=1)
        self.assertIn("текст в ней не тот", p.stderr)

    # ---------------------------------------------------------------- правка
    def test_edit_replaces_the_text(self):
        self.cli("notes", "--edit", "T-note", "--text", "прод через kubernetes")
        self.assertEqual(self.stored("T-note").strip(), "прод через kubernetes")
        self.assertEqual([set(b) for b in STATE["patches"]], [{"note"}],
                         "PATCH унёс не только заметку")

    def test_append_keeps_what_was_there(self):
        self.cli("notes", "--edit", "T-note", "--append", "--text", "и ещё вот что")
        text = self.stored("T-note")
        self.assertIn("прод через ansible", text, "дописывание затёрло прежний текст")
        self.assertIn("и ещё вот что", text)

    def test_writing_the_same_text_is_refused(self):
        """Критерий карточки: запись, которая ничего не меняет, обязана ронять
        команду. Подтверждать её нечем — текст равен ожидаемому и до запроса."""
        p = self.cli("notes", "--edit", "T-note", "--text", "прод через ansible",
                     code=1)
        self.assertIn("текст тот же", p.stderr)
        self.assertEqual(STATE["patches"], [], "отказ всё-таки что-то записал")

    def test_edit_without_text_is_refused(self):
        p = self.cli("notes", "--edit", "T-note", code=1)
        self.assertIn("нечего писать", p.stderr)
        p = self.cli("notes", "--edit", "T-note", "--text", "   ", code=1)
        self.assertIn("нечего писать", p.stderr)
        self.assertEqual(STATE["patches"], [])

    def test_silent_refusal_of_the_server_is_caught(self):
        STATE["ignore_write"] = True
        p = self.cli("notes", "--edit", "T-note", "--text", "новый текст", code=1)
        self.assertIn("не применилась", p.stderr)
        self.assertIn("прод через ansible", self.stored("T-note"))

    def test_a_lost_isnote_flag_is_caught(self):
        """Потеряв `isNote`, заметка встанет в очередь задач, и агент попробует её
        «выполнить». PATCH с лишним полем делает это молча."""
        STATE["drop_isnote"] = True
        p = self.cli("notes", "--edit", "T-note", "--text", "новый текст", code=1)
        self.assertIn("задела лишнее", p.stderr)
        self.assertIn("isNote", p.stderr)

    def test_edit_refuses_a_task_that_is_not_a_note(self):
        p = self.cli("notes", "--edit", "T-loose", "--text", "текст", code=1)
        self.assertIn("не заметка", p.stderr)
        self.assertEqual(STATE["patches"], [])

    # -------------------------------------------------------------- удаление
    def test_rm_needs_a_confirmation(self):
        p = self.cli("notes", "--rm", "T-note", code=1)
        self.assertIn("--yes", p.stderr)
        self.assertIn("T-note", STATE["tasks"], "заметка удалена без подтверждения")

    def test_rm_removes_the_note_for_real(self):
        r = self.cli("notes", "--rm", "T-note", "--yes")
        self.assertIn("удалена", r.stdout)
        self.assertNotIn("T-note", STATE["tasks"])

    def test_rm_checks_the_fact_not_the_status_code(self):
        """`200` у этого API успеха не доказывает — и у удаления тоже."""
        STATE["ignore_write"] = True
        p = self.cli("notes", "--rm", "T-note", "--yes", code=1)
        self.assertIn("заметка на месте", p.stderr)

    # ------------------------------------------------------------- остальное
    def test_two_modes_at_once_are_refused(self):
        p = self.cli("notes", "--add", "Раз", "--rm", "T-note", code=1)
        self.assertIn("за раз делается одно", p.stderr)

    def test_append_without_edit_is_refused(self):
        """Молча проигнорированный флаг — это правка, которая «прошла», но никуда."""
        p = self.cli("notes", "--add", "Раз", "--append", "--text", "два", code=1)
        self.assertIn("--append", p.stderr)
        self.assertEqual(len(STATE["tasks"]), 4, "заметка всё-таки создалась")

    def test_json_of_the_listing_and_of_one_note(self):
        items = json.loads(self.cli("notes", "--json").stdout)
        self.assertEqual([n["id"] for n in items], ["T-note"])
        self.assertEqual(CORE_KEYS - set(items[0]), set(),
                         "заметка описана не тем же объектом, что задача")
        one = json.loads(self.cli("notes", "--show", "T-note", "--json").stdout)
        for key in sorted(CORE_KEYS):
            self.assertEqual(one[key], items[0][key], f"поле {key} разное")
        self.assertIn("прод через ansible", one["note"])
        self.assertIsNone(one["column"], "заметки на канбане нет")

    def test_json_of_an_edit_is_the_reread_note(self):
        r = self.cli("notes", "--edit", "T-note", "--text", "новый текст", "--json")
        out = json.loads(r.stdout)
        self.assertIn("новый текст", out["note"])
        self.assertIn("переписана", r.stderr, "человеку не осталось ни строки")


if __name__ == "__main__":
    unittest.main()
