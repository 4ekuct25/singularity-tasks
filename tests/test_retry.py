"""Быстрые проверки политики повторов: локальная HTTP-заглушка, без токена.

Заглушка не своя — берётся из `tools/check-retry.py` (класс `Stub` и общий `STATE`),
чтобы не держать два разных сервера-двойника с разным поведением.

Разделение труда с `tools/check-retry.py`:
  · здесь — РЕШЕНИЯ политики: сколько запросов дошло до сервера, какие методы
    повторяются, читается ли `Retry-After`, срабатывают ли потолки. Константы на
    время проверки уменьшены, поэтому весь файл укладывается в секунды;
  · там — те же сценарии на БОЕВЫХ константах с замером фактических пауз
    (19 сценариев, ~50 с). Запуск: `tests/run.py slow`.

Токен здесь не нужен и не читается: `get_token` подменён заглушкой. Это не
косметика — быстрый набор обязан проходить в чужом окружении, где Keychain пуст.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

check_retry = support.load_tool("check-retry.py")


def ra(value):
    return (429, {"Retry-After": str(value)}, {"error": "slow down"})


def http_date(delta):
    """Дата считается в момент прогона: собранная заранее успеет протухнуть,
    пауза выйдет нулевой, и сценарий «проверит» не то."""
    return lambda: (429, {"Retry-After": time.strftime(
        "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + delta))},
        {"error": "slow down"})


PLAIN_429 = (429, {}, {"error": "slow down"})
E500 = (500, {}, {"error": "boom"})


class RetryPolicyTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), check_retry.Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls._stop)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.sing = support.load_sing("sing_retry", api=base)
        # Токен не ищем вовсе: ни Keychain, ни переменной окружения.
        cls.sing.get_token = lambda: "stub"
        assert cls.sing.API == base, "клиент не нацелился на заглушку"

    @classmethod
    def _stop(cls):
        cls.srv.shutdown()
        # ⚠ без server_close() сокет остаётся слушающим: соединение принимается и
        # висит до NET_TIMEOUT — это не «порт закрыт», а «сервер не отвечает»
        cls.srv.server_close()

    def setUp(self):
        # Быстрые константы: здесь меряются решения, а не боевые задержки.
        self.policy(NET_BACKOFF=0.05, RETRY_AFTER_CAP=30, RETRY_WAIT_BUDGET=90,
                    NET_RETRIES=3, THROTTLE_RETRIES=4)

    def policy(self, **values):
        for k, v in values.items():
            self.addCleanup(setattr, self.sing, k, getattr(self.sing, k))
            setattr(self.sing, k, v)

    def call(self, script, method="GET", path="/task", body=None, soft=False):
        """Прогнать запрос против сценария ответов и вернуть факты."""
        with check_retry.STATE["lock"]:
            check_retry.STATE["script"][:] = [s() if callable(s) else s for s in script]
            check_retry.STATE["log"][:] = []
        started = time.monotonic()
        outcome, result = None, None
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                result = self.sing.request(method, path, body=body, soft=soft)
            outcome = "none" if result is None else "ok"
        except SystemExit as e:
            outcome = f"exit {e.code}"
        elapsed = time.monotonic() - started
        with check_retry.STATE["lock"]:
            attempts = len(check_retry.STATE["log"])
        return {"attempts": attempts, "elapsed": elapsed, "outcome": outcome,
                "result": result, "stderr": err.getvalue()}

    # ---------------------------------------------------------------- Retry-After

    def test_numeric_retry_after_is_obeyed_over_own_backoff(self):
        """Сколько ждать, говорит сервер. Своя пауза — только если он молчит.

        Контроль осмысленности: своя пауза выставлена в 20 раз меньше, поэтому
        «не разобрали заголовок» и «разобрали» различаются замером, а не на глаз.
        """
        self.policy(NET_BACKOFF=0.02)
        r = self.call([ra(0.4)])
        self.assertEqual(r["attempts"], 2)
        self.assertGreater(r["elapsed"], 0.3, "Retry-After проигнорирован")
        self.assertLess(r["elapsed"], 1.0)
        self.assertEqual(r["outcome"], "ok")

    def test_http_date_retry_after_is_parsed(self):
        """Retry-After бывает HTTP-датой, а не числом. Нераспознанная дата дала бы
        свою паузу 0.05 — от двух секунд отличается замером."""
        self.policy(NET_BACKOFF=0.05)
        r = self.call([http_date(2)])
        self.assertEqual(r["attempts"], 2)
        self.assertGreater(r["elapsed"], 1.0, "HTTP-дата в Retry-After не разобрана")
        self.assertLess(r["elapsed"], 3.5)

    def test_garbage_retry_after_falls_back_to_own_backoff(self):
        # значение заголовка — только latin-1: кириллица роняет саму заглушку
        self.policy(NET_BACKOFF=0.1)
        r = self.call([(429, {"Retry-After": "whenever"}, {"error": "slow down"})])
        self.assertEqual(r["attempts"], 2)
        self.assertGreater(r["elapsed"], 0.05)
        self.assertLess(r["elapsed"], 1.0)
        self.assertEqual(r["outcome"], "ok")

    def test_single_pause_is_capped(self):
        """Сервер может попросить Retry-After: 3600 — столько ждать бессмысленно."""
        self.policy(RETRY_AFTER_CAP=0.1)
        r = self.call([ra(3600)] * 9)
        self.assertEqual(r["attempts"], 4)
        self.assertLess(r["elapsed"], 2.0, "потолок одной паузы не сработал")
        self.assertEqual(r["outcome"], "exit 1")

    def test_total_wait_is_capped_by_budget(self):
        self.policy(RETRY_WAIT_BUDGET=0.3)
        r = self.call([ra(0.2)] * 9)
        self.assertEqual(r["attempts"], 3, "бюджет ожидания не ограничил число попыток")
        self.assertLess(r["elapsed"], 1.0)
        self.assertEqual(r["outcome"], "exit 1")

    # ---------------------------------------------------------------- что повторяем

    def test_429_is_retried_on_post_because_server_refused_to_serve(self):
        """429 — отказ ОБСЛУЖИТЬ: запрос не выполнен, второго объекта он создать
        не мог, поэтому повтор безопасен для любого метода."""
        r = self.call([ra(0.05)], method="POST", body={"title": "x"})
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(r["outcome"], "ok")

    def test_429_without_header_still_retries_with_growing_pause(self):
        r = self.call([PLAIN_429, PLAIN_429])
        self.assertEqual(r["attempts"], 3)
        self.assertEqual(r["outcome"], "ok")

    def test_429_gives_up_after_throttle_retries(self):
        r = self.call([ra(0.05)] * 9)
        self.assertEqual(r["attempts"], 4)
        self.assertEqual(r["outcome"], "exit 1")
        self.assertIn("HTTP 429", r["stderr"])

    def test_5xx_is_retried_on_get_only(self):
        """5xx — «принял и сломался»: запрос мог примениться. Повторяем только там,
        где повтор ничего не создаёт, то есть на GET."""
        self.assertEqual(self.call([E500, E500])["attempts"], 3)
        self.assertEqual(self.call([E500] * 9)["attempts"], 3)

    def test_5xx_is_not_retried_on_post_or_patch(self):
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                r = self.call([E500] * 9, method=method, body={"title": "x"})
                self.assertEqual(r["attempts"], 1,
                                 f"{method} повторён после 5xx — можно создать дубль")
                self.assertEqual(r["outcome"], "exit 1")

    def test_4xx_is_never_retried(self):
        r = self.call([(404, {}, {"error": "no such task"})] * 9)
        self.assertEqual(r["attempts"], 1)
        self.assertIn("HTTP 404", r["stderr"])

    def test_401_says_it_is_about_the_token(self):
        r = self.call([(401, {}, {"error": "unauthorized"})] * 9)
        self.assertEqual(r["attempts"], 1)
        self.assertIn("401 Unauthorized", r["stderr"])

    # ---------------------------------------------------------------- soft

    def test_soft_returns_none_only_after_retries_are_spent(self):
        r = self.call([ra(0.05)] * 9, soft=True)
        self.assertEqual(r["attempts"], 4)
        self.assertEqual(r["outcome"], "none")

    def test_soft_returns_data_when_retry_succeeds(self):
        r = self.call([ra(0.05), ra(0.05)], soft=True)
        self.assertEqual(r["attempts"], 3)
        self.assertEqual(r["outcome"], "ok")

    def test_success_costs_exactly_one_request_and_no_pause(self):
        r = self.call([])
        self.assertEqual(r["attempts"], 1)
        self.assertLess(r["elapsed"], 0.5)
        self.assertEqual(r["outcome"], "ok")

    def test_unreachable_port_is_reported_not_hung(self):
        """Закрытый порт — URLError, вторая ветка повторов. GET повторяется
        NET_RETRIES раз и выходит с кодом, а не висит."""
        saved = self.sing.API
        self.addCleanup(setattr, self.sing, "API", saved)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        self.sing.API = f"http://127.0.0.1:{dead}"
        r = self.call([])
        self.assertEqual(r["outcome"], "exit 1")
        self.assertIn("Сеть недоступна", r["stderr"])


if __name__ == "__main__":
    unittest.main()
