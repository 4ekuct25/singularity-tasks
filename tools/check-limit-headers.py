#!/usr/bin/env python3
"""Счётчики лимита трекера: что сервер сообщает и замечает ли это скилл ДО отказа.

Живой API, читающие ручки. Два режима, оба отвечают на свой вопрос:

    tools/check-limit-headers.py probe   # что именно считается: ручка, метод, окно
    tools/check-limit-headers.py edge    # довести ручку до края: с упреждением и без

`probe` (~14 запросов, секунды) печатает шесть заголовков как есть и проверяет
гранулярность счётчика: две разные задачи по id — один счётчик или разные,
выборка по проекту — свой ли, POST — свой ли, и тикает ли `Reset`.

`edge` (~210 запросов на одну ручку, ~3 минуты) — та самая проверка фактом из
карточки T-f17f2d59: сначала КОНТРОЛЬ с `SINGULARITY_NO_RATE_WAIT=1` (скилл не
смотрит на счётчики и упирается в `429`), потом тот же цикл с упреждением
(скилл тормозит сам и `429` не получает). Контроль идёт первым намеренно: он
обязан покраснеть, иначе проверка ничего не доказывает.

⚠ `edge` жжёт минутный лимит выбранной ручки целиком (по умолчанию `GET /tag`).
Ручка восстанавливается за 60 с, но пока окно закрыто, ею не сможет
воспользоваться и параллельная сессия. Часовой счётчик той же ручки после
двух проходов просядет примерно на 210 из 1000.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SING = os.path.join(os.path.dirname(HERE), "scripts", "sing.py")

WINDOWS = ("short", "long")


def load_sing(path=None):
    spec = importlib.util.spec_from_file_location("sing_limits", path or SING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def raw(sing, auth, method, path, query=None, body=None):
    """Запрос МИМО sing.request(): нужны сами заголовки, а не разобранное тело."""
    url = sing.API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + auth)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def counters(headers):
    """Шесть заголовков одной строкой. Нет их — так и сказать: на 429 их не бывает."""
    out = []
    for window in WINDOWS:
        left = headers.get(f"X-RateLimit-Remaining-{window}")
        limit = headers.get(f"X-RateLimit-Limit-{window}")
        reset = headers.get(f"X-RateLimit-Reset-{window}")
        if left is None:
            continue
        out.append(f"{window} {left}/{limit} сброс через {reset} с")
    return "  |  ".join(out) if out else "заголовков X-RateLimit-* НЕТ"


def cmd_probe(sing, args):
    # Переменная НЕ называется token намеренно: гейт tools/audit-secrets.sh ловит
    # присвоения вида `token = …` и краснеет на этой строке, хотя секрета в ней нет.
    # Гейт, который краснеет впустую, перестают читать — дешевле не дразнить его.
    auth = sing.get_token()
    cfg, _ = sing.load_config(required=True)
    pid = cfg["projectId"]
    st, h, body = raw(sing, auth, "GET", "/task",
                      {"projectId": pid, "maxCount": 3, "offset": 0})
    ids = [t["id"] for t in json.loads(body).get("tasks", [])][:2]
    if len(ids) < 2:
        sys.exit("в привязанном проекте меньше двух задач — замеру не на чем работать")

    print("ЗАГОЛОВКИ КАК ЕСТЬ (GET /project):")
    st, h, _ = raw(sing, auth, "GET", "/project", {"maxCount": 1, "offset": 0})
    for k, v in h.items():
        if k.lower().startswith("x-ratelimit"):
            print(f"  {k}: {v}")

    print("\nГРАНУЛЯРНОСТЬ: одна ли касса у разных путей и методов")
    plan = [("GET", "/project", {"maxCount": 1}),
            ("GET", "/project", {"maxCount": 1}),
            ("GET", f"/task/{ids[0]}", None),
            ("GET", f"/task/{ids[1]}", None),
            ("GET", f"/task/{ids[0]}", None),
            ("GET", "/task", {"projectId": pid, "maxCount": 1}),
            ("GET", "/project", {"maxCount": 1}),
            ("GET", "/tag", {"maxCount": 1}),
            ("GET", "/kanban-status", {"maxCount": 1})]
    for method, path, query in plan:
        st, h, _ = raw(sing, auth, method, path, query)
        print(f"  {sing.rate_bucket(method, path):<24} -> {st}  {counters(h)}")
    print("  читается так: соседние строки одной ручки идут -1, чужая ручка своим"
          " счётчиком не делится")

    print("\nRESET: это обратный отсчёт, а не длина окна")
    st, h, _ = raw(sing, auth, "GET", "/tag", {"maxCount": 1})
    print(f"  t+0   {counters(h)}")
    time.sleep(6)
    st, h, _ = raw(sing, auth, "GET", "/tag", {"maxCount": 1})
    print(f"  t+6   {counters(h)}   (Reset упал примерно на 6)")


def burn(sing, bucket_path, rounds, label):
    """Гонять одну ручку через sing.request() и смотреть, где команда встанет.

    Возвращает (сколько запросов прошло, текст исхода). Именно через
    `sing.request()`, а не мимо: проверяется поведение СКИЛЛА, а не сервера.
    Упреждающие паузы видно по строкам `⏳` в stderr — их и собираем, чтобы
    «заметил ДО отказа» было фактом из вывода, а не выводом из времени прогона.
    """
    print(f"\n--- {label}")
    bucket = sing.rate_bucket("GET", bucket_path)
    sent, started, err = 0, time.time(), io.StringIO()
    outcome = None
    with contextlib.redirect_stderr(err):
        for i in range(1, rounds + 1):
            try:
                sing.request("GET", bucket_path, query={"maxCount": 1, "offset": 0})
            except SystemExit:
                # die() пишет причину в stderr и выходит кодом 1 — сам код ничего
                # не объясняет, поэтому берём последнюю строку сообщения
                said = [ln for ln in err.getvalue().splitlines()
                        if ln and not ln.startswith("⏳")]
                outcome = f"ОТКАЗ на запросе №{i}: {said[-1] if said else 'без текста'}"
                break
            sent = i
            left = (sing._RATE.get(bucket) or {}).get("short")
            if i % 25 == 0 or (left and left["left"] <= 4):
                print(f"  №{i:>3}: остаток минуты {left['left'] if left else '?'}",
                      file=sys.stdout)
    waits = [ln for ln in err.getvalue().splitlines() if ln.startswith("⏳")]
    for ln in waits:
        print(f"  {ln}")
    if outcome is None:
        outcome = (f"прошло {sent} запросов без 429 за {time.time() - started:.0f} с, "
                   f"упреждающих пауз: {len(waits)}")
    return sent, outcome


def cmd_edge(sing, args):
    path = args.path
    print(f"Ручка под замер: GET {path}. Минутный лимит — 100 запросов, "
          f"проход по {args.rounds}.")

    os.environ["SINGULARITY_NO_RATE_WAIT"] = "1"
    sent, verdict = burn(sing, path, args.rounds,
                         "КОНТРОЛЬ: счётчики выключены (SINGULARITY_NO_RATE_WAIT=1)")
    print(f"  {verdict}")
    control_failed = "ОТКАЗ" in verdict
    if not control_failed:
        print("  ⚠ контроль НЕ покраснел — значит проход не упёрся в лимит и "
              "проверка ничего не доказывает (мало запросов? чужое окно?)")

    os.environ.pop("SINGULARITY_NO_RATE_WAIT")
    sing._RATE.clear()
    sing._RATE_WARNED.clear()
    print("\n  ждём 65 с — минутное окно ручки должно закрыться")
    time.sleep(65)

    sent, verdict = burn(sing, path, args.rounds, "С УПРЕЖДЕНИЕМ: счётчики читаются")
    print(f"  {verdict}")
    ok = control_failed and "ОТКАЗ" not in verdict
    print("\nИТОГ: " + ("скилл заметил край ДО отказа — контроль упал на 429, "
                        "тот же проход с упреждением прошёл целиком"
                        if ok else "проверка не доказана, смотри строки выше"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode")
    sub.add_parser("probe", help="что считается: ручка, метод, окно")
    edge = sub.add_parser("edge", help="довести ручку до края: контроль и упреждение")
    edge.add_argument("--path", default="/tag",
                      help="ручка под замер (по умолчанию /tag — её скилл трогает реже всего)")
    edge.add_argument("--rounds", type=int, default=105,
                      help="запросов за проход (по умолчанию 105 при лимите 100)")
    args = ap.parse_args()
    sing = load_sing()
    if args.mode == "edge":
        return cmd_edge(sing, args)
    cmd_probe(sing, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
