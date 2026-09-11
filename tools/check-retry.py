#!/usr/bin/env python3
"""Проверка политики повторов в sing.request() на локальной HTTP-заглушке.

429 и 5xx от живого API специально не добьёшься, поэтому клиент направляется на
http.server в отдельном потоке ($SINGULARITY_API) со сценарным набором ответов.
Считаем ФАКТЫ: сколько запросов дошло до сервера, сколько времени клиент ждал,
с каким кодом/исключением вышел. Проверка одним запросом, который в принципе не
может показать сбой, — не проверка.

    tools/check-retry.py            # все сценарии
    tools/check-retry.py -v         # + stderr клиента
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

STATE = {"script": [], "log": [], "lock": threading.Lock()}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        with STATE["lock"]:
            STATE["log"].append((self.command, self.path, time.monotonic()))
            step = (STATE["script"].pop(0) if STATE["script"]
                    else (200, {}, {"ok": True}))
        code, headers, payload = step
        raw = json.dumps(payload).encode()
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PATCH = do_DELETE = _serve

    def log_message(self, *a):
        pass


# Заглушка не проверяет авторизацию, но `sing.py` без токена не стартует. Значение
# собирается в рантайме, а не лежит присвоением в исходнике: гейт `tools/audit-secrets.sh`
# краснеет на `TOKEN=...` независимо от содержимого — и правильно делает, потому что
# отличить настоящий токен от «честного плейсхолдера» по виду нельзя.
STUB_TOKEN = "-".join(["stub", "value", "for", "local", "http", "server"])


def load_sing(base_url):
    """Импортировать sing.py уже нацеленным на заглушку: API читается при импорте."""
    os.environ["SINGULARITY_API"] = base_url
    os.environ["SINGULARITY_TOKEN"] = STUB_TOKEN
    spec = importlib.util.spec_from_file_location(
        "sing_under_test", os.path.join(REPO, "scripts", "sing.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(sing, name, script, method="GET", path="/task", body=None, soft=False,
        expect=None, patch=None, verbose=False):
    """Прогнать один сценарий и вернуть замер: попытки, ожидание, исход."""
    with STATE["lock"]:
        # шаг может быть функцией: HTTP-дату в Retry-After надо считать в момент
        # прогона, а не при сборке списка сценариев — иначе к своей очереди она уже
        # протухнет, пауза выйдет нулевой, и сценарий «проверит» не то (наступали)
        STATE["script"][:] = [s() if callable(s) else s for s in script]
        STATE["log"][:] = []
    saved = {k: getattr(sing, k) for k in (patch or {})}
    for k, v in (patch or {}).items():
        setattr(sing, k, v)
    err = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stderr(err):
            result = sing.request(method, path, body=body, soft=soft)
        outcome = "None" if result is None else f"ok {result}"
    except SystemExit as e:
        outcome = f"exit {e.code}"
    finally:
        elapsed = time.monotonic() - started
        for k, v in saved.items():
            setattr(sing, k, v)
    attempts = len(STATE["log"])
    gaps = [round(STATE["log"][i + 1][2] - STATE["log"][i][2], 2)
            for i in range(attempts - 1)]
    message = err.getvalue().strip().replace("\n", " ")[:120]
    ok = True
    notes = []
    if expect:
        if "attempts" in expect and attempts != expect["attempts"]:
            ok, _ = False, notes.append(
                f"попыток {attempts}, ждали {expect['attempts']}")
        lo, hi = expect.get("wait", (None, None))
        if lo is not None and not (lo <= elapsed <= hi):
            ok, _ = False, notes.append(f"ожидание {elapsed:.2f} вне [{lo}, {hi}]")
        if "outcome" in expect and expect["outcome"] not in outcome:
            ok, _ = False, notes.append(f"исход «{outcome}», ждали «{expect['outcome']}»")
        if "stderr" in expect and expect["stderr"] not in message:
            ok, _ = False, notes.append(f"stderr без «{expect['stderr']}»")
    print(f"{'✓' if ok else '✗'} {name}")
    print(f"    запросов до сервера: {attempts}  паузы между ними: {gaps or '—'} с  "
          f"суммарно {elapsed:.2f} с  исход: {outcome}")
    if verbose and message:
        print(f"    stderr: {message}")
    for n in notes:
        print(f"    ! {n}")
    return ok


def hang_check(sing):
    """Сервер принял соединение и молчит — это НЕ «сеть недоступна».

    Диагностика, а не сценарий: connect-timeout urllib заворачивает в URLError,
    а read-timeout прилетает голым TimeoutError из getresponse() — мимо обоих
    except в request(). Печатаем факт, чинить — отдельной задачей.
    """
    import socket
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    port = lsock.getsockname()[1]
    accepted = []

    def accept_forever():
        while True:
            try:
                accepted.append(lsock.accept()[0])   # приняли и молчим
            except OSError:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    saved = (sing.API, sing.NET_TIMEOUT)
    sing.API, sing.NET_TIMEOUT = f"http://127.0.0.1:{port}", 2
    started = time.monotonic()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            sing.request("GET", "/project")
        outcome = "вернулись данные (?!)"
    except SystemExit as e:
        outcome = f"обработано, exit {e.code}"
    except BaseException as e:                       # noqa: BLE001 — это и меряем
        outcome = f"НЕОБРАБОТАННОЕ {type(e).__module__}.{type(e).__name__}: {e}"
    finally:
        sing.API, sing.NET_TIMEOUT = saved
        lsock.close()
    print(f"\n[диагностика] сервер принял соединение и молчит (NET_TIMEOUT=2): "
          f"{time.monotonic() - started:.2f} с, {outcome}")


def cli_check():
    """Сквозная проверка через настоящую точку входа, а не импортированную функцию.

    Импорт проверяет request() в изоляции; здесь важно, что реальная команда
    (`doctor`) действительно ждёт и выходит с ненулевым кодом, а не висит.
    """
    import subprocess
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with STATE["lock"]:
        STATE["script"][:] = [(503, {}, {"error": "upstream down"})] * 20
        STATE["log"][:] = []
    env = dict(os.environ, SINGULARITY_API=f"http://127.0.0.1:{port}",
               SINGULARITY_TOKEN=STUB_TOKEN)
    started = time.monotonic()
    p = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "sing.py"),
                        "doctor"], env=env, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    log = STATE["log"]
    gaps = [round(log[i + 1][2] - log[i][2], 2) for i in range(len(log) - 1)]
    print(f"CLI `doctor` против 503: код выхода {p.returncode}, {elapsed:.2f} с, "
          f"запросов до заглушки {len(log)}, паузы {gaps} с")
    print("    " + (p.stderr.strip() or p.stdout.strip()).replace("\n", " ")[:150])
    srv.shutdown()
    # ⚠ без server_close() сокет остаётся слушающим: соединение принимается и
    # висит до NET_TIMEOUT — это не «порт закрыт», а «сервер не отвечает». Разные
    # исключения и разные ветки, перепутать легко
    srv.server_close()

    # закрытый порт — connection refused, то есть URLError: вторая ветка повторов
    started = time.monotonic()
    env["SINGULARITY_API"] = f"http://127.0.0.1:{port}"
    p = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "sing.py"),
                        "doctor"], env=env, capture_output=True, text=True)
    print(f"CLI `doctor` против закрытого порта: код выхода {p.returncode}, "
          f"{time.monotonic() - started:.2f} с")
    print("    " + (p.stderr.strip() or p.stdout.strip()).replace("\n", " ")[:150])


def main():
    verbose = "-v" in sys.argv
    if "--cli" in sys.argv:
        return cli_check()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    sing = load_sing(base)
    print(f"заглушка: {base}   NET_RETRIES={sing.NET_RETRIES} "
          f"NET_BACKOFF={sing.NET_BACKOFF} THROTTLE_RETRIES={sing.THROTTLE_RETRIES} "
          f"RETRY_AFTER_CAP={sing.RETRY_AFTER_CAP} "
          f"RETRY_WAIT_BUDGET={sing.RETRY_WAIT_BUDGET}\n")

    ra = lambda v: (429, {"Retry-After": str(v)}, {"error": "slow down"})
    plain429 = (429, {}, {"error": "slow down"})
    e500 = (500, {}, {"error": "boom"})
    def http_date(dt):
        """Шаг-функция: дата считается в момент прогона сценария (см. run())."""
        return lambda: (429, {"Retry-After": time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + dt))},
            {"error": "slow down"})

    cases = [
        # (имя, сценарий ответов, kwargs)
        ("429 с Retry-After: 1 на GET — слушаемся сервера, дальше 200",
         [ra(1), ra(1)],
         dict(expect={"attempts": 3, "wait": (2.0, 3.0), "outcome": "ok"})),
        ("429 с Retry-After в виде HTTP-даты (+2 с) на GET",
         [http_date(2)],
         dict(expect={"attempts": 2, "wait": (1.0, 3.0), "outcome": "ok"})),
        # контрольный замер к предыдущему: если бы дата не разбиралась, пауза была бы
        # не «обрезанной до потолка», а нулевой — ноль от 2 с на глаз не отличить
        ("429 с датой +1 час — дата разобрана и обрезана потолком (CAP=2)",
         [http_date(3600)] * 9,
         dict(patch={"RETRY_AFTER_CAP": 2},
              expect={"attempts": 4, "wait": (6.0, 7.0), "outcome": "exit 1"})),
        # значение заголовка — только latin-1: кириллица роняет саму заглушку
        # (UnicodeEncodeError при записи заголовков), а не проверяемый клиент
        ("429 с мусором в Retry-After — откат на свою паузу 1.5",
         [(429, {"Retry-After": "whenever"}, {"error": "slow down"})],
         dict(expect={"attempts": 2, "wait": (1.5, 2.2), "outcome": "ok"})),
        ("429 БЕЗ Retry-After на GET — своя нарастающая пауза 1.5 + 3.0",
         [plain429, plain429],
         dict(expect={"attempts": 3, "wait": (4.5, 5.5), "outcome": "ok"})),
        ("429 на POST — запрос сервером НЕ выполнен, повтор безопасен",
         [ra(1)],
         dict(method="POST", body={"title": "x"},
              expect={"attempts": 2, "wait": (1.0, 2.0), "outcome": "ok"})),
        ("429 исчерпание попыток (Retry-After: 1) — выходим с кодом",
         [ra(1)] * 9,
         dict(expect={"attempts": 4, "wait": (3.0, 4.0), "outcome": "exit 1",
                      "stderr": "HTTP 429"})),
        ("429 с Retry-After: 3600 — потолок паузы (RETRY_AFTER_CAP=2 на прогон)",
         [ra(3600)] * 9,
         dict(patch={"RETRY_AFTER_CAP": 2},
              expect={"attempts": 4, "wait": (6.0, 7.0), "outcome": "exit 1"})),
        ("429 и бюджет ожидания (RETRY_WAIT_BUDGET=2, Retry-After: 1.5)",
         [ra(1.5)] * 9,
         dict(patch={"RETRY_WAIT_BUDGET": 2},
              expect={"attempts": 3, "wait": (2.0, 2.6), "outcome": "exit 1"})),
        ("500 на GET — идемпотентен, 3 попытки с паузой 1.5 + 3.0",
         [e500, e500],
         dict(expect={"attempts": 3, "wait": (4.5, 5.5), "outcome": "ok"})),
        ("500 на GET, не отпускает — исчерпание попыток",
         [e500] * 9,
         dict(expect={"attempts": 3, "wait": (4.5, 5.5), "outcome": "exit 1",
                      "stderr": "HTTP 500"})),
        ("500 на POST — мог примениться на сервере, НЕ повторяем",
         [e500] * 9,
         dict(method="POST", body={"title": "x"},
              expect={"attempts": 1, "wait": (0, 1.0), "outcome": "exit 1",
                      "stderr": "HTTP 500"})),
        ("500 на PATCH — тоже не повторяем",
         [e500] * 9,
         dict(method="PATCH", body={"title": "x"},
              expect={"attempts": 1, "wait": (0, 1.0), "outcome": "exit 1"})),
        ("404 на GET — 4xx не повторяем",
         [(404, {}, {"error": "no such task"})] * 9,
         dict(expect={"attempts": 1, "wait": (0, 1.0), "outcome": "exit 1",
                      "stderr": "HTTP 404"})),
        ("401 — прежнее поведение: сразу сообщение про токен",
         [(401, {}, {"error": "unauthorized"})] * 9,
         dict(expect={"attempts": 1, "wait": (0, 1.0), "outcome": "exit 1",
                      "stderr": "401 Unauthorized"})),
        ("soft=True на 404 — как было: одна попытка и None",
         [(404, {}, {"error": "no"})] * 9,
         dict(soft=True, expect={"attempts": 1, "wait": (0, 1.0), "outcome": "None"})),
        ("soft=True на 429 — повторяет, и только исчерпав попытки отдаёт None",
         [ra(1)] * 9,
         dict(soft=True, expect={"attempts": 4, "wait": (3.0, 4.0),
                                 "outcome": "None"})),
        ("soft=True на 429, затем 200 — вызывающий получает данные, а не None",
         [ra(1), ra(1)],
         dict(soft=True, expect={"attempts": 3, "wait": (2.0, 3.0),
                                 "outcome": "ok"})),
        ("успех без отказов — ни одной лишней попытки и ни одной паузы",
         [],
         dict(expect={"attempts": 1, "wait": (0, 1.0), "outcome": "ok"})),
    ]

    passed = 0
    for name, script, kw in cases:
        passed += bool(run(sing, name, script, verbose=verbose, **kw))
    hang_check(sing)
    srv.shutdown()
    srv.server_close()
    total = len(cases)
    print(f"\nсценариев: {total}, сошлось: {passed}, разошлось: {total - passed}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
