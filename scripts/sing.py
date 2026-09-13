#!/usr/bin/env python3
"""CLI поверх SingularityApp REST API v2 для агентской работы с задачами.

Только stdlib. Прокси берётся из HTTP(S)_PROXY (urllib делает это сам).

Токен ищется по порядку:
  1. $SINGULARITY_TOKEN
  2. macOS Keychain: security find-generic-password -s singularity-app -a rest-token
  3. ~/.claude/.singularity-token (chmod 600)

Привязка репозитория к проекту трекера ищется в <repo>/.agents/singularity.json,
затем .claude/singularity.json, затем в корне (секретов не содержит, коммитится).

Внутри репозитория-эталона самого скилла start и done дополнительно сверяют
установленные копии с деревом (локально, без сети) и молчат, если всё совпадает.
Отключается на запуск: SINGULARITY_NO_SYNC_CHECK=1.
"""

import argparse
import datetime
import email.utils
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("SINGULARITY_API", "https://api.singularity-app.com/v2")
CONFIG_NAME = "singularity.json"

NET_TIMEOUT = 45
NET_RETRIES = 3        # только для GET: повтор POST создаёт второй объект
NET_BACKOFF = 1.5      # секунды, множатся на номер попытки

# Отказ отказу рознь. 429 сервер отдаёт, ОТКАЗАВШИСЬ обслужить запрос: он не выполнен,
# поэтому повтор безопасен для любого метода, включая POST. 5xx же означает «принял и
# сломался» — запрос мог примениться, и повторять его можно только там, где повтор
# ничего не создаёт, то есть на GET.
HTTP_THROTTLED = 429
HTTP_RETRY_5XX = (500, 502, 503, 504)
THROTTLE_RETRIES = 4      # попыток на 429 — для любого метода
RETRY_AFTER_CAP = 30      # секунды: потолок ОДНОЙ паузы, даже если Retry-After больше
RETRY_WAIT_BUDGET = 90    # секунды: потолок суммарного ожидания на один запрос

# Жёсткое ограничение области работы: агент имеет дело ТОЛЬКО с подпроектами внутри
# этого проекта. Сам корень рабочим проектом быть не может. Намеренно константа, а не
# настройка окружения — иначе ограничение обходится одной переменной.
ROOT_PROJECT_TITLE = "ИИ проекты"

# Логические роли колонок -> названия по умолчанию в трекере.
DEFAULT_COLUMNS = {
    "todo": "К работе",
    "wip": "В работе",
    "review": "На проверке",
    "done": "Готово",
    "blocked": "Заблокировано",
}
COLUMN_ORDER = ["todo", "wip", "review", "done", "blocked"]

# Системные колонки проекта имеют детерминированный id KS-<projectId>-<SUFFIX>
# и материализуются сервером лениво — в свежесозданном проекте GET их ещё не
# отдаёт. Переиспользуем их, иначе на доске появятся дубли.
SYSTEM_SUFFIX = {"todo": "-TODO", "wip": "-IN-PROGRESS", "done": "-DONE"}

# Системные колонки идут с большим шагом (0 / ~50000 / ~100000), и сервер их
# позиции пересчитывает. Поэтому порядок своих колонок считаем от фактических
# соседей, а не по фиксированным числам.
COLUMN_ORDER_HINT = {"todo": 0, "wip": 50000, "review": 75000,
                     "done": 100000, "blocked": 150000}


AGENT_TAG_PREFIX = "agent:"


# Каждый инструмент запускает СВОЮ копию скилла, и каталоги у них разные —
# по собственному пути скрипт узнаёт, под кем работает. Это надёжнее переменных
# окружения: у большинства инструментов «свои» переменные (GEMINI_API_KEY,
# CODEX_*) — это конфиг, который может быть выставлен где угодно и кем угодно,
# а не метка «сейчас работаю я». Порядок каталогов совпадает с TARGETS в
# tools/install.sh: добавляешь инструмент туда — добавь и сюда.
AGENT_BY_SKILL_DIR = (
    (os.path.join(".claude", "skills", "singularity-tasks"), "claude"),
    (os.path.join(".codex", "skills", "singularity-tasks"), "codex"),
    (os.path.join(".config", "opencode", "skills", "singularity-tasks"), "opencode"),
    (os.path.join(".gemini", "config", "skills", "singularity-tasks"), "antigravity"),
    (os.path.join(".qwen", "skills", "singularity-tasks"), "qwen"),
)

# Запуск не из каталога скилла (из репозитория-эталона, из worktree) путь не
# опознаёт. Тогда — по метке хозяина сессии; надёжна только там, где инструмент
# ставит её сам, а не просит пользователя.
AGENT_BY_ENV = (
    ("CLAUDECODE", "claude"),
    ("CLAUDE_CODE_SESSION_ID", "claude"),
)


def detect_agent():
    """Под каким инструментом идёт запуск. None — опознать не удалось."""
    here = os.path.realpath(__file__)
    for marker, name in AGENT_BY_SKILL_DIR:
        if os.sep + marker + os.sep in here:
            return name
    for var, name in AGENT_BY_ENV:
        if os.environ.get(var):
            return name
    return None


def agent_name(cfg=None, override=None):
    """Кто сейчас работает.

    Имя определяется САМО — руками задавать не нужно и не надо: забытый
    `export` означал бы, что все агенты подписываются одинаково, а на метке
    «кто взял задачу» держится защита от гонки. Явные способы оставлены для
    проверок и нестандартных запусков и идут первыми: определение по каталогу
    запуска — это догадка по среде, и перебивать ею то, что человек задал
    руками, нельзя.
    """
    return (override or os.environ.get("SINGULARITY_AGENT")
            or (cfg or {}).get("agent") or detect_agent() or "claude").strip()


def find_tag(title):
    """Только поиск — на чтении теги не заводим."""
    for t in paged("/tag", "tags"):
        if not t.get("removed") and t["title"].strip().lower() == title.strip().lower():
            return t["id"]
    return None


def ensure_tag(title, color=None):
    """Найти тег по названию или завести. Теги в аккаунте общие, не по проектам."""
    found = find_tag(title)
    if found:
        return found
    body = {"title": title}
    if color:
        body["color"] = color
    return request("POST", "/tag", body=body)["id"]


def mark_agent(task_id, cfg=None, override=None):
    """Пометить задачу своим agent-тегом.

    Вешается на КАЖДОЙ команде, меняющей задачу, а не только в `start`: иначе
    задача, закрытая напрямую через `done`, остаётся без следа о том, кто её сделал.
    """
    who = agent_name(cfg, override)
    add_task_tag(task_id, ensure_tag(AGENT_TAG_PREFIX + who))
    return who


CLAIM_SETTLE = 1.5    # сколько ждать встречный захват, прежде чем считать задачу своей


def agent_tags_on(task):
    """Какие agent-теги висят на задаче: [(tag_id, 'agent:имя'), ...]."""
    titles = {t["id"]: t["title"] for t in paged("/tag", "tags")}
    return [(x, titles.get(x, x)) for x in (task.get("tags") or [])
            if titles.get(x, "").startswith(AGENT_TAG_PREFIX)]


def claim_task(task_id, cfg, override=None, take_over=False):
    """Захватить задачу так, чтобы её не забрал одновременно второй агент.

    Compare-and-swap этот API не умеет, поэтому захват оптимистичный: пометились
    → подождали → перечитали → разошлись по детерминированному правилу. Гонка
    воспроизводится (`tools/check-claim-race.py`): два `start` по одной задаче
    давали то два тега на доске, то — хуже — два кода 0 при одном уцелевшем теге,
    когда чтение-запись `tags` затирала чужую пометку. Второй агент при этом
    считал задачу своей, а доска показывала первого.

    Захват идёт ДО переноса колонки: проигравший не должен успеть подвинуть доску.
    """
    who = agent_name(cfg, override)
    my_title = AGENT_TAG_PREFIX + who
    task = request("GET", f"/task/{task_id}")
    foreign = [(tid, t) for tid, t in agent_tags_on(task) if t != my_title]
    if foreign and not take_over:
        die(f"{task_id}: задача уже занята — {', '.join(t for _, t in foreign)}.\n"
            "  Две сессии над одной задачей — это потерянный контекст, а не "
            "параллельная работа.\n"
            f"  Взять всё равно (снимет чужие метки): sing.py start {task_id} "
            "--take-over ...")
    # ⚠ Снять чужое и поставить своё — ОДНИМ PATCH, а не двумя.
    # Двумя было так: `tags=[]`, потом `tags=[мой]`. Между ними задача висит
    # вообще без agent-метки, то есть выглядит свободной; а если второй PATCH
    # не подтвердится, она такой и остаётся — исход хуже честного отказа.
    # `tags` перезаписывается целиком, поэтому окна можно не иметь вовсе:
    # сервер либо применит новый список (чужого нет, мой есть), либо не
    # применит ничего (чужой на месте) — промежуточного состояния не бывает.
    # Отсюда же ответ на «что делать с уже снятой чужой меткой»: её нечего
    # возвращать. Компенсация была бы вторым запросом, который тоже может не
    # дойти, и дырку она не закрывает, а удваивает.
    set_task_tags(task_id, add=[ensure_tag(my_title)],
                  drop=[tid for tid, _ in foreign], soft=True)
    time.sleep(CLAIM_SETTLE)

    fresh = request("GET", f"/task/{task_id}")
    marks = agent_tags_on(fresh)
    titles = [t for _, t in marks]
    if my_title not in titles:
        # чужой PATCH tags затёр мою пометку: чтение-записью иначе и не бывает.
        # Сюда же попадает запись, так и не доехавшая из очереди синхронизации, —
        # различать их незачем: исход один и тот же, задача не моя.
        die(f"{task_id}: захват не удержался — моей метки на задаче нет"
            + (f" (сейчас {', '.join(titles)})" if titles else "")
            + ".\n  Задачу взял другой агент либо запись не применилась. "
            "Возьми следующую: sing.py next")
    if len(titles) > 1:
        # встречный захват. Правило одно у всех, поэтому победитель ровно один
        winner = min(titles)
        if winner != my_title:
            for tid, t in marks:
                if t == my_title:
                    drop_task_tag(task_id, tid)
            die(f"{task_id}: встречный захват, задача уходит к {winner}.\n"
                "  Свою метку снял. Возьми следующую: sing.py next")
        for tid, t in marks:
            if t != my_title:
                # чужую метку не снимаю: проигравший снимет её сам, а если он
                # умер — останется след, по которому видно, что тут была гонка
                print(f"  ⚠ встречный захват {t}: задача осталась за {my_title}")
    return who


# Сервер кладёт запись в очередь синхронизации и отвечает РАНЬШЕ, чем применит
# её: на загруженной очереди немедленный GET после PATCH отдаёт ещё старый
# список тегов. Одно чтение поэтому не способно отличить «сервер ещё не
# применил» от «сервер не применил» — оно одинаково видит старое в обоих
# случаях. Различает их только время: не применённое не появится и потом.
# Та же болячка лечилась в `tools/zz-project.py` (delete_draft после DELETE).
TAG_SETTLE_TRIES = 4     # перечитываний подтверждения, включая немедленное
TAG_SETTLE_PAUSE = 1.0   # пауза между ними

# Повторного PATCH здесь намеренно НЕТ. Запись не теряется — она отстаёт;
# а повтор в этом месте переписал бы встречный захват и сломал правило
# «победитель ровно один» (см. claim_task): затёртая метка обязана остаться
# затёртой, иначе оба агента решат, что задача их.


def set_task_tags(task_id, add=(), drop=(), soft=False):
    """Переписать теги задачи ОДНИМ PATCH и подтвердить перечитыванием.

    True — записали и подтвердили, False — писать было нечего.
    Не подтвердилось за отведённые перечитывания: `die`, а при `soft=True` —
    None. Докладывать об успехе по коду 200 этому API нельзя (AGENTS.md §4).
    """
    drop, add = set(drop), [t for t in add]
    task = request("GET", f"/task/{task_id}")
    tags = list(task.get("tags") or [])
    target = [t for t in tags if t not in drop]
    for t in add:
        if t not in target:
            target.append(t)
    if target == tags:
        return False

    # Перечитать НЕПОСРЕДСТВЕННО перед записью и подмешать чужие метки,
    # появившиеся после первого чтения. Список тегов правится чтением-записью,
    # compare-and-swap этот API не умеет: между GET и PATCH другой агент успевает
    # добавить свою метку, и запись старым списком её стирает. Так и пропали
    # 16 меток из 23 при параллельной работе — молча, потому что проверка ниже
    # смотрела только на СВОИ теги (`want <= actual`) и чужую потерю не видела.
    latest = list(request("GET", f"/task/{task_id}").get("tags") or [])
    for t in latest:
        if t not in target and t not in drop:
            target.append(t)
    keep = {t for t in latest if t not in drop}
    request("PATCH", f"/task/{task_id}", body={"tags": target})

    want = set(add)
    for attempt in range(1, TAG_SETTLE_TRIES + 1):
        actual = set(request("GET", f"/task/{task_id}").get("tags") or [])
        lost = keep - actual
        if lost and attempt == TAG_SETTLE_TRIES:
            die(f"{task_id}: запись тегов потеряла чужие метки {sorted(lost)}.\n"
                "  Их добавил другой агент между чтением и записью. Это потеря следа "
                "«кто взял задачу», а не лаг: проверь задачу в трекере.")
        if want <= actual and not (drop & actual) and not lost:
            return True
        if attempt < TAG_SETTLE_TRIES:
            time.sleep(TAG_SETTLE_PAUSE)
    if soft:
        return None
    die(f"{task_id}: правка тегов не применилась за {TAG_SETTLE_TRIES} "
        f"перечитываний ({TAG_SETTLE_PAUSE * (TAG_SETTLE_TRIES - 1):.0f} с) — "
        f"у задачи теги {sorted(actual)}, ожидались {sorted(set(target))}.\n"
        "  Это уже не лаг синхронизации. Метки задачи не изменились так, как "
        "ожидалось: проверь её в трекере.")


def drop_task_tag(task_id, tag_id):
    """Снять тег, не тронув остальные, и убедиться, что он снят."""
    return set_task_tags(task_id, drop=[tag_id])


def add_task_tag(task_id, tag_id):
    """Добавить тег, не затирая уже висящие, и убедиться, что он применился."""
    return set_task_tags(task_id, add=[tag_id])


def desired_order(role, mapping, statuses):
    """review — между wip и done, blocked — после done, по живым значениям."""
    order = {s["id"]: (s.get("kanbanOrder") or 0) for s in statuses}
    wip, done = order.get(mapping.get("wip")), order.get(mapping.get("done"))
    if role == "review" and wip is not None and done is not None:
        return (wip + done) // 2
    if role == "blocked" and done is not None:
        return done + 50000
    return COLUMN_ORDER_HINT.get(role, 0)


def system_status_id(project_id, role):
    suffix = SYSTEM_SUFFIX.get(role)
    return f"KS-{project_id}{suffix}" if suffix else None


# --------------------------------------------------------------------------- токен


def get_token():
    tok = os.environ.get("SINGULARITY_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "singularity-app",
             "-a", "rest-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    path = os.path.expanduser("~/.claude/.singularity-token")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    die("Токен не найден. Создай его на https://me.singularity-app.com (раздел API) и положи:\n"
        "  security add-generic-password -s singularity-app -a rest-token -w '<ТОКЕН>'\n"
        "либо экспортируй SINGULARITY_TOKEN.")


# --------------------------------------------------------------------------- HTTP


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def request(method, path, query=None, body=None, soft=False):
    """soft=True — вернуть None вместо выхода при HTTP-ошибке."""
    url = API + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + get_token())
    if data:
        req.add_header("Content-Type", "application/json")

    def retry_after(headers):
        """Retry-After: число секунд ИЛИ HTTP-дата. Нет/мусор -> None."""
        raw = headers.get("Retry-After") if headers else None
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        stamp = email.utils.parsedate_tz(raw)
        if not stamp:
            return None
        # mktime_tz даёт UTC-эпоху, time.time() — тоже: часовой пояс уже учтён
        return max(0.0, email.utils.mktime_tz(stamp) - time.time())

    # Повторяем только то, что безопасно повторить. GET идемпотентен по определению;
    # POST/PATCH — нет (повтор `POST /task` создаст второй объект), поэтому сетевой
    # обрыв и 5xx на них отдаём вызывающему, который перечитывает состояние сам.
    # Наблюдали, как цепочка из восьми `add` порвалась на `POST /kanban-task-status:
    # timed out` и оставила задачу без колонки — тайм-ауты здесь штатный режим.
    idempotent = method == "GET"
    net_attempts = NET_RETRIES if idempotent else 1
    attempt, waited = 0, 0.0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code == HTTP_THROTTLED:
                # 429 — сервер ОТКАЗАЛСЯ обслужить запрос, то есть не выполнил его.
                # Это единственный класс отказа, где повтор безопасен для любого
                # метода: второго объекта он создать не мог. Сколько ждать — говорит
                # сервер (Retry-After), а не мы; своя пауза только если он молчит.
                limit, pause = THROTTLE_RETRIES, retry_after(e.headers)
                if pause is None:
                    pause = NET_BACKOFF * attempt
            elif e.code in HTTP_RETRY_5XX and idempotent:
                # 5xx — запрос мог быть выполнен до сбоя, поэтому повтор только GET
                limit, pause = NET_RETRIES, NET_BACKOFF * attempt
            else:
                limit, pause = 1, 0.0
            budget_left = RETRY_WAIT_BUDGET - waited
            if attempt < limit and budget_left > 0:
                # потолки — чтобы команда не висела: сервер может попросить
                # Retry-After: 3600, и столько ждать бессмысленно
                pause = min(pause, RETRY_AFTER_CAP, budget_left)
                time.sleep(pause)
                waited += pause
                continue
            if soft:
                return None
            if e.code == 401:
                die("401 Unauthorized: токен недействителен или ему не хватает прав.")
            spent = (f" (попыток: {attempt}, ждали {waited:.1f} с)"
                     if attempt > 1 else "")
            die(f"{method} {path} -> HTTP {e.code}{spent}: {detail}")
        except (urllib.error.URLError, TimeoutError,
                http.client.HTTPException) as e:
            # URLError покрывает только установку соединения и отправку запроса:
            # urlopen заворачивает в неё OSError из h.request(). Всё, что случилось
            # ПОСЛЕ отправки, прилетает как есть, мимо неё: сервер принял соединение
            # и молчит — read-timeout из getresponse()/read() голым TimeoutError
            # (с 3.10 socket.timeout — его псевдоним); оборвал ответ — RemoteDisconnected
            # или IncompleteRead из http.client. Для вызывающего это тот же сетевой
            # сбой, что и обрыв, и вести себя должен так же — иначе пользователь
            # получает traceback вместо сообщения.
            #
            # Шире брать нельзя. OSError — общий предок URLError, HTTPError, TimeoutError
            # и всего несетевого разом: except OSError проглотил бы и то, что обязано
            # падать. HTTPException же из другой иерархии и HTTPError ему не родня,
            # так что HTTP-ответы по-прежнему разбирает ветка выше.
            if attempt < net_attempts:
                pause = NET_BACKOFF * attempt
                time.sleep(pause)
                waited += pause
                continue
            if soft:
                return None
            reason = getattr(e, "reason", e)     # .reason есть только у URLError
            die(f"Сеть недоступна для {method} {path}: {reason}"
                + (f" (попыток: {attempt})" if attempt > 1 else ""))


PAGE_SIZE = 200          # окно одного запроса
PAGE_HARD_CAP = 50000    # предохранитель: сервер, игнорирующий offset, не должен крутить вечно


def paged(path, key, query=None, limit=None, page=PAGE_SIZE):
    """Собрать ВСЕ страницы списка.

    `limit=None` — без потолка: выборка идёт до пустой страницы. Прежний
    потолок в 1000 по умолчанию молча резал хвост — на проекте в 1150 задач
    board показывал «1000», и отличить это от правды было невозможно.
    `limit` остался для тех, кому хватает первой страницы (doctor).

    Два правила, за которые заплачено замером на живом API:

    * **offset шагает по ЗАПРОШЕННОМУ окну, а не по числу отданных строк.**
      Сервер при выборке задач post-фильтрует уже отобранные строки (см.
      references/api.md), и `count` выходит меньше `maxCount`. Шаг по
      `len(batch)` тогда сдвигает окно внахлёст: строки повторяются, хвост
      не доезжает.
    * **Выход — по пустой странице, а не по `offset >= total`.** `total`
      считается сервером до post-фильтра, поэтому как условие остановки он
      ненадёжен; здесь он нужен только чтобы заметить недобор. Цена честности —
      один лишний запрос на вызов.
    """
    items, seen, offset, total = [], set(), 0, None
    q = dict(query or {})
    while True:
        want = page if limit is None else min(page, limit - len(items))
        if want <= 0:
            break
        q.update({"maxCount": want, "offset": offset, "paginationData": "true"})
        resp = request("GET", path, query=q)
        batch = resp.get(key) or []
        pg = resp.get("pagination") or {}
        if pg.get("total") is not None:
            total = pg["total"]
        fresh = 0
        for it in batch:
            ident = it.get("id") if isinstance(it, dict) else None
            if ident is not None:
                if ident in seen:
                    continue           # окна могут перекрыться — дубли не копим
                seen.add(ident)
            items.append(it)
            fresh += 1
        if not batch:
            break
        if fresh == 0:
            # страница непустая, но вся из уже виденного: окно стоит на месте.
            # Считать предохранителем число СОБРАННЫХ объектов тут нельзя — при
            # дедупликации оно перестаёт расти, и цикл становится вечным.
            die(f"GET {path}: сервер вернул страницу целиком из уже полученных "
                f"объектов на offset={offset} — окно не двигается. Прерываю.")
        offset += want
        if limit is None and total is not None and len(items) >= total:
            break     # собрали ровно столько, сколько обещал сервер — добор не нужен
        if len(items) >= PAGE_HARD_CAP:
            die(f"GET {path}: выборка перевалила {PAGE_HARD_CAP} объектов. "
                "Прерываю, чтобы не крутить вечно.")
    if limit is None and total is not None and len(items) < total:
        # молчаливый недобор — ровно та ошибка, из-за которой правилась эта функция
        print(f"⚠ GET {path}: сервер обещал {total} объектов, собрано {len(items)}. "
              "Выборка неполная — выводы по ней делать нельзя.", file=sys.stderr)
    return items


# --------------------------------------------------------------------------- конфиг репо


_PROJECTS_CACHE = []


def all_projects():
    """Список проектов на процесс. Памятка не для скорости ради скорости:
    `assert_allowed` зовётся на каждой команде, а `show` звал его дважды — два
    одинаковых `/project` из трёх запросов, и каждый мог упасть в таймаут."""
    if not _PROJECTS_CACHE:
        _PROJECTS_CACHE.extend(p for p in paged("/project", "projects")
                               if not p.get("removed"))
    return _PROJECTS_CACHE


def forget_projects():
    """Сбросить памятку. Обязательна после создания проекта: иначе проверка
    области ищет свежесозданный проект в списке, снятом до его появления."""
    _PROJECTS_CACHE.clear()


def resolve_root(projects=None):
    projects = projects if projects is not None else all_projects()
    hits = [p for p in projects
            if p.get("title", "").strip().lower() == ROOT_PROJECT_TITLE.strip().lower()]
    if not hits:
        die(f"Корневой проект «{ROOT_PROJECT_TITLE}» не найден в аккаунте. "
            "Работать не с чем: скилл ограничен его подпроектами.")
    if len(hits) > 1:
        die(f"В аккаунте несколько проектов «{ROOT_PROJECT_TITLE}» — неясно, какой корневой.")
    return hits[0]


def project_chain(project_id, projects):
    """Цепочка от проекта вверх к корню дерева. Защищена от циклов."""
    by_id = {p["id"]: p for p in projects}
    chain, cur, seen = [], by_id.get(project_id), set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        cur = by_id.get(cur.get("parent"))
    return chain


def assert_allowed(project_id, what="проект"):
    """Пускать только к подпроектам ROOT_PROJECT_TITLE. Сам корень — не рабочий проект."""
    projects = all_projects()
    root = resolve_root(projects)
    chain = project_chain(project_id, projects)
    if not chain:
        die(f"{what} {project_id} не найден в аккаунте.")
    if project_id == root["id"]:
        die(f"«{root['title']}» — корневой проект, работать в нём напрямую нельзя. "
            "Задачи ведутся в его подпроектах.")
    if not any(p["id"] == root["id"] for p in chain):
        path = " → ".join(p.get("title", "?") for p in reversed(chain))
        die(f"ЗАПРЕЩЕНО: {what} «{chain[0].get('title')}» лежит вне «{root['title']}».\n"
            f"  фактический путь: {path}\n"
            f"  скилл работает только с подпроектами «{root['title']}».")
    return chain[0]


# Скилл раскатывается в Claude Code, Codex, OpenCode, Antigravity и Qwen Code,
# привязка репозитория не должна жить в каталоге одного из них. Новые репозитории
# получают нейтральный `.agents/`, старый `.claude/` продолжает читаться.
CONFIG_LOCATIONS = [
    os.path.join(".agents", CONFIG_NAME),
    os.path.join(".claude", CONFIG_NAME),
    CONFIG_NAME,
]


def find_config(start=None):
    d = os.path.abspath(start or os.getcwd())
    while True:
        for rel in CONFIG_LOCATIONS:
            p = os.path.join(d, rel)
            if os.path.exists(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_config(required=True, check_scope=True):
    p = find_config()
    if not p:
        if required:
            die("Репозиторий не привязан к проекту трекера.\n"
                "Запусти: sing.py init --project \"<название проекта>\"")
        return None, None
    with open(p) as f:
        cfg = json.load(f)
    # Ограничение области действует на каждую операцию, а не только на привязку:
    # конфиг мог быть отредактирован руками или прийти из чужого репозитория.
    if check_scope:
        assert_allowed(cfg["projectId"], "привязанный проект")
    return cfg, p


def save_config(cfg, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- note (delta)


# Заметка хранится как Quill-delta, но приложение ждёт ГОЛЫЙ МАССИВ операций
# `[{"insert": "..."}]`, а не объект `{"ops": [...]}`. Объект оно разобрать не
# может и показывает карточку с сырым JSON вместо текста. Проверено по локальной
# базе приложения: 698 из 704 заметок — массивы, остальные legacy-текст.
def note_ops(note):
    """Разобрать поле note в список операций. Понимает все три встречающиеся формы."""
    if not note:
        return []
    if isinstance(note, list):
        return note
    try:
        d = json.loads(note)
    except (json.JSONDecodeError, TypeError):
        return [{"insert": str(note)}]          # старый простой текст
    if isinstance(d, list):
        return d                                 # правильная форма
    if isinstance(d, dict) and isinstance(d.get("ops"), list):
        return d["ops"]                          # объект-обёртка, читаем но не пишем
    return [{"insert": str(note)}]


def note_to_text(note):
    return "".join(op.get("insert", "") for op in note_ops(note)
                   if isinstance(op, dict) and isinstance(op.get("insert"), str))


def note_dump(ops):
    return json.dumps(ops, ensure_ascii=False)


# Форматирование карточки — не украшение: отчёт из пяти фактов подряд читается как
# стена текста. Словарь взят не из документации Quill, а из локальной базы самого
# приложения — то, что оно пишет само, оно гарантированно и рисует:
#   на тексте:            bold, italic, link, singularityBackgroundAttribute
#   на переводе строки:   list=bullet|ordered|checked|unchecked, header=2|3, align, indent
# Блочный атрибут в Quill вешается на "\n", а не на текст строки — отсюда две разные
# ветки ниже. Перепутать легко, и приложение тогда покажет абзац без списка.
def _body_ops(text):
    """Текст в операции: строки, начинающиеся с «- » или «* », становятся списком."""
    ops = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped[:2] in ("- ", "* "):
            ops.append({"insert": stripped[2:]})
            ops.append({"insert": "\n", "attributes": {"list": "bullet"}})
        else:
            ops.append({"insert": line + "\n"})
    return ops


def note_append(note, text, label=None):
    """Дописать абзац, приведя заметку к формату, который понимает приложение.

    label — подпись вроде «ПЛАН (agent:qwen)»: пишется жирным отдельной операцией,
    чтобы в карточке было видно границу между записями, а не сплошной текст.
    """
    ops = note_ops(note)
    if ops:
        tail = ops[-1].get("insert") if isinstance(ops[-1], dict) else None
        sep = "\n" if isinstance(tail, str) and tail.endswith("\n") else "\n\n"
        ops.append({"insert": sep})
    if label:
        ops.append({"insert": label, "attributes": {"bold": True}})
        ops.append({"insert": ": "})
    ops.extend(_body_ops(text))
    return note_dump(ops)


# --------------------------------------------------------------------------- канбан


def project_statuses(project_id):
    return paged("/kanban-status", "kanbanStatuses", {"projectId": project_id})


def task_links(task_id, include_removed=False):
    q = {"taskId": task_id}
    if include_removed:
        q["includeRemoved"] = "true"
    return paged("/kanban-task-status", "kanbanTaskStatuses", q)


def task_project(task_id):
    return request("GET", f"/task/{task_id}").get("projectId")


_PROJECT_STATUS_IDS = {}


def project_status_ids(project_id):
    """id колонок проекта — множеством, с памяткой на процесс.

    Системные колонки добавляются по детерминированному id, даже если GET их
    ещё не отдал: в проекте, заведённом в приложении, список колонок бывает
    пустым до первой синхронизации (см. references/api.md).
    """
    if project_id not in _PROJECT_STATUS_IDS:
        ids = {s["id"] for s in project_statuses(project_id) if not s.get("removed")}
        ids.update(system_status_id(project_id, role) for role in SYSTEM_SUFFIX)
        _PROJECT_STATUS_IDS[project_id] = ids
    return _PROJECT_STATUS_IDS[project_id]


def board_links(task_id, project_id=None, include_removed=False):
    """Связки задачи, относящиеся к доске ЕЁ проекта.

    Связка у задачи не одна. Кроме колонки своего проекта бывает связка с
    системной доской «Сегодня»: псевдопроект `P-TODAY` со своими колонками,
    id связки `KTS-<taskId>-TODAY`. Брать первую попавшуюся нельзя — по живому
    аккаунту таких связок половина, и все они у задач, лежащих в обычных
    проектах. Поэтому связка выбирается по принадлежности её `statusId`
    колонкам нужного проекта, как это делает column_map().

    Живые связки идут первыми: помеченная удалённой — след снесённой колонки,
    воскресить её PATCH-ем всё равно нельзя.
    """
    project_id = project_id or task_project(task_id)
    own = project_status_ids(project_id) if project_id else set()
    links = [l for l in task_links(task_id, include_removed=include_removed)
             if l.get("statusId") in own]
    return sorted(links, key=lambda l: bool(l.get("removed")))


def task_column(task_id, project_id=None):
    """Колонка задачи на доске её проекта. None — задача вне колонок.

    project_id можно передать, чтобы не ходить за проектом задачи ещё раз;
    без него проект берётся из самой задачи.
    """
    live = [l for l in board_links(task_id, project_id) if not l.get("removed")]
    return live[0]["statusId"] if live else None


def move_to_column(task_id, status_id, fatal=True, project_id=None):
    """Идемпотентно поставить задачу в колонку и УБЕДИТЬСЯ, что она там.

    POST /task/{id}/change-column не используется намеренно: на системных
    колонках проекта он отвечает 200, но связку не меняет. Правим связку сама.

    Id связки детерминирован (`KTS-<taskId>`), поэтому повтор здесь безопасен —
    в отличие от `POST /task`. Ради этого и повторяем: таймаут ровно на этом
    запросе оставлял задачу вообще без колонки.

    ⚠ Правится только связка доски СВОЕГО проекта. PATCH по первой попавшейся
    снял бы задачу с доски «Сегодня» — это порча данных пользователя, а не
    косметика: связка там одна и переезжает целиком.

    fatal=False — вернуть None вместо die: вызывающий сам решит, что сказать
    (например, `add` обязан назвать уже созданный T-id, иначе его не найти).
    """
    project_id = project_id or task_project(task_id)
    if task_column(task_id, project_id) == status_id:
        return "уже в колонке"
    action = None
    for attempt in range(1, NET_RETRIES + 1):
        link = next(iter(board_links(task_id, project_id, include_removed=True)), None)
        if link and not link.get("removed"):
            request("PATCH", f"/kanban-task-status/{link['id']}",
                    body={"statusId": status_id}, soft=True)
            action = "перемещена"
        else:
            # связки нет или она помечена удалённой (колонку снесли) — заводим заново
            request("POST", "/kanban-task-status",
                    body={"taskId": task_id, "statusId": status_id}, soft=True)
            action = "привязана к колонке"
        if task_column(task_id, project_id) == status_id:
            return action
        if attempt < NET_RETRIES:
            time.sleep(NET_BACKOFF * attempt)
    actual = task_column(task_id, project_id)
    if not fatal:
        return None
    die(f"{task_id}: перенос не применился за {NET_RETRIES} попытки — колонка "
        f"осталась {actual}. Состояние трекера не изменилось так, как ожидалось.\n"
        f"  починить вручную: sing.py move {task_id} <роль>")


def assert_task_allowed(task_id, cfg=None):
    """Задача обязана лежать в проекте, разрешённом ограничением области.

    Команды принимают T-id с рук, поэтому проверять надо саму задачу, а не только
    привязку репозитория: иначе `report T-<чужая>` уйдёт мимо ограничения.
    """
    task = request("GET", f"/task/{task_id}")
    pid = task.get("projectId")
    if not pid:
        die(f"{task_id}: задача вне проекта (входящие), работать с ней скилл не будет.")
    if cfg and pid != cfg.get("projectId"):
        die(f"ЗАПРЕЩЕНО: {task_id} лежит в проекте {pid}, а репозиторий привязан "
            f"к {cfg['projectId']}.")
    assert_allowed(pid, "проект задачи")
    return task


def col_id(cfg, role):
    """id колонки под роль — или отказ.

    Асимметрия с `board` намеренная и одинаково честная: команда, которая двигает
    задачу (`start`, `move`, `next`, `list`), без колонки сделать нечего — она
    отказывает; `board` только читает и обязан показать остальную доску, поэтому
    помечает роль и идёт дальше. Чего быть не должно ни там, ни там — молчаливая
    подмена содержимого (см. board_layout).
    """
    cid = (cfg.get("columns") or {}).get(role)
    if not cid:
        die(f"В привязке ({find_config() or CONFIG_NAME}) нет колонки '{role}'.\n"
            "  посмотреть целиком: sing.py doctor  ·  починить: sing.py init --apply")
    return cid


# --------------------------------------------------------------------------- задачи


def fetch_tasks(project_id, include_archived=False):
    q = {"projectId": project_id, "includeAllRecurrenceInstances": "true"}
    if include_archived:
        # Без флага сервер сам не отдаёт задачи с journalDate — клиентского
        # фильтра мало, выборку надо расширять запросом.
        q["includeArchived"] = "true"
    return paged("/task", "tasks", q)


# `journalDate` ≠ удаление. Проверено на живом API:
#   POST /task/{id}/archive   -> journalDate=<время>, checked=1, removed=false,
#                                deleteDate=null, связка с колонкой цела;
#   POST /task/{id}/unarchive -> journalDate=null и checked обратно в 0;
#   DELETE /task/{id}         -> removed=true, journalDate не трогается;
#   deleteDate (корзина)      -> задача выпадает из выборки, но removed=false.
# Выборку по умолчанию сервер режет по обоим признакам, но разными флагами:
# архив возвращает `includeArchived=true`, удалённое и корзину — `includeRemoved=true`.
# Поэтому «в дневнике» — это закрытая, живая задача, а не удалённая.
def live_tasks(project_id):
    """Всё, с чем можно РАБОТАТЬ: не удалено и не унесено в дневник.

    Умышленно строгая: её читают open_tasks, next и list — брать в работу
    задачу, которую приложение уже унесло в дневник, нельзя.
    """
    return [t for t in fetch_tasks(project_id)
            if not t.get("removed") and not t.get("journalDate")
            and not t.get("deleteDate") and not t.get("isNote")]


def board_tasks(project_id):
    """То же плюс унесённое в дневник — для board.

    Доска показывает историю: закрытая задача, которую приложение унесло в
    дневник, обязана остаться в «Готово», иначе агент не видит сделанного и
    заводит его заново. Удалённое и корзина (removed / deleteDate) не в счёт.
    """
    return [t for t in fetch_tasks(project_id, include_archived=True)
            if not t.get("removed") and not t.get("deleteDate")
            and not t.get("isNote")]


def open_tasks(project_id):
    return [t for t in live_tasks(project_id) if int(t.get("checked") or 0) == 0]


def column_map(project_id):
    """taskId -> statusId для проекта.

    Запрос на колонку давал 8 обращений на пятиколоночной доске (`board` — 12
    запросов, 2.4 с). Без фильтра эндпоинт отдаёт связки всего аккаунта одним
    заходом (проверено: 46 штук, из них 8 этого проекта), и мы отбираем свои по
    statusId. ⚠️ `?projectId=` он НЕ поддерживает: возвращает пустой список, а не
    отказ, — фильтр, которого нет, выглядит как «связок нет».
    """
    mine = {s["id"] for s in project_statuses(project_id)}
    return {link["taskId"]: link["statusId"]
            for link in paged("/kanban-task-status", "kanbanTaskStatuses", {})
            if not link.get("removed") and link.get("statusId") in mine}


def prio_of(t):
    """0 = высокий, поэтому `or 1` тут нельзя — ноль ложный."""
    p = t.get("priority")
    return 1 if p is None else int(p)


def project_groups(project_id):
    """Секции проекта. У каждого проекта есть безымянная fake-группа — она не секция."""
    return [g for g in paged("/task-group", "taskGroups", {"parent": project_id})
            if not g.get("removed") and not g.get("fake") and (g.get("title") or "").strip()]


def resolve_group(project_id, ref, create=False):
    if not ref:
        return None
    if ref.startswith("Q-"):
        return ref
    groups = project_groups(project_id)
    hit = next((g for g in groups
                if g["title"].strip().lower() == ref.strip().lower()), None)
    if hit:
        return hit["id"]
    if create:
        return request("POST", "/task-group",
                       body={"title": ref, "parent": project_id})["id"]
    known = ", ".join(f"«{g['title']}»" for g in groups) or "нет ни одной"
    die(f"Секция «{ref}» не найдена. Есть: {known}.\n"
        f"Завести: sing.py groups --create \"{ref}\"")


def project_notes(project_id):
    return [t for t in fetch_tasks(project_id)
            if t.get("isNote") and not t.get("removed") and not t.get("deleteDate")]


# В заголовке может лежать HTML: отправленное `banlist-ufm-cft.md` наблюдалось в
# базе как <a href="http://banlist-ufm-cft.md">…</a>. Это НЕ REST — пять проб через
# API вернули заголовки байт в байт; автолинковку делает клиент приложения при
# синхронизации, уже после создания. Поэтому сверять сразу после записи бесполезно:
# чистим на показе, чтобы HTML не лез в доску и в списки.
# Разворачиваем ТОЛЬКО ссылку, а не любые угловые скобки: в заголовках этого
# проекта `<details>` и `<ac:structured-macro>` — осмысленный текст задачи, и
# общий стрип тегов их съедал, превращая заголовок в «Live-check FR12:  → expand».
_LINK_RE = re.compile(r'<a\s[^>]*>(.*?)</a>', re.I | re.S)


def plain(s):
    return _LINK_RE.sub(r"\1", s or "")


def brief(t, extra=""):
    prio = {0: "!высокий", 1: "обычный", 2: "низкий"}.get(prio_of(t), "?")
    dl = f" дедлайн={t['deadline'][:10]}" if t.get("deadline") else ""
    return f"{t['id']}  [{prio}]{dl}  {plain(t.get('title', ''))}{extra}"


# --------------------------------------------------------------------------- команды


# Название пробной задачи для `doctor --write`. Говорит само за себя: если уборка
# всё-таки не отработает, человек увидит на доске не загадочный «test», а объяснение.
PROBE_TITLE = "sing.py doctor --write: проба записи (удаляется сразу)"
PROBE_MARK = "проба записи doctor --write"


def _check(results, label, ok, detail=""):
    """Записать и напечатать результат одного шага пробы."""
    results.append((label, bool(ok)))
    print(f"  {'✓' if ok else '✗'} {label:<22} {detail}")
    return bool(ok)


def doctor_write_steps(cfg, results, probe):
    """Шаги пробы записи. Каждый шаг проверяется ПЕРЕЧИТЫВАНИЕМ состояния.

    HTTP-код здесь ничего не доказывает: этот API умеет ответить 200, ничего не
    сделав (`change-column` на системных колонках — ровно такой случай).
    """
    created = request("POST", "/task", body={"title": PROBE_TITLE,
                                             "projectId": cfg["projectId"]})
    probe["task"] = (created or {}).get("id")
    tid = probe["task"]
    back = request("GET", f"/task/{tid}", soft=True) if tid else None
    if not _check(results, "POST /task",
                  back and back.get("projectId") == cfg["projectId"]
                  and back.get("title") == PROBE_TITLE,
                  f"{tid} создана и перечитана в проекте" if back
                  else "задача не создалась — прав на запись нет"):
        return

    request("PATCH", f"/task/{tid}",
            body={"note": note_append(back.get("note"), PROBE_MARK)})
    note = note_to_text((request("GET", f"/task/{tid}", soft=True) or {}).get("note"))
    _check(results, "PATCH /task", PROBE_MARK in note,
           "заметка записалась и прочиталась обратно" if PROBE_MARK in note
           else "заметка не изменилась (ответ мог быть 200)")

    # todo -> wip -> todo: move_to_column сам перечитывает связку и падает,
    # если перенос не применился, — SystemExit ловится уровнем выше.
    pid = cfg["projectId"]
    move_to_column(tid, col_id(cfg, "todo"), project_id=pid)
    move_to_column(tid, col_id(cfg, "wip"), project_id=pid)
    move_to_column(tid, col_id(cfg, "todo"), project_id=pid)
    _check(results, "канбан-связка", task_column(tid) == col_id(cfg, "todo"),
           "todo → wip → todo, колонка перечитана после каждого шага")

    item = request("POST", "/checklist-item",
                   body={"parent": tid, "title": PROBE_MARK, "parentOrder": 0})
    probe["item"] = (item or {}).get("id")
    listed = [c["id"] for c in paged("/checklist-item", "checklistItems", {"parent": tid})
              if not c.get("removed")]
    _check(results, "чек-лист", probe["item"] and probe["item"] in listed,
           f"пункт {probe['item']} создан и виден в задаче" if probe["item"] in listed
           else "пункт не появился в задаче")

    request("POST", f"/task/{tid}/complete")
    checked_on = int((request("GET", f"/task/{tid}", soft=True) or {}).get("checked") or 0)
    request("POST", f"/task/{tid}/uncomplete")
    checked_off = int((request("GET", f"/task/{tid}", soft=True) or {}).get("checked") or 0)
    _check(results, "complete/uncomplete", checked_on == 1 and checked_off == 0,
           f"checked {checked_on} → {checked_off}")


def doctor_write_cleanup(results, probe):
    """Убрать пробу и УБЕДИТЬСЯ, что её больше нет. Вызывается всегда, из finally."""
    if probe.get("item"):
        request("DELETE", f"/checklist-item/{probe['item']}", soft=True)
    tid = probe.get("task")
    if not tid:
        return
    request("DELETE", f"/task/{tid}", soft=True)
    left = request("GET", f"/task/{tid}", soft=True)
    gone = left is None or left.get("removed") or left.get("deleteDate")
    _check(results, "уборка пробы", gone,
           "DELETE /task, задача больше не отдаётся — доска как была" if gone
           else f"ПРОБА ОСТАЛАСЬ НА ДОСКЕ: {tid} — удалить руками")


def doctor_write(cfg):
    """`--write`: честно проверить права на запись, а не только на чтение.

    Токен может быть read-only, и тогда всё ломается позже — на первом `start`,
    посреди работы. Проба живёт секунды и удаляется в том же заходе.
    """
    print("\nПрава на ЗАПИСЬ (--write): пробная задача создаётся и удаляется здесь же")
    results, probe = [], {"task": None, "item": None}
    try:
        doctor_write_steps(cfg, results, probe)
    except SystemExit:
        # die() уже написал причину в stderr; прерываться нельзя — надо убрать пробу
        _check(results, "проба записи", False, "прервана ошибкой API (сообщение выше)")
    except Exception as e:                       # уборка важнее красивого traceback
        _check(results, "проба записи", False, f"прервана: {type(e).__name__}: {e}")
    finally:
        doctor_write_cleanup(results, probe)
    ok = sum(1 for _, good in results if good)
    if ok == len(results):
        print(f"✓ токен умеет писать: {ok} из {len(results)} проверок")
        return
    failed = ", ".join(label for label, good in results if not good)
    die(f"✗ запись прошла {ok} из {len(results)} проверок; не прошли: {failed}\n"
        "  Права токена правятся только пересозданием токена "
        "(me.singularity-app.com → API).")


def cmd_doctor(args):
    projects = paged("/project", "projects", limit=5)
    print(f"✓ токен рабочий, доступно проектов (первая страница): {len(projects)}")
    cfg, path = load_config(required=False)
    if not cfg:
        print("· репозиторий не привязан — запусти init")
        if getattr(args, "write", False):
            die("Проверять запись негде: проба создаётся в привязанном проекте.", 2)
        return
    print(f"✓ привязка: {path}")
    print(f"  проект: {cfg.get('projectTitle')} ({cfg['projectId']})")
    live = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])
            if not s.get("removed")}
    for role in COLUMN_ORDER:
        cid = (cfg.get("columns") or {}).get(role)
        # Два разных диагноза, и путать их дорого: «пропала в трекере» отправляет
        # искать, кто удалил колонку, хотя в привязке её id не было никогда —
        # колонка при этом может спокойно жить на доске (её видно в board,
        # в блоке «КОЛОНКИ МИМО ПРИВЯЗКИ»).
        if not cid:
            print(f"  ✗ {role:8} -> НЕТ В ПРИВЯЗКЕ — роли нет в {CONFIG_NAME}")
            continue
        mark = "✓" if cid in live else "✗"
        print(f"  {mark} {role:8} -> {live.get(cid, 'КОЛОНКА ПРОПАЛА В ТРЕКЕРЕ')}")
    used_ids = set((cfg.get("columns") or {}).values())
    # Проверка по именам ловит «В работе» ×2, но пропускает главный случай: своя
    # «К работе» и системная «Новые» — тот же смысл, разные имена. Поэтому судим
    # по роли: системная колонка роли живёт, а скилл под эту роль взял другую.
    suspect = {}
    for role, suf in SYSTEM_SUFFIX.items():
        sys_id = f"KS-{cfg['projectId']}{suf}"
        if sys_id in live and (cfg.get("columns") or {}).get(role) != sys_id:
            suspect[role] = sys_id
    seen = {}
    for cid, name in live.items():
        seen.setdefault(name.strip().lower(), []).append(cid)
    dups = {n: ids for n, ids in seen.items() if len(ids) > 1}
    if suspect or dups:
        print("  ⚠ доска раздвоена: рядом с колонками скилла живут системные.")
        for role, sys_id in sorted(suspect.items()):
            print(f"      роль {role:8} скилл держит {(cfg.get('columns') or {}).get(role)}"
                  f" «{live.get((cfg.get('columns') or {}).get(role))}», "
                  f"а системная {sys_id} «{live[sys_id]}» стоит рядом")
        for name, ids in dups.items():
            for cid in ids:
                mark = " (используется скиллом)" if cid in used_ids else ""
                print(f"      одинаковое имя «{name}»: {cid}{mark}")
        print("      системные колонки удалить нельзя — чинится переездом задач на "
              "них и удалением своих (см. README, раздел про раздвоенную доску).")
    if getattr(args, "write", False):
        doctor_write(cfg)


def cmd_projects(args):
    projects = all_projects()
    root = resolve_root(projects)
    items = [p for p in projects
             if p["id"] != root["id"]
             and any(x["id"] == root["id"] for x in project_chain(p["id"], projects))]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    print(f"Доступные проекты — только внутри «{root['title']}» ({root['id']}):")
    for p in sorted(items, key=lambda x: x.get("title", "")):
        arch = " (архив)" if p.get("journalDate") else ""
        depth = len(project_chain(p["id"], projects)) - 2
        print(f"  {'  ' * max(depth, 0)}{p['id']}  {p.get('title', '')}{arch}")
    hidden = len(projects) - len(items) - 1
    if hidden > 0:
        print(f"\nСкрыто вне области действия скилла: {hidden}")


def plan_columns(project_id, names, existing, own_columns, plan):
    """Разложить роли по колонкам проекта: что переиспользовать, что создать.

    Считается ТОЛЬКО когда проект уже существует — id системных колонок выводится
    из id проекта, и до его создания их не от чего вычислять.
    """
    by_id = {s["id"]: s for s in existing}
    mapping, to_create, missing_system = {}, [], []
    for role in COLUMN_ORDER:
        want = names[role]
        # 1) системная колонка проекта — приоритет, даже если GET её ещё не отдал
        sys_id = system_status_id(project_id, role)
        sys_col = by_id.get(sys_id)
        if sys_id and not sys_col:
            # список мог её не отдать — спрашиваем напрямую по id
            sys_col = request("GET", f"/kanban-status/{sys_id}", soft=True)
        # 2) иначе колонка с нужным названием
        hit = next((s for s in existing
                    if s["name"].strip().lower() == want.strip().lower()), None)
        if sys_col:
            mapping[role] = sys_id
            plan.append(f"ИСПОЛЬЗОВАТЬ системную колонку «{sys_col.get('name')}» (роль {role})")
        elif hit:
            mapping[role] = hit["id"]
            plan.append(f"ИСПОЛЬЗОВАТЬ колонку «{hit['name']}» (роль {role})")
        else:
            if sys_id:
                missing_system.append(role)
            to_create.append((role, want))
            plan.append(f"СОЗДАТЬ колонку «{want}» (роль {role})")

    # Системные колонки (todo/wip/done) появляются не всегда сразу: в проекте,
    # заведённом в приложении и пока пустом, их нет — а через минуту клиент их
    # досоздаёт. Если сейчас создать свои, доска раздвоится: наблюдали на живой
    # сессии — init в 14:17:34 создал пять колонок, приложение в 14:18:17 добавило
    # свои три, и на доске стало две «В работе» и две «Готово» с одинаковым порядком.
    # Удалить системные нельзя (DELETE отвечает ошибкой), создать их с нужным id
    # тоже нельзя — значит, единственная защита в том, чтобы не начинать.
    if missing_system and not own_columns:
        die("Канбан проекта ещё не развёрнут: системных колонок "
            + ", ".join(f"«{names[r]}» ({r})" for r in missing_system) + " нет.\n"
            "  Создать свои поверх — значит получить доску-двойник: приложение "
            "досоздаст системные позже, и они встанут рядом с одинаковыми именами.\n"
            "  Открой проект в приложении один раз (или заведи в нём любую задачу), "
            "дай ему синхронизироваться и повтори init.\n"
            "  Осознанно хочешь свои колонки вместо системных — явно: --own-columns.")
    return mapping, to_create


def repo_project_name(path="."):
    """Имя проекта по умолчанию — имя каталога репозитория.

    Спрашивать его у человека незачем: в подавляющем большинстве случаев проект
    называется как репозиторий, а промпт, где надо что-то подставить руками,
    подставляют неправильно или забывают.
    """
    root = os.path.abspath(path)
    git = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if git.returncode == 0 and git.stdout.strip():
        root = git.stdout.strip()
    return os.path.basename(root.rstrip(os.sep))


# Правило «работа идёт по доске» кладётся в правила самого репозитория: агент,
# зашедший в него, должен узнать о доске оттуда, а не от человека в каждом промпте.
# AGENTS.md читают Codex, OpenCode, Antigravity и Qwen; Claude Code читает CLAUDE.md,
# поэтому рядом заводится заглушка-ссылка — но ТОЛЬКО если её ещё нет: чужие правила
# перезаписывать нельзя.
AGENTS_MARK = "<!-- singularity-tasks: работа по доске -->"


def agents_rule_text(project_title, cfg_path, repo_root):
    rel = os.path.relpath(cfg_path, repo_root)
    return f"""{AGENTS_MARK}
## Задачи — на доске трекера

Работа в этом репозитории ведётся по доске SingularityApp, проект «{project_title}»
(привязка — `{rel}`). Правила и команды — в скилле `singularity-tasks`, его `SKILL.md`.

- До любых правок — `sing.py board`. Работа берётся из очереди: `next` → `start --plan`
  → … → `done --report`. Задача не отражена на доске — её как будто не было.
- Работа не из очереди (нашёл попутно, попросили в чате) — сначала `add` с описанием,
  потом `start`. Заводить задачу задним числом поздно: в очереди могла уже лежать
  карточка ровно про это, с готовой постановкой.
- Не заканчивать сессию с задачей в колонке «В работе».
"""


def ensure_agents_rule(repo_root, project_title, cfg_path, apply=False, plan=None):
    """Прописать правило в AGENTS.md репозитория. Идемпотентно.

    Возвращает список сделанного (или запланированного). Существующий текст не
    трогает — дописывает в конец; повторный запуск ничего не добавляет.
    """
    done = []
    agents = os.path.join(repo_root, "AGENTS.md")
    block = agents_rule_text(project_title, cfg_path, repo_root)
    have = ""
    if os.path.exists(agents):
        with open(agents, encoding="utf-8") as f:
            have = f.read()
    if AGENTS_MARK not in have:
        done.append(("ДОПИСАТЬ правило работы по доске в " if have else
                     "СОЗДАТЬ ") + agents)
        if apply:
            with open(agents, "a", encoding="utf-8") as f:
                f.write(("\n" if have and not have.endswith("\n") else "") +
                        ("\n" if have else "") + block)
            with open(agents, encoding="utf-8") as f:
                if AGENTS_MARK not in f.read():
                    die(f"{agents}: правило не записалось.")

    claude = os.path.join(repo_root, "CLAUDE.md")
    if not os.path.exists(claude):
        done.append(f"СОЗДАТЬ {claude} — заглушка @AGENTS.md (Claude Code читает её)")
        if apply:
            with open(claude, "w", encoding="utf-8") as f:
                f.write("@AGENTS.md\n")
    elif "AGENTS.md" not in open(claude, encoding="utf-8").read():
        # чужой файл не трогаем: там могут быть правила, которые не наши
        done.append(f"⚠ {claude} существует и не ссылается на AGENTS.md — "
                    "Claude Code правило про доску не увидит, поправьте руками")
    if plan is not None:
        plan.extend(d for d in done)
    return done


def cmd_init(args):
    """По умолчанию — сухой прогон: показывает план, ничего не меняет."""
    guessed = not args.project
    if guessed:
        # Уже привязанный репозиторий: проект берём из привязки, а не из имени
        # каталога. Каталог и проект называются одинаково не всегда, и повторный
        # init (например, чтобы дописать правило в AGENTS.md) не должен уводить
        # репозиторий на другой проект или упираться в «проекта нет».
        bound = find_config(os.path.abspath(args.path))
        if bound and bound.startswith(os.path.abspath(args.path) + os.sep):
            with open(bound, encoding="utf-8") as f:
                known = json.load(f)
            args.project = known.get("projectId") or known.get("projectTitle") or ""
            guessed = not args.project
            if args.project:
                print(f"Репозиторий уже привязан — беру проект из {os.path.relpath(bound, args.path)}: "
                      f"«{known.get('projectTitle') or args.project}»")
        if not args.project:
            args.project = repo_project_name(args.path)
            print(f"Проект не указан — беру имя репозитория: «{args.project}»")
    projects = all_projects()
    root = resolve_root(projects)
    # искать только среди подпроектов корня — тёзка снаружи не должен даже находиться
    allowed = [p for p in projects
               if p["id"] != root["id"]
               and any(x["id"] == root["id"] for x in project_chain(p["id"], projects))]
    target = None
    if args.project.startswith("P-"):
        target = next((p for p in allowed if p["id"] == args.project), None)
        if not target:
            assert_allowed(args.project, "проект")  # выдаст внятный отказ
    else:
        matches = [p for p in allowed
                   if p.get("title", "").strip().lower() == args.project.strip().lower()]
        if not matches:
            matches = [p for p in allowed
                       if args.project.strip().lower() in p.get("title", "").lower()]
        if len(matches) > 1:
            die("Под запрос подходит несколько проектов:\n  " +
                "\n  ".join(f"{p['id']}  {p['title']}" for p in matches))
        target = matches[0] if matches else None

    if not target:
        # тёзка корня сломал бы resolve_root — второй проект с тем же названием
        if args.project.strip().lower() == root["title"].strip().lower():
            die(f"«{root['title']}» — корневой проект области. Ни работать в нём, ни "
                "заводить проект с таким же названием нельзя: по названию ищется сам корень.")
        # проект с таким названием есть, но вне области — это отказ, а не повод
        # создать одноимённый внутри
        outside = [p for p in projects
                   if p.get("title", "").strip().lower() == args.project.strip().lower()]
        if outside:
            assert_allowed(outside[0]["id"], "проект")

    plan = []
    if not target:
        plan.append(f"СОЗДАТЬ проект «{args.project}» внутри «{root['title']}»")
        existing = []
    else:
        print(f"Проект: {target['title']} ({target['id']})")
        existing = [s for s in project_statuses(target["id"]) if not s.get("removed")]
        if existing:
            print("Колонки в трекере: " + ", ".join(s["name"] for s in existing))

    names = dict(DEFAULT_COLUMNS)
    if args.columns:
        names.update(json.loads(args.columns))

    if target:
        mapping, to_create = plan_columns(target["id"], names, existing,
                                          args.own_columns, plan)
    else:
        # Проекта ещё нет — системные id считать не от чего. Раскладку колонок
        # определяем ПОСЛЕ создания: иначе создаются все пять, а сервер отдаёт
        # свои три сразу же, и новый проект рождается с раздвоенной доской.
        mapping, to_create = {}, []
        plan.append("РАЗОБРАТЬ колонки после создания проекта "
                    "(системные становятся известны только тогда)")

    # уже привязанный репозиторий переписываем на месте, новый получает
    # нейтральный `.agents/` — он общий для всех пяти инструментов
    # ВНИМАНИЕ: не `root` — это имя занято проектом-корнем области, и его затирание
    # ломало создание нового проекта ниже (`root["id"]` по строке пути → TypeError).
    repo_root = os.path.abspath(args.path)
    cfg_path = find_config(repo_root)
    if not cfg_path or not cfg_path.startswith(repo_root + os.sep):
        cfg_path = os.path.join(repo_root, CONFIG_LOCATIONS[0])
    plan.append(f"ЗАПИСАТЬ {cfg_path}")
    ensure_agents_rule(repo_root, (target or {}).get("title") or args.project,
                       cfg_path, apply=False, plan=plan)

    # Привязка к существующему проекту — рутина; создание нового в трекере человека
    # рутиной не является. По УГАДАННОМУ имени не создаём: иначе опечатка в имени
    # каталога или запуск не в том месте тихо заводят лишний проект. Проверено на
    # себе: повторный `init --apply` без --project из каталога проверки создал в
    # трекере проект «init-proba» вместе с колонками.
    if args.apply and not target and guessed:
        die(f"Проекта «{args.project}» в «{root['title']}» нет, а имя я угадал по "
            "каталогу репозитория.\n"
            "  Создавать проект по догадке не буду — назови явно:\n"
            f'    sing.py init --project "{args.project}" --apply\n'
            "  Либо укажи существующий: sing.py projects")

    if not args.apply:
        print("\nПлан (ничего не изменено, добавь --apply):")
        for line in plan:
            print("  · " + line)
        return

    if not target:
        created = request("POST", "/project",
                          body={"title": args.project, "parent": root["id"]})
        target = created.get("project", created)
        forget_projects()
        assert_allowed(target["id"], "созданный проект")
        print(f"создан проект {target['id']} внутри «{root['title']}»")
        # только теперь системные колонки существуют и имеют вычислимый id
        after = [s for s in project_statuses(target["id"]) if not s.get("removed")]
        lines = []
        mapping, to_create = plan_columns(target["id"], names, after,
                                          args.own_columns, lines)
        for line in lines:
            print("  · " + line)
    fresh = project_statuses(target["id"])
    for role, want in to_create:
        st = request("POST", "/kanban-status",
                     body={"name": want, "projectId": target["id"],
                           "kanbanOrder": desired_order(role, mapping, fresh)})
        mapping[role] = st["id"]
        print(f"создана колонка «{want}» -> {st['id']}")

    cfg = {
        "projectId": target["id"],
        "projectTitle": target.get("title"),
        "columns": mapping,
        "columnNames": names,
    }
    save_config(cfg, cfg_path)
    print(f"записан {cfg_path}")
    for line in ensure_agents_rule(repo_root, target.get("title") or args.project,
                                   cfg_path, apply=True):
        print("  " + line)


# Потолок ширины колонки «кто держит»: одно неудачно длинное имя агента не
# должно сдвинуть всю доску вправо. Длиннее — обрезается многоточием.
HOLDER_COL_MAX = 16


def board_holders(tasks):
    """taskId -> «@имя» держателя по тегам `agent:*` (несколько — через запятую).

    Один общий GET /tag на всю доску: справочник тегов в аккаунте один, теги
    у задачи лежат массивом id, и поштучно их разворачивать незачем — цена
    ровно та же, что у `list` (см. cmd_list).

    Если ни на одной показанной задаче тегов нет — запроса не будет вовсе:
    доска нового проекта не должна дорожать ради колонки, которая всё равно
    окажется пустой.
    """
    if not any(t.get("tags") for t in tasks):
        return {}
    titles = {tg["id"]: (tg.get("title") or "") for tg in paged("/tag", "tags")}
    holders = {}
    for t in tasks:
        who = sorted(titles[x][len(AGENT_TAG_PREFIX):] for x in (t.get("tags") or [])
                     if titles.get(x, "").startswith(AGENT_TAG_PREFIX))
        if who:
            holders[t["id"]] = "@" + ",".join(who)
    return holders


def board_pad(holders):
    """Форматтер левой колонки доски: фиксированная ширина, метка ПЕРЕД задачей.

    Держатель печатается слева, а не в хвосте строки, именно из-за длины
    заголовков: в хвосте метка у каждой строки вставала бы в своё место, и
    «кто занят» пришлось бы вычитывать построчно вместо взгляда вдоль колонки.
    Ширина считается по тому, что реально печатается (а не по всем задачам
    проекта), и ограничена HOLDER_COL_MAX.

    Тегов на доске нет — колонки нет: вывод байт в байт совпадает с прежним.
    """
    if not holders:
        return lambda who: "  "
    width = min(max(len(v) for v in holders.values()), HOLDER_COL_MAX)

    def pad(who):
        if not who:
            return "  " + " " * width + "  "
        if len(who) > width:
            who = who[:width - 1] + "…"
        return f"  {who:<{width}}  "

    return pad


UNBOUND_COLUMN = "колонки нет в привязке"


def board_layout(cfg, by_col, statuses, limit):
    """Разложить задачи по ролям: (роль, id колонки, название, все, показываемые).

    Чистая — сети не трогает, поэтому проверяется без токена.

    Роль, которой нет в привязке, получает cid=None и ЗАВЕДОМО ПУСТОЙ список.
    Раньше здесь стояло `by_col.get(cid)`, а `by_col[None]` — это задачи ВНЕ
    колонок: доска печатала их под каждой непривязанной ролью, то есть показывала
    то, чего на доске нет, да ещё и повторно (T-278ad639). Пустой список — не
    утверждение «задач нет»: задачи под эту роль могут стоять в живой колонке,
    которую привязка потеряла, и тогда их видно в блоке «КОЛОНКИ МИМО ПРИВЯЗКИ».
    """
    layout = []
    for role in COLUMN_ORDER:
        cid = (cfg.get("columns") or {}).get(role)
        items = by_col.get(cid, []) if cid else []
        shown = items
        if role == "done":  # закрытых копится много — показываем свежие
            shown = sorted(items, key=lambda t: t.get("modificatedDate") or "",
                           reverse=True)[:limit]
        name = statuses.get(cid, "?") if cid else UNBOUND_COLUMN
        layout.append((role, cid, name, items, sorted(shown, key=prio_of)))
    return layout


def cmd_board(args):
    cfg, _ = load_config()
    statuses = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])}
    cmap = column_map(cfg["projectId"])
    by_col = {}
    for t in board_tasks(cfg["projectId"]):
        by_col.setdefault(cmap.get(t["id"]), []).append(t)

    # Раскладку считаем до печати: и теги, и ширина колонки держателя должны
    # опираться на то, что реально попадёт на экран, а не на весь проект —
    # иначе скрытые под --limit закрытые задачи раздвигали бы доску.
    layout = board_layout(cfg, by_col, statuses, args.limit)
    # задачу из дневника «вне колонок» показывать незачем: она закрыта и унесена
    # приложением, а не потеряна — сирота, которую надо чинить, выглядит иначе
    loose = [t for t in by_col.get(None, [])
             if int(t.get("checked") or 0) == 0 and not t.get("journalDate")]
    holders = board_holders([t for *_, shown in layout for t in shown] + loose)
    pad = board_pad(holders)

    print(f"{cfg.get('projectTitle')} ({cfg['projectId']})")

    # Плохие новости — вперёд. Сирота без колонки и лишние колонки печатались
    # последними и ничем не выделялись: на живой сессии агент не заметил ни того,
    # ни другого за шесть вызовов подряд и завёл дубль задачи.
    #
    # Роль без колонки — первой: пока привязка неполная, доска неполна целиком,
    # и половину команд (start/next/list/move) она всё равно не пустит.
    # Диагноз «пропала в трекере» или «никогда не привязывали» ставит doctor —
    # здесь только факт и ссылка на него.
    unbound = [role for role, cid, *_ in layout if not cid]
    if unbound:
        print(f"\n⚠ РОЛЕЙ БЕЗ КОЛОНКИ — {len(unbound)} ({', '.join(unbound)}): "
              "привязка неполная, задачи по этим ролям доска не покажет."
              "\n  разобраться: sing.py doctor  ·  починить: sing.py init --apply")
    if loose:
        print(f"\n⚠ ВНЕ КОЛОНОК — {len(loose)}: задача есть, на доске её не видно."
              "\n  почини: sing.py move <id> <роль>")
        for t in loose:
            print(pad(holders.get(t["id"])) + brief(t))
    known = set((cfg.get("columns") or {}).values())
    extra = {cid: name for cid, name in statuses.items() if cid not in known}
    if extra:
        print(f"\n⚠ КОЛОНКИ МИМО ПРИВЯЗКИ — {len(extra)}: доска шире, чем знает скилл."
              "\n  разобраться: sing.py doctor")
        for cid, name in sorted(extra.items(), key=lambda x: x[1]):
            print(f"  {cid}  «{name}»  задач={len(by_col.get(cid, []))}")

    for role, cid, name, items, shown in layout:
        if not cid:
            # Счётчика намеренно нет: «— 0» читалось бы как «в колонке пусто», а
            # про колонку, которой нет в привязке, доска не знает ничего.
            print(f"\n[{role}] {name} — sing.py doctor")
            continue
        print(f"\n[{role}] {name} — {len(items)}"
              + (f" (показаны {len(shown)})" if len(shown) < len(items) else ""))
        for t in shown:
            done = " ✓" if int(t.get("checked") or 0) == 1 else ""
            # в дневнике = приложение унесло закрытую задачу из активного списка;
            # на доске она остаётся, но в самом приложении её там уже не видно
            done += " (в дневнике)" if t.get("journalDate") else ""
            print(pad(holders.get(t["id"])) + brief(t) + done)


def open_children_counts(tasks):
    """Сколько НЕзакрытых подзадач у каждой задачи. Считается из уже полученной
    выборки — отдельного запроса на это не делаем."""
    counts = {}
    for t in tasks:
        parent = t.get("parent")
        if parent and int(t.get("checked") or 0) == 0:
            counts[parent] = counts.get(parent, 0) + 1
    return counts


def not_ready_reason(task, today=None, open_children=0):
    """Почему задачу нельзя брать ПРЯМО СЕЙЧАС, хотя она в очереди. None — можно.

    Это решения человека, принятые в приложении: «отложить» и «начать такого-то
    числа». Игнорировать их нельзя — очередь, выдающая отодвинутое, перестаёт быть
    очередью. Но и прятать такие задачи с доски нельзя: исчезнувшая карточка
    выглядит как потерянная, поэтому режем только на выдаче (`next`), а `board` и
    `list` их показывают с пометкой.
    """
    if task.get("deferred"):
        return "отложена"
    # `start` приходит полным ISO со временем — сравниваем календарные даты,
    # иначе «сегодня, но позже» выглядит как будущее и задача не берётся весь день
    start = (task.get("start") or "")[:10]
    today = today or datetime.date.today().isoformat()
    if start and start > today:
        return f"начало {start}"
    if open_children:
        # Родитель — это его подзадачи. Взять его раньше них значит либо сделать
        # их работу мимо доски, либо закрыть заголовок, под которым осталось
        # незакрытое. Сами подзадачи при этом берутся как обычные задачи.
        return f"ждёт подзадач: {open_children}"
    return None


def _pick_pool(cfg, role, include_done=False, group=None, ready_only=False,
               reasons=None):
    """include_done — для просмотра; `next` обязан брать только незакрытые.

    ready_only — убрать отложенные, запланированные на будущее и ждущие своих
    подзадач (см. not_ready_reason). Включается только для выдачи задачи, не для
    показа. reasons — если передан словарь, заполняется {id задачи: причина}.
    """
    cid = col_id(cfg, role)
    cmap = column_map(cfg["projectId"])
    source = live_tasks(cfg["projectId"]) if include_done else open_tasks(cfg["projectId"])
    pool = [t for t in source if cmap.get(t["id"]) == cid]
    if group:
        gid = resolve_group(cfg["projectId"], group)
        pool = [t for t in pool if t.get("group") == gid]
    kids = open_children_counts(source)
    if reasons is not None:
        for t in pool:
            r = not_ready_reason(t, open_children=kids.get(t["id"], 0))
            if r:
                reasons[t["id"]] = r
    if ready_only:
        pool = [t for t in pool
                if not not_ready_reason(t, open_children=kids.get(t["id"], 0))]
    return sorted(pool, key=lambda t: (prio_of(t),
                                       t.get("deadline") or "9999",
                                       t.get("createdDate") or ""))


def cmd_groups(args):
    cfg, _ = load_config()
    if args.create:
        gid = resolve_group(cfg["projectId"], args.create, create=True)
        print(f"секция «{args.create}» -> {gid}")
        return
    groups = project_groups(cfg["projectId"])
    if not groups:
        print("Секций нет — задачи лежат в проекте без разбиения.")
        return
    counts = {}
    for t in open_tasks(cfg["projectId"]):
        counts[t.get("group")] = counts.get(t.get("group"), 0) + 1
    for g in sorted(groups, key=lambda x: x.get("parentOrder") or 0):
        print(f"{g['id']}  открытых={counts.get(g['id'], 0):<3} «{g['title']}»")
    loose = counts.get(None, 0) + sum(v for k, v in counts.items()
                                      if k and k not in {g["id"] for g in groups})
    if loose:
        print(f"{'вне секций':<41}открытых={loose}")


def cmd_notes(args):
    cfg, _ = load_config()
    if args.add:
        n = request("POST", "/task",
                    body={"title": args.add, "projectId": cfg["projectId"],
                          "isNote": True,
                          "note": note_append(None, args.text or "")})
        print(f"{n['id']}: заметка создана — {args.add}")
        return
    notes = project_notes(cfg["projectId"])
    if not notes:
        print("Заметок в проекте нет.")
        return
    for n in notes:
        print(f"\n=== {n['id']}  {n.get('title', '')}")
        body = note_to_text(n.get("note")).strip()
        if body:
            print(body)


def cmd_next(args):
    cfg, _ = load_config()
    pool = _pick_pool(cfg, args.column, group=args.group, ready_only=True)
    if not pool:
        reasons = {}
        everything = _pick_pool(cfg, args.column, group=args.group, reasons=reasons)
        held = [(t, reasons[t["id"]]) for t in everything if t["id"] in reasons]
        if held:
            # «очередь пуста» здесь было бы неправдой: задачи есть, их отодвинул человек
            print(f"Свободных задач нет: все {len(held)} пока брать нельзя.")
            for t, r in held[:5]:
                print(f"  {t['id']}  [{r}]  {t.get('title', '')}")
        else:
            print("Свободных задач нет.")
        sys.exit(2)
    t = pool[0]
    if args.json:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    print(brief(t))
    note = note_to_text(t.get("note"))
    if note:
        print("\n--- заметка ---\n" + note)
    print_checklist(checklist_items(t["id"]))
    print(f"\nВзять в работу: sing.py start {t['id']}")


def cmd_list(args):
    cfg, _ = load_config()
    reasons = {}
    pool = _pick_pool(cfg, args.column, include_done=True, group=args.group,
                      reasons=reasons)
    if args.mine:
        tag_id = find_tag(AGENT_TAG_PREFIX + agent_name(cfg, args.agent))
        pool = [t for t in pool if tag_id and tag_id in (t.get("tags") or [])]
    tag_titles = {t["id"]: t["title"] for t in paged("/tag", "tags")}
    for t in pool:
        marks = [tag_titles.get(x, x) for x in (t.get("tags") or [])]
        extra = "  " + " ".join("#" + s for s in marks) if marks else ""
        if int(t.get("checked") or 0) == 1:
            extra += " ✓"
        reason = reasons.get(t["id"])
        if reason and int(t.get("checked") or 0) == 0:
            extra += f"  [{reason}]"
        print(brief(t, extra))


def cmd_whoami(args):
    cfg, _ = load_config(required=False)
    who = agent_name(cfg, args.agent)
    print(f"агент: {who}\nтег:   {AGENT_TAG_PREFIX}{who}")
    src = ("--agent" if args.agent else
           "$SINGULARITY_AGENT" if os.environ.get("SINGULARITY_AGENT") else
           f"определено по каталогу запуска ({os.path.realpath(__file__)})"
           if detect_agent() and any(os.sep + m + os.sep in os.path.realpath(__file__)
                                     for m, _ in AGENT_BY_SKILL_DIR) else
           "определено по метке сессии инструмента" if detect_agent() else
           "singularity.json" if (cfg or {}).get("agent") else "значение по умолчанию")
    print(f"откуда: {src}")


def cmd_show(args):
    cfg, _ = load_config(required=False)
    t = assert_task_allowed(args.id, cfg)
    print(brief(t))
    # Колонка и теги — не украшение: по карточке не было видно ни где задача на
    # доске, ни держит ли её уже другой агент, а инструментов над этим трекером пять.
    cid = task_column(args.id, t.get("projectId"))
    roles = {v: k for k, v in (cfg or {}).get("columns", {}).items()}
    names = {s["id"]: s["name"] for s in project_statuses(t["projectId"])}
    where = "ВНЕ КОЛОНОК ⚠" if not cid else f"{names.get(cid, cid)} [{roles.get(cid, 'мимо привязки')}]"
    tag_titles = {x["id"]: x["title"] for x in paged("/tag", "tags")}
    marks = " ".join("#" + tag_titles.get(x, x) for x in (t.get("tags") or []))
    print("проект:", t.get("projectId"), "| выполнена:", t.get("checked"))
    print("колонка:", where, ("| теги: " + marks) if marks else "| тегов нет")
    note = note_to_text(t.get("note"))
    if note:
        print("\n--- заметка ---\n" + note)
    # Чек-лист `show` не показывал вовсе, хотя `next` показывал: карточка,
    # открытая по T-id, выглядела как задача без шагов — и прогресс внутри
    # задачи был не виден ровно там, где его смотрят.
    print_checklist(checklist_items(args.id))


def cmd_start(args):
    cfg, _ = load_config()
    task = assert_task_allowed(args.id, cfg)
    if not args.plan and not args.no_plan:
        die(f"{args.id}: нужен план — что собираешься сделать, в двух-трёх пунктах.\n"
            f"  sing.py start {args.id} --plan \"...\"\n"
            "Задача на одно движение и планировать нечего — явно: --no-plan.")
    # Захват — первым действием: проигравший гонку не должен успеть подвинуть доску.
    who = claim_task(args.id, cfg, args.agent, take_over=args.take_over)
    res = move_to_column(args.id, col_id(cfg, "wip"), project_id=cfg["projectId"])
    if args.plan:
        # план пишем после захвата: если задачу перехватили, план не мусорит в чужой карточке
        fresh = request("GET", f"/task/{args.id}")
        request("PATCH", f"/task/{args.id}",
                body={"note": note_append(fresh.get("note"), args.plan,
                                          label=f"ПЛАН ({AGENT_TAG_PREFIX}{who})")})
    print(f"{args.id}: {res} (в работе), тег {AGENT_TAG_PREFIX}{who}"
          + (", план записан" if args.plan else ", без плана"))


def cmd_release(args):
    """Вернуть задачу в очередь: снять свой тег, отметку выполнения и колонку.

    Полный откат взятия задачи. Частичный откат (вернул колонку, забыл тег)
    оставляет на доске задачу, которая выглядит занятой, хотя ею никто не занят.
    """
    cfg, _ = load_config()
    task = assert_task_allowed(args.id, cfg)
    if args.report:
        request("PATCH", f"/task/{args.id}",
                body={"note": note_append(task.get("note"), args.report,
                                          label="ВОЗВРАТ В ОЧЕРЕДЬ")})
    if int(task.get("checked") or 0) == 1:
        request("POST", f"/task/{args.id}/uncomplete")
    who = agent_name(cfg, args.agent)
    tag_id = find_tag(AGENT_TAG_PREFIX + who)
    dropped = drop_task_tag(args.id, tag_id) if tag_id else False
    move_to_column(args.id, col_id(cfg, "todo"), project_id=cfg["projectId"])
    print(f"{args.id}: возвращена в очередь"
          + (f", тег {AGENT_TAG_PREFIX}{who} снят" if dropped else ""))


def cmd_report(args):
    cfg, _ = load_config(required=False)
    t = assert_task_allowed(args.id, cfg)
    request("PATCH", f"/task/{args.id}",
            body={"note": note_append(t.get("note"), args.text)})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    print(f"{args.id}: отчёт дописан в заметку")


def cmd_done(args):
    cfg, _ = load_config()
    task = assert_task_allowed(args.id, cfg)
    if not args.report and not args.no_report:
        die(f"{args.id}: нужен результат — что сделано, чем проверено, каким коммитом.\n"
            f"  sing.py done {args.id} --report \"...\"\n"
            "Карточка без результата бесполезна: через неделю неясно, что именно закрыли.\n"
            "Совсем нечего написать — явно: --no-report.")
    # Обязательность плана держалась только со стороны `start`, и обойти её было
    # штатным путём: `done` по задаче из очереди закрывал её с РЕЗУЛЬТАТОМ, но
    # вообще без ПЛАНА. Наблюдали на живой сессии — карточка T-fcbdb208.
    if not args.no_plan and "ПЛАН (" not in note_to_text(task.get("note")):
        col = task_column(args.id, cfg["projectId"])
        if col != col_id(cfg, "wip"):
            die(f"{args.id}: задача не бралась в работу — ни ПЛАНа в карточке, ни "
                "колонки «в работе».\n"
                f"  сначала: sing.py start {args.id} --plan \"...\"\n"
                "Задача на одно движение и планировать нечего — явно: --no-plan.")
    if args.report:
        label = "НА ПРОВЕРКУ" if args.review else "РЕЗУЛЬТАТ"
        who = agent_name(cfg, getattr(args, "agent", None))
        request("PATCH", f"/task/{args.id}",
                body={"note": note_append(task.get("note"), args.report,
                                          label=f"{label} ({AGENT_TAG_PREFIX}{who})")})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    role = "review" if args.review else "done"
    move_to_column(args.id, col_id(cfg, role), project_id=cfg["projectId"])
    if not args.review:
        request("POST", f"/task/{args.id}/complete")
    print(f"{args.id}: {'отправлена на проверку' if args.review else 'закрыта'}")


def cmd_block(args):
    cfg, _ = load_config()
    t = assert_task_allowed(args.id, cfg)
    request("PATCH", f"/task/{args.id}",
            body={"note": note_append(t.get("note"), args.reason, label="БЛОКЕР")})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    move_to_column(args.id, col_id(cfg, "blocked"), project_id=cfg["projectId"])
    print(f"{args.id}: заблокирована, причина записана в заметку")


def same_title(a, b):
    """Сравнение заголовков «на глаз»: без HTML, регистра и лишних пробелов.

    HTML тут не теория: клиент приложения оборачивает похожее на домен в <a href>,
    и после синхронизации тот же самый заголовок перестаёт совпадать побайтно.
    """
    norm = lambda s: " ".join(plain(s or "").split()).strip().lower()
    return norm(a) == norm(b)


def cmd_add(args):
    cfg, _ = load_config()
    # Наблюдение с живой сессии: из 11 вызовов `add` ни один не пришёл с --note,
    # и вся очередь встала на доску голыми заголовками. Пользователь это увидел
    # как «в карточках нет описания» — описание не потерялось, его не писали.
    # План и результат обязательны, а постановка задачи была необязательной:
    # именно её читает человек, решая, браться ли.
    if not args.note and not args.no_note:
        die(f"«{args.title}»: нужно описание — что сделать и как понять, что готово.\n"
            f"  sing.py add \"...\" --note \"...\"\n"
            "Заголовок на доске виден целиком, карточка нужна ради подробностей:\n"
            "контекст, критерий приёмки, где смотреть. Заголовок исчерпывает задачу —\n"
            "явно: --no-note.")
    # Механизм под правило «доска — первое действие»: само правило кодом не
    # подпирается, а вот его следствие — да. На живой сессии оборвавшийся `add`
    # оставил задачу без колонки, агент не посмотрел доску и создал её заново —
    # T-07bd60fd и T-3874bba6 с побайтно одинаковым заголовком.
    if not args.dup_ok:
        twins = [t for t in open_tasks(cfg["projectId"])
                 if same_title(t.get("title"), args.title)]
        if twins:
            cmap = column_map(cfg["projectId"])
            roles = {v: k for k, v in (cfg.get("columns") or {}).items()}
            lines = "\n".join(
                f"    {t['id']}  [{roles.get(cmap.get(t['id']), 'вне колонок')}]"
                for t in twins)
            die(f"Такая задача уже открыта — {len(twins)} шт.:\n{lines}\n"
                "  Продолжить её, а не заводить вторую: sing.py show <id>\n"
                "  Задача «вне колонок» — это оборвавшийся add, чинится: "
                "sing.py move <id> todo\n"
                "  Нужны две задачи с одним заголовком — явно: --dup-ok.")
    body = {"title": args.title, "projectId": cfg["projectId"]}
    if args.note:
        body["note"] = note_append(None, args.note)
    if args.parent:
        assert_task_allowed(args.parent, cfg)
        body["parent"] = args.parent
    if args.group:
        body["group"] = resolve_group(cfg["projectId"], args.group, create=args.new_group)
    if args.priority is not None:
        body["priority"] = args.priority
    if args.deadline:
        body["deadline"] = args.deadline
    t = request("POST", "/task", body=body)
    tid = t["id"]
    # Задача уже создана. Всё, что упадёт дальше, обязано назвать её id: без него
    # сироту не найти — на доске она проваливается в «вне колонок», и агент просто
    # создаёт задачу заново. Ровно так на живой сессии появился дубль.
    if move_to_column(tid, col_id(cfg, args.column), fatal=False,
                      project_id=cfg["projectId"]) is None:
        die(f"{tid}: СОЗДАНА, но осталась без колонки (сеть не дала привязать).\n"
            f"  задача существует, повторный add сделает дубль — почини её:\n"
            f"    sing.py move {tid} {args.column}")
    print(f"{tid}: создана в колонке '{args.column}' — {args.title}")


def cmd_move(args):
    """Поставить задачу в колонку, ничего больше не трогая.

    Нужна для починки: задача без колонки (упал `add`) видна только в разделе
    «вне колонок», а вернуть её было нечем — `release` заодно снимает теги и
    отметку выполнения, то есть чинит не то.
    """
    cfg, _ = load_config()
    assert_task_allowed(args.id, cfg)
    res = move_to_column(args.id, col_id(cfg, args.column), project_id=cfg["projectId"])
    print(f"{args.id}: {res} — {args.column}")


def rename_task(task_id, new_title, task=None):
    """Сменить заголовок и подтвердить ПЕРЕЧИТЫВАНИЕМ, вернув сохранённый.

    Возвращает (старый, сохранённый). Сохранённый может отличаться от
    отправленного: клиент приложения нормализует заголовки при синхронизации,
    поэтому показывать надо то, что реально лежит в трекере, а не то, что мы
    послали. Код 200 здесь ничего не доказывает (AGENTS.md §4), а одно
    немедленное чтение не отличает «ещё не применил» от «не применил» —
    различает только время, отсюда тот же цикл, что у set_task_tags().

    Правка заголовка не должна задевать ничего другого: PATCH с лишним полем
    легко стирает состояние задачи, поэтому checked/journalDate/tags/complete
    сверяются до и после.
    """
    task = task or request("GET", f"/task/{task_id}")
    old = task.get("title", "")
    watched = ("checked", "journalDate", "complete", "projectId")

    def snapshot(t):
        state = {k: t.get(k) for k in watched}
        state["tags"] = sorted(t.get("tags") or [])
        return state

    before = snapshot(task)
    request("PATCH", f"/task/{task_id}", body={"title": new_title})

    fresh = None
    for attempt in range(1, TAG_SETTLE_TRIES + 1):
        fresh = request("GET", f"/task/{task_id}")
        if (fresh.get("title") or "").strip() == new_title.strip():
            break
        if attempt < TAG_SETTLE_TRIES:
            time.sleep(TAG_SETTLE_PAUSE)
    else:
        die(f"{task_id}: заголовок не применился за {TAG_SETTLE_TRIES} "
            f"перечитываний ({TAG_SETTLE_PAUSE * (TAG_SETTLE_TRIES - 1):.0f} с) — "
            f"в трекере по-прежнему «{(fresh or {}).get('title', '')}».\n"
            "  Это уже не лаг синхронизации: проверь задачу в трекере.")

    after = snapshot(fresh)
    touched = [k for k in before if before[k] != after[k]]
    if touched:
        die(f"{task_id}: переименование задело лишнее — {', '.join(touched)}.\n"
            f"  было {  {k: before[k] for k in touched} }, стало { {k: after[k] for k in touched} }.\n"
            "  Заголовок изменён, остальное состояние задачи изменяться не должно.")
    return old, fresh.get("title", "")


def cmd_rename(args):
    """Переименовать карточку. Заголовок — то, по чему человек находит задачу."""
    cfg, _ = load_config(required=False)
    task = assert_task_allowed(args.id, cfg)
    new = args.title.strip()
    if not new:
        die(f"{args.id}: пустой заголовок — переименовывать не во что.")
    if new == (task.get("title") or "").strip():
        print(f"{args.id}: заголовок уже такой — {task.get('title')}")
        return
    old, saved = rename_task(args.id, new, task)
    print(f"{args.id}: переименована\n  было:  {old}\n  стало: {saved}"
          + ("\n  ⚠ трекер сохранил не то, что отправлено — показан сохранённый"
             if saved.strip() != new else ""))


def cmd_rm(args):
    """Удалить задачу. Для уборки за собой: правила требуют убирать тестовые
    данные в тот же заход, а команды удаления в скилле не было вовсе."""
    cfg, _ = load_config(required=False)
    t = assert_task_allowed(args.id, cfg)
    title = t.get("title", "")
    if not args.yes:
        die(f"{args.id}: «{title}»\n"
            "  удаление необратимо — подтверди явно: sing.py rm <id> --yes")
    request("DELETE", f"/task/{args.id}")
    if request("GET", f"/task/{args.id}", soft=True) is not None:
        die(f"{args.id}: сервер ответил, но задача на месте — не удалена.")
    print(f"{args.id}: удалена — {title}")


def checklist_items(task_id):
    """Пункты чек-листа задачи в ТОМ ЖЕ порядке, в каком их видит агент.

    Один источник порядка на показ и на поиск — принципиально: ссылаться на
    пункт по номеру можно только если нумерация в выводе `show`/`next` и
    нумерация в `check` считаются одинаково. Разойдись сортировка — команда
    молча отметит соседний пункт.
    """
    items = [c for c in paged("/checklist-item", "checklistItems", {"parent": task_id})
             if not c.get("removed")]
    return sorted(items, key=lambda c: (c.get("parentOrder")
                                        if c.get("parentOrder") is not None else 0))


def item_done(item):
    return bool(item.get("done"))


def print_checklist(items, indent="  "):
    """Показать чек-лист с номерами и прогрессом.

    Номер — не украшение: это дешёвый способ сослаться на пункт, и он обязан
    быть в каждом выводе чек-листа, иначе агенту придётся делать лишний запрос
    ради `CH-`id.
    """
    if not items:
        return
    ready = sum(1 for c in items if item_done(c))
    print(f"\n--- чек-лист {ready}/{len(items)} ---")
    for n, c in enumerate(items, 1):
        print(f"{indent}{n}. " + ("[x] " if item_done(c) else "[ ] ")
              + plain(c.get("title", "")))


def resolve_item(items, ref):
    """Найти пункт чек-листа по номеру из вывода, тексту или CH-id.

    Три способа, потому что ссылаются на пункт трое разных: агент только что
    прочитал `show`/`next` и держит в руках номер; человек смотрит в карточку и
    называет пункт словами; скрипт знает `CH-`id. Требовать id было бы лишним
    запросом на каждую отметку, а запрещать текст — заставлять человека считать
    строки.

    Неоднозначность — отказ, а не «возьму первый»: отмеченный не тот пункт
    выглядит как выполненная работа, и никто не пойдёт это перепроверять.
    """
    ref = (ref or "").strip()
    if ref.startswith("CH-"):
        hit = next((c for c in items if c.get("id") == ref), None)
        if not hit:
            die(f"«{ref}»: такого пункта в чек-листе этой задачи нет.")
        return hit
    if re.fullmatch(r"\d+", ref):
        n = int(ref)
        if not 1 <= n <= len(items):
            die(f"«{ref}»: в чек-листе {len(items)} пункт(ов), номера — от 1 "
                f"до {len(items)}.")
        return items[n - 1]
    low = ref.casefold()
    if not low:
        die("Пустая ссылка на пункт: нужен номер, текст пункта или CH-id.")
    # Точное совпадение бьёт подстроку: пункт «тесты» не должен спорить с
    # пунктом «тесты на пагинацию», если назвали его целиком.
    pool = [c for c in items if plain(c.get("title", "")).strip().casefold() == low]
    if not pool:
        pool = [c for c in items if low in plain(c.get("title", "")).casefold()]
    if not pool:
        die(f"«{ref}»: пункт не найден. Список — sing.py checklist <T-id>.")
    if len(pool) > 1:
        listing = "\n".join("    " + plain(c.get("title", "")) for c in pool)
        die(f"«{ref}»: под описание подходит пунктов — {len(pool)}:\n{listing}\n"
            "  уточни текст или сошлись на номер из вывода show/next.")
    return pool[0]


def cmd_checklist(args):
    cfg, _ = load_config(required=False)
    assert_task_allowed(args.id, cfg)
    if not args.items:
        # Без аргументов — показать: нумерованный список и есть тот вход, по
        # которому потом зовут check, а отдельная команда ради этого лишняя.
        items = checklist_items(args.id)
        if not items:
            print(f"{args.id}: чек-листа нет.")
            return
        print_checklist(items)
        return
    existing = checklist_items(args.id)
    base = max((c.get("parentOrder") or 0 for c in existing), default=-1) + 1
    for i, title in enumerate(args.items):
        request("POST", "/checklist-item",
                body={"parent": args.id, "title": title, "parentOrder": base + i})
    fresh = checklist_items(args.id)
    # По коду ответа не верим (AGENTS.md §4): сверяем, что пункты реально легли.
    added = len(fresh) - len(existing)
    if added != len(args.items):
        die(f"{args.id}: отправлено пунктов {len(args.items)}, а в задаче их стало "
            f"больше на {added}. Сервер ответил, но применил не всё.")
    print(f"{args.id}: добавлено пунктов — {len(args.items)}")
    print_checklist(fresh)


def set_checklist(args, done):
    """`check` / `uncheck`: отметить пункты и УБЕДИТЬСЯ, что отметка встала.

    `POST /checklist-item/{id}/check` отвечает 200 и на пункте, который не
    изменился, поэтому единственная настоящая проверка — перечитать список
    тем же запросом, каким его показывают `show`/`next`, и посмотреть на `done`.
    """
    cfg, _ = load_config(required=False)
    assert_task_allowed(args.id, cfg)
    items = checklist_items(args.id)
    if not items:
        die(f"{args.id}: чек-листа нет — отмечать нечего.\n"
            f"  завести: sing.py checklist {args.id} \"шаг 1\" \"шаг 2\"")
    # Сначала разбираем ВСЕ ссылки и только потом пишем: отказ на третьем
    # аргументе не должен оставить половину пунктов отмеченной.
    targets, seen = [], set()
    for ref in args.items:
        hit = resolve_item(items, ref)
        if hit["id"] not in seen:
            seen.add(hit["id"])
            targets.append(hit)
    verb = "check" if done else "uncheck"
    touched = [c for c in targets if item_done(c) != done]
    for c in touched:
        request("POST", f"/checklist-item/{c['id']}/{verb}")
    fresh = {c["id"]: c for c in checklist_items(args.id)}
    stuck = [c for c in touched if item_done(fresh.get(c["id"], c)) != done]
    if stuck:
        names = ", ".join(plain(c.get("title", "")) for c in stuck)
        die(f"{args.id}: сервер ответил 200, но done не изменился у пунктов: {names}.\n"
            "  Состояние трекера не изменилось так, как ожидалось.")
    word = "отмечено" if done else "снято отметок"
    skipped = len(targets) - len(touched)
    tail = f", уже было — {skipped}" if skipped else ""
    print(f"{args.id}: {word} — {len(touched)}{tail}")
    print_checklist(sorted(fresh.values(),
                           key=lambda c: (c.get("parentOrder")
                                          if c.get("parentOrder") is not None else 0)))


def cmd_check(args):
    set_checklist(args, done=True)


def cmd_uncheck(args):
    set_checklist(args, done=False)


# ------------------------------------------------------------- сверка с эталоном

# Скилл живёт в репозитории-эталоне, а работают агенты с копиями, разложенными по
# каталогам инструментов. Правило «после правки — раскатать, до работы — сверить»
# было текстом в AGENTS.md и не сработало ни разу: правку помнят, раскатку нет.
# Поэтому сверка висит на командах, которые рабочий цикл и так делает
# обязательными, — их выполняет любой из пяти агентов и ровно в те два момента,
# когда расхождение ещё можно отработать: перед началом правок и при закрытии.
SYNC_CHECK_COMMANDS = {"start", "done"}


def skill_repo_root(start=None):
    """Корень репозитория-эталона этого скилла, если работа идёт именно в нём.

    В любом другом репозитории сверять нечего и предупреждать не о чем. Признак —
    install.sh рядом с SKILL.md именно этого скилла: одного install.sh мало,
    он есть у половины репозиториев на диске.
    """
    d = os.path.abspath(start or os.getcwd())
    while True:
        skill = os.path.join(d, "SKILL.md")
        if os.path.isfile(os.path.join(d, "tools", "install.sh")) and os.path.isfile(skill):
            try:
                with open(skill, encoding="utf-8") as f:
                    head = f.read(512)
            except OSError:
                return None
            return d if "name: singularity-tasks" in head else None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def warn_if_skill_drifted(command):
    """Предупредить, если установленные копии скилла разошлись с этим деревом.

    Проверка локальная (сравнение файлов), без сети и без обращения к трекеру.
    Три правила, от которых зависит, будут ли её читать:

      * при совпадении не печатает ничего — гейт, который краснеет на каждом
        действии, перестают читать;
      * никогда не раскатывает сама: из worktree раскатка залила бы в общие
        каталоги чужую незакоммиченную ветку;
      * никогда не роняет команду — сверка не важнее задачи, ради которой её
        позвали, поэтому любая её собственная поломка проходит молча.
    """
    if command not in SYNC_CHECK_COMMANDS or os.environ.get("SINGULARITY_NO_SYNC_CHECK"):
        return
    root = skill_repo_root()
    if not root:
        return
    try:
        r = subprocess.run(
            ["bash", os.path.join(root, "tools", "install.sh"), "--check", "--quiet"],
            cwd=root, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    # 0 — совпадает; 1 — расхождение; прочее — сверить не удалось, это не повод шуметь
    if r.returncode != 1:
        return
    targets = (r.stdout or "").strip() or "подробности ниже"
    out = ["", f"⚠ установленные копии скилла разошлись с этим деревом: {targets}",
           "  Либо правка ещё не раскатана, либо база этого дерева устарела:",
           "  пять агентов сейчас работают не по тому, что ты видишь.",
           "  Что именно разошлось: tools/install.sh --check"]
    if os.path.isfile(os.path.join(root, ".git")):   # .git-файл => подключённый worktree
        out.append("  Это worktree — отсюда не раскатывать: install.sh зальёт эту ветку"
                   " в общие каталоги поверх работы параллельных сессий.")
    else:
        out.append("  Раскатать: tools/install.sh")
    print("\n".join(out), file=sys.stderr)


# --------------------------------------------------------------------------- CLI


def main():
    p = argparse.ArgumentParser(prog="sing.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="проверить токен и привязку репо")
    sp.add_argument("--write", action="store_true",
                    help="проверить и права на запись: пробная задача создаётся "
                         "в привязанном проекте и удаляется в том же заходе")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("projects", help="список проектов")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("init", help="привязать репо к проекту (по умолчанию — сухой прогон)")
    sp.add_argument("--project",
                    help="название проекта или P-id; по умолчанию — имя каталога репозитория")
    sp.add_argument("--path", default=".", help="корень репозитория")
    sp.add_argument("--columns", help='JSON вида {"todo":"К работе",...}')
    sp.add_argument("--own-columns", action="store_true",
                    help="создать свои колонки, даже если системных ещё нет "
                         "(доска раздвоится, когда приложение досоздаст свои)")
    sp.add_argument("--apply", action="store_true", help="выполнить план")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("board", help="доска проекта по колонкам")
    sp.add_argument("--limit", type=int, default=10,
                    help="сколько закрытых показывать в колонке done")
    sp.set_defaults(fn=cmd_board)

    sp = sub.add_parser("next", help="следующая задача из очереди")
    sp.add_argument("--column", default="todo", choices=COLUMN_ORDER)
    sp.add_argument("--group", help="брать только из секции (название или Q-id)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_next)

    sp = sub.add_parser("groups", help="секции проекта")
    sp.add_argument("--create", metavar="НАЗВАНИЕ", help="завести секцию")
    sp.set_defaults(fn=cmd_groups)

    sp = sub.add_parser("notes", help="заметки проекта (контекст для агента)")
    sp.add_argument("--add", metavar="ЗАГОЛОВОК", help="создать заметку")
    sp.add_argument("--text", help="текст создаваемой заметки")
    sp.set_defaults(fn=cmd_notes)

    sp = sub.add_parser("list", help="задачи в колонке")
    sp.add_argument("--column", default="todo", choices=COLUMN_ORDER)
    sp.add_argument("--group", help="только из секции (название или Q-id)")
    sp.add_argument("--mine", action="store_true", help="только со своим agent-тегом")
    sp.add_argument("--agent", help="имя агента (по умолчанию $SINGULARITY_AGENT)")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("whoami", help="под каким тегом работает этот агент")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_whoami)

    sp = sub.add_parser("show", help="карточка задачи")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("start", help="взять задачу в работу: план, колонка, agent-тег")
    sp.add_argument("id")
    sp.add_argument("--plan", help="что собираешься сделать, в двух-трёх пунктах")
    sp.add_argument("--no-plan", action="store_true",
                    help="осознанно без плана (задача на одно движение)")
    sp.add_argument("--take-over", action="store_true",
                    help="забрать задачу, занятую другим агентом (снимет его метку)")
    sp.add_argument("--agent", help="имя агента (по умолчанию $SINGULARITY_AGENT)")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("report", help="дописать отчёт в заметку задачи")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("done", help="закрыть задачу (или отправить на проверку)")
    sp.add_argument("id")
    sp.add_argument("--report", help="результат: что сделано, чем проверено, коммит")
    sp.add_argument("--no-report", action="store_true",
                    help="осознанно без результата")
    sp.add_argument("--review", action="store_true", help="в колонку review, не закрывать")
    sp.add_argument("--no-plan", action="store_true",
                    help="закрыть задачу, которую не брали через start")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_done)

    sp = sub.add_parser("release", help="вернуть задачу в очередь и снять свой тег")
    sp.add_argument("id")
    sp.add_argument("--report", help="объяснение, почему возвращена")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("block", help="пометить задачу заблокированной")
    sp.add_argument("id")
    sp.add_argument("reason")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_block)

    sp = sub.add_parser("add", help="создать задачу/подзадачу")
    sp.add_argument("title")
    sp.add_argument("--parent", help="T-id родительской задачи")
    sp.add_argument("--group", help="секция проекта (название или Q-id)")
    sp.add_argument("--new-group", action="store_true",
                    help="завести секцию, если её нет")
    sp.add_argument("--column", default="todo", choices=COLUMN_ORDER)
    sp.add_argument("--dup-ok", action="store_true",
                    help="завести, даже если задача с таким заголовком уже открыта")
    sp.add_argument("--note", help="описание: что сделать, критерий готовности")
    sp.add_argument("--no-note", action="store_true",
                    help="осознанно без описания (заголовок исчерпывает задачу)")
    sp.add_argument("--priority", type=int, choices=[0, 1, 2])
    sp.add_argument("--deadline", help="ISO-дата")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("move", help="переставить задачу в колонку (починка доски)")
    sp.add_argument("id")
    sp.add_argument("column", choices=COLUMN_ORDER)
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("rename", help="переименовать карточку с подтверждением по факту")
    sp.add_argument("id")
    sp.add_argument("title")
    sp.set_defaults(fn=cmd_rename)

    sp = sub.add_parser("rm", help="удалить задачу (уборка за собой)")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true", help="подтвердить удаление")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("checklist", help="чек-лист задачи: показать или добавить пункты")
    sp.add_argument("id")
    sp.add_argument("items", nargs="*",
                    help="пункты для добавления; без них — показать текущий чек-лист")
    sp.set_defaults(fn=cmd_checklist)

    ITEM_REF = ("номер из вывода show/next, текст пункта "
                "(точное совпадение или однозначная часть) либо CH-id")

    sp = sub.add_parser("check", help="отметить пункт чек-листа выполненным")
    sp.add_argument("id")
    sp.add_argument("items", nargs="+", metavar="ПУНКТ", help=ITEM_REF)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("uncheck", help="снять отметку с пункта чек-листа")
    sp.add_argument("id")
    sp.add_argument("items", nargs="+", metavar="ПУНКТ", help=ITEM_REF)
    sp.set_defaults(fn=cmd_uncheck)

    args = p.parse_args()
    args.fn(args)
    warn_if_skill_drifted(args.cmd)


if __name__ == "__main__":
    main()
