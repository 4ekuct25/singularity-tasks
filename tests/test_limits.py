"""Учёт счётчиков лимита: заголовки успешных ответов и упреждающая пауза.

Заглушка, без токена и без сети. Живым прогоном это проверить нельзя: чтобы
увидеть край, ручку надо сначала исчерпать, а восстанавливается она минуту —
живая проверка края живёт в `tools/check-limit-headers.py edge` и запускается
руками.

Что измерено на живом API и заложено в заглушку (числа — в references/api.md):

  * шесть заголовков на КАЖДОМ успешном ответе, `Reset` — обратный отсчёт
    секунд до сброса окна (замер: 17 с, через 6 секунд 11 с);
  * счётчик свой у пары (МЕТОД, шаблон маршрута): `GET /task/T-1` и
    `GET /task/T-2` идут одной кассой (92 → 91 → 90), `GET /task` и
    `POST /task` — разными (в ту же секунду 98 и 99);
  * на самом `429` заголовков нет ВОВСЕ — ни `Retry-After`, ни `X-RateLimit-*`.

Пять свойств, каждое из которых можно сломать одной строкой:

  1. остаток из заголовков запоминается по бакетам;
  2. упреждающая пауза срабатывает ДО отказа — тот же цикл без неё падает на 429;
  3. на ровном месте скилл не спит: пока запас цел, пауз нет;
  4. часовое окно не пережидается (до сброса бывает 50 минут) — только предупреждение;
  5. `429` не затирает прежнее знание, а повторы на нём остаются прежними:
     упреждающая пауза бывает только на ПЕРВОЙ попытке.

Контрольный красный прогон на старом коде:

    git show <base>:scripts/sing.py > /tmp/sing-old.py
    SING_UNDER_TEST=/tmp/sing-old.py tests/run.py fast

Запуск: tests/run.py fast
"""

import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

SING_UNDER_TEST = os.environ.get("SING_UNDER_TEST") or support.SING

# Числа заглушки маленькие намеренно: проверяется РЕШЕНИЕ (заметил край или нет),
# а не боевые 100/1000 — гонять сотню запросов на каждую проверку незачем.
LIMIT_SHORT, WINDOW_SHORT = 5, 20.0
LIMIT_LONG, WINDOW_LONG = 40, 3600.0

# Идентификаторы — в той же форме, что у живого трекера (`T-` + uuid): короткий
# «T-1» шаблоном маршрута не признаётся ни здесь, ни в скилле, и проверка на нём
# зеленела бы не на том.
TASK_A = "T-aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "T-bbbbbbbb-1111-2222-3333-444444444444"

STATE = {"lock": threading.Lock(), "buckets": {}, "skew": 0.0,
         "long_left_override": None, "always_429": False}


def stub_now():
    """Часы СЕРВЕРА. Их двигает фальшивый сон проверки — см. FakeClock."""
    with STATE["lock"]:
        return time.time() + STATE["skew"]


class LimitedStub(BaseHTTPRequestHandler):
    """Трекер со счётчиком на пару (метод, шаблон пути) — как живой."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    @staticmethod
    def bucket(method, path):
        parts = path.split("/")
        norm = ["{id}" if p.startswith(("T-", "P-")) else p for p in parts]
        return f"{method} {'/'.join(norm)}"

    def _count(self, key):
        """Сколько потрачено в каждом окне. Окно кончилось — счёт с нуля."""
        now = stub_now()
        with STATE["lock"]:
            st = STATE["buckets"].setdefault(
                key, {"short": [0, now + WINDOW_SHORT], "long": [0, now + WINDOW_LONG]})
            for window, span in (("short", WINDOW_SHORT), ("long", WINDOW_LONG)):
                if now >= st[window][1]:
                    st[window] = [0, now + span]
                st[window][0] += 1
            return {"short": (st["short"][0], st["short"][1] - now),
                    "long": (st["long"][0], st["long"][1] - now)}

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if STATE["always_429"]:
            return self._send({"statusCode": 429,
                               "message": "ThrottlerException: Too Many Requests"}, 429)
        spent = self._count(self.bucket("GET", path))
        left_short = LIMIT_SHORT - spent["short"][0]
        left_long = LIMIT_LONG - spent["long"][0]
        if STATE["long_left_override"] is not None:
            left_long = STATE["long_left_override"]
        if left_short < 0 or left_long < 0:
            # Ровно как живой сервер: отказ БЕЗ единого заголовка про лимит.
            return self._send({"statusCode": 429,
                               "message": "ThrottlerException: Too Many Requests"}, 429)
        self._send({"tasks": [], "pagination": {"total": 0}}, headers={
            "X-RateLimit-Limit-short": LIMIT_SHORT,
            "X-RateLimit-Remaining-short": left_short,
            "X-RateLimit-Reset-short": int(spent["short"][1]),
            "X-RateLimit-Limit-long": LIMIT_LONG,
            "X-RateLimit-Remaining-long": left_long,
            "X-RateLimit-Reset-long": int(spent["long"][1]),
        })

    def _send(self, payload, code=200, headers=None):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(raw)


class FakeClock:
    """Часы вместо модуля `time` внутри sing.

    Сон не настоящий: он двигает ЧАСЫ — и клиента, и заглушки сразу. Иначе
    проверка края стоила бы реального минутного окна, а быстрый набор обязан
    оставаться быстрым. Двигать надо обе стороны: клиент, поспавший «вхолостую»,
    получил бы от сервера тот же отказ, и проверка врала бы в зелёную сторону.
    """

    def __init__(self):
        self.slept = []

    def time(self):
        return time.time() + STATE["skew"]

    def sleep(self, seconds):
        self.slept.append(seconds)
        with STATE["lock"]:
            STATE["skew"] += seconds


class RateLimitTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), LimitedStub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        with support.env(SINGULARITY_API=base):
            cls.sing = support.load_module("sing_limits", SING_UNDER_TEST)
        cls.sing.get_token = lambda: "stub"
        assert cls.sing.API == base, "клиент не нацелился на заглушку"
        if SING_UNDER_TEST != support.SING:
            print(f"\n  ⚠ контрольный прогон: {SING_UNDER_TEST}", file=sys.stderr)

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        sing = self.sing
        # Пропуска «в этой версии учёта нет» здесь намеренно нет: на коде без
        # учёта набор обязан покраснеть, а не позеленеть тихим skip.
        with STATE["lock"]:
            STATE["buckets"].clear()
            STATE["skew"] = 0.0
            STATE["long_left_override"] = None
            STATE["always_429"] = False
        sing._RATE.clear()
        sing._RATE_WARNED.clear()
        self.addCleanup(sing._RATE.clear)
        self.addCleanup(sing._RATE_WARNED.clear)
        self.clock = FakeClock()
        real_time = sing.time
        sing.time = self.clock
        self.addCleanup(setattr, sing, "time", real_time)
        for k, v in (("NET_BACKOFF", 0.01), ("THROTTLE_RETRIES", 2)):
            self.addCleanup(setattr, sing, k, getattr(sing, k))
            setattr(sing, k, v)
        os.environ.pop("SINGULARITY_NO_RATE_WAIT", None)
        self.addCleanup(os.environ.pop, "SINGULARITY_NO_RATE_WAIT", None)

    def hammer(self, count, path="/task"):
        """Гонять одну ручку и вернуть (сколько прошло, stderr)."""
        err, sent = io.StringIO(), 0
        with redirect_stderr(err):
            for i in range(count):
                try:
                    self.sing.request("GET", path)
                except SystemExit:
                    break
                sent += 1
        return sent, err.getvalue()

    # --- 1. остаток запоминается ------------------------------------------

    def test_counters_from_headers_are_remembered_per_bucket(self):
        """Заголовки успешного ответа разбираются и ложатся в свой бакет."""
        self.sing.request("GET", "/task")
        self.sing.request("GET", f"/task/{TASK_A}")
        self.sing.request("GET", f"/task/{TASK_B}")
        by_id = self.sing.rate_known("GET /task/{id}", "short")
        listing = self.sing.rate_known("GET /task", "short")
        self.assertEqual(by_id["limit"], LIMIT_SHORT)
        self.assertEqual(by_id["left"], LIMIT_SHORT - 2,
                         "две разные задачи по id обязаны идти ОДНОЙ кассой")
        self.assertEqual(listing["left"], LIMIT_SHORT - 1,
                         "выборка по проекту не должна делить счётчик с /task/{id}")
        self.assertIn("GET /task/{id}", dict(self.sing.rate_report()))

    # --- 2. край замечен ДО отказа ----------------------------------------

    def test_preflight_pause_keeps_the_run_off_the_refusal(self):
        """Цикл длиннее лимита проходит целиком: скилл ждёт сам, 429 не случается."""
        sent, err = self.hammer(LIMIT_SHORT + 3)
        self.assertEqual(sent, LIMIT_SHORT + 3, f"цикл оборвался: {err}")
        # Искать «429» здесь нельзя: это слово есть в тексте самой паузы («чтобы
        # не упереться в 429») — проверка ловила бы собственное сообщение.
        self.assertNotIn("ThrottlerException", err, "до отказа всё-таки дошло")
        self.assertIn("⏳", err, "паузы не было — значит и упреждения нет")
        self.assertTrue(self.clock.slept, "скилл не спал, а обязан был")

    def test_same_run_without_the_counters_hits_429(self):
        """Контроль: выключи учёт — и тот же цикл падает. Без него проверка пустая."""
        os.environ["SINGULARITY_NO_RATE_WAIT"] = "1"
        sent, err = self.hammer(LIMIT_SHORT + 3)
        self.assertEqual(sent, LIMIT_SHORT, "лимит заглушки не сработал")
        self.assertIn("429", err)
        self.assertNotIn("⏳", err, "с выключенным учётом пауз быть не должно")

    # --- 3. на ровном месте не спим ---------------------------------------

    def test_no_pause_while_the_budget_is_untouched(self):
        """Пока запас цел, скилл не спит: цена учёта — ноль запросов и ноль секунд."""
        sent, err = self.hammer(LIMIT_SHORT - self.sing.RATE_RESERVE - 1)
        self.assertEqual(self.clock.slept, [], f"сон на ровном месте: {err}")
        self.assertNotIn("⏳", err)
        self.assertGreater(sent, 0)

    def test_stale_knowledge_does_not_cause_a_pause(self):
        """Окно, которое давно сброшено, — это НЕ «остатка нет»."""
        self.sing._RATE["GET /task"] = {
            "short": {"limit": 100, "left": 0,
                      "reset_at": self.clock.time() - 1}}
        err = io.StringIO()
        with redirect_stderr(err):
            waited = self.sing.rate_pause("GET /task", sleep=self.clock.sleep, out=err)
        self.assertEqual(waited, 0.0)
        self.assertEqual(self.clock.slept, [])

    # --- 4. часовое окно не пережидаем ------------------------------------

    def test_hour_window_is_warned_about_but_never_waited_out(self):
        """До сброса часового окна бывает 50 минут — спать столько нельзя."""
        with STATE["lock"]:
            STATE["long_left_override"] = 0
        err = io.StringIO()
        with redirect_stderr(err):
            self.sing.request("GET", "/task")          # принесёт long=0
            self.sing.rate_pause("GET /task", sleep=self.clock.sleep, out=err)
        self.assertEqual(self.clock.slept, [], "скилл собрался ждать часовое окно")
        self.assertIn("часовой счётчик", err.getvalue(),
                      "про исчерпанный часовой лимит не сказано ничего")

    # --- 5. политика повторов на 429 не тронута ---------------------------

    def test_refusal_does_not_erase_what_the_server_said(self):
        """`429` приходит без заголовков — прежнее знание он затирать не смеет."""
        self.sing.request("GET", "/task")
        before = dict(self.sing.rate_known("GET /task", "short"))
        os.environ["SINGULARITY_NO_RATE_WAIT"] = "1"
        self.hammer(LIMIT_SHORT + 2)
        after = self.sing.rate_known("GET /task", "short")
        self.assertIsNotNone(after, "состояние бакета пропало после 429")
        self.assertEqual(after["limit"], before["limit"])

    def test_pause_happens_once_per_request_not_on_every_retry(self):
        """Упреждение — только на первой попытке.

        Иначе четыре повтора на `429` превратились бы в четыре минутных сна:
        один запрос вместо внятного отказа за 9 с висел бы минуты. Политика
        повторов принята как Accepted и этим решением не отменяется.
        """
        with STATE["lock"]:              # сервер отказывает при любом раскладе
            STATE["always_429"] = True
        # клиент думает, что запас кончился и ждать до сброса далеко
        self.sing._RATE["GET /task"] = {
            "short": {"limit": LIMIT_SHORT, "left": 0,
                      "reset_at": self.clock.time() + 600}}
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                self.sing.request("GET", "/task")
        text = err.getvalue()
        self.assertEqual(text.count("⏳"), 1,
                         f"упреждающих пауз больше одной на запрос:\n{text}")
        self.assertIn("429", text, "отказ перестал быть отказом")

    # --- форма ключа ------------------------------------------------------

    def test_bucket_key_is_method_plus_route_template(self):
        """Ключ — метод и шаблон маршрута; id из пути вычищается, query не в счёт."""
        b = self.sing.rate_bucket
        self.assertEqual(b("GET", "/task/T-f17f2d59-dc95-4723-8cef-f4424bb54a93"),
                         "GET /task/{id}")
        self.assertEqual(b("GET", f"/task/{TASK_A}"), b("GET", f"/task/{TASK_B}"))
        self.assertNotEqual(b("GET", "/task"), b("POST", "/task"))
        self.assertNotEqual(b("GET", "/task"), b("GET", f"/task/{TASK_A}"))
        self.assertEqual(b("POST", f"/task/{TASK_A}/complete"),
                         "POST /task/{id}/complete")
        self.assertEqual(b("DELETE", "/kanban-status/KS-P-e33c2f1a-edd5-4c90-TODO"),
                         "DELETE /kanban-status/{id}")
        # имена ручек не должны принять за идентификаторы
        for path in ("/kanban-status", "/kanban-task-status", "/checklist-item",
                     "/tag", "/project"):
            self.assertEqual(b("GET", path), f"GET {path}")


if __name__ == "__main__":
    unittest.main()
