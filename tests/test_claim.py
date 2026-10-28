"""Захват задачи против ОТСТАЮЩЕЙ очереди синхронизации. Заглушка, без токена.

Зачем отдельная заглушка, а не живой трекер: дефект T-fc3a3096 зависит от
загруженности сервера. На спокойной очереди перехват проходит с первого раза —
значит живой прогон здесь зеленеет сам по себе и ничего не доказывает. Проверка,
которая в принципе не может покраснеть, — не проверка (AGENTS.md §4).

Что моделирует `LaggingStub`: сервер принимает запись в очередь синхронизации и
отвечает 200 РАНЬШЕ, чем применит её. До применения GET отдаёт прежнее
состояние. Задержка меряется в числе чтений задачи, а не в секундах: так
сценарий детерминирован и не зависит от скорости машины.

Две ручки, и обе нужны:
  · `lag_all=N` — каждая запись невидима следующие N чтений. Ровная нагрузка;
    на ней сравниваются старый и новый код в одинаковых условиях.
  · `lag_schedule={2: N}` — отстаёт только ВТОРАЯ запись. Это ровно тот случай
    из карточки: очередь загрузилась между двумя PATCH перехвата, первый
    (снятие чужого тега) виден сразу, второй (своя метка) — нет.

Контрольный красный прогон на старом коде (обязателен, без него правка
недоказуема) — тем же файлом, другой реализацией:

    git show <base>:scripts/sing.py > /tmp/sing-old.py
    SING_UNDER_TEST=/tmp/sing-old.py tests/run.py fast

Запуск: tests/run.py fast
"""

import io
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

# Какую реализацию проверяем. По умолчанию — эта, из репозитория; переменной
# подставляется старая, чтобы получить контрольный красный.
SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

TASK = "T-stub-0001"
MINE = "agent:zz-taker"
FOREIGN = "agent:zz-holder"

# Привязка проекта: роли колонок для проверки «мёртвая метка — не захват».
# Доску не поднимаем — колонку задачи задаёт сам тест patch-ем task_column.
CFG = {"columns": {"todo": "KS-todo", "wip": "KS-wip",
                   "review": "KS-review", "done": "KS-done",
                   "blocked": "KS-blocked"}}


class LaggingStub(BaseHTTPRequestHandler):
    """Трекер, у которого запись доезжает не сразу. Состояние — в `STATE`."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # --- очередь синхронизации -------------------------------------------
    def _tick_task_read(self):
        """Одно чтение задачи. Возвращает то, что сервер СЕЙЧАС отдаёт."""
        with STATE["lock"]:
            if STATE["pending"] is not None:
                if STATE["stale_left"] > 0:
                    STATE["stale_left"] -= 1
                else:
                    STATE["committed"] = STATE["pending"]
                    STATE["pending"] = None
                    # история — те состояния, через которые задача РЕАЛЬНО
                    # прошла: по ней видно окно «ни одной метки»
                    STATE["history"].append(list(STATE["committed"]))
            return list(STATE["committed"])

    def _write_task(self, tags):
        with STATE["lock"]:
            STATE["writes"] += 1
            n = STATE["writes"]
            # незаехавшую запись новая вытесняет: очередь хранит последнее
            STATE["pending"] = list(tags)
            STATE["stale_left"] = STATE["lag_schedule"].get(n, STATE["lag_all"])

    # --- HTTP -------------------------------------------------------------
    def _send(self, payload, code=200):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _path(self):
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self):
        path = self._path()
        if path == f"/task/{TASK}":
            self._send({"id": TASK, "title": "zz: захват",
                        "tags": self._tick_task_read()})
        elif path == "/tag":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            off = int(q.get("offset", ["0"])[0])
            with STATE["lock"]:
                items = [{"id": t, "title": t} for t in STATE["tags"]]
            page = items[off:off + int(q.get("maxCount", ["200"])[0])]
            self._send({"tags": page, "pagination": {"total": len(items)}})
        else:
            self._send({"error": f"нет такого пути: {path}"}, 404)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_PATCH(self):
        if self._path() != f"/task/{TASK}":
            return self._send({"error": "нет такого пути"}, 404)
        self._write_task(self._body().get("tags") or [])
        self._send({"id": TASK})          # 200 — но ещё НЕ применено

    def do_POST(self):
        if self._path() != "/tag":
            return self._send({"error": "нет такого пути"}, 404)
        title = self._body()["title"]
        with STATE["lock"]:
            if title not in STATE["tags"]:
                STATE["tags"].append(title)
        self._send({"id": title})


# id тега в заглушке равен его названию — так тесты читаются без таблицы перевода
STATE = {"lock": threading.Lock()}


def reset(initial, lag_all=0, lag_schedule=None):
    with STATE["lock"]:
        STATE.update({
            "tags": [MINE, FOREIGN],          # общий на аккаунт список тегов
            "committed": list(initial),
            "pending": None,
            "stale_left": 0,
            "writes": 0,
            "lag_all": lag_all,
            "lag_schedule": dict(lag_schedule or {}),
            "history": [list(initial)],
        })


class ClaimAgainstLagTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), LaggingStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        with support.env(SINGULARITY_API=base):
            cls.sing = support.load_module("sing_claim", SING_UNDER_TEST)
        cls.sing.get_token = lambda: "stub"     # Keychain не трогаем вовсе
        assert cls.sing.API == base, "клиент не нацелился на заглушку"
        cls.old = not hasattr(cls.sing, "set_task_tags")
        if cls.old:
            print(f"\n  ⚠ контрольный прогон: {SING_UNDER_TEST} — код ДО правки",
                  file=sys.stderr)

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        # Быстрые константы: здесь меряется решение, а не боевые паузы.
        for name, value in (("CLAIM_SETTLE", 0.01), ("TAG_SETTLE_PAUSE", 0.01)):
            if hasattr(self.sing, name):
                self.addCleanup(setattr, self.sing, name,
                                getattr(self.sing, name))
                setattr(self.sing, name, value)
        # Колонку задачи задаёт тест (self.column): доску в заглушке не
        # поднимаем — claim-решение читает только роль колонки.
        self.column = None
        orig = self.sing.task_column
        self.sing.task_column = lambda task_id, pid=None: self.column
        self.addCleanup(setattr, self.sing, "task_column", orig)

    def claim(self):
        """Перехват под своим именем. Возвращает (код выхода|None, stderr)."""
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                self.sing.claim_task(TASK, None, override="zz-taker",
                                     take_over=True)
            return None, err.getvalue()
        except SystemExit as e:
            return e.code, err.getvalue()

    def visible(self):
        return list(STATE["committed"])

    def claim_cfg(self, column, take_over=False):
        """Захват по привязке: колонку карты задаёт `column`, take_over явно."""
        self.column = column
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                self.sing.claim_task(TASK, CFG, override="zz-taker",
                                     take_over=take_over)
            return None, err.getvalue()
        except SystemExit as e:
            return e.code, err.getvalue()

    # ---------------------------------------------------------------- тесты
    def test_take_over_survives_a_lagging_queue(self):
        """Ровная нагрузка: каждая запись невидима три следующих чтения.

        Старый код судит по ПЕРВОМУ чтению и падает здесь всегда; новый ждёт,
        перечитывает и доводит перехват до конца.
        """
        reset([FOREIGN], lag_all=3)
        code, err = self.claim()
        self.assertIsNone(code, f"перехват не прошёл под лагом очереди: {err}")
        self.assertEqual(self.visible(), [MINE])

    def test_take_over_is_a_single_write(self):
        """Одна запись, а не две: окно «задача без метки» закрыто по построению.

        Не стиль, а суть требования: пока снятие чужого тега и постановка своего
        — два PATCH, между ними задача выглядит свободной, и никакой ретрай
        этого не лечит.
        """
        reset([FOREIGN])
        code, err = self.claim()
        self.assertIsNone(code, err)
        self.assertEqual(STATE["writes"], 1,
                         f"перехват сделал {STATE['writes']} записи вместо одной")

    def test_task_never_looks_free_during_a_take_over(self):
        """Задача ни на миг не остаётся без agent-метки.

        Ровно тот сценарий из карточки: очередь загрузилась между двумя
        записями. Старый код успевает применить `tags=[]` и падает на второй
        записи — задача остаётся вообще без меток, то есть выглядит свободной,
        хотя её только что перехватывали. Это хуже честного отказа.
        """
        reset([FOREIGN], lag_schedule={2: 3})
        self.claim()
        self.assertNotIn([], STATE["history"],
                         "задача прошла через состояние без agent-метки: "
                         f"{STATE['history']}")

    def test_refusal_leaves_the_holder_in_place(self):
        """Не подтвердилось вовсе — отказ, но чужая метка на месте.

        Атомарность по смыслу: `tags` переписывается целиком, поэтому сервер
        либо применит новый список, либо не применит ничего. Возвращать снятую
        чужую метку нечем и незачем — её никто не снимал.
        """
        reset([FOREIGN], lag_all=999)          # запись не доезжает никогда
        code, err = self.claim()
        self.assertEqual(code, 1, "молчаливый успех при неприменённой записи")
        self.assertEqual(self.visible(), [FOREIGN],
                         f"держатель потерян при отказе; stderr={err}")

    def test_plain_claim_of_a_free_task_still_works(self):
        """Обычный захват свободной задачи правкой не задет."""
        reset([], lag_all=1)
        code, err = self.claim()
        self.assertIsNone(code, err)
        self.assertEqual(self.visible(), [MINE])

    # --- мёртвая метка завершившейся сессии (T-3ef6b167) -------------------
    def test_stale_mark_in_review_does_not_block(self):
        """Карта в «На проверке» с чужой меткой берётся без --take-over.

        Метка завершившейся сессии — не захват: держатель только в wip (и в
        окне claim в todo). Ровно сценарий T-3ef6b167: сабагент закрыл карту
        done --review и завершился, метка осталась, следующий start упирался
        в «Две сессии над одной задачей».
        """
        reset([FOREIGN], lag_all=0)
        code, err = self.claim_cfg(CFG["columns"]["review"])
        self.assertIsNone(code, f"отказ по карте из «На проверке»: {err}")
        self.assertEqual(self.visible(), [MINE],
                         "своя метка не стала держателем")

    def test_stale_mark_in_done_does_not_block(self):
        """Карта в «Готово» с чужой меткой — то же: след, не захват."""
        reset([FOREIGN], lag_all=0)
        code, err = self.claim_cfg(CFG["columns"]["done"])
        self.assertIsNone(code, f"отказ по карте из «Готово»: {err}")
        self.assertEqual(self.visible(), [MINE])

    def test_held_mark_in_wip_still_refused(self):
        """Карта в «В работе» с чужой меткой — отказ сохранён.

        Граница карты: защита от двух живых сессий на карте в wip не
        ослабляется. Отказ не трогает чужую метку (она остаётся держателем).
        """
        reset([FOREIGN], lag_all=0)
        code, err = self.claim_cfg(CFG["columns"]["wip"])
        self.assertEqual(code, 1,
                         "взял карту в wip с чужой меткой без --take-over")
        self.assertIn("уже занята", err)
        self.assertEqual(self.visible(), [FOREIGN], "отказ тронул чужую метку")

    def test_held_mark_in_todo_still_refused(self):
        """Карта в очереди с чужой меткой — отказ: окно claim, не мёртвый след.

        Метку вешают ДО переноса в wip, поэтому размеченная todo-карта может
        быть свежим захватом, а не остатком; пропустить такой отказ — отдать
        задачу двоим.
        """
        reset([FOREIGN], lag_all=0)
        code, err = self.claim_cfg(CFG["columns"]["todo"])
        self.assertEqual(code, 1,
                         "взял карту из todo с чужой меткой без --take-over")
        self.assertIn("уже занята", err)
        self.assertEqual(self.visible(), [FOREIGN])


if __name__ == "__main__":
    unittest.main()
