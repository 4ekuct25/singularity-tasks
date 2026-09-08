#!/usr/bin/env python3
"""CLI поверх SingularityApp REST API v2 для агентской работы с задачами.

Только stdlib. Прокси берётся из HTTP(S)_PROXY (urllib делает это сам).

Токен ищется по порядку:
  1. $SINGULARITY_TOKEN
  2. macOS Keychain: security find-generic-password -s singularity-app -a rest-token
  3. ~/.claude/.singularity-token (chmod 600)

Привязка репозитория к проекту трекера ищется в <repo>/.agents/singularity.json,
затем .claude/singularity.json, затем в корне (секретов не содержит, коммитится).
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("SINGULARITY_API", "https://api.singularity-app.com/v2")
CONFIG_NAME = "singularity.json"

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


def agent_name(cfg=None, override=None):
    """Кто сейчас работает. Каждый агент выставляет свой $SINGULARITY_AGENT."""
    return (override or os.environ.get("SINGULARITY_AGENT")
            or (cfg or {}).get("agent") or "claude").strip()


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


def drop_task_tag(task_id, tag_id):
    """Снять тег, не тронув остальные, и убедиться, что он снят."""
    task = request("GET", f"/task/{task_id}")
    tags = list(task.get("tags") or [])
    if tag_id not in tags:
        return False
    request("PATCH", f"/task/{task_id}",
            body={"tags": [t for t in tags if t != tag_id]})
    actual = request("GET", f"/task/{task_id}").get("tags") or []
    if tag_id in actual:
        die(f"{task_id}: тег не снялся, у задачи теги {actual}")
    return True


def add_task_tag(task_id, tag_id):
    """Добавить тег, не затирая уже висящие, и убедиться, что он применился."""
    task = request("GET", f"/task/{task_id}")
    tags = list(task.get("tags") or [])
    if tag_id in tags:
        return False
    request("PATCH", f"/task/{task_id}", body={"tags": tags + [tag_id]})
    actual = request("GET", f"/task/{task_id}").get("tags") or []
    if tag_id not in actual:
        die(f"{task_id}: тег не применился, у задачи теги {actual}")
    return True


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
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if soft:
            return None
        if e.code == 401:
            die("401 Unauthorized: токен недействителен или ему не хватает прав.")
        die(f"{method} {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"Сеть недоступна для {method} {path}: {e.reason}")


def paged(path, key, query=None, limit=1000):
    """Собрать все страницы списка."""
    items, offset = [], 0
    q = dict(query or {})
    while len(items) < limit:
        q.update({"maxCount": min(200, limit - len(items)), "offset": offset,
                  "paginationData": "true"})
        resp = request("GET", path, query=q)
        batch = resp.get(key, [])
        items.extend(batch)
        pg = resp.get("pagination") or {}
        total = pg.get("total")
        offset += max(len(batch), 1)
        if not batch or (total is not None and offset >= total):
            break
    return items


# --------------------------------------------------------------------------- конфиг репо


def all_projects():
    return [p for p in paged("/project", "projects") if not p.get("removed")]


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


def note_append(note, text):
    """Дописать абзац, приведя заметку к формату, который понимает приложение."""
    ops = note_ops(note)
    if ops:
        tail = ops[-1].get("insert") if isinstance(ops[-1], dict) else None
        sep = "\n" if isinstance(tail, str) and tail.endswith("\n") else "\n\n"
        ops.append({"insert": sep + text + "\n"})
    else:
        ops = [{"insert": text + "\n"}]
    return note_dump(ops)


# --------------------------------------------------------------------------- канбан


def project_statuses(project_id):
    return paged("/kanban-status", "kanbanStatuses", {"projectId": project_id})


def task_links(task_id, include_removed=False):
    q = {"taskId": task_id}
    if include_removed:
        q["includeRemoved"] = "true"
    return paged("/kanban-task-status", "kanbanTaskStatuses", q)


def task_column(task_id):
    live = [l for l in task_links(task_id) if not l.get("removed")]
    return live[0]["statusId"] if live else None


def move_to_column(task_id, status_id):
    """Идемпотентно поставить задачу в колонку и УБЕДИТЬСЯ, что она там.

    POST /task/{id}/change-column не используется намеренно: на системных
    колонках проекта он отвечает 200, но связку не меняет. Правим связку сама.
    """
    if task_column(task_id) == status_id:
        return "уже в колонке"
    link = next(iter(task_links(task_id, include_removed=True)), None)
    action = None
    if link and not link.get("removed"):
        request("PATCH", f"/kanban-task-status/{link['id']}",
                body={"statusId": status_id}, soft=True)
        action = "перемещена"
    if task_column(task_id) != status_id:
        # связки нет или она помечена удалённой (колонку снесли) — заводим заново
        request("POST", "/kanban-task-status",
                body={"taskId": task_id, "statusId": status_id}, soft=True)
        action = "привязана к колонке"
    actual = task_column(task_id)
    if actual != status_id:
        die(f"{task_id}: перенос не применился — колонка осталась {actual}. "
            "Состояние трекера не изменилось так, как ожидалось.")
    return action


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
    cid = (cfg.get("columns") or {}).get(role)
    if not cid:
        die(f"В .claude/{CONFIG_NAME} нет колонки '{role}'. Перезапусти init.")
    return cid


# --------------------------------------------------------------------------- задачи


def fetch_tasks(project_id):
    return paged("/task", "tasks",
                 {"projectId": project_id, "includeAllRecurrenceInstances": "true"})


def live_tasks(project_id):
    """Всё, что не удалено и не в архиве, включая уже выполненное."""
    return [t for t in fetch_tasks(project_id)
            if not t.get("removed") and not t.get("journalDate")
            and not t.get("deleteDate") and not t.get("isNote")]


def open_tasks(project_id):
    return [t for t in live_tasks(project_id) if int(t.get("checked") or 0) == 0]


def column_map(project_id):
    """taskId -> statusId для всего проекта (одним запросом на колонку)."""
    m = {}
    for st in project_statuses(project_id):
        for link in paged("/kanban-task-status", "kanbanTaskStatuses",
                          {"statusId": st["id"]}):
            if not link.get("removed"):
                m[link["taskId"]] = link["statusId"]
    return m


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


def brief(t, extra=""):
    prio = {0: "!высокий", 1: "обычный", 2: "низкий"}.get(prio_of(t), "?")
    dl = f" дедлайн={t['deadline'][:10]}" if t.get("deadline") else ""
    return f"{t['id']}  [{prio}]{dl}  {t.get('title', '')}{extra}"


# --------------------------------------------------------------------------- команды


def cmd_doctor(args):
    projects = paged("/project", "projects", limit=5)
    print(f"✓ токен рабочий, доступно проектов (первая страница): {len(projects)}")
    cfg, path = load_config(required=False)
    if not cfg:
        print("· репозиторий не привязан — запусти init")
        return
    print(f"✓ привязка: {path}")
    print(f"  проект: {cfg.get('projectTitle')} ({cfg['projectId']})")
    live = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])
            if not s.get("removed")}
    for role in COLUMN_ORDER:
        cid = (cfg.get("columns") or {}).get(role)
        mark = "✓" if cid in live else "✗"
        print(f"  {mark} {role:8} -> {live.get(cid, 'КОЛОНКА ПРОПАЛА В ТРЕКЕРЕ')}")
    seen = {}
    for cid, name in live.items():
        seen.setdefault(name.strip().lower(), []).append(cid)
    dups = {n: ids for n, ids in seen.items() if len(ids) > 1}
    if dups:
        print("  ⚠ на доске дубли колонок — вероятно, init создал их поверх системных:")
        for name, ids in dups.items():
            for cid in ids:
                used = " (используется скиллом)" if cid in (cfg.get("columns") or {}).values() else ""
                print(f"      «{name}» {cid}{used}")


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


def cmd_init(args):
    """По умолчанию — сухой прогон: показывает план, ничего не меняет."""
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
    by_id = {s["id"]: s for s in existing}
    mapping, to_create = {}, []
    for role in COLUMN_ORDER:
        want = names[role]
        # 1) системная колонка проекта — приоритет, даже если GET её ещё не отдал
        sys_id = system_status_id(target["id"], role) if target else None
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
            to_create.append((role, want))
            plan.append(f"СОЗДАТЬ колонку «{want}» (роль {role})")

    # уже привязанный репозиторий переписываем на месте, новый получает
    # нейтральный `.agents/` — он общий для всех пяти инструментов
    root = os.path.abspath(args.path)
    cfg_path = find_config(root)
    if not cfg_path or not cfg_path.startswith(root + os.sep):
        cfg_path = os.path.join(root, CONFIG_LOCATIONS[0])
    plan.append(f"ЗАПИСАТЬ {cfg_path}")

    if not args.apply:
        print("\nПлан (ничего не изменено, добавь --apply):")
        for line in plan:
            print("  · " + line)
        return

    if not target:
        created = request("POST", "/project",
                          body={"title": args.project, "parent": root["id"]})
        target = created.get("project", created)
        assert_allowed(target["id"], "созданный проект")
        print(f"создан проект {target['id']} внутри «{root['title']}»")
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


def cmd_board(args):
    cfg, _ = load_config()
    statuses = {s["id"]: s["name"] for s in project_statuses(cfg["projectId"])}
    cmap = column_map(cfg["projectId"])
    by_col = {}
    for t in live_tasks(cfg["projectId"]):
        by_col.setdefault(cmap.get(t["id"]), []).append(t)
    print(f"{cfg.get('projectTitle')} ({cfg['projectId']})")
    for role in COLUMN_ORDER:
        cid = (cfg.get("columns") or {}).get(role)
        items = by_col.get(cid, [])
        shown = items
        if role == "done":  # закрытых копится много — показываем свежие
            shown = sorted(items, key=lambda t: t.get("modificatedDate") or "",
                           reverse=True)[:args.limit]
        print(f"\n[{role}] {statuses.get(cid, '?')} — {len(items)}"
              + (f" (показаны {len(shown)})" if len(shown) < len(items) else ""))
        for t in sorted(shown, key=prio_of):
            done = " ✓" if int(t.get("checked") or 0) == 1 else ""
            print("  " + brief(t) + done)
    loose = [t for t in by_col.get(None, []) if int(t.get("checked") or 0) == 0]
    if loose:
        print(f"\n[вне колонок] — {len(loose)}")
        for t in loose:
            print("  " + brief(t))


def _pick_pool(cfg, role, include_done=False, group=None):
    """include_done — для просмотра; `next` обязан брать только незакрытые."""
    cid = col_id(cfg, role)
    cmap = column_map(cfg["projectId"])
    source = live_tasks(cfg["projectId"]) if include_done else open_tasks(cfg["projectId"])
    pool = [t for t in source if cmap.get(t["id"]) == cid]
    if group:
        gid = resolve_group(cfg["projectId"], group)
        pool = [t for t in pool if t.get("group") == gid]
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
    pool = _pick_pool(cfg, args.column, group=args.group)
    if not pool:
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
    items = sorted(
        (c for c in paged("/checklist-item", "checklistItems", {"parent": t["id"]})
         if not c.get("removed")),
        key=lambda c: (c.get("parentOrder") if c.get("parentOrder") is not None else 0))
    if items:
        print("\n--- чек-лист ---")
        for c in items:
            print(("  [x] " if c.get("done") else "  [ ] ") + c.get("title", ""))
    print(f"\nВзять в работу: sing.py start {t['id']}")


def cmd_list(args):
    cfg, _ = load_config()
    pool = _pick_pool(cfg, args.column, include_done=True, group=args.group)
    if args.mine:
        tag_id = find_tag(AGENT_TAG_PREFIX + agent_name(cfg, args.agent))
        pool = [t for t in pool if tag_id and tag_id in (t.get("tags") or [])]
    tag_titles = {t["id"]: t["title"] for t in paged("/tag", "tags")}
    for t in pool:
        marks = [tag_titles.get(x, x) for x in (t.get("tags") or [])]
        extra = "  " + " ".join("#" + s for s in marks) if marks else ""
        if int(t.get("checked") or 0) == 1:
            extra += " ✓"
        print(brief(t, extra))


def cmd_whoami(args):
    cfg, _ = load_config(required=False)
    who = agent_name(cfg, args.agent)
    print(f"агент: {who}\nтег:   {AGENT_TAG_PREFIX}{who}")
    src = ("--agent" if args.agent else
           "$SINGULARITY_AGENT" if os.environ.get("SINGULARITY_AGENT") else
           ".claude/singularity.json" if (cfg or {}).get("agent") else "значение по умолчанию")
    print(f"откуда: {src}")


def cmd_show(args):
    cfg, _ = load_config(required=False)
    t = assert_task_allowed(args.id, cfg)
    print(brief(t))
    print("проект:", t.get("projectId"), "| выполнена:", t.get("checked"))
    note = note_to_text(t.get("note"))
    if note:
        print("\n--- заметка ---\n" + note)


def cmd_start(args):
    cfg, _ = load_config()
    assert_task_allowed(args.id, cfg)
    res = move_to_column(args.id, col_id(cfg, "wip"))
    who = mark_agent(args.id, cfg, args.agent)
    print(f"{args.id}: {res} (в работе), тег {AGENT_TAG_PREFIX}{who}")


def cmd_release(args):
    """Вернуть задачу в очередь: снять свой тег, отметку выполнения и колонку.

    Полный откат взятия задачи. Частичный откат (вернул колонку, забыл тег)
    оставляет на доске задачу, которая выглядит занятой, хотя ею никто не занят.
    """
    cfg, _ = load_config()
    task = assert_task_allowed(args.id, cfg)
    if args.report:
        request("PATCH", f"/task/{args.id}",
                body={"note": note_append(task.get("note"), args.report)})
    if int(task.get("checked") or 0) == 1:
        request("POST", f"/task/{args.id}/uncomplete")
    who = agent_name(cfg, args.agent)
    tag_id = find_tag(AGENT_TAG_PREFIX + who)
    dropped = drop_task_tag(args.id, tag_id) if tag_id else False
    move_to_column(args.id, col_id(cfg, "todo"))
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
    if args.report:
        t = task
        request("PATCH", f"/task/{args.id}",
                body={"note": note_append(t.get("note"), args.report)})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    role = "review" if args.review else "done"
    move_to_column(args.id, col_id(cfg, role))
    if not args.review:
        request("POST", f"/task/{args.id}/complete")
    print(f"{args.id}: {'отправлена на проверку' if args.review else 'закрыта'}")


def cmd_block(args):
    cfg, _ = load_config()
    t = assert_task_allowed(args.id, cfg)
    request("PATCH", f"/task/{args.id}",
            body={"note": note_append(t.get("note"), "БЛОКЕР: " + args.reason)})
    mark_agent(args.id, cfg, getattr(args, "agent", None))
    move_to_column(args.id, col_id(cfg, "blocked"))
    print(f"{args.id}: заблокирована, причина записана в заметку")


def cmd_add(args):
    cfg, _ = load_config()
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
    move_to_column(t["id"], col_id(cfg, args.column))
    print(f"{t['id']}: создана в колонке '{args.column}' — {args.title}")


def cmd_checklist(args):
    cfg, _ = load_config(required=False)
    assert_task_allowed(args.id, cfg)
    existing = [c for c in paged("/checklist-item", "checklistItems", {"parent": args.id})
                if not c.get("removed")]
    base = max((c.get("parentOrder") or 0 for c in existing), default=-1) + 1
    for i, title in enumerate(args.items):
        request("POST", "/checklist-item",
                body={"parent": args.id, "title": title, "parentOrder": base + i})
    print(f"{args.id}: добавлено пунктов — {len(args.items)}")


# --------------------------------------------------------------------------- CLI


def main():
    p = argparse.ArgumentParser(prog="sing.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="проверить токен и привязку репо").set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("projects", help="список проектов")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("init", help="привязать репо к проекту (по умолчанию — сухой прогон)")
    sp.add_argument("--project", required=True, help="название проекта или P-id")
    sp.add_argument("--path", default=".", help="корень репозитория")
    sp.add_argument("--columns", help='JSON вида {"todo":"К работе",...}')
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

    sp = sub.add_parser("start", help="взять задачу в работу (вешает свой agent-тег)")
    sp.add_argument("id")
    sp.add_argument("--agent", help="имя агента (по умолчанию $SINGULARITY_AGENT)")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("report", help="дописать отчёт в заметку задачи")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.add_argument("--agent")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("done", help="закрыть задачу (или отправить на проверку)")
    sp.add_argument("id")
    sp.add_argument("--report", help="текст отчёта в заметку")
    sp.add_argument("--review", action="store_true", help="в колонку review, не закрывать")
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
    sp.add_argument("--note")
    sp.add_argument("--priority", type=int, choices=[0, 1, 2])
    sp.add_argument("--deadline", help="ISO-дата")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("checklist", help="добавить пункты чек-листа в задачу")
    sp.add_argument("id")
    sp.add_argument("items", nargs="+")
    sp.set_defaults(fn=cmd_checklist)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
