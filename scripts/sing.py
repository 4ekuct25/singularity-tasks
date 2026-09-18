#!/usr/bin/env python3
"""CLI поверх SingularityApp REST API v2 для агентской работы с задачами.

Только stdlib. Прокси берётся из HTTP(S)_PROXY (urllib делает это сам).

Токен ищется по порядку:
  1. $SINGULARITY_TOKEN
  2. macOS Keychain: security find-generic-password -s singularity-app -a rest-token
  3. ~/.claude/.singularity-token (chmod 600)
Отказ Keychain (sandbox, замок, не-macOS) от отсутствия записи отличается по stderr,
а не по коду возврата: он у обоих случаев 44 (см. classify_keychain).

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


TOKEN_FILE = "~/.claude/.singularity-token"

# Четыре исхода чтения Keychain. Три последних раньше сливались в один диагноз
# «Токен не найден» — и человек шёл пересоздавать токен вместо того, чтобы дать
# среде доступ.
KC_OK = "ok"                    # запись прочитана
KC_ABSENT = "absent"            # Keychain отвечает, записи в нём нет
KC_DENIED = "denied"            # до Keychain не достучались: sandbox, замок, отказ
KC_NO_SECURITY = "no-security"  # утилиты `security` нет вовсе — не macOS

# ⚠️ Замерено, а не взято из документации (числа — в JOURNAL.md).
# Под sandbox (`sandbox-exec` с `deny mach-lookup com.apple.SecurityServer` —
# ровно то, что делает Codex) `security find-generic-password` отвечает ТЕМ ЖЕ
# кодом возврата 44 и ТОЙ ЖЕ строкой «could not be found in the keychain», что и
# при реально отсутствующей записи. Различает случаи только ЛИШНЯЯ строка перед
# ней — «SecKeychainSearchCreateFromAttributes: One or more parameters passed to
# a function were not valid»: поиск не смог даже начаться.
# Поэтому решение принимается по составу stderr, а не по коду возврата: гейт на
# коде возврата здесь в принципе не умеет покраснеть.
KC_NOT_FOUND_MARK = "could not be found in the keychain"


def classify_keychain(returncode, stdout, stderr):
    """«Записи нет» против «Keychain недоступен» — по составу stderr.

    Чистая функция: сама никуда не ходит, поэтому и проверяется без Keychain.
    """
    if returncode == 0 and (stdout or "").strip():
        return KC_OK
    errs = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    # «Записи нет» — это РОВНО строки про ненайденный элемент и ничего кроме.
    # Любая другая строка означает, что поиск не состоялся.
    if [ln for ln in errs if KC_NOT_FOUND_MARK not in ln]:
        return KC_DENIED
    if errs:
        return KC_ABSENT
    # Ошибка без единого слова на stderr — например, процесс убит песочницей.
    # Это не «записи нет»: об отсутствии записи `security` всегда говорит вслух.
    return KC_ABSENT if returncode == 0 else KC_DENIED


def read_keychain_token():
    """(токен|None, статус). Наружу токен отдаётся только вызывающему get_token."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "singularity-app",
             "-a", "rest-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None, KC_NO_SECURITY
    except (OSError, subprocess.SubprocessError):
        # Сюда же таймаут: залоченный Keychain ждёт диалога разблокировки,
        # которого в неинтерактивной среде никто не увидит (проверено — висит).
        return None, KC_DENIED
    status = classify_keychain(out.returncode, out.stdout, out.stderr)
    return (out.stdout.strip() if status == KC_OK else None), status


# Короткая строка — для doctor, развёрнутая — для отказа. Ни та ни другая не
# печатает сам токен и не предлагает «положить токен в переменную»: подсказка,
# уводящая секрет в окружение и историю шелла, — не безопасное действие.
KC_DIAGNOSIS = {
    KC_ABSENT: (
        "записи в Keychain нет",
        "Создай токен на https://me.singularity-app.com (раздел API) и положи:\n"
        "  security add-generic-password -s singularity-app -a rest-token -w\n"
        "Значение у -w опущено намеренно: токен спросят скрытым вводом, и он не\n"
        "попадёт в историю шелла.",
    ),
    KC_DENIED: (
        "Keychain не отвечает — доступ закрыт средой, а не запись отсутствует",
        "Это типичная картина в sandbox (Codex, sandbox-exec): песочница закрывает\n"
        "доступ к com.apple.SecurityServer, и `security` отвечает тем же кодом 44,\n"
        "что и при отсутствующей записи. Запись при этом, скорее всего, на месте —\n"
        "НЕ пересоздавай токен, он не виноват.\n"
        "Что сделать:\n"
        "  1. Проверь среду: `security list-keychains`. Список keychain'ов —\n"
        "     доступ есть, дело в записи; ошибка SecKeychainCopySearchList —\n"
        "     доступа нет, дело в среде.\n"
        "  2. Дай среде доступ к Keychain (запуск без sandbox либо с разрешением\n"
        "     на securityd) или выполни команду вне ограниченной среды.\n"
        "  3. Если Keychain просто заперт — разблокируй его:\n"
        "     `security unlock-keychain` (пароль спросят скрытым вводом).",
    ),
    KC_NO_SECURITY: (
        "утилиты `security` в системе нет — Keychain здесь недоступен в принципе",
        "Похоже, это не macOS. Положи токен в файл " + TOKEN_FILE + " с правами 600\n"
        "(`chmod 600`) — скилл читает его следом за Keychain.",
    ),
}


def token_problem(status):
    """Текст отказа: заголовок по факту, а не общее «Токен не найден»."""
    short, advice = KC_DIAGNOSIS.get(status, KC_DIAGNOSIS[KC_ABSENT])
    return f"Токен не прочитан: {short}.\n{advice}"


def token_source():
    """Откуда берётся токен — БЕЗ самого токена. (источник|None, статус Keychain)."""
    if os.environ.get("SINGULARITY_TOKEN"):
        return "переменная окружения SINGULARITY_TOKEN", None
    tok, status = read_keychain_token()
    if tok:
        return "macOS Keychain (singularity-app / rest-token)", status
    if os.path.exists(os.path.expanduser(TOKEN_FILE)):
        return f"файл {TOKEN_FILE}", status
    return None, status


def get_token():
    tok = os.environ.get("SINGULARITY_TOKEN")
    if tok:
        return tok.strip()
    tok, status = read_keychain_token()
    if tok:
        return tok
    path = os.path.expanduser(TOKEN_FILE)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    die(token_problem(status))


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


def projects_in_scope(projects=None):
    """Подпроекты корня области. Сам корень в список не входит — в нём не работают.

    Единственное место, где считается «что вообще разрешено»: и `init`, и разовая
    адресация `--project` ищут проект ТОЛЬКО здесь, поэтому тёзка снаружи области
    не должен даже находиться по имени.
    """
    projects = projects if projects is not None else all_projects()
    root = resolve_root(projects)
    return [p for p in projects
            if p["id"] != root["id"]
            and any(x["id"] == root["id"] for x in project_chain(p["id"], projects))]


def match_projects(ref, allowed):
    """Проекты под ссылку «название | часть названия | P-id». Список, а не один:
    неоднозначность обязана дойти до человека, а не решаться за него.

    Точное совпадение названия важнее подстроки — иначе «tasks» выбирало бы сразу
    и «tasks», и «singularity-tasks», то есть однозначный запрос выглядел бы
    неоднозначным.
    """
    ref = (ref or "").strip()
    if ref.startswith("P-"):
        return [p for p in allowed if p["id"] == ref]
    exact = [p for p in allowed
             if p.get("title", "").strip().lower() == ref.lower()]
    if exact:
        return exact
    return [p for p in allowed if ref.lower() in p.get("title", "").lower()]


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


def project_columns(project_id, statuses=None):
    """Роли -> колонки по ФАКТИЧЕСКОЙ доске проекта, ничего в нём не создавая.

    Нужна для разовой адресации `--project`: у соседнего проекта файла привязки
    здесь нет, и раскладку колонок приходится узнавать по его доске. Порядок тот
    же, что у `init`: сначала системная колонка проекта (её id детерминирован,
    см. SYSTEM_SUFFIX), затем колонка с названием по умолчанию.

    Роль, которой на доске не нашлось, в ответ НЕ ПОПАДАЕТ — придумать колонку
    значило бы молча положить задачу не туда. Отказ выдаст col_id, и он назовёт
    проект: `--project` читает чужую доску, а не чинит её.
    """
    statuses = project_statuses(project_id) if statuses is None else statuses
    live = [s for s in statuses if not s.get("removed")]
    ids = {s["id"] for s in live}
    by_name = {}
    for s in live:
        by_name.setdefault((s.get("name") or "").strip().lower(), s["id"])
    mapping = {}
    for role in COLUMN_ORDER:
        sys_id = system_status_id(project_id, role)
        if sys_id and sys_id in ids:
            mapping[role] = sys_id
            continue
        hit = by_name.get(DEFAULT_COLUMNS[role].strip().lower())
        if hit:
            mapping[role] = hit
    return mapping


def config_for_project(ref):
    """Привязка НА ОДНУ КОМАНДУ по `--project <название|P-id>`. Диск не трогается.

    Это адресация, а не переключение репозитория: файл привязки не читается и не
    переписывается, следующая команда снова работает со своим проектом.

    Ограничение области от этого не слабеет, а проверяется дважды: искать можно
    только среди подпроектов ROOT_PROJECT_TITLE (projects_in_scope), и найденное
    ещё раз проходит assert_allowed — область считается по адресуемому проекту,
    а не по тому, в каком каталоге запущена команда.
    """
    projects = all_projects()
    hits = match_projects(ref, projects_in_scope(projects))
    if len(hits) > 1:
        die(f"--project «{ref}»: подходит несколько проектов:\n  " +
            "\n  ".join(f"{p['id']}  {p['title']}" for p in hits))
    if not hits:
        # Отказ обязан отличать «вне области» от «нет такого»: иначе запрет
        # выглядит как опечатка, и его повторяют, подбирая написание.
        needle = (ref or "").strip().lower()
        outside = [p for p in projects
                   if p["id"] == (ref or "").strip()
                   or p.get("title", "").strip().lower() == needle]
        if outside:
            assert_allowed(outside[0]["id"], "проект --project")  # назовёт путь
        die(f"--project «{ref}»: среди подпроектов «{ROOT_PROJECT_TITLE}» такого нет.\n"
            "  что есть: sing.py projects")
    target = hits[0]
    assert_allowed(target["id"], "проект --project")
    return {"projectId": target["id"], "projectTitle": target.get("title"),
            "columns": project_columns(target["id"]), "adhoc": ref}


def command_config(args):
    """Откуда команда берёт проект: привязка репозитория или разовый `--project`.

    Одна точка на все команды, умеющие адресовать соседний проект, — чтобы
    правило «`--project` ничего не пишет на диск» не приходилось повторять
    (и однажды забыть) в каждой из них.
    """
    ref = getattr(args, "project", None)
    if ref:
        return config_for_project(ref), None
    return load_config()


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
        if cfg.get("adhoc"):
            # Совет «init --apply» здесь был бы вредным: он привязал бы ТЕКУЩИЙ
            # репозиторий к чужому проекту, то есть сделал не то, о чём просили.
            die(f"В проекте «{cfg.get('projectTitle')}» нет колонки под роль '{role}'.\n"
                f"  искали системную колонку проекта и колонку «{DEFAULT_COLUMNS[role]}».\n"
                "  --project читает чужую доску как есть и ничего в ней не создаёт;\n"
                "  разложить колонки по ролям может только init — из того репозитория,\n"
                "  которому этот проект принадлежит.")
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


# Голый `T-...` человеку бесполезен: открыть его нечем. Форматов два, оба
# подтверждены — веб-адрес прислал пользователь, схему нашли в бандле приложения
# (`singularityapp://?&page=any&id=${n}`, зарегистрирована в его Info.plist).
#
# Основной — веб: он кликается в любом чате и терминале и открывается с телефона.
# Схема `singularityapp://` работает только там, где стоит десктоп-приложение, и
# во многих интерфейсах вообще не кликабельна, поэтому её печатает только `show`.
TASK_LINK_WEB = "https://web.singularity-app.com/#/?&id={}"
TASK_LINK_APP = "singularityapp://?&page=any&id={}"


def task_link(task_id):
    return TASK_LINK_WEB.format(task_id)


def task_link_app(task_id):
    return TASK_LINK_APP.format(task_id)


def prio_of(t):
    """0 = высокий, поэтому `or 1` тут нельзя — ноль ложный."""
    p = t.get("priority")
    return 1 if p is None else int(p)


# Полдень UTC, а не полночь. Дата без времени — это КАЛЕНДАРНЫЙ день, и он не
# должен съезжать при переводе в зону: полночь по Москве — это 21:00 предыдущих
# суток по UTC, и доска (`brief()` режет `deadline[:10]`) показала бы 14 октября
# вместо 15-го. Полдень UTC держит тот же календарный день при сдвиге от -11:59
# до +11:59, то есть всюду, кроме крайних UTC+12…+14 (Новая Зеландия, Фиджи,
# Кирибати) — там в приложении дата покажется следующим днём. Единой точки,
# верной для всех зон, не существует: их диапазон 26 часов шире суток.
DATE_ONLY_TIME = "T12:00:00.000Z"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Полный ISO-8601: дата, T или пробел, время, обязательная явная зона (Z или ±HH:MM).
# Дробная часть не фиксирована — в базе живут формы с 6, 3 и 0 знаками (api.md).
_ISO_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})$", re.I)

DEADLINE_HELP = (
    "дата 2026-10-15 (станет полднем UTC того же дня) либо полный ISO-8601 "
    "с явной таймзоной: 2026-10-15T18:00:00.000Z, 2026-10-15T18:00:00+03:00")


def parse_deadline(raw, field="--deadline"):
    """Привести дату к тому виду, который API принимает, ЛИБО объяснить отказ.

    Голую дату `2026-10-15` сервер отвечает `400` — и это была не косметика: `add`
    падал сырым дампом ответа API, задача не создавалась (T-daa1e87c). Справка при
    этом обещала «ISO-дату», то есть ровно ту форму, которую сервер не берёт.

    Разбор здесь, ДО запроса: непонятный формат обязан ловиться локально, с
    примером в тексте, а не превращаться в дамп чужого ответа. Календарная
    корректность (`2026-02-30`, `2026-13-01`) проверяется тем же разбором —
    регулярка её не видит, а сервер видит и отвечает тем же 400.

    Возвращает строку для тела запроса; `None` — только для пустого ввода
    (`--deadline ''` = снять дедлайн), и отличать «снять» от «не трогать» обязан
    вызывающий, по `is None` самого аргумента.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    def bad(why):
        die(f"{field}: {why}\n  ожидается: {DEADLINE_HELP}\n  получено: {raw}")

    if _DATE_ONLY_RE.match(raw):
        y, m, d = (int(x) for x in raw.split("-"))
        try:
            datetime.date(y, m, d)
        except ValueError as e:
            bad(f"такой даты нет в календаре ({e})")
        return raw + DATE_ONLY_TIME

    hit = _ISO_DT_RE.match(raw)
    if not hit:
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d", raw):
            bad("нет таймзоны — сервер берёт только время с явной зоной "
                "(Z или ±HH:MM)")
        bad("не похоже ни на дату, ни на ISO-8601 datetime")
    y, m, d, hh, mm, ss, frac, zone = hit.groups()
    try:
        datetime.datetime(int(y), int(m), int(d), int(hh), int(mm), int(ss or 0))
    except ValueError as e:
        bad(f"такой даты/времени нет в календаре ({e})")
    if zone.upper() != "Z":
        off_h, off_m = int(zone[1:3]), int(zone[-2:])
        if off_h > 14 or off_m > 59:
            bad(f"таймзона {zone} вне диапазона ±14:00")
    # Полный ISO отдаём КАК ПРИСЛАЛИ: явная зона — это осознанный выбор автора,
    # и нормализовать её в UTC значило бы молча подменить то, что он написал.
    return raw


def deadline_instant(raw):
    """Дедлайн как МОМЕНТ времени; `None` — пусто или не разбирается.

    Сверять сохранённое строкой нельзя: одно и то же время записывается
    по-разному (замер, api.md: в базе живут формы с 6, 3 и 0 знаками дробной
    части). Сегодня сервер отдаёт строку байт в байт как прислана — проверено
    на живом API, `+03:00` вернулся `+03:00`, — но подтверждение правки обязано
    переживать нормализацию зоны, иначе безобидное `18:00+03:00` -> `15:00Z`
    выглядело бы как «поле не изменилось» и роняло команду на ровном месте.
    """
    hit = _ISO_DT_RE.match((raw or "").strip())
    if not hit:
        return None
    y, m, d, hh, mm, ss, frac, zone = hit.groups()
    try:
        moment = datetime.datetime(int(y), int(m), int(d), int(hh), int(mm),
                                   int(ss or 0),
                                   int(float(frac or 0) * 1_000_000))
    except ValueError:
        return None
    if zone.upper() == "Z":
        shift = datetime.timedelta(0)
    else:
        sign = -1 if zone[0] == "-" else 1
        shift = sign * datetime.timedelta(hours=int(zone[1:3]),
                                          minutes=int(zone[-2:]))
    return moment.replace(tzinfo=datetime.timezone.utc) - shift


def field_applied(name, task, want):
    """Поле задачи в трекере уже равно тому, что мы хотим записать?

    Сверка по СМЫСЛУ поля, а не по «== из словаря»: у приоритета отсутствие
    значения означает «обычный» (`prio_of`), у дедлайна сравниваются моменты
    времени, а не строки.
    """
    if name == "priority":
        return prio_of(task) == int(want)
    if name == "deadline":
        return deadline_instant(task.get("deadline")) == deadline_instant(want)
    if name == "note":
        # Сверять дельту строкой нельзя: одна и та же заметка записывается
        # разными операциями (а сервер вправе их нормализовать). Значение имеет
        # текст, его человек и читает в карточке.
        return note_to_text(task.get("note")) == note_to_text(want)
    return task.get(name) == want


def field_show(name, value):
    """Значение поля так, как его читает человек."""
    if name == "priority":
        return prio_label(int(value)) if value is not None else "—"
    if name == "note":
        text = note_to_text(value).strip()
        return (f"«{text[:60]}…»" if len(text) > 60 else f"«{text}»") if text else "—"
    return value or "—"


# Имена полей в выводе — человеческие, в теле запроса — те, что понимает API.
FIELD_TITLES = {"priority": "приоритет", "deadline": "дедлайн", "group": "секция",
                "note": "заметка"}

# Что сверяется до и после ЛЮБОЙ правки полей (`set_task_fields`): PATCH с лишним
# полем стирает состояние задачи, и ловится это только снимком. Команды со своим
# набором дополняют этот — `REGROUP_WATCHED`, `NOTE_WATCHED`.
SET_WATCHED = ("title", "checked", "journalDate", "complete", "projectId")


def project_groups(project_id):
    """Секции проекта. У каждого проекта есть безымянная fake-группа — она не секция."""
    return [g for g in paged("/task-group", "taskGroups", {"parent": project_id})
            if not g.get("removed") and not g.get("fake") and (g.get("title") or "").strip()]


def fake_group(project_id):
    """id безымянной служебной группы проекта — то самое «вне секций».

    «Вне секций» — это НЕ `null`: замер на живой задаче (2026-09-19) показал, что
    только что созданная задача уже лежит в `group=Q-…` служебной группы, а
    `PATCH {"group": null}` и `{"group": ""}` сервер отвергает — `400 Must start
    with one of: "Q-"`. Поэтому снятие секции = запись сюда этого id.

    Вычислять его нельзя (api.md): `Q-<projectId>` совпадает лишь у 9 служебных
    групп из 56, остальные — обычные `Q-<uuid>`. Только чтение списка.
    """
    hit = next((g for g in paged("/task-group", "taskGroups", {"parent": project_id})
                if not g.get("removed")
                and (g.get("fake") or not (g.get("title") or "").strip())), None)
    return hit["id"] if hit else None


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


PRIORITY_NAMES = {0: "высокий", 1: "обычный", 2: "низкий"}


def prio_label(p):
    """Метка приоритета для человека. «!» у высокого — чтобы он был виден в списке."""
    name = PRIORITY_NAMES.get(p, "?")
    return "!" + name if p == 0 else name


def brief(t, extra=""):
    dl = f" дедлайн={t['deadline'][:10]}" if t.get("deadline") else ""
    return (f"{t['id']}  [{prio_label(prio_of(t))}]{dl}  "
            f"{plain(t.get('title', ''))}{extra}")


# --------------------------------------------------------------------------- машинный вывод

# `--json` — один формат на все команды, которые что-то показывают.
#
# Чем обошлось его отсутствие (T-16593b9c, перенос backlog): сверить 30 созданных
# карточек с доской было нечем, и сверку писали регексом по человекочитаемому
# выводу — по префиксу `T-` и метке `[обычный]`. Такой парсер ломается от любой
# косметической правки формата, причём молча: выдаёт «не найдено 0» вместо отказа.
#
# Отсюда три правила, которые держат этот вывод пригодным для машины:
#   * объект задачи одинаков ВЕЗДЕ (task_json) — разный набор ключей у `list` и
#     `show` означал бы, что парсер всё равно пишется под конкретную команду;
#   * ключи присутствуют всегда, даже пустые — «то есть, то нет» заставляет
#     проверять каждое обращение;
#   * в stdout только JSON. Все предупреждения и подсказки уходят в stderr:
#     одна человеческая строка сверху — и `json.loads` падает на всём выводе.


def json_out(payload):
    """Единственная точка печати машинного вывода — чтобы форма была одна."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def tag_titles(tasks):
    """id тега -> заголовок, одним запросом на всю показываемую выборку.

    Справочник тегов в аккаунте один, у задачи лежат только их id. Если тегов нет
    ни на одной задаче — запроса не будет вовсе: доска нового проекта не должна
    дорожать ради колонки, которая всё равно окажется пустой.
    """
    if not any(t.get("tags") for t in tasks):
        return {}
    return {tg["id"]: (tg.get("title") or "") for tg in paged("/tag", "tags")}


def task_tags(task, titles):
    """Теги задачи ЗАГОЛОВКАМИ, по алфавиту. Неизвестный id остаётся id-ом:
    молча потерять чужой тег дороже, чем показать его сырым."""
    return sorted(titles.get(x, x) for x in (task.get("tags") or []))


def group_titles(project_id):
    """id секции -> название. Запрос делается только ради `--json`."""
    return {g["id"]: g["title"] for g in project_groups(project_id)}


def checklist_json(items):
    """Чек-лист машинно: номер тот же, что в выводе `show`/`next` (см. checklist_items)."""
    return [{"n": n, "id": c.get("id"), "title": plain(c.get("title", "")),
             "done": item_done(c)} for n, c in enumerate(items, 1)]


def task_json(task, role=None, column_name=None, tags=(), group_title=None,
              open_children=0, not_ready=None, **extra):
    """Задача одним и тем же объектом во всех командах с `--json`.

    Каждая пометка, которую человек видит текстом, здесь — отдельное поле, иначе
    машинный вывод беднее человеческого и парсить всё равно приходится строки:

        ✓                    -> done            (в дневнике)      -> journal
        [отложена]           -> deferred        [начало ГГГГ-ММ-ДД] -> start
        [ждёт подзадач: N]   -> openChildren    ⚠ ВНЕ КОЛОНОК     -> column: null
        повторяющаяся        -> recurring

    `notReady` — та же причина словами, ровно как её печатает `next`; поля рядом
    позволяют не разбирать её текст.

    `column` — РОЛЬ скилла (todo/doing/…), а не название колонки в трекере:
    название человек меняет в приложении, роль — контракт скилла. Название лежит
    рядом, в `columnName`.
    """
    prio = prio_of(task)
    # «Ничего нет» в машинном выводе выглядит одинаково — `null`. API отдаёт
    # отсутствующего родителя пустой строкой (замер на живой задаче: parent=""),
    # и отличать `""` от `null` пришлось бы каждому, кто это читает.
    empty = lambda v: v or None          # noqa: E731 — короче именованной функции
    d = {
        "id": task.get("id"),
        "title": plain(task.get("title", "")),
        "column": role,
        "columnName": column_name,
        "project": task.get("projectId"),
        # id группы отдаётся как есть; groupTitle=None при непустом group значит
        # безымянную fake-группу проекта, то есть «вне секций» (см. project_groups)
        "group": empty(task.get("group")),
        "groupTitle": group_title,
        "tags": list(tags),
        "priority": prio,
        "priorityName": PRIORITY_NAMES.get(prio, "?"),
        "deadline": empty(task.get("deadline")),
        "done": int(task.get("checked") or 0) == 1,
        "journal": bool(task.get("journalDate")),
        "deferred": bool(task.get("deferred")),
        "start": (task.get("start") or "")[:10] or None,
        "openChildren": open_children,
        "recurring": task.get("recurrence") is not None,
        "notReady": not_ready,
        "parent": empty(task.get("parent")),
        "url": task_link(task.get("id")),
    }
    d.update(extra)
    return d


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
    # Источник токена называется ДО запроса к API. Иначе единственным, что видит
    # человек в ограниченной среде, остаётся отказ get_token — и «Keychain закрыт
    # песочницей» неотличимо от «токена нет». Сам токен не печатается.
    src, status = token_source()
    if src is None:
        die(token_problem(status))
    print(f"✓ токен: {src}")
    if status in (KC_DENIED, KC_NO_SECURITY):
        # Токен взялся из запасного источника, а Keychain при этом молчит:
        # без этой строки расхождение всплывёт только на чужой машине.
        print(f"  ⚠ Keychain недоступен: {KC_DIAGNOSIS[status][0]}")
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
        # Раньше здесь печатался сырой ответ API — полсотни служебных полей, из
        # которых осмысленны четыре, и ни одного вычисленного (глубина, архив).
        # Машинный вывод обязан быть контрактом скилла, а не транзитом чужой схемы:
        # поля API меняются на их стороне и молча ломают того, кто их читал.
        json_out([{"id": p["id"], "title": p.get("title", ""),
                   "parent": p.get("parent"),
                   "depth": max(len(project_chain(p["id"], projects)) - 2, 0),
                   "archived": bool(p.get("journalDate"))}
                  for p in sorted(items, key=lambda x: x.get("title", ""))])
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
    #
    # Сделать это автоматически нельзя, и это ЗАМЕРЕНО, а не предположено
    # (tools/check-kanban-lazy.py, разбор — references/api.md):
    #   · POST /kanban-status со своим `id` -> 400 «property id should not exist»:
    #     занять детерминированный KS-<pid>-TODO заранее невозможно;
    #   · POST /kanban-task-status на несуществующую колонку -> 400 «Kanban status
    #     not found»: отложенная привязка «id запишем, колонки появятся потом» даёт
    #     нерабочую доску, а не терпеливую;
    #   · DELETE системной колонки -> 403 «Cannot delete default columns»: ошибку
    #     потом не убрать.
    # Значит, единственная защита — не начинать. Зато у проекта, созданного через
    # API (в том числе самим `init --apply`), все три системные колонки есть сразу,
    # и ручное переключение режима не нужно вовсе — это и есть короткий путь.
    if missing_system and not own_columns:
        die("Канбан проекта ещё не развёрнут: системных колонок "
            + ", ".join(f"«{names[r]}» ({r})" for r in missing_system) + " нет.\n"
            "  Создать свои поверх — значит получить доску-двойник: приложение "
            "досоздаст системные позже (замер: через 43 с), и они встанут рядом "
            "с одинаковыми именами.\n"
            "  Развернуть их за тебя нельзя, это замер, а не осторожность: свой id "
            "колонке API не даёт (400 «property id should not exist»), ссылка на "
            "ещё не созданную колонку отвергается (400 «Kanban status not found»), "
            "а системную колонку не удалить (403 «Cannot delete default columns»).\n"
            "  Что делать — любое из двух:\n"
            "    · пусть проект заведёт сам init: у созданного через API все три "
            "системные колонки есть сразу, режим руками переключать не надо —\n"
            f'        sing.py init --project "<название нового проекта>" --apply\n'
            "    · открой ЭТОТ проект в приложении (или заведи в нём любую задачу), "
            "дай синхронизироваться и повтори init.\n"
            "  Осознанно хочешь свои колонки вместо системных — явно: --own-columns.")
    return mapping, to_create


def _git_out(root, *args):
    """Вывод git-команды строкой или None, если git не ответил."""
    try:
        r = subprocess.run(["git", "-C", root, *args],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def git_main_worktree(path="."):
    """Корень ОСНОВНОГО рабочего дерева репозитория и признак worktree.

    Возвращает `(root, is_linked_worktree)`; `(None, False)` — если это не git.

    В подключённом worktree `--show-toplevel` отдаёт каталог самого worktree, а он
    называется по ВЕТКЕ (`.claude/worktrees/se-aeza-timeout-963b4c`), а не по
    репозиторию. Поймано на живой сессии: `init` в worktree репозитория multihop
    напечатал план «СОЗДАТЬ проект se-aeza-timeout-963b4c», хотя проект multihop
    существовал и привязаться надо было к нему.

    Основное дерево считается от ОБЩЕГО служебного каталога (`--git-common-dir`):
    у подключённого worktree он указывает на `<основное дерево>/.git`, тогда как
    `--git-dir` — на `<общий>/worktrees/<имя>`. Совпали — мы в основном дереве.
    """
    root = os.path.abspath(path)
    top = _git_out(root, "rev-parse", "--show-toplevel")
    if not top:
        return None, False
    common = _git_out(root, "rev-parse", "--git-common-dir")
    git_dir = _git_out(root, "rev-parse", "--git-dir")
    if not common or not git_dir:
        return top, False
    # git печатает пути относительно своего cwd, а это `root` из-за `-C`
    common_abs = os.path.realpath(os.path.join(root, common))
    git_abs = os.path.realpath(os.path.join(root, git_dir))
    if common_abs == git_abs:
        return top, False
    # `<основное дерево>/.git` -> основное дерево; голый `repo.git` -> `repo`
    base = os.path.basename(common_abs)
    if base == ".git":
        return os.path.dirname(common_abs), True
    if base.endswith(".git"):
        return common_abs[: -len(".git")].rstrip(os.sep), True
    return common_abs, True


def repo_project_name(path="."):
    """Имя проекта по умолчанию — имя каталога репозитория.

    Спрашивать его у человека незачем: в подавляющем большинстве случаев проект
    называется как репозиторий, а промпт, где надо что-то подставить руками,
    подставляют неправильно или забывают.

    В git worktree берётся имя ОСНОВНОГО рабочего дерева: имя каталога worktree —
    это имя ветки, и проект по нему получился бы одноразовым (см.
    `git_main_worktree`).
    """
    root = os.path.abspath(path)
    main, _ = git_main_worktree(root)
    if main:
        root = main
    return os.path.basename(root.rstrip(os.sep))


def is_git_repo(path="."):
    """Проверить, является ли путь частью git-репозитория."""
    root = os.path.abspath(path)
    res = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                         capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() == "true"


def has_logs_or_journal(path="."):
    """Проверить, есть ли в каталоге журнал решений (JOURNAL.md) или файлы логов."""
    root = os.path.abspath(path)
    if os.path.exists(os.path.join(root, "JOURNAL.md")) or os.path.exists(os.path.join(root, "logs")):
        return True
    try:
        for item in os.listdir(root):
            if item.endswith(".log") or item.endswith("_LOG.md"):
                return True
    except OSError:
        pass
    return False


DEFAULT_PROJECT_TASKS = [
    {
        "title": "Удалить влитые и устаревшие ветки в локальном и удалённом репозитории",
        "column": "todo",
        "git_only": True,
        "note": (
            "Что сделать:\n"
            "1. Проверить статус веток через git branch -a и их смерженность с main.\n"
            "2. Удалить смерженные локальные ветки через git branch -d.\n"
            "3. Удалить смерженные удаленные ветки через git push origin --delete (если есть).\n"
            "4. Если ветка не смержена, но устарела/брошена — согласовать или удалить через -D.\n\n"
            "Критерий готовности:\n"
            "git branch -a содержит только ветку main (и актуальные рабочие ветки при их наличии)."
        ),
    },
    {
        "title": "Выполнить ротацию логов проекта",
        "column": "todo",
        "logs_or_git": True,
        "note": (
            "Что сделать:\n"
            "1. Проверить размер и дату записей в логах и журнале проекта (JOURNAL.md, каталоги logs/ и runtime-логи).\n"
            "2. Для разросшихся текстовых логов/журнала перенести устаревшие записи (старше 7-14 дней) в архивные файлы (например, по неделям/месяцам в archive/).\n"
            "3. Для файлов, в которые пишет работающий процесс, применять copy-truncate (не mv), чтобы не сломать запись в дескриптор открытого файла.\n"
            "4. Убедиться, что основной файл содержит только актуальный контекст, а архивные записи сохранены без потерь.\n\n"
            "Критерий готовности:\n"
            "Размер основного лога/журнала уменьшен до актуального окна, старые записи сохранены в архиве, активные процессы продолжают писать без сбоев."
        ),
    },
]


def plan_default_tasks(existing_tasks, is_git, has_logs=True, no_tasks=False):
    """Определить список обязательных задач для проекта.

    Идемпотентно: если задача с таким заголовком уже есть в проекте (открыта,
    закрыта или оформлена шаблоном повторяющейся серии), она не дублируется.
    Задачи с git_only=True создаются только в git-репозиториях.
    Задачи с logs_or_git=True создаются, если есть git или логи/журнал.
    """
    if no_tasks:
        return [], []
    plan_lines = []
    to_create = []
    for tdef in DEFAULT_PROJECT_TASKS:
        title = tdef["title"]
        if tdef.get("git_only") and not is_git:
            continue
        if tdef.get("logs_or_git") and not (is_git or has_logs):
            continue
        already = any(same_title(t.get("title"), title) for t in existing_tasks)
        if already:
            plan_lines.append(f"ПРОПУСТИТЬ задачу «{title}» (уже есть в проекте)")
        else:
            plan_lines.append(f"СОЗДАТЬ задачу «{title}» (роль {tdef['column']})")
            to_create.append(tdef)
    return plan_lines, to_create


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


def refuse_guessed_project(title, root_title):
    """Единственный текст отказа «проект по угаданному имени не создаю».

    Один текст на оба режима намеренно: сухой прогон и `--apply` обязаны говорить
    одно и то же. Разошлись они ровно потому, что жили в разных местах.
    """
    return (f"Проекта «{title}» в «{root_title}» нет, а имя я угадал по "
            "каталогу репозитория.\n"
            "  Создавать проект по догадке не буду — назови явно:\n"
            f'    sing.py init --project "{title}" --apply\n'
            "  Либо укажи существующий: sing.py projects")


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
            _, in_worktree = git_main_worktree(os.path.abspath(args.path))
            if in_worktree:
                print(f"Проект не указан, и это git worktree — имя беру по основному "
                      f"рабочему дереву: «{args.project}» "
                      f"(каталог worktree назван по ветке, проект по нему был бы лишним)")
            else:
                print(f"Проект не указан — беру имя репозитория: «{args.project}»")
    projects = all_projects()
    root = resolve_root(projects)
    # искать только среди подпроектов корня — тёзка снаружи не должен даже находиться
    allowed = projects_in_scope(projects)
    matches = match_projects(args.project, allowed)
    if len(matches) > 1:
        die("Под запрос подходит несколько проектов:\n  " +
            "\n  ".join(f"{p['id']}  {p['title']}" for p in matches))
    target = matches[0] if matches else None
    if not target and args.project.startswith("P-"):
        assert_allowed(args.project, "проект")  # выдаст внятный отказ

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

    # Привязка к существующему проекту — рутина; создание нового в трекере человека
    # рутиной не является. По УГАДАННОМУ имени не создаём: иначе опечатка в имени
    # каталога или запуск не в том месте тихо заводят лишний проект. Проверено на
    # себе: повторный `init --apply` без --project из каталога проверки создал в
    # трекере проект «init-proba» вместе с колонками.
    #
    # Отказ считается ДО плана и печатается в обоих режимах из одного текста: раньше
    # сухой прогон обещал «СОЗДАТЬ проект», а `--apply` по тому же вводу отказывал, —
    # план, расходящийся с поведением, читают как разрешение (поймано в worktree
    # репозитория multihop). Заодно это экономит запросы: колонки и задачи
    # несуществующего проекта спрашивать не у кого.
    if not target and guessed:
        if not args.apply:
            print("\nПлан (ничего не изменено, добавь --apply):")
            print(f"  · ОТКАЗАТЬСЯ создавать проект «{args.project}»: "
                  "имя угадано по каталогу, --apply его не создаст")
            print("  Дальше не произойдёт ничего: без проекта нет ни колонок, "
                  "ни привязки, ни обязательных задач.")
            print(refuse_guessed_project(args.project, root["title"]), file=sys.stderr)
            return
        die(refuse_guessed_project(args.project, root["title"]))

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

    is_git = is_git_repo(repo_root)
    has_logs = has_logs_or_journal(repo_root)
    existing_tasks = board_tasks(target["id"]) if target else []
    tasks_plan, tasks_to_create = plan_default_tasks(
        existing_tasks, is_git, has_logs=has_logs, no_tasks=getattr(args, "no_tasks", False))
    plan.extend(tasks_plan)

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

    if not getattr(args, "no_tasks", False) and tasks_to_create:
        fresh_tasks = board_tasks(target["id"])
        for tdef in tasks_to_create:
            if any(same_title(t.get("title"), tdef["title"]) for t in fresh_tasks):
                continue
            body = {
                "title": tdef["title"],
                "projectId": target["id"],
                "note": note_append(None, tdef["note"]),
            }
            t = request("POST", "/task", body=body)
            tid = t["id"]
            role = tdef["column"]
            cid = mapping.get(role)
            if cid:
                move_to_column(tid, cid, fatal=False, project_id=target["id"])
            print(f"создана обязательная задача «{tdef['title']}» ({tid}) в роли {role}")


# Потолок ширины колонки «кто держит»: одно неудачно длинное имя агента не
# должно сдвинуть всю доску вправо. Длиннее — обрезается многоточием.
HOLDER_COL_MAX = 16


def board_holders(tasks, titles=None):
    """taskId -> «@имя» держателя по тегам `agent:*` (несколько — через запятую).

    Справочник тегов берётся одним запросом (tag_titles) и может быть передан
    готовым: `board --json` разворачивает те же теги полностью, и ходить за ними
    второй раз незачем.
    """
    titles = tag_titles(tasks) if titles is None else titles
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
    cfg, _ = command_config(args)
    statuses = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])}
    cmap = column_map(cfg["projectId"])
    tasks = board_tasks(cfg["projectId"])
    by_col = {}
    for t in tasks:
        by_col.setdefault(effective_column(t, cmap, cfg), []).append(t)

    # Раскладку считаем до печати: и теги, и ширина колонки держателя должны
    # опираться на то, что реально попадёт на экран, а не на весь проект —
    # иначе скрытые под --limit закрытые задачи раздвигали бы доску.
    layout = board_layout(cfg, by_col, statuses, args.limit)
    # задачу из дневника «вне колонок» показывать незачем: она закрыта и унесена
    # приложением, а не потеряна — сирота, которую надо чинить, выглядит иначе
    loose = [t for t in by_col.get(None, [])
             if int(t.get("checked") or 0) == 0 and not t.get("journalDate")]
    known = set((cfg.get("columns") or {}).values())
    extra = {cid: name for cid, name in statuses.items() if cid not in known}
    titles = tag_titles([t for *_, shown in layout for t in shown] + loose)

    if args.json:
        kids = open_children_counts(tasks)
        gnames = group_titles(cfg["projectId"])

        def one(t, role, column_name):
            n = kids.get(t["id"], 0)
            return task_json(t, role=role, column_name=column_name,
                             tags=task_tags(t, titles),
                             group_title=gnames.get(t.get("group")),
                             open_children=n,
                             not_ready=not_ready_reason(t, open_children=n))

        json_out({
            # adhoc — тот же признак «доска чужая», что в человеческом выводе стоит
            # предупреждением: машинный вывод не должен быть беднее человеческого.
            # null — доска своего репозитория, строка — как её адресовали в --project.
            "project": {"id": cfg["projectId"], "title": cfg.get("projectTitle"),
                        "adhoc": cfg.get("adhoc")},
            # Роль без колонки: count=null, а не 0. «0» читалось бы как «в колонке
            # пусто», а про непривязанную роль доска не знает ничего — ровно то
            # различие, ради которого в человеческом выводе там нет счётчика.
            # name берётся из statuses, а не из раскладки: там у пропавшей в
            # трекере колонки стоит человеческое «?», а машине нужен null
            "columns": [{"role": role, "id": cid, "name": statuses.get(cid),
                         "bound": bool(cid),
                         "count": len(items) if cid else None,
                         "tasks": [one(t, role, statuses.get(cid)) for t in shown]}
                        for role, cid, name, items, shown in layout],
            "looseTasks": [one(t, None, None) for t in loose],
            "unboundRoles": [role for role, cid, *_ in layout if not cid],
            "unknownColumns": [{"id": cid, "name": name,
                                "count": len(by_col.get(cid, []))}
                               for cid, name in sorted(extra.items(),
                                                       key=lambda x: x[1])],
        })
        return

    holders = board_holders([t for *_, shown in layout for t in shown] + loose,
                            titles)
    pad = board_pad(holders)

    # Чужую доску обязательно называть чужой: без пометки агент принимает её за
    # доску своего репозитория и берёт задачу «из очереди», которая не его.
    print(f"{cfg.get('projectTitle')} ({cfg['projectId']})"
          + (f"\n⚠ ЧУЖОЙ ПРОЕКТ — показан по --project «{cfg['adhoc']}»; "
             "привязка репозитория не менялась.\n"
             "  брать отсюда задачу в работу нельзя: start/next работают только "
             "с привязанным проектом." if cfg.get("adhoc") else ""))

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
        # Сюда попадает только то, что не показывает и приложение: задача без
        # связки и без колонки-роли `todo` в привязке. Раньше блок ловил ещё и
        # обычные задачи из приложения — и пугал поломкой там, где её нет.
        print(f"\n⚠ ВНЕ КОЛОНОК — {len(loose)}: задача есть, на доске её не видно."
              "\n  почини: sing.py move <id> <роль>")
        for t in loose:
            print(pad(holders.get(t["id"])) + brief(t))
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
    # `is not None`, а не проверка на истинность: пустой словарь ложный, и шаблон
    # с пустым правилом прошёл бы мимо — та же ловушка, что с приоритетом 0.
    # У обычной задачи API ключ не отдаёт вовсе (проверено на живом проекте).
    if task.get("recurrence") is not None:
        # Шаблон повторяющейся серии — не задача, а правило её порождения.
        # Закрыть его как обычную задачу значит тронуть всю серию. Экземпляры
        # серии (`recurrenceGeneratorId`) — наоборот, обычная работа, и когда их
        # срок пришёл, они берутся как всё остальное.
        return "повторяющаяся: это шаблон серии, а не задача"
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


def effective_column(task, cmap, cfg):
    """Колонка задачи так, как её видит ПРИЛОЖЕНИЕ.

    Задача, заведённая в приложении, приходит без `kanban-task-status` — связку
    создаёт только наш `add`. Приложение при этом показывает её в «Новые»
    (проверено на канбане проекта: задача без связки лежит там рядом с
    привязанной). Значит и очередь обязана: иначе агент говорит «очередь пуста»
    при непустом проекте, а человек видит свои задачи на доске.

    Закрытые и унесённые в дневник исключение: в «Новые» их не показывает и
    приложение.
    """
    linked = cmap.get(task["id"])
    if linked:
        return linked
    if int(task.get("checked") or 0) == 1 or task.get("journalDate"):
        return None
    return (cfg.get("columns") or {}).get("todo")


def _pick_pool(cfg, role, include_done=False, group=None, ready_only=False,
               reasons=None, children=None):
    """include_done — для просмотра; `next` обязан брать только незакрытые.

    ready_only — убрать отложенные, запланированные на будущее и ждущие своих
    подзадач (см. not_ready_reason). Включается только для выдачи задачи, не для
    показа. reasons — если передан словарь, заполняется {id задачи: причина};
    children — тем же способом {id задачи: сколько незакрытых подзадач}. Оба
    словаря заполняются из УЖЕ полученной выборки, лишних запросов не будет.
    """
    cid = col_id(cfg, role)
    cmap = column_map(cfg["projectId"])
    source = live_tasks(cfg["projectId"]) if include_done else open_tasks(cfg["projectId"])
    pool = [t for t in source if effective_column(t, cmap, cfg) == cid]
    if group:
        gid = resolve_group(cfg["projectId"], group)
        pool = [t for t in pool if t.get("group") == gid]
    kids = open_children_counts(source)
    if children is not None:
        children.update({t["id"]: kids.get(t["id"], 0) for t in pool})
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
        if not args.json:
            print(f"секция «{args.create}» -> {gid}")
            return
        # С --json создание не отчитывается строкой, а ПЕРЕЧИТЫВАЕТ список: и
        # stdout остаётся чистым JSON, и созданная секция подтверждается фактом,
        # а не кодом ответа (AGENTS.md §4).
        print(f"секция «{args.create}» -> {gid}", file=sys.stderr)
    groups = project_groups(cfg["projectId"])
    if not groups and not args.json:
        print("Секций нет — задачи лежат в проекте без разбиения.")
        return
    counts = {}
    for t in open_tasks(cfg["projectId"]):
        counts[t.get("group")] = counts.get(t.get("group"), 0) + 1
    if args.json:
        # «Секций нет» машине не сообщение, а пустой список: отдельная ветка с
        # человеческой фразой на месте JSON — ровно тот случай, когда парсер
        # падает на редком состоянии проекта.
        json_out({
            "groups": [{"id": g["id"], "title": g["title"],
                        "openTasks": counts.get(g["id"], 0)}
                       for g in sorted(groups, key=lambda x: x.get("parentOrder") or 0)],
            "outsideGroups": counts.get(None, 0)
            + sum(v for k, v in counts.items()
                  if k and k not in {g["id"] for g in groups}),
        })
        return
    for g in sorted(groups, key=lambda x: x.get("parentOrder") or 0):
        print(f"{g['id']}  открытых={counts.get(g['id'], 0):<3} «{g['title']}»")
    loose = counts.get(None, 0) + sum(v for k, v in counts.items()
                                      if k and k not in {g["id"] for g in groups})
    if loose:
        print(f"{'вне секций':<41}открытых={loose}")


# Заметка — это задача с `isNote=true` (api.md), поэтому её id такой же `T-…`, и
# правится она тем же `PATCH /task`. Сверяемое при правке шире, чем у `set`: у
# заметки нет колонки, зато есть признак `isNote` — потеряв его, заметка встанет
# в очередь задач, и агент попробует её «выполнить».
NOTE_WATCHED = SET_WATCHED + ("isNote", "parent", "group")


def note_task(note_id, cfg=None):
    """Заметка по id — с проверкой области и того, что это действительно заметка."""
    t = assert_task_allowed(note_id, cfg)
    if not t.get("isNote"):
        die(f"{note_id}: это задача, а не заметка проекта.\n"
            f"  карточка: sing.py show {note_id}")
    if t.get("removed") or t.get("deleteDate"):
        die(f"{note_id}: заметка удалена — читать и править нечего.")
    return t


def note_json(note, group_title=None, tags=()):
    """Заметка тем же объектом, что и задача: формат один на все команды.

    Заметка и есть задача (`isNote`), просто не попавшая на канбан, — отсюда
    `column: null`. Отдельная форма объекта заставила бы вызывающего писать
    разбор под каждую команду, от чего `--json` и уходил.
    """
    return task_json(note, tags=tags, group_title=group_title,
                     note=note_to_text(note.get("note")),
                     appUrl=task_link_app(note["id"]))


def notes_out(args, payload, lines):
    """Человеку — строки, машине — JSON; и то и другое ровно в свой поток.

    Под `--json` человеческие строки не исчезают, а уходят в stderr: в stdout
    должен разбираться JSON ЦЕЛИКОМ, одна строка сверху — и вызывающий получает
    исключение вместо данных.
    """
    out = sys.stderr if args.json else sys.stdout
    for line in lines:
        print(line, file=out)
    if args.json:
        json_out(payload)


def cmd_notes(args):
    """Заметки проекта: показать, создать, дописать, переписать, удалить.

    Раньше здесь было только создание (`--add/--text`): долгоживущий контекст,
    который SKILL.md велит читать перед работой, нельзя было ни поправить, ни
    убрать иначе как руками в приложении (T-ac96c736). Контекст, который агент не
    может сопровождать, расходится с реальностью — ровно тот класс расхождений,
    против которого написан весь скилл.
    """
    cfg, _ = load_config()
    pid = cfg["projectId"]
    modes = [f"--{m}" for m in ("add", "show", "edit", "rm") if getattr(args, m)]
    if len(modes) > 1:
        die(f"{', '.join(modes)} — это разные действия, за раз делается одно.")
    # Молча проигнорированный флаг — это правка, которая «прошла», но не туда:
    # `--append` без `--edit` выглядел бы как дописывание, а был бы ничем.
    if args.append and not args.edit:
        die("--append дописывает в СУЩЕСТВУЮЩУЮ заметку и работает только с --edit.\n"
            "  sing.py notes --edit <T-id> --append --text \"...\"")

    if args.show:
        n = note_task(args.show, cfg)
        text = note_to_text(n.get("note")).strip()
        notes_out(args, note_json(n, group_titles(pid).get(n.get("group")))
                  if args.json else None,
                  [f"=== {n['id']}  {n.get('title', '')}", text or "(пусто)",
                   f"  {task_link(n['id'])}"])
        return

    if args.rm:
        n = note_task(args.rm, cfg)
        title = n.get("title", "")
        if not args.yes:
            die(f"{args.rm}: «{title}»\n"
                "  удаление необратимо — подтверди явно: "
                f"sing.py notes --rm {args.rm} --yes")
        request("DELETE", f"/task/{args.rm}")
        # Факт, а не код ответа: этот API умеет ответить 200, ничего не сделав.
        if request("GET", f"/task/{args.rm}", soft=True) is not None:
            die(f"{args.rm}: сервер ответил, но заметка на месте — не удалена.")
        notes_out(args, note_json(n) if args.json else None,
                  [f"{args.rm}: заметка удалена — {title}"])
        return

    if args.edit:
        n = note_task(args.edit, cfg)
        if not (args.text or "").strip():
            die(f"{args.edit}: нечего писать — нужен --text с непустым текстом.\n"
                f"  переписать:  sing.py notes --edit {args.edit} --text \"...\"\n"
                f"  дописать:    sing.py notes --edit {args.edit} --append --text \"...\"\n"
                f"  удалить:     sing.py notes --rm {args.edit} --yes")
        body = (note_append(n.get("note"), args.text) if args.append
                else note_dump(_body_ops(args.text)))
        # Запись, которая ничего не меняет, обязана падать, а не рапортовать
        # успехом: подтверждать её нечем — «текст равен ожидаемому» верно и до
        # запроса, и сервер на повторную запись тем же значением отвечает 200
        # (замер). Молчаливое «ок» здесь и есть способ не заметить, что правка
        # ушла не туда.
        if field_applied("note", n, body):
            die(f"{args.edit}: текст тот же — правка ничего не изменит.\n"
                "  Проверить, что в заметке сейчас: "
                f"sing.py notes --show {args.edit}")
        was = len(note_to_text(n.get("note")))
        _, fresh = set_task_fields(args.edit, {"note": body}, n, watched=NOTE_WATCHED)
        text = note_to_text(fresh.get("note"))
        notes_out(args,
                  note_json(fresh, group_titles(pid).get(fresh.get("group")))
                  if args.json else None,
                  [f"{args.edit}: заметка {'дополнена' if args.append else 'переписана'}"
                   f" — было {was} символов, стало {len(text)}",
                   f"  {task_link(args.edit)}"])
        return

    if args.add:
        n = request("POST", "/task",
                    body={"title": args.add, "projectId": pid, "isNote": True,
                          "note": note_append(None, args.text or "")})
        # Перечитывание, а не ответ на POST: созданная заметка обязана быть
        # заметкой и нести тот текст, который просили (AGENTS.md §4).
        fresh = note_task(n["id"], cfg)
        if not field_applied("note", fresh, note_append(None, args.text or "")):
            die(f"{n['id']}: заметка создана, но текст в ней не тот, что отправлен"
                f" — {field_show('note', fresh.get('note'))}.\n"
                f"  дописать: sing.py notes --edit {n['id']} --append --text \"...\"")
        notes_out(args,
                  note_json(fresh, group_titles(pid).get(fresh.get("group")))
                  if args.json else None,
                  [f"{n['id']}: заметка создана — {args.add}",
                   f"  {task_link(n['id'])}"])
        return

    notes = project_notes(pid)
    if args.json:
        gnames = group_titles(pid)
        json_out([note_json(n, gnames.get(n.get("group"))) for n in notes])
        return
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
    kids = {}
    pool = _pick_pool(cfg, args.column, group=args.group, ready_only=True,
                      children=kids)
    if not pool:
        # Пустая очередь под --json — это `null` в stdout и объяснение в stderr:
        # код возврата 2 остаётся, но вывод не перестаёт быть JSON. Пустой stdout
        # заставил бы вызывающего отличать «нечего брать» от сбоя парсера.
        out = sys.stderr if args.json else sys.stdout
        reasons = {}
        everything = _pick_pool(cfg, args.column, group=args.group, reasons=reasons)
        held = [(t, reasons[t["id"]]) for t in everything if t["id"] in reasons]
        if held:
            # «очередь пуста» здесь было бы неправдой: задачи есть, их отодвинул человек
            print(f"Свободных задач нет: все {len(held)} пока брать нельзя.", file=out)
            for t, r in held[:5]:
                print(f"  {t['id']}  [{r}]  {t.get('title', '')}", file=out)
        else:
            print("Свободных задач нет.", file=out)
        if args.json:
            json_out(None)
        sys.exit(2)
    t = pool[0]
    if args.json:
        # Раньше здесь печатался сырой объект API: ни колонки, ни названий тегов
        # (только их id), зато полсотни служебных полей. Теперь — тот же объект,
        # что у show/list/board, плюс заметка и чек-лист: `next --json` для того и
        # зовут, чтобы решить по задаче, не ходя за ней вторым запросом.
        titles = tag_titles([t])
        statuses = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])}
        json_out(task_json(
            t, role=args.column, column_name=statuses.get(col_id(cfg, args.column)),
            tags=task_tags(t, titles),
            group_title=group_titles(cfg["projectId"]).get(t.get("group")),
            open_children=kids.get(t["id"], 0),
            note=note_to_text(t.get("note")),
            checklist=checklist_json(checklist_items(t["id"])),
            appUrl=task_link_app(t["id"])))
        return
    print(brief(t))
    note = note_to_text(t.get("note"))
    if note:
        print("\n--- заметка ---\n" + note)
    print_checklist(checklist_items(t["id"]))
    print(f"\n  {task_link(t['id'])}"
          f"\nВзять в работу: sing.py start {t['id']} --plan \"...\"")


def cmd_list(args):
    cfg, _ = command_config(args)
    if cfg.get("adhoc"):
        # Под --json пометка уходит в stderr: в stdout только JSON.
        print(f"# {cfg.get('projectTitle')} ({cfg['projectId']}) — чужой проект "
              f"по --project, только чтение",
              file=sys.stderr if args.json else sys.stdout)
    reasons, kids = {}, {}
    pool = _pick_pool(cfg, args.column, include_done=True, group=args.group,
                      reasons=reasons, children=kids)
    if args.mine:
        tag_id = find_tag(AGENT_TAG_PREFIX + agent_name(cfg, args.agent))
        pool = [t for t in pool if tag_id and tag_id in (t.get("tags") or [])]
    titles = tag_titles(pool)
    if args.json:
        statuses = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])}
        cname = statuses.get(col_id(cfg, args.column))
        gnames = group_titles(cfg["projectId"])
        json_out([task_json(
            t, role=args.column, column_name=cname, tags=task_tags(t, titles),
            group_title=gnames.get(t.get("group")),
            open_children=kids.get(t["id"], 0),
            # причина «пока брать нельзя» у закрытой задачи бессмысленна — то же
            # правило, что и в человеческом выводе строкой ниже
            not_ready=(reasons.get(t["id"])
                       if int(t.get("checked") or 0) == 0 else None))
            for t in pool])
        return
    for t in pool:
        marks = task_tags(t, titles)
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
    # Колонка и теги — не украшение: по карточке не было видно ни где задача на
    # доске, ни держит ли её уже другой агент, а инструментов над этим трекером пять.
    cid = task_column(args.id, t.get("projectId"))
    roles = {v: k for k, v in (cfg or {}).get("columns", {}).items()}
    names = {s["id"]: s["name"] for s in project_statuses(t["projectId"])}
    where = "ВНЕ КОЛОНОК ⚠" if not cid else f"{names.get(cid, cid)} [{roles.get(cid, 'мимо привязки')}]"
    titles = tag_titles([t])
    marks = " ".join("#" + s for s in task_tags(t, titles))

    if args.json:
        # Подзадачи считаются по выборке проекта — тем же счётом, что у board и
        # list. Лишний запрос здесь только под --json: `show` без него за всем
        # проектом не ходит, а поле openChildren, которое иногда 0 «потому что не
        # считали», хуже отсутствующего.
        kids = open_children_counts(live_tasks(t["projectId"])).get(args.id, 0)
        json_out(task_json(
            t, role=roles.get(cid), column_name=names.get(cid) if cid else None,
            tags=task_tags(t, titles),
            group_title=group_titles(t["projectId"]).get(t.get("group")),
            open_children=kids,
            not_ready=not_ready_reason(t, open_children=kids),
            note=note_to_text(t.get("note")),
            checklist=checklist_json(checklist_items(args.id)),
            appUrl=task_link_app(args.id)))
        return

    print(brief(t))
    print(f"  {task_link(args.id)}\n  {task_link_app(args.id)}")
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
    print(f"{args.id}: {'отправлена на проверку' if args.review else 'закрыта'}\n"
          f"  {task_link(args.id)}")


def cmd_block(args):
    cfg, _ = load_config()
    t = assert_task_allowed(args.id, cfg)
    request("PATCH", f"/task/{args.id}",
            body={"note": note_append(t.get("note"), args.reason, label="БЛОКЕР")})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    move_to_column(args.id, col_id(cfg, "blocked"), project_id=cfg["projectId"])
    print(f"{args.id}: заблокирована, причина записана в заметку\n"
          f"  {task_link(args.id)}")


def same_title(a, b):
    """Сравнение заголовков «на глаз»: без HTML, регистра и лишних пробелов.

    HTML тут не теория: клиент приложения оборачивает похожее на домен в <a href>,
    и после синхронизации тот же самый заголовок перестаёт совпадать побайтно.
    """
    norm = lambda s: " ".join(plain(s or "").split()).strip().lower()
    return norm(a) == norm(b)


def cmd_add(args):
    cfg, _ = command_config(args)
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
        body["deadline"] = parse_deadline(args.deadline)
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
    # Сервер в ответе на POST отдаёт созданный объект — это готовое свидетельство
    # того, что поле он взял, и стоит оно ноль лишних запросов. Молча проглоченный
    # `priority`/`deadline` выглядел бы как успех: задача создана, поле пустое.
    # Не `die`: задача уже существует, ронять команду здесь значит толкать на
    # повторный add и дубль. Поэтому — предупреждение и готовая починка.
    dropped = [k for k in ("priority", "deadline")
               if k in body and not field_applied(k, t, body[k])]
    if dropped:
        fix = " ".join(f"--{k} '{body[k]}'" for k in dropped)
        print(f"  ⚠ трекер не взял {', '.join(FIELD_TITLES[k] for k in dropped)} — "
              f"дописать: sing.py set {tid} {fix}")
    print(f"{tid}: создана в колонке '{args.column}' — {args.title}")
    if cfg.get("adhoc"):
        # Задача уехала в соседний проект: из этого репозитория её больше ничем
        # не открыть (show/report сверяют проект задачи с привязкой), поэтому
        # ссылка обязана быть в выводе — иначе карточку не найти.
        print(f"  проект «{cfg.get('projectTitle')}» ({cfg['projectId']}) — "
              "по --project, привязка репозитория не менялась\n"
              f"  {task_link(tid)}")


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


def set_task_fields(task_id, fields, task=None, watched=SET_WATCHED):
    """Записать поля задачи ОДНИМ PATCH и подтвердить ПЕРЕЧИТЫВАНИЕМ.

    Возвращает `(было, стало)` — словарь прежних значений и свежую задачу.
    Поле не применилось за отведённые перечитывания — `die`: этот API умеет
    ответить `200`, ничего не сделав (AGENTS.md §4), и докладывать об успехе по
    коду ответа нельзя. Одно немедленное чтение при этом не отличает «ещё не
    применил» от «не применил» — различает только время, отсюда тот же цикл с
    паузой, что у `rename_task()` и `set_task_tags()`.

    PATCH с лишним полем легко стирает состояние задачи, поэтому то, чего
    правка касаться не должна, сверяется до и после. Набор сверяемых полей —
    параметр: `regroup` стережёт ещё и родителя с заметкой (`REGROUP_WATCHED`),
    для приоритета с дедлайном они избыточны.
    """
    task = task or request("GET", f"/task/{task_id}")

    def snapshot(t):
        state = {k: t.get(k) for k in watched}
        state["tags"] = sorted(t.get("tags") or [])
        return state

    before_guard = snapshot(task)
    before = {k: task.get(k) for k in fields}
    request("PATCH", f"/task/{task_id}", body=fields)

    fresh = None
    for attempt in range(1, TAG_SETTLE_TRIES + 1):
        fresh = request("GET", f"/task/{task_id}")
        missed = [k for k, v in fields.items() if not field_applied(k, fresh, v)]
        if not missed:
            break
        if attempt < TAG_SETTLE_TRIES:
            time.sleep(TAG_SETTLE_PAUSE)
    else:
        lines = "\n".join(
            f"    {FIELD_TITLES.get(k, k)}: просили {field_show(k, fields[k])}, "
            f"в трекере {field_show(k, (fresh or {}).get(k))}" for k in missed)
        die(f"{task_id}: правка не применилась за {TAG_SETTLE_TRIES} "
            f"перечитываний ({TAG_SETTLE_PAUSE * (TAG_SETTLE_TRIES - 1):.0f} с):\n"
            f"{lines}\n"
            "  Сервер ответил успехом, но поле не изменилось. Это уже не лаг "
            "синхронизации: проверь задачу в трекере.")

    touched = [k for k, v in snapshot(fresh).items() if before_guard[k] != v]
    if touched:
        die(f"{task_id}: правка задела лишнее — {', '.join(touched)}.\n"
            f"  было { {k: before_guard[k] for k in touched} }, "
            f"стало { {k: snapshot(fresh)[k] for k in touched} }.\n"
            "  Меняться должны только названные поля.")
    return before, fresh


def cmd_set(args):
    """Сменить приоритет и/или дедлайн у СУЩЕСТВУЮЩЕЙ задачи.

    До этой команды `--priority`/`--deadline` были только у `add`, то есть поля
    задавались один раз при создании и больше не менялись. На живой сессии
    шесть карточек оказались принятыми рисками, а понизить им приоритет было
    нечем: либо руками в приложении, либо пересоздавать карточку — а это потеря
    id, тегов `agent:*` и истории отчётов (T-62ba2372).
    """
    cfg, _ = load_config(required=False)
    task = assert_task_allowed(args.id, cfg)

    fields = {}
    if args.priority is not None:
        fields["priority"] = args.priority
    # `is None` против пустой строки — это не придирка: «не трогать» и «снять»
    # обязаны различаться. `--deadline ''` пишет null, отсутствие флага не
    # отправляет поле вовсе.
    if args.deadline is not None:
        fields["deadline"] = parse_deadline(args.deadline)
    if not fields:
        die(f"{args.id}: нечего менять — нужен --priority и/или --deadline.\n"
            "  sing.py set <id> --priority 0            # 0=высокий, 1=обычный, 2=низкий\n"
            "  sing.py set <id> --deadline 2026-10-15\n"
            "  sing.py set <id> --deadline ''           # снять дедлайн")

    def value_of(t, k):
        """Приоритета может не быть в задаче вовсе, и это «обычный», а не пусто."""
        return prio_of(t) if k == "priority" else t.get(k)

    # Уже такое значение — не пишем вовсе. Иначе подтверждение перечитыванием
    # ничего не подтверждает: «поле равно ожидаемому» было бы верно и до PATCH.
    already = [k for k, v in fields.items() if field_applied(k, task, v)]
    todo = {k: v for k, v in fields.items() if k not in already}
    for k in already:
        print(f"{args.id}: {FIELD_TITLES[k]} уже {field_show(k, value_of(task, k))}"
              " — не трогаю")
    if not todo:
        print(f"  {task_link(args.id)}")
        return

    _, fresh = set_task_fields(args.id, todo, task)
    for k in todo:
        print(f"{args.id}: {FIELD_TITLES[k]} {field_show(k, value_of(task, k))}"
              f" -> {field_show(k, value_of(fresh, k))}")
    print(f"  {task_link(args.id)}")


# Секция и колонка ортогональны (api.md), поэтому `regroup` стережёт и то, чего
# `set` не стережёт: родителя и заметку. Именно их теряет PATCH с лишним полем, а
# заметка в этом скилле — постановка задачи и вся история отчётов.
REGROUP_WATCHED = SET_WATCHED + ("parent", "note")


def cmd_regroup(args):
    """Перенести СУЩЕСТВУЮЩУЮ задачу в секцию проекта или снять секцию.

    `--group` был только у `add`: секция задавалась один раз при создании. Разложить
    уже стоящую на доске очередь по разделам было нечем — либо руками в приложении,
    либо пересоздавать карточку с потерей `T-`id, тегов `agent:*` и отчётов.

    Снятие секции — не `null`: у задач «вне секций» в поле `group` лежит id
    безымянной служебной группы проекта (см. `fake_group`), а `null` сервер
    отвергает четырёхсотым. Отсюда `--clear`, который эту группу находит чтением.
    """
    cfg, _ = load_config(required=False)
    task = assert_task_allowed(args.id, cfg)
    pid = task["projectId"]
    if bool(args.group) == bool(args.clear):
        die(f"{args.id}: нужно ровно одно — название секции или --clear.\n"
            "  sing.py regroup <T-id> \"Название секции\"   # перенести\n"
            "  sing.py regroup <T-id> --clear              # вернуть вне секций")

    known = {g["id"]: g["title"] for g in project_groups(pid)}
    if args.clear:
        gid = fake_group(pid)
        if not gid:
            die(f"{args.id}: у проекта {pid} нет служебной группы — снимать секцию "
                "некуда.\n  «вне секций» в этом API — это её id, а не null: "
                "PATCH {\"group\": null} отвергается (400).")
        where = "вне секций"
    else:
        # resolve_group пропускает любой `Q-…` без проверки — для `add` этого
        # хватает (ошибётся сервер при создании), а здесь неизвестная секция
        # обязана отказывать ДО записи, поэтому id сверяется со списком секций.
        gid = resolve_group(pid, args.group)
        if gid not in known:
            listing = ", ".join(f"«{t}»" for t in known.values()) or "нет ни одной"
            die(f"{args.id}: секции {gid} нет в проекте. Есть: {listing}.\n"
                f"  Завести: sing.py groups --create \"...\"")
        where = f"«{known[gid]}»"

    was = task.get("group")
    was_where = f"«{known[was]}»" if was in known else "вне секций"
    # Уже в этой секции — PATCH не отправляется вовсе: подтверждать было бы
    # нечего, «поле равно ожидаемому» верно и до записи (та же логика, что в set).
    if was == gid:
        out = sys.stderr if args.json else sys.stdout
        print(f"{args.id}: уже {where} — не трогаю\n  {task_link(args.id)}", file=out)
        if args.json:
            _regroup_json(args.id, task, cfg, known)
        return

    # Колонка — не поле задачи, а отдельная связка, и снимком до/после её не
    # поймать изнутри set_task_fields. Проверяется здесь: перенос между секциями
    # доски касаться не должен вовсе (секция и колонка ортогональны).
    col_before = task_column(args.id, pid)
    _, fresh = set_task_fields(args.id, {"group": gid}, task, watched=REGROUP_WATCHED)
    col_after = task_column(args.id, pid)
    if col_before != col_after:
        die(f"{args.id}: перенос в секцию задел доску — колонка была {col_before}, "
            f"стала {col_after}.\n  Секция и колонка ортогональны, меняться должна "
            "только секция: проверь задачу в трекере.")

    out = sys.stderr if args.json else sys.stdout
    print(f"{args.id}: {was_where} -> {where}\n  {task_link(args.id)}", file=out)
    if args.json:
        _regroup_json(args.id, fresh, cfg, known)


def _regroup_json(task_id, task, cfg, known):
    """Подтверждённое состояние карточки тем же объектом, что у show/list.

    Машинный вывод у меняющей команды — это ПЕРЕЧИТАННОЕ состояние, а не эхо
    запроса: по нему вызывающий и сверяет, что секция действительно та.
    """
    pid = task.get("projectId")
    cid = task_column(task_id, pid)
    roles = {v: k for k, v in (cfg or {}).get("columns", {}).items()}
    names = {s["id"]: s["name"] for s in project_statuses(pid)}
    titles = tag_titles([task])
    json_out(task_json(task, role=roles.get(cid),
                       column_name=names.get(cid) if cid else None,
                       tags=task_tags(task, titles),
                       group_title=known.get(task.get("group")),
                       appUrl=task_link_app(task_id)))


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


# Команды с машинным выводом. Список нужен не для красоты: на нём стоит проверка
# в tests/test_pure.py, и новая команда без флага уронит её, а не обнаружится
# через месяц ответом argparse «unrecognized arguments: --json» посреди чужой
# сверки. Проверяются обе стороны — флаг без строки в списке тоже красный.
#
# Показывающие команды здесь все до одной. Из меняющих попали `regroup` и `notes`:
# обе печатают не эхо запроса, а ПЕРЕЧИТАННОЕ состояние, то есть ровно то, ради
# чего вызывающий и читает машинный вывод. Остальные меняющие (`set`, `rename`,
# `move`) флага не имеют: добавлять его надо тем же механизмом и вместе со строкой
# здесь, а не вторым способом печатать JSON.
JSON_COMMANDS = ("projects", "board", "next", "groups", "list", "show", "regroup",
                 "notes")

JSON_HELP = ("машинный вывод: в stdout только JSON, предупреждения и подсказки — "
             "в stderr; формат объекта задачи одинаков во всех командах")


def json_flag(sp):
    sp.add_argument("--json", action="store_true", help=JSON_HELP)


def build_parser():
    """Разбор аргументов отдельно от запуска: набор проверяет состав флагов, не
    выполняя команд."""
    p = argparse.ArgumentParser(prog="sing.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="проверить токен и привязку репо")
    sp.add_argument("--write", action="store_true",
                    help="проверить и права на запись: пробная задача создаётся "
                         "в привязанном проекте и удаляется в том же заходе")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("projects", help="список проектов")
    json_flag(sp)
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("init", help="привязать репо к проекту (по умолчанию — сухой прогон)")
    sp.add_argument("--project",
                    help="название проекта или P-id; по умолчанию — имя каталога репозитория")
    sp.add_argument("--path", default=".", help="корень репозитория")
    sp.add_argument("--columns", help='JSON вида {"todo":"К работе",...}')
    sp.add_argument("--own-columns", action="store_true",
                    help="создать свои колонки, даже если системных ещё нет "
                         "(доска раздвоится, когда приложение досоздаст свои)")
    sp.add_argument("--no-tasks", action="store_true",
                    help="не создавать обязательные задачи проекта")
    sp.add_argument("--apply", action="store_true", help="выполнить план")
    sp.set_defaults(fn=cmd_init)

    # Разовая адресация соседнего проекта. Текст один на три команды: расхождение
    # в справке читается как разница в поведении, которой нет.
    PROJECT_REF = ("другой подпроект «" + ROOT_PROJECT_TITLE + "» "
                   "(название или P-id) — на одну эту команду; "
                   "привязку репозитория не меняет")

    sp = sub.add_parser("board", help="доска проекта по колонкам")
    sp.add_argument("--limit", type=int, default=10,
                    help="сколько закрытых показывать в колонке done")
    sp.add_argument("--project", metavar="ПРОЕКТ", help=PROJECT_REF)
    json_flag(sp)
    sp.set_defaults(fn=cmd_board)

    sp = sub.add_parser("next", help="следующая задача из очереди")
    sp.add_argument("--column", default="todo", choices=COLUMN_ORDER)
    sp.add_argument("--group", help="брать только из секции (название или Q-id)")
    json_flag(sp)
    sp.set_defaults(fn=cmd_next)

    sp = sub.add_parser("groups", help="секции проекта")
    sp.add_argument("--create", metavar="НАЗВАНИЕ", help="завести секцию")
    json_flag(sp)
    sp.set_defaults(fn=cmd_groups)

    sp = sub.add_parser("notes", help="заметки проекта (контекст для агента): "
                                      "без аргументов — показать все")
    sp.add_argument("--add", metavar="ЗАГОЛОВОК", help="создать заметку")
    sp.add_argument("--show", metavar="ID", help="одна заметка целиком")
    sp.add_argument("--edit", metavar="ID",
                    help="переписать заметку текстом из --text")
    sp.add_argument("--append", action="store_true",
                    help="с --edit: дописать --text в конец, а не переписывать")
    sp.add_argument("--rm", metavar="ID", help="удалить заметку (нужен --yes)")
    sp.add_argument("--yes", action="store_true", help="подтвердить удаление")
    sp.add_argument("--text", help="текст заметки (для --add и --edit)")
    json_flag(sp)
    sp.set_defaults(fn=cmd_notes)

    sp = sub.add_parser("list", help="задачи в колонке")
    sp.add_argument("--column", default="todo", choices=COLUMN_ORDER)
    sp.add_argument("--group", help="только из секции (название или Q-id)")
    sp.add_argument("--mine", action="store_true", help="только со своим agent-тегом")
    sp.add_argument("--agent", help="имя агента (по умолчанию $SINGULARITY_AGENT)")
    sp.add_argument("--project", metavar="ПРОЕКТ", help=PROJECT_REF)
    json_flag(sp)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("whoami", help="под каким тегом работает этот агент")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_whoami)

    sp = sub.add_parser("show", help="карточка задачи")
    sp.add_argument("id")
    json_flag(sp)
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
    sp.add_argument("--priority", type=int, choices=[0, 1, 2],
                    help="0=высокий, 1=обычный, 2=низкий")
    sp.add_argument("--deadline", help=DEADLINE_HELP)
    sp.add_argument("--project", metavar="ПРОЕКТ", help=PROJECT_REF)
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("move", help="переставить задачу в колонку (починка доски)")
    sp.add_argument("id")
    sp.add_argument("column", choices=COLUMN_ORDER)
    sp.set_defaults(fn=cmd_move)

    sp = sub.add_parser("rename", help="переименовать карточку с подтверждением по факту")
    sp.add_argument("id")
    sp.add_argument("title")
    sp.set_defaults(fn=cmd_rename)

    sp = sub.add_parser("set", help="сменить приоритет и/или дедлайн у существующей задачи")
    sp.add_argument("id")
    sp.add_argument("--priority", type=int, choices=[0, 1, 2],
                    help="0=высокий, 1=обычный, 2=низкий")
    sp.add_argument("--deadline",
                    help=DEADLINE_HELP + "; пустая строка '' — снять дедлайн")
    sp.set_defaults(fn=cmd_set)

    sp = sub.add_parser("regroup", help="перенести задачу в секцию проекта или снять секцию")
    sp.add_argument("id")
    sp.add_argument("group", nargs="?", metavar="СЕКЦИЯ",
                    help="название секции или Q-id; секции нет — отказ "
                         "(завести: sing.py groups --create)")
    sp.add_argument("--clear", action="store_true",
                    help="снять секцию: задача вернётся «вне секций»")
    json_flag(sp)
    sp.set_defaults(fn=cmd_regroup)

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

    return p


def main():
    args = build_parser().parse_args()
    args.fn(args)
    warn_if_skill_drifted(args.cmd)


if __name__ == "__main__":
    main()
