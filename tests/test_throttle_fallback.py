"""Обход троттлинга ручки `GET /task/{id}`. Заглушка, без токена и без сети.

Лимит трекера считается ПО РУЧКЕ (замер 19.09: 100 ответов `200` на
`GET /task/{id}`, 101-й — `429`, и в ту же секунду `GET /task?projectId=…`
отвечает `200`). Отсюда `get_task_data`: зажатую ручку подменяет выборка по
проекту. Живым прогоном это не проверишь — чтобы трекер начал отвечать `429`,
надо его сначала зажать, а выходит он из этого состояния десятками минут.
Поэтому здесь заглушка, которая отдаёт `429` по требованию.

Что именно проверяется — три свойства обхода, и каждое из них можно сломать:

  1. обход — ЗАПАСНОЙ путь: пока прямое чтение работает, выборки по проекту
     быть не должно (иначе каждая карточка стоила бы 743 КБ вместо 5,5 КБ);
  2. обход не ПРЯЧЕТ отказ: нет задачи ни в одной из ручек — команда падает
     настоящей ошибкой сервера, а не пустым объектом;
  3. обход не двигает точку отказа внутрь записи: у пишущих команд проверка
     области остаётся без обхода и падает ДО первого `PATCH`.

Контрольный красный прогон на старом коде (тем же файлом, другой реализацией):

    git show <base>:scripts/sing.py > /tmp/sing-old.py
    SING_UNDER_TEST=/tmp/sing-old.py tests/run.py fast

Запуск: tests/run.py fast
"""

import io
import ast
import json
import os
import sys
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

ROOT = "P-root-0001"
PROJECT = "P-stub-0001"
TASK = "T-stub-0007"
OTHER = "T-stub-0008"

# Задача ровно в той форме, в какой её отдают ОБЕ ручки. Что формы совпадают —
# не допущение заглушки, а замер на живой задаче: 39 ключей против 39, ни одного
# расхождения в значениях (see references/api.md).
TASK_DATA = {"id": TASK, "title": "zz: троттлинг", "projectId": PROJECT,
             "tags": ["TG-1"], "checked": 0, "note": "заметка"}
OTHER_DATA = {"id": OTHER, "title": "zz: соседняя", "projectId": PROJECT,
              "tags": [], "checked": 0}

STATE = {"lock": threading.Lock(), "by_id_throttled": False,
         "in_listing": True, "log": []}


class ThrottlingStub(BaseHTTPRequestHandler):
    """Трекер, у которого зажата ОДНА ручка — `GET /task/{id}`.

    Остальные отвечают как обычно: именно это и есть измеренное поведение
    лимита, а не удобное допущение.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, payload, code=200):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _note(self, method, path):
        with STATE["lock"]:
            STATE["log"].append(f"{method} {path}")

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _page(self, items, key):
        q = self._query()
        off = int(q.get("offset", ["0"])[0])
        window = int(q.get("maxCount", ["200"])[0])
        return {key: items[off:off + window], "pagination": {"total": len(items)}}

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        self._note("GET", path)
        if path == f"/task/{TASK}":
            with STATE["lock"]:
                throttled = STATE["by_id_throttled"]
            if throttled:
                # ровно то, что отдаёт живой сервер: ни Retry-After, ни
                # X-RateLimit-* на отказе нет (замер 19.09)
                return self._send(
                    {"statusCode": 429,
                     "message": "ThrottlerException: Too Many Requests"}, 429)
            return self._send(dict(TASK_DATA))
        if path == "/task":
            with STATE["lock"]:
                listed = [dict(OTHER_DATA)]
                if STATE["in_listing"]:
                    listed.insert(0, dict(TASK_DATA))
            return self._send(self._page(listed, "tasks"))
        if path == "/project":
            return self._send(self._page(
                [{"id": ROOT, "title": "ИИ проекты", "parent": None},
                 {"id": PROJECT, "title": "zz-stub", "parent": ROOT}], "projects"))
        if path == "/checklist-item":
            return self._send(self._page([], "checklistItems"))
        if path == "/kanban-task-status":
            return self._send(self._page([], "kanbanTaskStatuses"))
        if path == "/kanban-status":
            return self._send(self._page([], "kanbanStatuses"))
        if path == "/tag":
            return self._send(self._page([{"id": "TG-1", "title": "метка"}], "tags"))
        self._send({"message": f"нет такого пути: {path}"}, 404)

    def do_PATCH(self):
        self._note("PATCH", urllib.parse.urlsplit(self.path).path)
        self._send({"id": TASK})

    def do_POST(self):
        self._note("POST", urllib.parse.urlsplit(self.path).path)
        self._send({"id": TASK})


class ThrottleFallbackTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ThrottlingStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        with support.env(SINGULARITY_API=base):
            # именно SING_UNDER_TEST, а не support.SING: иначе контрольный
            # красный прогон на старом коде молча проверял бы новый
            cls.sing = support.load_module("sing_throttle", SING_UNDER_TEST)
        cls.sing.get_token = lambda: "stub"          # Keychain здесь не нужен
        assert cls.sing.API == base, "клиент не нацелился на заглушку"
        if SING_UNDER_TEST != support.SING:
            print(f"\n  ⚠ контрольный прогон: {SING_UNDER_TEST}", file=sys.stderr)

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # Привязка читается от cwd вверх, поэтому её кладём в свой каталог и
        # туда же переходим на время проверки.
        import tempfile
        d = tempfile.mkdtemp(prefix="zz-throttle-")
        os.makedirs(os.path.join(d, ".agents"))
        with open(os.path.join(d, ".agents", "singularity.json"), "w") as f:
            json.dump({"projectId": PROJECT, "projectTitle": "zz-stub",
                       "columns": {}}, f)
        here = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, here)
        import shutil
        self.addCleanup(shutil.rmtree, d, True)

        # Паузы повторов — быстрые: здесь меряются решения, а не задержки.
        for k, v in (("NET_BACKOFF", 0.01), ("THROTTLE_RETRIES", 2)):
            self.addCleanup(setattr, self.sing, k, getattr(self.sing, k))
            setattr(self.sing, k, v)
        self.sing.forget_projects()
        self.addCleanup(self.sing.forget_projects)
        with STATE["lock"]:
            STATE.update(by_id_throttled=False, in_listing=True)
            STATE["log"].clear()

    # --- помощники --------------------------------------------------------

    def throttle_by_id(self):
        with STATE["lock"]:
            STATE["by_id_throttled"] = True

    def log(self):
        with STATE["lock"]:
            return list(STATE["log"])

    def failing(self, fn, *a, **kw):
        """Прогнать то, что обязано упасть, и вернуть (код выхода, stderr)."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(err):
            fn(*a, **kw)
        return cm.exception.code, err.getvalue()

    # --- 1. обход запасной, а не основной ---------------------------------

    def test_healthy_endpoint_is_read_directly(self):
        """Пока `/task/{id}` отвечает, выборки по проекту быть не должно.

        Контроль осмысленности: одна карточка по id — 5,5 КБ, выборка по
        проекту в 62 задачи — 743 КБ; обход «на всякий случай» стоил бы этой
        разницы на каждом чтении.
        """
        self.assertEqual(self.sing.get_task_data(TASK)["id"], TASK)
        self.assertEqual(self.log(), [f"GET /task/{TASK}"])

    # --- 2. обход работает и отдаёт ТУ ЖЕ задачу --------------------------

    def test_throttled_endpoint_falls_back_to_project_listing(self):
        """`429` по id — задача берётся из выборки по проекту, целиком."""
        self.throttle_by_id()
        got = self.sing.get_task_data(TASK)
        self.assertEqual(got, TASK_DATA, "из выборки пришла не та задача")
        self.assertIn("GET /task", self.log(), "выборка по проекту не сработала")

    def test_show_works_while_endpoint_is_throttled(self):
        """`show` — чтение, и оно обязано пережить зажатую ручку.

        Это и есть инцидент 18.09: `show` отвечал `429` сорок две минуты,
        хотя выборка по проекту всё это время работала.
        """
        self.throttle_by_id()
        args = type("A", (), {"id": TASK, "json": False, "agent": None})()
        out = io.StringIO()
        with __import__("contextlib").redirect_stdout(out):
            self.sing.cmd_show(args)
        self.assertIn(TASK, out.getvalue())
        self.assertIn("zz: троттлинг", out.getvalue())

    # --- 3. обход не прячет отказ ----------------------------------------

    def test_both_sources_silent_is_a_red_refusal(self):
        """Ни по id, ни в выборке — отказ сервера наружу, а не пустой объект.

        Главное свойство: скилл держится на «красный остаётся красным».
        Обход, который на отказе возвращает `None`/`{}`, превращает внятный
        `429` в тихо деградировавший ответ — и вызывающий код читает пустые
        теги как «тегов нет».
        """
        self.throttle_by_id()
        with STATE["lock"]:
            STATE["in_listing"] = False
        code, err = self.failing(self.sing.get_task_data, TASK)
        self.assertEqual(code, 1)
        self.assertIn("429", err, "отказ сервера потерялся по дороге")

    def test_missing_task_is_not_invented_by_the_fallback(self):
        """Чужая задача (`404` по id, в выборке проекта её нет) — отказ.

        Обход ищет только в ПРИВЯЗАННОМ проекте, поэтому расширить область
        (AGENTS.md §3) он не может: задача не из этого проекта в выборку не
        попадёт, и наружу выйдет ошибка.
        """
        code, err = self.failing(self.sing.get_task_data, "T-stub-9999")
        self.assertEqual(code, 1)
        self.assertIn("404", err)

    # --- 4. точка отказа пишущей команды не двигается ---------------------

    def test_writing_command_still_fails_before_the_first_write(self):
        """Проверка области БЕЗ обхода падает до единого `PATCH`.

        Так устроены `done`/`block`/`report`: упереться в `429` до первой
        записи — чистый отказ, в трекере не изменилось ничего. С обходом
        команда прошла бы проверку и умерла позже, уже записав отчёт, —
        получилась бы наполовину закрытая задача.
        """
        self.throttle_by_id()
        cfg = {"projectId": PROJECT}
        code, err = self.failing(self.sing.assert_task_allowed, TASK, cfg)
        self.assertEqual(code, 1)
        self.assertIn("429", err)
        self.assertEqual([r for r in self.log() if not r.startswith("GET ")], [],
                         "до отказа успела уйти запись")

    def test_read_only_guard_passes_the_same_task(self):
        """С обходом та же проверка области проходит и отдаёт ту же задачу."""
        self.throttle_by_id()
        got = self.sing.assert_task_allowed(TASK, {"projectId": PROJECT},
                                            fallback=True)
        self.assertEqual(got["id"], TASK)

    def test_only_read_only_commands_ask_for_the_fallback(self):
        """Обход включён ровно у команд, которые ничего не пишут.

        Проверка на исходнике, а не на поведении, потому что стережёт она
        РЕШЕНИЕ, а не механику: добавить `fallback=True` в `cmd_done` — одна
        строка, и последствие (наполовину закрытая задача) всплывёт не здесь,
        а на живом трекере под нагрузкой.
        """
        with open(SING_UNDER_TEST) as f:
            tree = ast.parse(f.read())
        # разбор по AST, а не грепом строки: грепу одинаково видны вызов,
        # упоминание в docstring и перенос аргумента на другую строку
        callers = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and any(
                        kw.arg == "fallback" and getattr(kw.value, "value", None) is True
                        for kw in node.keywords):
                    callers.add(fn.name)
        self.assertEqual(callers, {"cmd_show"},
                         f"обход просит не только читающая команда: {callers}")


if __name__ == "__main__":
    unittest.main()
