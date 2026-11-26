"""Project-scoped скан kanban-task-status через statusId.in (26.11, T-d20fae93).

До правки board/next/list тянули ВСЕ связки аккаунта (замер 26.11: 2870 связок
= 15 запросов) и фильтровали локально; мёртвые связки (инцидент 10-23)
виднелись из того же полного скана. Теперь:

- column_map при links=None — один запрос `statusId.in` по id колонок проекта;
- dead-check — dead_link_check(): ОДИН запрос `taskId.in` по задачам без
  живой связки (их statusId в фильтре невидим).

Оба фильтра пробы live 26.11: .qwen/tmp/probe-statusid-result.txt,
probe-taskid-in-result.txt. Мок — граница сети sing.request; «сервер» честный:
фильтры применяет (как живой API), поэтому тест краснеет, если фильтр
перестанут шлать, — а не только при смене поведения.

Запуск: tests/run.py fast
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

sing = support.load_sing("sing_board_scan")

PID = "P-1"
TODO, WIP, DONE = "KS-P-1-TODO", "KS-P-1-WIP", "KS-P-1-DONE"
DEAD = "KS-DEAD-REVIEW"        # колонка исчезла из трекера
OTHER = "KS-OTHER-PROJECT"     # живая колонка ЧУЖОГО проекта
TODAY = "KS-P-TODAY-TODO"      # системная доска «Сегодня»
OWN = {TODO, WIP, DONE}

STATUSES = [
    {"id": TODO, "name": "Новые", "removed": False, "kanbanOrder": 0},
    {"id": WIP, "name": "В работе", "removed": False, "kanbanOrder": 1},
    {"id": DONE, "name": "Готово", "removed": False, "kanbanOrder": 2},
]

LINKS = [
    {"id": "KTS-T1", "taskId": "T1", "statusId": TODO, "removed": False},
    {"id": "KTS-T2", "taskId": "T2", "statusId": WIP, "removed": False},
    {"id": "KTS-T3", "taskId": "T3", "statusId": DEAD, "removed": False},
    {"id": "KTS-T4-TODAY", "taskId": "T4", "statusId": TODAY, "removed": False},
    {"id": "KTS-TX", "taskId": "TX", "statusId": OTHER, "removed": False},
    {"id": "KTS-T5", "taskId": "T5", "statusId": TODO, "removed": True},
]


def task(tid, **kw):
    t = {"id": tid, "title": f"задача {tid}", "projectId": PID,
         "checked": 0, "journalDate": None, "deleteDate": None,
         "isNote": False, "priority": 1, "deadline": None,
         "createdDate": "2026-01-01T00:00:00Z", "group": None,
         "start": None, "tags": [], "parent": None}
    t.update(kw)
    return t


TASKS = [task("T1"), task("T2"), task("T3"), task("T4"), task("TX"),
         task("T5", checked=1)]


class FakeServer:
    """«Сервер»-сторона: применяет statusId.in / taskId.in / taskId, как живой
    API (пробы 26.11), и считает каждый GET /kanban-task-status."""

    def __init__(self, statuses=None, links=None, tasks=None):
        self.statuses = STATUSES if statuses is None else statuses
        self.links = LINKS if links is None else links
        self.tasks = TASKS if tasks is None else tasks
        self.kts_queries = []
        self.kts_hits = 0

    def request(self, method, path, query=None, body=None, soft=False):
        q = dict(query or {})
        if method != "GET":
            raise AssertionError(f"неожиданный не-GET: {method} {path}")
        if path == "/kanban-status":
            return {"kanbanStatuses": list(self.statuses),
                    "pagination": {"total": len(self.statuses)}}
        if path == "/task":
            items = [t for t in self.tasks
                     if t["projectId"] == q.get("projectId")]
            if "includeArchived" not in q:
                items = [t for t in items if not t.get("journalDate")]
            return {"tasks": items, "pagination": {"total": len(items)}}
        if path == "/kanban-task-status":
            self.kts_hits += 1
            self.kts_queries.append(q)
            items = list(self.links)
            if "statusId.in" in q:
                want = set(q["statusId.in"].split(","))
                items = [l for l in items if l["statusId"] in want]
            if "taskId.in" in q:
                want = set(q["taskId.in"].split(","))
                items = [l for l in items if l["taskId"] in want]
            if "taskId" in q:
                items = [l for l in items if l["taskId"] == q["taskId"]]
            return {"kanbanTaskStatuses": items,
                    "pagination": {"total": len(items)}}
        raise AssertionError(f"неожиданный путь: {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        patcher = mock.patch.object(sing, "request",
                                    side_effect=self.server.request)
        patcher.start()
        self.addCleanup(patcher.stop)


class ColumnMapScanTest(Base):
    """column_map(links=None) — фильтрованный скан, а не полный аккаунт."""

    def test_filtered_query_carries_all_column_ids(self):
        cmap = sing.column_map(PID)
        self.assertEqual(cmap, {"T1": TODO, "T2": WIP})
        self.assertEqual(len(self.server.kts_queries), 1,
                         "один запрос на скан — не страницы всего аккаунта")
        q = self.server.kts_queries[0]
        self.assertEqual(sorted(q.get("statusId.in", "").split(",")),
                         sorted(OWN),
                         "фильтр обязан нести ВСЕ id колонок проекта")
        self.assertNotIn("projectId", q,
                         "?projectId= не поддерживается (замер: пустой список)")

    def test_removed_link_not_in_map(self):
        cmap = sing.column_map(PID)
        self.assertNotIn("T5", cmap, "removed-связка не считается")

    def test_no_statuses_no_request(self):
        self.server.statuses = []
        cmap = sing.column_map(PID)
        self.assertEqual(cmap, {})
        self.assertEqual(self.server.kts_queries, [],
                         "пустое множество колонок — скан бессмыслен, запросов ноль")

    def test_explicit_links_not_refetched(self):
        given = [{"id": "KTS-T1", "taskId": "T1", "statusId": TODO,
                  "removed": False}]
        cmap = sing.column_map(PID, statuses=STATUSES, links=given)
        self.assertEqual(cmap, {"T1": TODO})
        self.assertEqual(self.server.kts_queries, [],
                         "переданные links не должны вызывать допзапрос")


class DeadLinkCheckTest(Base):
    """dead_link_check — one taskId.in request for all tasks без живой связки."""

    def test_one_taskid_in_request_for_all_missing(self):
        tasks = [task("T1"), task("T2"), task("T3"), task("T4"), task("TX")]
        dead = sing.dead_link_check(tasks, {"T1": TODO, "T2": WIP}, OWN)
        self.assertEqual(dead, {"T3": DEAD, "TX": OTHER},
                         "мёртвые видны: исчезнувшая колонка и колонка чужого "
                         "проекта (семантика dead_board_links не изменилась)")
        self.assertEqual(len(self.server.kts_queries), 1,
                         "один запрос taskId.in на ВСЕХ подозрительных")
        q = self.server.kts_queries[0]
        self.assertEqual(sorted(q["taskId.in"].split(",")),
                         ["T3", "T4", "TX"])
        self.assertNotIn("statusId.in", q)

    def test_todays_link_is_not_dead(self):
        # Только TODAY-связка: по id-паттерну KTS-<taskId> это не канбан-связка —
        # задача легитимно без проектной связки (в «Новые»), а не мёртвая.
        dead = sing.dead_link_check([task("T4")], {}, OWN)
        self.assertEqual(dead, {})

    def test_no_missing_no_request(self):
        dead = sing.dead_link_check([task("T1")], {"T1": TODO}, OWN)
        self.assertEqual(dead, {})
        self.assertEqual(self.server.kts_queries, [],
                         "все задачи со связкой — dead-check не нужен вовсе")


class PickPoolScanTest(Base):
    """_pick_pool (next/list): очередь без мёртвых, цена — 2 запроса на связки."""

    def setUp(self):
        super().setUp()
        self.cfg = {"projectId": PID,
                    "columns": {"todo": TODO, "wip": WIP, "done": DONE}}

    def test_queue_without_dead_and_request_budget(self):
        pool = sing._pick_pool(self.cfg, "todo")
        ids = [t["id"] for t in pool]
        self.assertIn("T1", ids)
        self.assertIn("T4", ids,
                      "связки нет вовсе — легитимные «Новые», не мёртвые")
        self.assertNotIn("T3", ids, "мёртвая связка — не в очереди (T-ec4c39e1)")
        self.assertNotIn("TX", ids, "связка на колонку чужого проекта — не в очереди")
        self.assertNotIn("T5", ids, "checked — не открытая задача")
        self.assertEqual(self.server.kts_hits, 2,
                         "скан с фильтром + dead-check; прежде — страницы "
                         "всего аккаунта")
        self.assertEqual(sorted(self.server.kts_queries[0]
                                .get("statusId.in", "").split(",")),
                         sorted(OWN))
        self.assertEqual(sorted(self.server.kts_queries[1]
                                .get("taskId.in", "").split(",")),
                         ["T3", "T4", "TX"])

    def test_wip_queue_uses_same_scan_semantics(self):
        pool = sing._pick_pool(self.cfg, "wip")
        self.assertEqual([t["id"] for t in pool], ["T2"])


if __name__ == "__main__":
    unittest.main()
